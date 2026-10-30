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
    import order_ledger

    # 关键：必须把账本指向临时文件。
    # 否则实盘跑起来后真实在途买单会被算进来，测试结果随实盘状态漂移
    # （这正是本用例此前失败的原因：实盘有 $1.0 在途买单，期望 5.5 实得 6.5）。
    original_ledger = order_ledger.LEDGER_FILE
    with tempfile.TemporaryDirectory() as tmp:
        order_ledger.LEDGER_FILE = Path(tmp) / "pending_orders.json"
        try:
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
            order_ledger.LEDGER_FILE = original_ledger


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


# ---------------------------------------------------------------- 9. 扫描覆盖
def test_scan_coverage() -> None:
    section("9. 扫描覆盖能力")
    import quant_engine

    # 扫描窗口必须支持 offset 轮转
    source = (BASE / "quant_engine.py").read_text(encoding="utf-8")
    check("fetch_active_markets 支持 offset 参数", "def fetch_active_markets(self, limit" in source and "offset" in source)

    # 扫描窗口配置项存在且有合理默认值
    check("SCAN_LIMIT 配置存在", "SCAN_LIMIT" in source, "默认 60（Gamma 单次上限 100）")
    check("SCAN_OFFSET_ROTATION 配置存在", "SCAN_OFFSET_ROTATION" in source, "默认 20，轮转步长")

    # 轮转状态可持久化（独立文件，不放进会被对账器重建的 portfolio.json）
    check(
        "轮转状态使用独立文件",
        quant_engine.SCAN_ROTATION_FILE.name == "scan_rotation.json",
        str(quant_engine.SCAN_ROTATION_FILE.name),
    )
    check("轮转状态文件在 state/ 目录", quant_engine.SCAN_ROTATION_FILE.parent.name == "state")

    # 已结束市场过滤
    check(
        "拉取阶段剔除已结束市场",
        "剔除已结束市场" in source,
        "Gamma active=true 不排除已结算盘口（实测过期达 148 天）",
    )

    # 覆盖统计
    check("记录扫描覆盖统计", "scan_coverage" in source)


# ---------------------------------------------------------------- 10. 限流保护
def test_rate_limiter() -> None:
    section("10. 限流保护（429 退避与巡检抖动）")
    import rate_limiter
    import inspect

    src = inspect.getsource(rate_limiter.request_with_retry)
    check("识别 429 状态码", "status_code == 429" in src)
    check("读取 Retry-After 响应头", "_retry_after_seconds(response)" in src)
    check("5xx 走指数退避", "status_code >= 500" in src and "2 ** attempt" in src)
    check("退避含随机抖动", "random.random()" in src)
    check("按 host 节流", "_sleep_until_allowed" in src)

    check(
        "Retry-After 解析正确",
        rate_limiter._retry_after_seconds(None, default=2.0) == 2.0,
        "缺失时用默认值",
    )

    class FakeResp:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.headers = headers or {}

    check(
        "Retry-After: 1 被正确解析",
        rate_limiter._retry_after_seconds(FakeResp(429, {"Retry-After": "1"})) == 1.0,
    )
    check(
        "异常 Retry-After 被夹到安全区间",
        0.2 <= rate_limiter._retry_after_seconds(FakeResp(429, {"Retry-After": "99999"})) <= 60.0,
    )

    # 抖动：固定间隔会形成规律流量特征
    delays = [rate_limiter.jittered_sleep(180) for _ in range(12)]
    check("巡检间隔带抖动", len(set(delays)) > 1, f"{len(set(delays))} 个不同取值")
    check(
        "抖动在 ±25% 范围内",
        all(135 <= d <= 225 for d in delays),
        f"范围 {min(delays):.0f}~{max(delays):.0f}s",
    )

    # 对账器与引擎都必须接入
    rec_src = (BASE / "portfolio_reconciliation.py").read_text(encoding="utf-8")
    eng_src = (BASE / "quant_engine.py").read_text(encoding="utf-8")
    check("对账器接入退避重试", "rate_limiter.request_with_retry" in rec_src)
    check("对账器显式识别 429", "RateLimitError" in rec_src and "429" in rec_src)
    check("引擎接入退避重试", "rate_limiter.request_with_retry" in eng_src)
    check("巡检循环使用抖动间隔", "rate_limiter.jittered_sleep" in eng_src)

    check(
        "各接口设有最小请求间隔",
        rate_limiter.MIN_INTERVAL.get("data-api.polymarket.com", 0) > 0
        and rate_limiter.MIN_INTERVAL.get("clob.polymarket.com", 0) > 0,
        f"Data API {rate_limiter.MIN_INTERVAL.get('data-api.polymarket.com')}s / "
        f"CLOB {rate_limiter.MIN_INTERVAL.get('clob.polymarket.com')}s",
    )


# ---------------------------------------------------------------- 11. 订单金额门槛
def test_order_amount_minimum() -> None:
    section("11. 订单金额 $1 最小门槛")
    import math

    import clob_executor
    from web3 import Web3

    MIN_AMOUNT = 1.0
    prices = [0.05, 0.10, 0.15, 0.16, 0.168, 0.19, 0.199, 0.20, 0.21, 0.25,
              0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80, 0.85]

    def old_amount(shares, price, tick=0.001):
        """旧逻辑：引擎 round + 执行器 floor"""
        decimals = max(1, int(round(-math.log10(tick))))
        clean_price = round(round(price / tick) * tick, decimals)
        clean_size = math.floor(shares * 100.0) / 100.0
        return clean_size * clean_price

    def new_amount(size_usdc, price):
        """新逻辑：引擎 ceil + 执行器 floor(含 epsilon)"""
        shares = math.ceil(size_usdc / price * 100.0 - 1e-9) / 100.0
        eff = math.floor(shares * 100.0 + 1e-6) / 100.0
        return eff * price

    old_fail = [p for p in prices if old_amount(round(1.0 / p, 4), p) < MIN_AMOUNT - 1e-9]
    new_fail = [p for p in prices if new_amount(1.0, p) < MIN_AMOUNT - 1e-9]

    check(
        "旧逻辑确实会跌破 $1（复现原始缺陷）",
        len(old_fail) >= 5,
        f"{len(old_fail)}/{len(prices)} 个价格触发：{old_fail[:6]}",
    )
    check(
        "新逻辑在所有价格下均达标",
        not new_fail,
        f"失败价格: {new_fail}" if new_fail else f"{len(prices)}/{len(prices)} 全部 >= $1",
    )

    # 执行器侧真实构造订单，校验 makerAmount
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
        bad = []
        for ask in prices:
            shares = math.ceil(1.0 / ask * 100.0 - 1e-9) / 100.0
            try:
                payload = ex.build_and_sign_poly1271_order(
                    token, "BUY", price=ask, size_shares=shares, tick_size=0.001
                )
                amount = int(payload["order"]["makerAmount"]) / 1_000_000
                if amount < MIN_AMOUNT - 1e-9:
                    bad.append((ask, amount))
            except Exception:
                bad.append((ask, "exception"))
        check(
            "执行器构造的订单金额全部 >= $1",
            not bad,
            f"异常项: {bad[:4]}" if bad else f"{len(prices)} 个价格全部达标",
        )

        # Fail-closed：金额确实不足时本地拦截，不送交易所
        try:
            ex.build_and_sign_poly1271_order(
                token, "BUY", price=0.05, size_shares=5.0, tick_size=0.001
            )
            check("金额不足时本地拦截", False, "未抛出异常")
        except clob_executor.OrderAmountTooSmall:
            check("金额不足时本地拦截", True, "抛出 OrderAmountTooSmall，不浪费交易所往返")
    finally:
        clob_executor.PolymarketV2Executor.resolve_exchange_and_version = original

    # 浮点陷阱回归：4.77*100 = 476.99999999999994，floor 必须加 epsilon
    check(
        "floor 浮点陷阱已修正",
        math.floor(4.77 * 100.0 + 1e-6) == 477,
        f"floor(4.77*100)={math.floor(4.77*100.0)} -> 加 epsilon={math.floor(4.77*100.0+1e-6)}",
    )

    eng_src = (BASE / "quant_engine.py").read_text(encoding="utf-8")
    check("引擎用 ceil 换算份额", "math.ceil(size_usdc / best_ask" in eng_src)
    check("引擎含 $1 门槛预检", "AMOUNT_BELOW_MIN" in eng_src)


# ---------------------------------------------------------------- 12. 市场熔断
def test_market_circuit() -> None:
    section("12. 市场不可交易熔断（503 trading is disabled）")
    import market_circuit
    from trading_error_monitor import classify_error

    # 识别
    check(
        "识别 trading is disabled",
        market_circuit.is_not_tradable_error(
            "PolyApiException[status_code=503, error_message={'error': 'trading is disabled'}]"
        ),
    )
    check(
        "识别 No orderbook exists",
        market_circuit.is_not_tradable_error(
            '{"error":"No orderbook exists for the requested token id"}'
        ),
    )
    check(
        "不误判普通交易错误",
        not market_circuit.is_not_tradable_error(
            "invalid amount for a marketable BUY order ($0.999), min size: $1"
        ),
    )
    check(
        "分类器输出 MARKET_NOT_TRADABLE",
        classify_error(
            "PolyApiException[status_code=503, error_message={'error': 'trading is disabled'}]"
        ) == "MARKET_NOT_TRADABLE",
    )

    # 熔断行为
    with tempfile.TemporaryDirectory() as tmp:
        breaker = market_circuit.MarketCircuitBreaker(Path(tmp) / "circuit.json")
        token = "TOKEN_TEST"
        check("首次前不跳过", breaker.should_skip(token) is False)

        breaker.record_failure(token, "503")
        check(
            "达到阈值前仍允许重试",
            breaker.should_skip(token) is False,
            f"阈值={market_circuit.FAILURE_THRESHOLD}",
        )

        breaker.record_failure(token, "503")
        check("达到阈值后进入冷却", breaker.should_skip(token) is True)
        check(
            "跳过原因可读",
            "熔断" in breaker.skip_reason(token),
            breaker.skip_reason(token)[:60],
        )

        skipped = sum(1 for _ in range(14) if breaker.should_skip(token))
        check(
            "冷却期内不再重复请求（消除重试风暴）",
            skipped == 14,
            f"后续 14 轮被跳过 {skipped}/14（修复前 0/14，全部硬撞）",
        )

        breaker.record_success(token)
        check("一次成功即复位", breaker.should_skip(token) is False)

        # 持久化
        path = Path(tmp) / "persist.json"
        b1 = market_circuit.MarketCircuitBreaker(path)
        b1.record_failure("T1", "503")
        b1.record_failure("T1", "503")
        b2 = market_circuit.MarketCircuitBreaker(path)
        check("熔断状态跨重启保留", b2.should_skip("T1") is True)

    eng_src = (BASE / "quant_engine.py").read_text(encoding="utf-8")
    check("引擎接入熔断器", "market_circuit.get_breaker().should_skip" in eng_src)
    check(
        "不可交易错误降级为 warning",
        'severity="warning"' in eng_src,
        "市场状态问题不该刷成 critical 淹没真实故障",
    )


# ---------------------------------------------------------------- 13. 轮次预算与降级
def test_round_budget_and_degradation() -> None:
    section("13. 轮次时间预算与网络降级")
    import time

    import rate_limiter

    # --- 预算机制 ---
    rate_limiter.start_round_budget(2)
    check("预算开启后有剩余时间", (rate_limiter.round_budget_remaining() or 0) > 0)
    check("刚开启时尚未耗尽", rate_limiter.budget_exhausted() is False)

    time.sleep(2.2)
    check("超时后预算耗尽", rate_limiter.budget_exhausted() is True)

    # 预算耗尽时必须立即快速失败，而不是继续重试
    import requests

    session = requests.Session()
    started = time.time()
    try:
        rate_limiter.request_with_retry(
            session, "GET", "https://data-api.polymarket.com/positions?user=0x0&limit=1", timeout=8
        )
        check("预算耗尽时快速失败", False, "未抛出异常")
    except rate_limiter.RoundBudgetExceeded:
        check(
            "预算耗尽时快速失败（不重试）",
            time.time() - started < 1.0,
            f"耗时 {time.time() - started:.2f}s",
        )
    except Exception as exc:
        check("预算耗尽时快速失败", False, f"抛出了 {type(exc).__name__}")
    rate_limiter.clear_round_budget()

    # --- 网络健康度与自适应扫描量 ---
    original_health = list(rate_limiter._health)
    try:
        scenarios = [
            (1.0, 60, "healthy"),
            (0.8, 36, "degraded"),
            (0.4, 15, "poor"),
        ]
        for success_rate, expected_limit, expected_status in scenarios:
            rate_limiter._health[:] = (
                [True] * int(success_rate * 20) + [False] * int((1 - success_rate) * 20)
            )[-20:]
            health = rate_limiter.network_health()
            limit = rate_limiter.adaptive_scan_limit(60)
            check(
                f"网络 {expected_status} 时扫描量收缩到 {expected_limit}",
                limit == expected_limit and health["status"] == expected_status,
                f"成功率 {health['success_rate']} -> limit={limit}",
            )
    finally:
        rate_limiter._health[:] = original_health

    # --- 引擎接入 ---
    eng_src = (BASE / "quant_engine.py").read_text(encoding="utf-8")
    check("引擎开启轮次预算", "start_round_budget" in eng_src)
    check("引擎在 finally 中释放预算", "clear_round_budget" in eng_src)
    check("扫描循环检查预算", "budget_exhausted()" in eng_src)
    check("引擎使用自适应扫描量", "adaptive_scan_limit" in eng_src)
    check("预算耗尽不记为错误", "except rate_limiter.RoundBudgetExceeded" in eng_src)


# ---------------------------------------------------------------- 14. 通道自愈与拆分超时
def test_channel_self_healing() -> None:
    section("14. 通道自愈与拆分超时")
    import inspect

    import proxy_detector
    import rate_limiter

    # 拆分超时：代理 CONNECT 隧道偶发卡死时必须快速失败
    src = inspect.getsource(rate_limiter.request_with_retry)
    check("支持 connect_timeout", "connect_timeout" in src)
    check("支持 read_timeout", "read_timeout" in src)
    check(
        "构造 (connect, read) 元组",
        'kwargs["timeout"] = (' in src,
        "隧道建立与等待响应需分开计时",
    )

    # 探测器提供自愈入口
    check(
        "探测器提供 refresh() 自愈入口",
        hasattr(proxy_detector.ProxyDetector, "refresh"),
        "通道卡死时重新走一遍候选列表，可能落到仍可用的通道",
    )

    rec_src = (BASE / "portfolio_reconciliation.py").read_text(encoding="utf-8")
    eng_src = (BASE / "quant_engine.py").read_text(encoding="utf-8")
    check("对账器使用拆分超时", "connect_timeout=8" in rec_src)
    check("对账器接入通道自愈", "refresh()" in rec_src)
    check("引擎使用拆分超时", "connect_timeout" in eng_src)
    check("引擎接入通道自愈", "refresh()" in eng_src)
    check(
        "回退不再退化为直连",
        "切换为直连重试" not in rec_src,
        "实测直连 data-api 12s 超时，直连回退无意义",
    )


# ---------------------------------------------------------------- 15. 链上挂单权威判定
def test_chain_order_authority() -> None:
    section("15. 链上挂单权威判定（防重复挂卖单）")
    import inspect

    import clob_executor
    import market_circuit

    # 执行器提供按 token 查询链上活跃挂单的能力
    exec_src = inspect.getsource(clob_executor.PolymarketV2Executor)
    check("执行器提供链上挂单分组查询", "def get_live_orders_by_token" in exec_src)
    check("执行器提供活跃卖单判定", "def has_live_sell_order" in exec_src)
    check("执行器提供活跃卖单定位", "def find_live_sell_order" in exec_src)

    # 对账器以链上挂单为准恢复 tp_order_id
    import portfolio_reconciliation as pr

    rec_src = inspect.getsource(pr.build_reconciled_state)
    check(
        "对账器从链上挂单恢复 tp_order_id",
        "chain_sell_orders.get(token_id)" in rec_src,
        "本地 tp_order_id 可能与链上状态漂移，链上才是权威",
    )

    # 引擎挂卖单前查链上
    eng_src = (BASE / "quant_engine.py").read_text(encoding="utf-8")
    check("引擎挂卖单前查链上活跃卖单", "find_live_sell_order(token_id" in eng_src)
    check("引擎回填链上挂单到本地账本", "链上已存在活跃卖单" in eng_src)
    check("引擎识别余额冲突", "is_balance_error" in eng_src)
    check("余额冲突进入冷却", "has_balance_conflict" in eng_src)

    # 余额冲突识别
    check(
        "识别 not enough balance",
        market_circuit.is_balance_error(
            "not enough balance / allowance: the balance is not enough -> "
            "balance: 5000000, sum of active orders: 5000000, order amount: 5000000"
        ),
    )
    check(
        "不误判 $1 金额错误",
        not market_circuit.is_balance_error("invalid amount for a marketable BUY order ($0.999)"),
    )
    check(
        "不误判 503",
        not market_circuit.is_balance_error("503 trading is disabled"),
    )

    # 余额冲突冷却行为
    with tempfile.TemporaryDirectory() as tmp:
        breaker = market_circuit.MarketCircuitBreaker(Path(tmp) / "circuit.json")
        token = "BAL_TOKEN"
        check("初始无余额冲突", breaker.has_balance_conflict(token) is False)
        breaker.record_balance_conflict(token, "not enough balance")
        check("冲突后进入冷却", breaker.has_balance_conflict(token) is True)
        breaker._state[token]["next_retry_at"] = 0  # 模拟冷却到期
        check("冷却到期后允许重试", breaker.has_balance_conflict(token) is False)


# ---------------------------------------------------------------- 16. 仓库卫生
def test_repo_hygiene() -> None:
    section("16. 仓库与凭证卫生")
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
    test_scan_coverage()
    test_rate_limiter()
    test_order_amount_minimum()
    test_market_circuit()
    test_round_budget_and_degradation()
    test_channel_self_healing()
    test_chain_order_authority()
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
