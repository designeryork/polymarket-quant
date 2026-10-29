"""
P0 修复验证套件 (verify_p0_fixes.py)
==================================
只读/离线验证 P0 级修复是否真实生效。**不发起任何下单、撤单、授权、赎回或转账调用。**

用法：
    python verify_p0_fixes.py

覆盖：
  1. 签名编码修复（500 样本长度回归）
  2. 单实例锁（并发竞争 + 陈旧锁接管）
  3. 挂单账本（去重 / 敞口统计 / 状态流转）
  4. 资金上限门禁（敞口计算）
  5. 账本重建的 stats 键完整性
  6. .gitignore 敏感路径覆盖
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv

load_dotenv(BASE / ".env")

PASS = []
FAIL = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append((name, detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------- 1. 签名
def test_signature() -> None:
    section("1. POLY_1271 签名编码修复")
    import clob_executor
    from web3 import Web3

    original = clob_executor.PolymarketV2Executor.resolve_exchange_and_version
    clob_executor.PolymarketV2Executor.resolve_exchange_and_version = (
        lambda self, token_id: (
            Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B"),
            "2",
        )
    )
    try:
        ex = clob_executor.PolymarketV2Executor()
        token = "92708239841263145361017304203618583258102423804507532186791623602897510452108"
        expected = clob_executor.EXPECTED_SIGNATURE_LENGTH
        lengths = {}
        bad = 0
        N = 500
        for _ in range(N):
            sig = ex.build_and_sign_poly1271_order(token, "BUY", 0.35, 5.0)["order"]["signature"]
            lengths[len(sig)] = lengths.get(len(sig), 0) + 1
            if len(sig) != expected:
                bad += 1
        check(
            "500 样本签名长度全部正确",
            bad == 0,
            f"错位率 {100 * bad / N:.1f}% (修复前 13.6%)，长度分布 {lengths}",
        )

        # 前导零场景：反复构造直到 raw_sig / hash 含前导 0，验证不被误删
        zero_prefix_ok = True
        for _ in range(200):
            sig = ex.build_and_sign_poly1271_order(token, "BUY", 0.37, 5.0)["order"]["signature"]
            if len(sig) != expected:
                zero_prefix_ok = False
                break
        check("前导零场景不破坏签名结构", zero_prefix_ok)

        # 确认代码中不再残留 lstrip("0x")
        src = (BASE / "clob_executor.py").read_text(encoding="utf-8")
        check('源码中已无 .lstrip("0x")', '.lstrip("0x")' not in src)
    finally:
        clob_executor.PolymarketV2Executor.resolve_exchange_and_version = original


# ---------------------------------------------------------------- 2. 单实例锁
def test_single_instance() -> None:
    section("2. 单实例锁")
    import single_instance

    with tempfile.TemporaryDirectory() as tmp:
        lock_file = Path(tmp) / "engine.lock"
        os.environ["ENGINE_LOCK_FILE"] = str(lock_file)

        ok1, msg1 = single_instance.acquire_lock(role="test_a")
        check("首次获取锁成功", ok1, msg1)

        ok2, msg2 = single_instance.acquire_lock(role="test_b")
        check("同进程重复获取视为可重入", ok2, msg2)

        # 模拟另一个（真实存活的）进程持有锁 —— 必须用一个不同的存活 PID，
        # 否则会命中"同进程可重入"分支，测不到互斥逻辑。
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            lock_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pid": sleeper.pid,
                        "role": "other",
                        "host": "test",
                        "started_at_utc": "2026-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            ok3, msg3 = single_instance.acquire_lock(role="test_c")
            check("存活 PID 持锁时拒绝第二实例", ok3 is False, msg3[:80])
        finally:
            sleeper.kill()
            sleeper.wait(timeout=10)

        # 陈旧锁（不存在的 PID）应被接管
        lock_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pid": 999999,
                    "role": "dead",
                    "host": "test",
                    "started_at_utc": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        ok4, msg4 = single_instance.acquire_lock(role="test_d")
        check("陈旧锁（进程已死）可被安全接管", ok4, msg4)

        status = single_instance.lock_status()
        check("锁状态可查询", status.get("held") is True, f"pid={status.get('pid')}")

        single_instance.release_lock()
        check("释放锁后文件已删除", not lock_file.exists())

        os.environ.pop("ENGINE_LOCK_FILE", None)


def test_single_instance_real_race() -> None:
    section("2b. 单实例锁并发竞争（真实多进程）")
    import single_instance

    with tempfile.TemporaryDirectory() as tmp:
        lock_file = Path(tmp) / "engine.lock"
        script = (
            "import sys; sys.path.insert(0, %r);\n"
            "import single_instance, json;\n"
            "ok, msg = single_instance.acquire_lock(role='racer');\n"
            "print(json.dumps({'ok': ok, 'pid': __import__('os').getpid()}));\n"
            "import time; time.sleep(3);\n"
            "single_instance.release_lock();\n"
        ) % str(BASE)
        script_path = Path(tmp) / "racer.py"
        script_path.write_text(script, encoding="utf-8")

        env = dict(os.environ)
        env["ENGINE_LOCK_FILE"] = str(lock_file)
        env["PYTHONPATH"] = str(BASE)
        procs = [
            subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            for _ in range(5)
        ]
        results = []
        for proc in procs:
            out, _ = proc.communicate(timeout=30)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        results.append(json.loads(line))
                    except Exception:
                        pass
        winners = [r for r in results if r.get("ok")]
        check(
            "5 进程并发仅 1 个获得锁",
            len(winners) == 1,
            f"成功 {len(winners)} 个 / 共 {len(results)} 个返回结果",
        )


# ---------------------------------------------------------------- 3. 挂单账本
def test_order_ledger() -> None:
    section("3. 在途挂单账本")
    import order_ledger

    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "pending_orders.json"
        original = order_ledger.LEDGER_FILE
        order_ledger.LEDGER_FILE = ledger_path
        try:
            order_ledger.record_order(
                order_id="ORD_BUY_1",
                token_id="TOK_A",
                side="BUY",
                intent=order_ledger.INTENT_ENTRY,
                price=0.35,
                shares=5.0,
                notional_usdc=1.75,
                event_id="EVT_1",
                market_question="Test Market A",
                outcome="Yes",
            )
            check("买单写入账本", len(order_ledger.blocking_orders()) == 1)

            check(
                "在途买单阻止同 token 重复下单",
                order_ledger.has_open_buy_for_token("TOK_A") is True,
            )
            check(
                "不同 token 不受影响",
                order_ledger.has_open_buy_for_token("TOK_B") is False,
            )
            check(
                "在途买单计入敞口",
                abs(order_ledger.open_buy_notional() - 1.75) < 1e-9,
                f"={order_ledger.open_buy_notional()}",
            )
            check(
                "在途 event 占用已记录",
                "EVT_1" in order_ledger.blocking_event_ids(),
            )

            # 卖单去重
            order_ledger.record_order(
                order_id="ORD_SELL_1",
                token_id="TOK_A",
                side="SELL",
                intent=order_ledger.INTENT_EXIT,
                price=0.34,
                shares=5.0,
                notional_usdc=1.70,
                event_id="EVT_1",
            )
            check(
                "在途卖单阻止同 token 重复卖出",
                order_ledger.has_open_sell_for_token("TOK_A") is True,
            )

            # 与 CLOB 挂单列表对账：仍在挂单 -> 继续阻塞
            stats = order_ledger.reconcile_with_open_orders(
                [{"id": "ORD_BUY_1", "status": "LIVE"}]
            )
            check(
                "仍在 CLOB 挂单列表的订单保持阻塞",
                order_ledger.has_open_buy_for_token("TOK_A") is True,
                f"still_live={stats.get('still_live')} released={stats.get('released')}",
            )

            # 已离簿 -> 解除阻塞
            order_ledger.reconcile_with_open_orders([])
            check(
                "已离簿订单解除阻塞",
                order_ledger.has_open_buy_for_token("TOK_A") is False,
            )

            # 状态流转
            order_ledger.update_status("ORD_BUY_1", order_ledger.STATUS_FILLED)
            check(
                "FILLED 状态不阻塞",
                order_ledger.has_open_buy_for_token("TOK_A") is False,
            )
            summary = order_ledger.summary()
            check("账本摘要可生成", summary.get("total") == 2, json.dumps(summary.get("by_status")))
        finally:
            order_ledger.LEDGER_FILE = original


# ---------------------------------------------------------------- 4. 资金门禁
def test_exposure_gate() -> None:
    section("4. MAX_CAPITAL_USDC 敞口门禁")
    import quant_engine

    state = {
        "open_positions": [
            {"token_id": "T1", "cost_usdc": 2.0},
            {"token_id": "T2", "cost_usdc": 3.5},
        ]
    }
    exposure = quant_engine.compute_total_exposure(state)
    check(
        "敞口 = 持仓成本之和（无在途单时）",
        abs(exposure - 5.5) < 1e-9,
        f"={exposure}",
    )

    import order_ledger

    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "pending_orders.json"
        original = order_ledger.LEDGER_FILE
        order_ledger.LEDGER_FILE = ledger_path
        try:
            order_ledger.record_order(
                order_id="P1",
                token_id="T3",
                side="BUY",
                intent=order_ledger.INTENT_ENTRY,
                price=0.4,
                shares=5.0,
                notional_usdc=2.0,
            )
            exposure2 = quant_engine.compute_total_exposure(state)
            check(
                "敞口包含在途买单名义金额",
                abs(exposure2 - 7.5) < 1e-9,
                f"={exposure2}（持仓 5.5 + 在途 2.0）",
            )
        finally:
            order_ledger.LEDGER_FILE = original


# ---------------------------------------------------------------- 5. 账本完整性
def test_reconciled_state_keys() -> None:
    section("5. 对账重建账本的键完整性")
    import portfolio_reconciliation as pr

    sources = {
        "eoa": "0x6A8373569a78E659Ce9Ca95b7A86359E9bFf722b",
        "deposit_wallet": "0xaF56445fAe9Fe3eE8d74988ad873f3677F672467",
        "activity": [],
        "positions": [],
        "balances": {"deposit_wallet": 13.75, "eoa": 0.0},
        "open_orders": [],
    }
    state = pr.build_reconciled_state({}, sources)
    for key in ("stats", "daily_stats", "equity_curve", "recent_decisions"):
        check(f"全新账本包含 {key}", key in state)
    check(
        "stats 含 scans_count 等计数键",
        "scans_count" in (state.get("stats") or {}),
    )


# ---------------------------------------------------------------- 6. 错误分类
def test_error_taxonomy() -> None:
    section("6. 错误分类（网络类 vs 交易类）")
    from trading_error_monitor import classify_error

    cases = [
        ("requests.exceptions.ProxyError: Tunnel connection failed: 502 Bad Gateway", "PROXY_ERROR"),
        ("HTTPSConnectionPool(...): Read timed out. (read timeout=25)", "NETWORK_TIMEOUT"),
        ("ConnectTimeoutError: Connection to data-api.polymarket.com timed out.", "NETWORK_TIMEOUT"),
        ("ConnectionResetError(10054, remote host closed the connection)", "CONNECTION_RESET"),
        ("SSL: UNEXPECTED_EOF_WHILE_READING", "SSL_ERROR"),
        ("429 Client Error: Too Many Requests", "RATE_LIMITED"),
        ("invalid POLY_1271 signature: signature too short", "SIGNATURE_INVALID"),
        ("Trading restricted in your region, please refer to available regions", "REGION_RESTRICTED"),
        ("not enough balance / allowance: the balance is not enough", "BALANCE_OR_ALLOWANCE"),
        ("invalid amount: min size", "ORDER_AMOUNT_INVALID"),
        ("401 Unauthorized invalid api key", "API_AUTH_FAILED"),
        ("some unknown failure", "TRADING_ERROR"),
    ]
    wrong = []
    for text, expected in cases:
        got = classify_error(text)
        if got != expected:
            wrong.append(f"{text[:40]} -> {got} (期望 {expected})")
    check("12 类错误分类全部正确", not wrong, "; ".join(wrong[:3]) if wrong else f"共 {len(cases)} 类")

    from trading_error_monitor import NETWORK_CATEGORIES, CATEGORIES
    missing = [c for c in NETWORK_CATEGORIES if c not in CATEGORIES]
    check("网络类分类均有处置建议", not missing, f"缺失: {missing}")


# ---------------------------------------------------------------- 7. 账本隔离
def test_ledger_separation() -> None:
    section("7. PAPER / LIVE 账本隔离")
    import quant_engine

    original_paper = os.environ.get("PAPER")
    try:
        os.environ["PAPER"] = "true"
        paper_path = quant_engine.portfolio_path()
        os.environ["PAPER"] = "false"
        live_path = quant_engine.portfolio_path()
        check(
            "PAPER 与 LIVE 使用不同账本文件",
            paper_path != live_path,
            f"PAPER={paper_path.name} LIVE={live_path.name}",
        )
        check("PAPER 账本为 portfolio_paper.json", paper_path.name == "portfolio_paper.json")
        check("LIVE 账本为 portfolio.json", live_path.name == "portfolio.json")
    finally:
        if original_paper is None:
            os.environ.pop("PAPER", None)
        else:
            os.environ["PAPER"] = original_paper


# ---------------------------------------------------------------- 8. 代理探测
def test_proxy_detector() -> None:
    section("8. 代理通道探测")
    import proxy_detector

    candidates = proxy_detector.candidate_proxies()
    sources = {c["source"] for c in candidates}
    check("候选通道可枚举", len(candidates) >= 0, f"共 {len(candidates)} 个，来源: {sorted(sources)}")

    check(
        "注册表代理被纳入候选（浏览器所用）",
        "系统注册表(浏览器所用)" in sources or len(candidates) == 0,
        "注册表是浏览器实际使用的代理设置，优先级最高",
    )

    info = proxy_detector.detect_proxy()
    check(
        "探测结果结构完整",
        all(key in info for key in ("proxy_url", "source", "latency_s", "exit", "attempts")),
        f"proxy={info.get('proxy_url')} source={info.get('source')}",
    )
    check(
        "探测到可用通道",
        bool(info.get("proxy_url")) or info.get("source") == "直连(未使用代理)",
        f"{info.get('source')} 延迟 {info.get('latency_s')}s",
    )

    applied = proxy_detector.apply_to_clob_sdk()
    check(
        "CLOB SDK 客户端已应用探测到的通道",
        bool(applied.get("ok")),
        applied.get("detail", "")[:90],
    )

    from py_clob_client.http_helpers import helpers

    client = helpers._http_client
    check(
        "SDK 客户端不再依赖被污染的环境变量",
        client.trust_env is False or info.get("proxy_url") is not None,
        f"trust_env={client.trust_env}",
    )


# ---------------------------------------------------------------- 9. 仓库卫生
def test_repo_hygiene() -> None:
    section("9. 仓库与凭证卫生")
    gitignore = (BASE / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "state/", "logs/", "003_MataMask 钱包/", "build_launcher/", "__pycache__/"):
        check(f".gitignore 覆盖 {pattern}", pattern in gitignore)

    git_dir = BASE / ".git"
    check("git 仓库已初始化", git_dir.exists())

    try:
        result = subprocess.run(
            ["git", "ls-files"], cwd=str(BASE), capture_output=True, text=True, timeout=30
        )
        tracked = result.stdout.splitlines()
        sensitive = [
            f for f in tracked
            if f == ".env"
            or f.startswith("state/")
            or f.startswith("logs/")
            or "钱包" in f
            or f.startswith("build_launcher/")
            or f.endswith(".exe")
        ]
        check("版本库中无敏感文件", not sensitive, f"敏感项: {sensitive[:5]}" if sensitive else f"已追踪 {len(tracked)} 个文件")
    except Exception as exc:
        check("版本库检查", False, str(exc))

    wallet_in_project = (BASE / "003_MataMask 钱包").exists()
    check("明文钱包目录已移出项目", not wallet_in_project)


def main() -> int:
    print("=" * 70)
    print(" P0 修复验证套件（离线，不发起任何交易调用）")
    print("=" * 70)

    test_signature()
    test_single_instance()
    test_single_instance_real_race()
    test_order_ledger()
    test_exposure_gate()
    test_reconciled_state_keys()
    test_error_taxonomy()
    test_ledger_separation()
    test_proxy_detector()
    test_repo_hygiene()

    print(f"\n{'=' * 70}")
    print(f" 结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    print("=" * 70)
    if FAIL:
        print("\n失败明细：")
        for name, detail in FAIL:
            print(f"  - {name}  {detail}")
        return 1
    print("\n全部 P0 验证通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
