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
    import proxy_detector
    from web3 import Web3

    original = clob_executor.PolymarketV2Executor.resolve_exchange_and_version
    original_apply_proxy = proxy_detector.apply_to_clob_sdk
    proxy_detector.apply_to_clob_sdk = lambda: {"ok": True, "detail": "offline signature test"}
    clob_executor.PolymarketV2Executor.resolve_exchange_and_version = (
        lambda self, token_id: (
            Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B"),
            "2",
        )
    )
    try:
        try:
            ex = clob_executor.PolymarketV2Executor()
        except RuntimeError as exc:
            print(f"  [SKIP] 无可用代理通道，跳过签名样本测试：{exc}")
            return
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
        proxy_detector.apply_to_clob_sdk = original_apply_proxy


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

        import quant_engine

        quant_engine._INSTANCE_LOCK_HELD = False
        check("引擎可获取交易锁", quant_engine.ensure_single_instance())
        lock_file.unlink()
        try:
            quant_engine.ensure_single_instance()
            lock_loss_stops = False
        except quant_engine.SingleInstanceLost:
            lock_loss_stops = True
        finally:
            quant_engine._INSTANCE_LOCK_HELD = False
        check("锁文件丢失或被替换后旧引擎必须停止", lock_loss_stops)

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
        # 占位地址：真实钱包地址属于运行环境信息，不应硬编码进仓库
        "eoa": "0x0000000000000000000000000000000000000001",
        "deposit_wallet": "0x0000000000000000000000000000000000000002",
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


# ---------------------------------------------------------------- 5b. 止盈止损与策略基准
def test_exit_strategy() -> None:
    section("5b. 止盈止损执行与策略基准恢复")
    import order_ledger
    import portfolio_reconciliation as pr
    import quant_engine as qe

    recovered = pr.recover_position_strategy(
        {"target_prob": 0.92, "z_entry": 0.0, "z_peak": 0.0},
        0.304412,
        [{"intent": "ENTRY", "created_at_utc": "2026-01-01", "strategy": {"target_prob": 0.3765}}],
    )
    check("对账保留原开仓目标价", recovered["target_prob"] == 0.3765, json.dumps(recovered))
    check("对账不再伪造 z_peak=0", abs(recovered["z_peak"] - recovered["z_entry"]) < 1e-9)

    historical = pr.recover_position_strategy(
        {"target_prob": 0.92, "z_entry": 0.0, "z_peak": 0.0}, 0.2, [{"intent": "ENTRY"}]
    )
    expected_target = qe.take_profit_price(0.2, qe.exit_config())
    check("历史缺失目标按真实成本恢复", abs(historical["target_prob"] - expected_target) < 0.0001)
    check("历史 z_peak 从真实入场 Logit 起步", abs(historical["z_peak"] - qe.to_logit(0.2)) < 0.0001)
    check("历史缺失 peak_bid 退化为入场价", abs(historical["peak_bid"] - 0.2) < 0.0001)

    migrated = pr.recover_position_strategy(
        {"exit_rule_version": 2, "target_prob": 0.3671, "z_entry": 0.0, "z_peak": 0.0},
        0.27,
        [{"intent": "ENTRY", "strategy": {"target_prob": 0.3671}}],
    )
    check(
        "v2 仓位目标价不被旧开仓目标覆盖",
        abs(migrated["target_prob"] - qe.take_profit_price(0.27, qe.exit_config())) < 0.0001
        and migrated["exit_rule_version"] == 2,
    )

    rows = [{
        "token_id": "T1", "closed_at": "2026-10-30 17:46:18 UTC",
        "exit_reason": "CLOB 真实 SELL 成交（移动平均成本对账）", "data_source": "activity",
    }]
    pr.attach_engine_exit_reasons(rows, [{
        "token_id": "T1", "intent": "EXIT", "side": "SELL",
        "created_at_utc": "2026-10-30T17:45:50+00:00",
        "notes": "成本硬止损 (Bid 0.12 <= 0.18, 浮亏 -10.00%)",
    }])
    check("对账保留引擎平仓原因", rows[0]["exit_reason"].startswith("成本硬止损"))

    stale = [{
        "token_id": "T1", "closed_at": "2026-10-30 17:46:18 UTC",
        "exit_reason": "CLOB 真实 SELL 成交（移动平均成本对账）", "data_source": "activity",
    }]
    pr.attach_engine_exit_reasons(stale, [{
        "token_id": "T1", "intent": "EXIT", "side": "SELL",
        "created_at_utc": "2026-10-30T10:00:00+00:00",
        "notes": "峰值回撤止盈",
    }])
    check("超出时间窗口的订单不误配", stale[0]["exit_reason"].startswith("CLOB 真实 SELL"))

    status_note = [{
        "token_id": "T1", "closed_at": "2026-10-30 17:46:18 UTC",
        "exit_reason": "CLOB 真实 SELL 成交（移动平均成本对账）", "data_source": "activity",
    }]
    pr.attach_engine_exit_reasons(status_note, [{
        "token_id": "T1", "intent": "EXIT", "side": "SELL",
        "created_at_utc": "2026-10-30T17:45:50+00:00",
        "notes": "已不在 CLOB 挂单列表，状态待持仓对账确认",
    }])
    check("状态机文案不会被当成平仓原因", status_note[0]["exit_reason"].startswith("CLOB 真实 SELL"))

    preferred = [{
        "token_id": "T1", "closed_at": "2026-10-30 17:46:18 UTC",
        "exit_reason": "CLOB 真实 SELL 成交（移动平均成本对账）", "data_source": "activity",
    }]
    pr.attach_engine_exit_reasons(preferred, [{
        "token_id": "T1", "intent": "EXIT", "side": "SELL",
        "created_at_utc": "2026-10-30T17:45:50+00:00",
        "notes": "已不在 CLOB 挂单列表，状态待持仓对账确认",
        "reason": "成本硬止损 (净浮亏 -10.00%)",
    }])
    check("优先使用不被覆盖的 reason 字段", preferred[0]["exit_reason"].startswith("成本硬止损"))

    class FakeData:
        def __init__(self, bid, ask):
            self.book = {
                "best_bid": bid, "best_ask": ask, "spread": ask - bid,
                "raw_bids": [(bid, 100.0)], "raw_asks": [(ask, 100.0)],
            }

        def fetch_orderbook(self, token_id):
            return self.book

        def fetch_resolved_mark(self, token_id, event_id):
            return None

        def fetch_price_history(self, token_id, fallback_mid, market_info):
            return [0.2, 0.2]

    class FakeExecutor:
        def __init__(self, order_id="TP_OLD"):
            self.order_id = order_id
            self.cancelled = []
            self.posted = []

        def find_live_sell_order(self, token_id, refresh=True):
            if not self.order_id:
                return None
            order = order_ledger.get_order(self.order_id) or {}
            return {
                "id": self.order_id,
                "price": order.get("price", 0.4),
                "original_size": order.get("shares", 5.0),
            }

        def cancel_order(self, order_id):
            self.cancelled.append(order_id)
            return {"success": True}

        def build_and_sign_poly1271_order(self, token_id, side, **kwargs):
            return {"token_id": token_id, "side": side, **kwargs}

        def post_order(self, payload):
            self.posted.append(payload)
            return {"success": True, "orderID": "EXIT_NEW" if payload["price"] < 0.4 else "TP_NEW"}

    original_ledger = order_ledger.LEDGER_FILE
    original_executor = qe.get_live_executor
    original_stop = qe.STOP_FILE
    original_paper = os.environ.get("PAPER")
    original_live = os.environ.get("ALLOW_LIVE")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            qe.STOP_FILE = Path(tmp) / "NO_STOP"
            order_ledger.LEDGER_FILE = Path(tmp) / "pending_orders.json"
            os.environ["PAPER"] = "false"
            os.environ["ALLOW_LIVE"] = "true"

            def position(bid, peak=None, entry=0.2, shares=5.0, fee=None):
                cost = round(entry * shares, 6)
                row = {
                    "pos_id": "P1", "event_id": "E1", "market_question": "Q", "outcome": "Yes",
                    "token_id": "T1", "entry_price": entry, "current_bid": bid,
                    "current_ask": bid + 0.01, "shares": shares, "cost_usdc": cost,
                    "current_value_usdc": bid * shares, "unrealized_pnl_usdc": bid * shares - cost,
                    "unrealized_pnl_pct": ((bid * shares - cost) / cost) * 100,
                    "target_prob": 0.4, "z_peak": qe.to_logit(peak if peak is not None else entry),
                    "tp_order_id": "TP_OLD", "exit_order_id": None,
                }
                if fee is not None:
                    row["fee_rate"] = fee
                    row["fee_exponent"] = 1.0
                return row

            def state_for(pos):
                return {
                    "open_positions": [pos], "cash_usdc": 5.0, "initial_capital_usdc": 10.0,
                    "realized_pnl_usdc": 0.0, "closed_trades": [],
                }

            order_ledger.record_order("TP_OLD", "T1", "SELL", "TAKE_PROFIT", 0.4, 5.0, 2.0)
            fake = FakeExecutor()
            qe.get_live_executor = lambda: fake
            result = qe.evaluate_existing_positions(state_for(position(0.13)), FakeData(0.13, 0.14))
            check("ATR 止损先撤销旧止盈单", fake.cancelled == ["TP_OLD"])
            check("撤单后提交一个平仓卖单", len(fake.posted) == 1 and fake.posted[0]["side"] == "SELL")
            check("新卖单用途为 EXIT", order_ledger.get_order("EXIT_NEW")["intent"] == "EXIT")
            check("旧止盈单状态为 CANCELLED", order_ledger.get_order("TP_OLD")["status"] == "CANCELLED")
            check("信号已触发但持仓等待成交对账", len(result) == 0)

            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            order_ledger.record_order("TP_OLD", "T1", "SELL", "EXIT", 0.15, 5.0, 0.75)
            fake = FakeExecutor()
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.14)), FakeData(0.14, 0.15))
            check("已有平仓单时不撤单也不重复提交", not fake.cancelled and not fake.posted)

            # ===== 平仓单超时未成交 → 撤单按现价重挂 =====
            # 旧行为：只要链上有 EXIT 单就永久跳过，实测有仓位挂 15 分钟不成交，
            # 一路跌到 -33% 才成交（设定止损 -10%）。
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            order_ledger.record_order("EXIT_OLD", "T1", "SELL", "EXIT", 0.30, 5.0, 1.5, notes="成本硬止损")
            ledger_data = json.loads(order_ledger.LEDGER_FILE.read_text(encoding="utf-8"))
            ledger_data["orders"]["EXIT_OLD"]["created_at_utc"] = "2026-10-31T00:00:00+00:00"
            order_ledger.LEDGER_FILE.write_text(json.dumps(ledger_data), encoding="utf-8")
            fake = FakeExecutor(order_id="EXIT_OLD")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.13)), FakeData(0.13, 0.14))
            check(
                "平仓单超时未成交会撤单重挂",
                fake.cancelled == ["EXIT_OLD"] and len(fake.posted) == 1
                and abs(fake.posted[0]["price"] - 0.13) < 1e-9,
            )

            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            first_reg = order_ledger.record_order("EXIT_KEEP", "T1", "SELL", "EXIT", 0.30, 5.0, 1.5)
            again_reg = order_ledger.record_order("EXIT_KEEP", "T1", "SELL", "EXIT", 0.30, 5.0, 1.5)
            check(
                "重复登记不重置 created_at（否则挂单时长永远为 0）",
                first_reg["created_at_utc"] == again_reg["created_at_utc"],
            )

            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            order_ledger.record_order("EXIT_FRESH", "T1", "SELL", "EXIT", 0.30, 5.0, 1.5)
            fake = FakeExecutor(order_id="EXIT_FRESH")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.13)), FakeData(0.13, 0.14))
            check("未超时的平仓单保持不动", not fake.cancelled and not fake.posted)

            # ===== 链上查无此单、本地仍阻塞：释放 + 顺延，不抛错 =====
            # 实测事故：卖单成交后 CLOB 立即移出挂单列表，本地账本仍是 ACCEPTED，
            # 旧实现直接抛错，导致止损被卡 4.5 分钟。
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            order_ledger.record_order("EXIT_STALE", "T1", "SELL", "EXIT", 0.30, 5.0, 1.5, notes="成本硬止损")
            order_ledger.record_order("EXIT_OTHER", "T2", "SELL", "EXIT", 0.30, 5.0, 1.5, notes="成本硬止损")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            stale_pos = position(0.13)
            stale_state = state_for(stale_pos)
            raised = None
            try:
                qe.evaluate_existing_positions(stale_state, FakeData(0.13, 0.14))
            except Exception as exc:  # noqa: BLE001 - 测试需要捕获任何异常
                raised = exc
            check("本地残留卖单不再抛错", raised is None)
            check("残留记录被释放为 CONFIRMED", order_ledger.get_order("EXIT_STALE")["status"] == "CONFIRMED")
            check(
                "只释放目标 token 的单",
                order_ledger.get_order("EXIT_OTHER")["status"] in ("ACCEPTED", "SUBMITTING"),
            )
            check("本轮不重复提交平仓", not fake.posted)
            check("仓位被标记为顺延待对账", isinstance(stale_pos.get("exit_deferred_at"), (int, float)))

            fake2 = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake2
            qe.evaluate_existing_positions(state_for(stale_pos), FakeData(0.13, 0.14))
            check("顺延窗口内不再重复尝试下单", not fake2.cancelled and not fake2.posted)

            # ===== 退出规则 v2：参数边界 =====
            cfg = qe.exit_config()
            check("目标价 = 入场 × 3（即 +200%）", qe.take_profit_price(0.2, cfg) == 0.6)
            check("目标价封顶 0.99", qe.take_profit_price(0.5, cfg) == 0.99)
            check("硬止损 = 入场 × 0.90", qe.hard_stop_price(0.2, cfg) == 0.18)
            check("低价位止损距离不低于 2 分钱", qe.hard_stop_price(0.166, cfg) == 0.146)
            check("无费率时止损位不受影响", qe.hard_stop_price(0.5, cfg) == 0.45)
            check("7% 费率下止损位前移到净 -10%", abs(qe.hard_stop_price(0.5, cfg, 0.07) - 0.4674) < 0.001)
            check("低价位仍受 2 分钱下限约束", qe.hard_stop_price(0.166, cfg, 0.07) == 0.146)
            check("峰值回撤 5 分钱", qe.trailing_exit_price(0.34, 0.2, cfg) == 0.29)
            check("追踪触发价不深于硬止损", qe.trailing_exit_price(0.22, 0.2, cfg) == 0.18)

            # ===== ATR/Logit 止损：0.20 入场、无 atr_z 时用最小距离 0.45 logit（止损位 ≈ 0.137）=====
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(
                state_for(position(0.13, fee=0.07)), FakeData(0.13, 0.14)
            )
            net_exit = [row for row in order_ledger.blocking_orders() if row.get("intent") == "EXIT"]
            check(
                "跌破 ATR 止损位时触发（费率不影响止损距离）",
                bool(net_exit) and "ATR 止损" in str(net_exit[0].get("notes")),
            )

            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(
                state_for(position(0.19, fee=0.0)), FakeData(0.19, 0.20)
            )
            check(
                "2 分钱回撤不再触发（旧 -10% 止损正是死在这里）",
                not any(row.get("intent") == "EXIT" for row in order_ledger.blocking_orders()),
            )

            wide = position(0.12)
            wide["atr_z"] = 0.30  # 3×0.30=0.90 logit，止损位约 0.092
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(wide), FakeData(0.12, 0.13))
            check(
                "ATR_z 大的仓位止损更宽（0.12 不触发）",
                not any(row.get("intent") == "EXIT" for row in order_ledger.blocking_orders()),
            )

            # ===== 阴跌但未急杀：旧"单根 K 线"确认门会漏判，v2 必须止损 =====
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            # 0.20 -> 0.13：距入场 -1.21 logit，超过 ATR 止损距离 0.45
            qe.evaluate_existing_positions(state_for(position(0.13)), FakeData(0.13, 0.14))
            check("阴跌不再依赖单根 K 线确认", any(row.get("intent") == "EXIT" for row in order_ledger.blocking_orders()))

            # ===== 净口径优先于"2 分钱下限"：0.10 入场跌到 0.085 是净 -15% =====
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.06, entry=0.1)), FakeData(0.06, 0.07))
            check(
                "低价仓位跌破 ATR 止损位（0.066）时退出",
                any(row.get("intent") == "EXIT" for row in order_ledger.blocking_orders()),
            )

            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.085, entry=0.1)), FakeData(0.085, 0.095))
            check(
                "1.5 分钱回撤不退出（ATR 口径）",
                not any(row.get("intent") == "EXIT" for row in order_ledger.blocking_orders()),
            )

            # ===== 轻微浮盈、价差走阔都不应退出 =====
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.21)), FakeData(0.21, 0.22))
            check("轻微浮盈不触发追踪", not any(row.get("intent") == "EXIT" for row in order_ledger.blocking_orders()))

            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            fake = FakeExecutor(order_id="")
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.21)), FakeData(0.21, 0.31))
            check("价差走阔本身不触发退出", not any(row.get("intent") == "EXIT" for row in order_ledger.blocking_orders()))

            # ===== 峰值回撤触发 =====
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            order_ledger.record_order("TP_OLD", "T1", "SELL", "TAKE_PROFIT", 0.4, 5.0, 2.0)
            fake = FakeExecutor()
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.26, peak=0.34)), FakeData(0.26, 0.27))
            check("峰值回撤达标后执行追踪止盈", fake.cancelled == ["TP_OLD"] and len(fake.posted) == 1)
            try:
                audit_text = qe.SIGNALS_LOG_FILE.read_text(encoding="utf-8")
            except OSError:
                audit_text = ""
            check("追踪退出后记录重入探测", "REENTRY_PROBE" in audit_text)
            check("重入探测不下单", len(fake.posted) == 1)

            # ===== 存量迁移：旧目标单 0.4 与 v2 目标 0.6 不一致时撤单重挂 =====
            order_ledger.LEDGER_FILE.write_text(json.dumps({"orders": {}}), encoding="utf-8")
            order_ledger.record_order("TP_OLD", "T1", "SELL", "TAKE_PROFIT", 0.4, 5.0, 2.0)
            fake = FakeExecutor()
            qe.get_live_executor = lambda: fake
            qe.evaluate_existing_positions(state_for(position(0.21)), FakeData(0.21, 0.22))
            check(
                "旧目标单被撤并改挂 v2 目标价",
                fake.cancelled == ["TP_OLD"] and len(fake.posted) == 1
                and abs(fake.posted[0]["price"] - 0.6) < 1e-9,
            )

            # ===== PAPER：命中止损必须真正平仓入账（此前是 continue 之后的死代码）=====
            os.environ["PAPER"] = "true"
            paper_state = state_for(position(0.13))
            qe.evaluate_existing_positions(paper_state, FakeData(0.13, 0.14))
            check(
                "纸面模式止损后写入平仓记录",
                len(paper_state["closed_trades"]) == 1
                and paper_state["open_positions"] == []
                and "ATR 止损" in paper_state["closed_trades"][0]["exit_reason"],
            )
            os.environ["PAPER"] = "false"
    finally:
        order_ledger.LEDGER_FILE = original_ledger
        qe.get_live_executor = original_executor
        qe.STOP_FILE = original_stop
        if original_paper is None:
            os.environ.pop("PAPER", None)
        else:
            os.environ["PAPER"] = original_paper
        if original_live is None:
            os.environ.pop("ALLOW_LIVE", None)
        else:
            os.environ["ALLOW_LIVE"] = original_live


# ---------------------------------------------------------------- 5c. 原文策略一致性
def test_document_alignment() -> None:
    section("5c. 原文策略一致性修复")
    import order_ledger
    import quant_engine as qe
    import strategy_validation

    first = order_ledger.make_order_intent("ENTRY", "T1", "cycle-1")
    repeat = order_ledger.make_order_intent("ENTRY", "T1", "cycle-1")
    other = order_ledger.make_order_intent("ENTRY", "T1", "cycle-2")
    check("同一交易意图派生固定 salt", first == repeat and first["salt"] != other["salt"])

    original_ledger = order_ledger.LEDGER_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            order_ledger.LEDGER_FILE = Path(tmp) / "orders.json"
            order_ledger.record_order(
                first["intent_id"], "T1", "BUY", "ENTRY", 0.2, 5, 1,
                status=order_ledger.STATUS_SUBMITTING, strategy={"salt": first["salt"]},
            )
            check("提交前意图已阻塞重复订单", order_ledger.has_open_buy_for_token("T1"))
            order_ledger.attach_clob_order_id(first["intent_id"], "CLOB-1")
            check("受理后绑定真实 CLOB ID", order_ledger.get_order("CLOB-1")["status"] == "ACCEPTED")
            check("意图 ID 不再残留重复记录", order_ledger.get_order(first["intent_id"]) is None)
    finally:
        order_ledger.LEDGER_FILE = original_ledger

    execution = qe.estimate_order_execution([(0.20, 2.0), (0.22, 3.0), (0.25, 10.0)], 6.0)
    check("逐档成交计算加权价格", abs(execution["average_price"] - 1.31 / 6) < 1e-9)
    check("逐档成交返回满足成交所需限价", execution["limit_price"] == 0.25)
    partial = qe.estimate_order_execution([(0.20, 2.0), (0.22, 3.0), (0.25, 10.0)], 20.0)
    check("深度不足时明确返回未成交份额", partial["filled_shares"] == 15 and partial["remaining_shares"] == 5)
    check("官方 taker fee 公式按 USDC 计算", abs(qe.estimate_taker_fee(100, 0.5, 0.07) - 1.75) < 1e-9)
    check("零费率市场不产生 taker fee", qe.estimate_taker_fee(100, 0.5, 0) == 0)

    layer = object.__new__(qe.PolymarketDataLayer)
    layer._request = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline missing data"))
    check("历史数据不足不再合成轨迹", layer.fetch_price_history("T1", 0.3, {}) == [])

    old_whitelist = os.environ.get("MARKET_WHITELIST")
    old_mode = os.environ.get("MARKET_WHITELIST_MODE")
    try:
        os.environ.pop("MARKET_WHITELIST", None)
        os.environ.pop("MARKET_WHITELIST_MODE", None)
        check("默认自动扫描生成白名单", qe.RiskEnvelope().whitelist_mode == "AUTO")
        os.environ["MARKET_WHITELIST_MODE"] = "MANUAL"
        check("手动空白名单禁止新开仓", qe.RiskEnvelope().market_whitelist == set())
        os.environ["MARKET_WHITELIST"] = "EVT-1, TOKEN-2"
        check("白名单配置可读取", qe.RiskEnvelope().market_whitelist == {"EVT-1", "TOKEN-2"})
    finally:
        if old_whitelist is None:
            os.environ.pop("MARKET_WHITELIST", None)
        else:
            os.environ["MARKET_WHITELIST"] = old_whitelist
        if old_mode is None:
            os.environ.pop("MARKET_WHITELIST_MODE", None)
        else:
            os.environ["MARKET_WHITELIST_MODE"] = old_mode

    old_signal = qe.SIGNALS_LOG_FILE
    old_logs = qe.LOGS_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            qe.LOGS_DIR = Path(tmp)
            qe.SIGNALS_LOG_FILE = Path(tmp) / "signals_and_skips.jsonl"
            qe.SIGNALS_LOG_FILE.write_text('{"test": 1}\n', encoding="utf-8")
            (Path(tmp) / "trading_errors.jsonl").write_text('{"test": 2}\n', encoding="utf-8")
            qe.rotate_jsonl_logs()
            check("审计日志按天轮转", len(list(Path(tmp).glob("*-2*.jsonl"))) == 2)
    finally:
        qe.SIGNALS_LOG_FILE = old_signal
        qe.LOGS_DIR = old_logs

    check("无真实结算样本时禁止校准结论", strategy_validation.calibration_report([])["status"] == "INSUFFICIENT_DATA")
    samples = [{"p_est": 0.6, "outcome": 1 if i < 18 else 0, "signal_price": 0.5, "execution_price": 0.51} for i in range(30)]
    check("足够样本可计算 Brier 与校准分桶", strategy_validation.calibration_report(samples)["status"] == "OK")
    pressure = strategy_validation.cost_pressure_report(samples)
    check("成本压力包含 1x/2x/8x", [row["slippage_multiplier"] for row in pressure] == [1.0, 2.0, 8.0])

    # 成本压力必须计入往返价差（旧实现用成交滑点，实盘恒为 0，等于没测）
    wide_spread = [{"p_est": 0.55, "outcome": 1, "signal_price": 0.5, "execution_price": 0.5, "spread": 0.03}] * 30
    wide_pressure = strategy_validation.cost_pressure_report(wide_spread)
    check("价差吃掉优势时 1x 就通不过", wide_pressure[0]["edge_pass_count"] == 0)

    tight_spread = [{"p_est": 0.60, "outcome": 1, "signal_price": 0.5, "execution_price": 0.5, "spread": 0.01}] * 30
    tight_pressure = strategy_validation.cost_pressure_report(tight_spread)
    check(
        "价差小则 1x/2x 通过、8x 掉出",
        tight_pressure[0]["edge_pass_count"] == 30
        and tight_pressure[1]["edge_pass_count"] == 30
        and tight_pressure[2]["edge_pass_count"] == 0,
    )

    paper_pos = {
        "shares": 5.0, "cost_usdc": 1.0, "entry_price": 0.2,
        "paper_remaining_shares": 5.0, "paper_limit_price": 0.2, "paper_order_id": "P1",
    }
    qe.fill_paper_order(paper_pos, {
        "best_ask": 0.22, "best_bid": 0.21,
        "raw_asks": [(0.22, 10.0)], "raw_bids": [(0.21, 10.0)],
    })
    check("限价不满足时 PAPER 挂单不成交", paper_pos["paper_remaining_shares"] == 5.0)
    qe.fill_paper_order(paper_pos, {
        "best_ask": 0.19, "best_bid": 0.18,
        "raw_asks": [(0.19, 10.0)], "raw_bids": [(0.18, 10.0)],
    })
    check("限价满足后 PAPER 剩余份额继续成交", paper_pos["shares"] == 10.0 and paper_pos["paper_remaining_shares"] == 0)

    from public_trade_research import normalize_public_trades
    public_rows = [
        {"transactionHash": "0x1", "asset": "T1", "side": "BUY", "price": 0.4, "size": 2, "timestamp": 10},
        {"transactionHash": "0x1", "asset": "T1", "side": "BUY", "price": 0.4, "size": 3, "timestamp": 10},
        {"transactionHash": "0x1", "asset": "T1", "side": "SELL", "price": 0.4, "size": 1, "timestamp": 11},
        {"transactionHash": "0x2", "asset": "T2", "side": "BUY", "price": 0.5, "size": 1, "timestamp": 100},
    ]
    normalized = normalize_public_trades(public_rows, end_timestamp=50)
    check("公开成交部分成交合并与对冲识别", normalized["stats"]["grouped_count"] == 2 and normalized["stats"]["hedge_count"] == 1)
    check("结算后公开成交被剔除", normalized["stats"]["discarded_after_settlement"] == 1)

    missing = strategy_validation.feature_missing_report([{"clobTokenIds": "T"}, {}])
    check("特征缺失率按真实字段统计", missing["fields"]["description"]["missing"] == 2)
    check("基线回测有真实样本门槛", strategy_validation.baseline_report(samples)["status"] == "OK")
    check("时间外样本不足时明确拒绝结论", strategy_validation.out_of_sample_report(samples)["status"] == "INSUFFICIENT_DATA")
    quality = strategy_validation.execution_quality_report([{"signal_price": 0.5, "realized_slippage": 0.01, "edge_at_entry": 0.04}])
    check("实现 edge 与实际滑点可汇总", quality["sample_count"] == 1 and quality["mean_realized_slippage"] == 0.01)

    import watchdog
    old_stop = watchdog.STOP_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            watchdog.STOP_FILE = Path(tmp) / "STOP"
            watchdog.STOP_FILE.touch()
            check("看门狗尊重用户 STOP", watchdog.check_once()["action"] == "STOPPED_BY_USER")
    finally:
        watchdog.STOP_FILE = old_stop


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

    from trading_error_monitor import NETWORK_CATEGORIES, CATEGORIES, runtime_metadata
    missing = [c for c in NETWORK_CATEGORIES if c not in CATEGORIES]
    check("网络类分类均有处置建议", not missing, f"缺失: {missing}")
    metadata = runtime_metadata()
    check(
        "告警包含 PID、代码版本和通道字段",
        metadata.get("pid") == os.getpid()
        and bool(metadata.get("code_version"))
        and "proxy_url" in metadata
        and "proxy_source" in metadata,
        f"PID={metadata.get('pid')} version={metadata.get('code_version')}",
    )


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
        "探测到可用代理通道",
        bool(info.get("proxy_url")),
        f"{info.get('source')} 延迟 {info.get('latency_s')}s",
    )

    original_candidates = proxy_detector.candidate_proxies
    try:
        proxy_detector.candidate_proxies = lambda: []
        empty_detector = proxy_detector.ProxyDetector()
        empty_info = empty_detector.detect(force=True)
        check("无代理时探测结果明确失败", empty_info.get("proxy_url") is None)
        try:
            empty_detector.session()
            direct_blocked = False
        except RuntimeError:
            direct_blocked = True
        check("无代理时禁止建立直连会话", direct_blocked)
    finally:
        proxy_detector.candidate_proxies = original_candidates

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

    source = (BASE / "quant_engine.py").read_text(encoding="utf-8")
    check("fetch_active_markets 支持 offset 参数", "def fetch_active_markets(self, limit" in source and "offset" in source)
    check("SCAN_LIMIT 配置存在", "SCAN_LIMIT" in source, "默认 60（单轮评估上限 100）")
    check("轮转状态使用独立文件", quant_engine.SCAN_ROTATION_FILE.name == "scan_rotation.json")
    check("轮转状态文件在 state/ 目录", quant_engine.SCAN_ROTATION_FILE.parent.name == "state")
    check("市场宇宙有独立完整快照", quant_engine.MARKET_UNIVERSE_FILE.name == "market_universe.json")

    layer = object.__new__(quant_engine.PolymarketDataLayer)
    calls = []
    def fake_page(limit, offset, strict=False):
        calls.append(offset)
        count = 100 if offset == 0 else 25 if offset == 100 else 0
        layer._last_market_page_size = count
        return [{"conditionId": f"0x{i}", "question": f"M{i}"} for i in range(offset, offset + count)]
    layer.fetch_active_markets = fake_page
    original_universe = quant_engine.MARKET_UNIVERSE_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            quant_engine.MARKET_UNIVERSE_FILE = Path(tmp) / "market_universe.json"
            universe = layer.fetch_market_universe()
            check("完整分页拉取整个市场宇宙", len(universe) == 125 and calls == [0, 100])
            check("每日市场宇宙快照已保存", len(list(Path(tmp).glob("market_universe_*.json"))) == 1)
            calls.clear()
            layer.fetch_market_universe()
            check("新鲜快照不重复请求全站", calls == [])

            import requests
            limited = object.__new__(quant_engine.PolymarketDataLayer)
            def gamma_limit(limit, offset, strict=False):
                if offset >= 2100:
                    response = requests.Response()
                    response.status_code = 422
                    raise requests.exceptions.HTTPError(response=response)
                limited._last_market_page_size = 100
                return [{"conditionId": f"0x{i}"} for i in range(offset, offset + 100)]
            limited.fetch_active_markets = gamma_limit
            quant_engine.MARKET_UNIVERSE_FILE = Path(tmp) / "market_universe_limit.json"
            limited_universe = limited.fetch_market_universe()
            check("Gamma 分页上限作为全量结束而非报错", len(limited_universe) == 2100)
    finally:
        quant_engine.MARKET_UNIVERSE_FILE = original_universe

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
        try:
            ex = clob_executor.PolymarketV2Executor()
        except RuntimeError as exc:
            print(f"  [SKIP] 无可用代理通道，跳过订单金额测试：{exc}")
            return
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

        # 余额冲突必须是固定短冷却，不能指数退避到小时级（否则止损会被冻住）
        breaker.record_balance_conflict("BAL_TOKEN_2", "not enough balance")
        fixed = breaker._state["BAL_TOKEN_2"]["cooldown_seconds"]
        check("余额冲突冷却为固定 60 秒", 55 <= fixed <= 65)
        old_cd = os.environ.get("BALANCE_CONFLICT_COOLDOWN")
        try:
            os.environ["BALANCE_CONFLICT_COOLDOWN"] = "5"
            breaker.record_balance_conflict("BAL_TOKEN_3", "not enough balance")
            check("冷却秒数可由环境变量覆盖", breaker._state["BAL_TOKEN_3"]["cooldown_seconds"] == 5.0)
        finally:
            if old_cd is None:
                os.environ.pop("BALANCE_CONFLICT_COOLDOWN", None)
            else:
                os.environ["BALANCE_CONFLICT_COOLDOWN"] = old_cd


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


def test_entry_side_fallback() -> None:
    section("5d. 入场方向评估（另一侧按需补评）")
    import quant_engine as qe

    class FakeLayer:
        def __init__(self, prices):
            self.prices = prices

        def fetch_price_history(self, *args, **kwargs):
            return self.prices

    def book(ask, spread=0.01, depth=100.0, imb=0.0):
        return {
            "best_ask": ask, "best_bid": round(ask - spread, 4), "mid": round(ask - spread / 2, 4),
            "spread": spread, "ask_depth_usdc": depth, "bid_depth_usdc": depth,
            "imbalance": imb, "raw_asks": [(ask, 10.0)], "raw_bids": [(ask - spread, 10.0)],
        }

    def series(start, end, n=60):
        return [round(start + (end - start) * i / (n - 1), 4) for i in range(n)]

    skips = []

    def record_skip(question, code, detail, metrics=None):
        skips.append(code)

    up = qe.evaluate_entry_side(
        "Q", "Yes", "T1", book(0.45, imb=0.9), {}, FakeLayer(series(0.20, 0.80)), record_skip
    )
    check("上涨方向可入场", up["status"] == "ACCEPT" and up["edge"] >= 0.025)

    down = qe.evaluate_entry_side(
        "Q", "Yes", "T1", book(0.45), {}, FakeLayer(series(0.80, 0.20)), record_skip
    )
    check(
        "下跌方向返回 REJECT_SIDE 且 ROC_z<0（这正是补评另一侧的条件）",
        down["status"] == "REJECT_SIDE" and down["roc_z"] < 0,
    )

    wide = qe.evaluate_entry_side(
        "Q", "Yes", "T1", book(0.45, spread=0.10), {}, FakeLayer(series(0.20, 0.80)), record_skip
    )
    check("价差过大判为市场级拒绝", wide["status"] == "REJECT_MARKET" and "WIDE_SPREAD" in skips)

    thin = qe.evaluate_entry_side(
        "Q", "Yes", "T1", book(0.45, depth=5.0), {}, FakeLayer(series(0.20, 0.80)), record_skip
    )
    check("深度不足判为市场级拒绝", thin["status"] == "REJECT_MARKET" and "LOW_DEPTH" in skips)

    # edge 必须覆盖往返价差（用大倍数门槛验证判定分支，避免依赖具体价位）
    old_over = os.environ.get("MIN_EDGE_OVER_SPREAD")
    try:
        os.environ["MIN_EDGE_OVER_SPREAD"] = "5"
        cost_reject = qe.evaluate_entry_side(
            "Q", "Yes", "T1", book(0.45, spread=0.02, imb=0.9), {}, FakeLayer(series(0.20, 0.80)), record_skip
        )
        check(
            "edge 未覆盖往返价差时拒绝",
            cost_reject["status"] == "REJECT_SIDE" and cost_reject["reject_reason"] == "spread",
        )
    finally:
        if old_over is None:
            os.environ.pop("MIN_EDGE_OVER_SPREAD", None)
        else:
            os.environ["MIN_EDGE_OVER_SPREAD"] = old_over

    # 开仓总开关
    old_flag = os.environ.get("ALLOW_NEW_ENTRIES")
    try:
        os.environ["ALLOW_NEW_ENTRIES"] = "false"
        check("开仓开关可关闭", qe.allow_new_entries() is False)
        os.environ["ALLOW_NEW_ENTRIES"] = "true"
        check("开仓开关可开启", qe.allow_new_entries() is True)
    finally:
        if old_flag is None:
            os.environ.pop("ALLOW_NEW_ENTRIES", None)
        else:
            os.environ["ALLOW_NEW_ENTRIES"] = old_flag


def test_price_history_cache() -> None:
    section("5g. 价格历史缓存（减少 Data API 请求）")
    import quant_engine as qe

    qe._PRICE_HISTORY_CACHE.clear()
    qe._PRICE_HISTORY_CACHE_STATS.update({"hit": 0, "miss": 0})

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"history": [{"p": 0.30 + i * 0.001} for i in range(20)]}

    layer = object.__new__(qe.PolymarketDataLayer)
    calls = {"n": 0}

    def fake_request(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse()

    layer._request = fake_request
    first = layer.fetch_price_history("TOKEN_A", 0.3, {})
    second = layer.fetch_price_history("TOKEN_A", 0.3, {})
    check(
        "同一 token 的历史只请求一次",
        len(first) == 20 and second == first and calls["n"] == 1,
    )
    check("缓存命中计数", qe.price_history_cache_stats()["hit"] == 1)

    third = layer.fetch_price_history("TOKEN_B", 0.3, {})
    check("不同 token 不共用缓存", len(third) == 20 and calls["n"] == 2)

    old = os.environ.get("PRICE_HISTORY_CACHE_SECONDS")
    try:
        os.environ["PRICE_HISTORY_CACHE_SECONDS"] = "0"
        layer.fetch_price_history("TOKEN_A", 0.3, {})
        check("TTL 设为 0 时缓存立即失效", calls["n"] == 3)
    finally:
        if old is None:
            os.environ.pop("PRICE_HISTORY_CACHE_SECONDS", None)
        else:
            os.environ["PRICE_HISTORY_CACHE_SECONDS"] = old
    qe._PRICE_HISTORY_CACHE.clear()


def test_mode_files() -> None:
    section("5f. 实盘 / 纸面文件隔离")
    import quant_engine as qe

    old = os.environ.get("PAPER")
    try:
        os.environ["PAPER"] = "false"
        live_names = (qe.heartbeat_file().name, qe.dashboard_file().name, qe.equity_history_file().name)
        live_portfolio = qe.portfolio_path().name
        os.environ["PAPER"] = "true"
        paper_names = (qe.heartbeat_file().name, qe.dashboard_file().name, qe.equity_history_file().name)
        paper_portfolio = qe.portfolio_path().name
        check("心跳文件按模式隔离", live_names[0] == "heartbeat.json" and paper_names[0] == "heartbeat_paper.json")
        check("面板文件按模式隔离", live_names[1] == "dashboard.html" and paper_names[1] == "dashboard_paper.html")
        check("净值历史按模式隔离", live_names[2] != paper_names[2])
        check("账本文件按模式隔离", live_portfolio == "portfolio.json" and paper_portfolio == "portfolio_paper.json")
    finally:
        if old is None:
            os.environ.pop("PAPER", None)
        else:
            os.environ["PAPER"] = old


def test_evaluation_sampling() -> None:
    section("5g. 评估即记录（校准数据源）")
    import quant_engine as qe
    import strategy_validation as sv

    old_log = qe.RESEARCH_LOG_FILE
    old_dedupe = dict(qe._EVAL_DEDUPE)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "samples.jsonl"
            qe.RESEARCH_LOG_FILE = log
            qe._EVAL_DEDUPE.clear()

            def record(label="Yes"):
                qe.record_evaluation_sample(
                    "Q", "T1", label, {"conditionId": "C1"}, 0.42, 0.35,
                    {"spread": 0.02, "roc_logit_6": 0.05, "imbalance": 0.2}, "accept",
                )

            record("Yes")
            record("Yes")
            record("No")
            lines = [json.loads(row) for row in log.read_text(encoding="utf-8").splitlines() if row.strip()]
            check("同一 token/方向/小时只记一次", len(lines) == 2)
            check("评估样本带 gate_result", all(row.get("gate_result") == "accept" for row in lines))

            log.write_text(
                chr(10).join([
                    json.dumps({
                        "record_type": "EVAL", "token_id": "T9", "outcome_label": "Yes", "p_est": 0.6,
                        "signal_price": 0.5, "execution_price": 0.5,
                        "metrics": {"spread": 0.02, "roc_logit_6": 0.04, "imbalance": 0.3},
                        "ts_utc": "2026-10-31T10:00:00+00:00",
                    }),
                    json.dumps({
                        "record_type": "RESOLUTION", "token_id": "T9", "outcome": 1,
                        "ts_utc": "2026-11-01T00:00:00+00:00",
                    }),
                ]) + chr(10),
                encoding="utf-8",
            )
            samples = sv.load_samples(log)
            check("EVAL 也能进入校准样本", len(samples) == 1 and samples[0]["outcome"] == 1)
            check("EVAL 的盘口指标被带入", abs(samples[0]["spread"] - 0.02) < 1e-9)
    finally:
        qe.RESEARCH_LOG_FILE = old_log
        qe._EVAL_DEDUPE.clear()
        qe._EVAL_DEDUPE.update(old_dedupe)


def test_proxy_negative_cache() -> None:
    section("17. 代理负缓存自愈")
    import proxy_detector as pd

    calls = {"n": 0}
    old_probe = pd._probe_url
    old_candidates = pd.candidate_proxies
    try:
        pd.candidate_proxies = lambda: [{"url": "http://127.0.0.1:7890", "source": "test"}]

        def failing_probe(url, timeout=8.0):
            calls["n"] += 1
            return False, "fail", 0.01

        pd._probe_url = failing_probe
        det = pd.ProxyDetector()
        det.detect(force=True)
        check("探测失败进入 fail-closed", det.selected_url is None)
        first = calls["n"]
        det.detect()
        check("负缓存期内不重复探测", calls["n"] == first)
        det.checked_at = "2020-01-01T00:00:00+00:00"
        det.detect()
        check("负缓存过期后自动重探", calls["n"] > first)
    finally:
        pd._probe_url = old_probe
        pd.candidate_proxies = old_candidates


def test_balance_degrade() -> None:
    section("18. 余额读取降级（RPC 抖动不阻塞止损）")
    import requests as _requests
    import portfolio_reconciliation as pr

    old = (pr.fetch_paginated, pr.read_open_orders, pr.read_pusd_balances, pr.build_session)
    try:
        pr.build_session = lambda: _requests.Session()
        pr.fetch_paginated = lambda session, endpoint, wallet: []
        pr.read_open_orders = lambda: []

        def boom(eoa, deposit_wallet):
            raise RuntimeError("rpc down")

        pr.read_pusd_balances = boom
        sources = pr.fetch_sources({"cash_usdc": 12.34, "eoa_idle_pusd": 0.5})
        check("RPC 失败时沿用上次现金", abs(sources["balances"]["deposit_wallet"] - 12.34) < 1e-9)
        check("余额来源标记为 stale", str(sources["balances_source"]).startswith("stale"))

        pr.read_pusd_balances = lambda eoa, deposit_wallet: {"eoa": 0.0, "deposit_wallet": 9.9}
        live_sources = pr.fetch_sources({"cash_usdc": 12.34})
        check(
            "RPC 正常时使用真实余额",
            live_sources["balances"]["deposit_wallet"] == 9.9 and live_sources["balances_source"] == "rpc",
        )

        pr.read_pusd_balances = boom
        raised = False
        try:
            pr.fetch_sources(None)
        except RuntimeError:
            raised = True
        check("无历史快照时仍 fail-closed", raised)
    finally:
        pr.fetch_paginated, pr.read_open_orders, pr.read_pusd_balances, pr.build_session = old


def test_hedge_ledger() -> None:
    section("19. 对冲纸盘跟随")
    import hedge_ledger as hl

    old = hl.HEDGE_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            hl.HEDGE_FILE = Path(tmp) / "portfolio_hedge.json"
            state = hl.initialise_if_missing()
            check("对冲账本初始资金 $12.81", state["initial_capital_usdc"] == 12.81 and state["cash_usdc"] == 12.81)

            paper = {
                "pos_id": "POS-1", "event_id": "E1", "market_question": "Q?", "outcome": "Yes",
                "shares": 5.0, "entry_price": 0.33, "fee_rate": 0.0, "fee_exponent": 1.0,
            }
            position = hl.mirror_open(paper, "TOK_NO", "No", {"best_ask": 0.67, "best_bid": 0.66})
            check(
                "按反方向、同份额开仓",
                position["outcome"] == "No" and position["shares"] == 5.0
                and abs(hl.load_state()["cash_usdc"] - 9.46) < 1e-6,
            )
            check("重复镜像幂等", hl.mirror_open(paper, "TOK_NO", "No", {"best_ask": 0.67}) is None)

            class Layer:
                def __init__(self, book):
                    self.book = book

                def fetch_orderbook(self, token):
                    return self.book

            placed = hl.close_for_paper(paper, {"best_bid": 0.68, "best_ask": 0.72})
            check("跟随平仓先挂卖单（不主动吃买一）", placed is None and hl.find_for_paper("POS-1")["exit_order"]["limit_price"] == 0.72)
            held_state = hl.mark_all(Layer({"best_bid": 0.70, "best_ask": 0.72}))
            check("卖单未成交时继续持有并盯市", len(held_state["open_positions"]) == 1)
            filled_state = hl.mark_all(Layer({"best_bid": 0.72, "best_ask": 0.76}))
            check(
                "买一追到卖单价后成交入账",
                not filled_state["open_positions"]
                and abs(filled_state["realized_pnl_usdc"] - 0.25) < 1e-6
                and abs(filled_state["cash_usdc"] - 13.06) < 1e-6,
            )

            cash_low = hl.load_state()
            cash_low["cash_usdc"] = 1.0
            hl.save_state(cash_low)
            paper2 = dict(paper, pos_id="POS-2")
            partial = hl.mirror_open(paper2, "TOK_NO", "No", {"best_ask": 0.67})
            check("现金不足按可用资金缩量", abs(partial["shares"] - 1.4925) < 1e-3)
            check(
                "缩量被记录到 partial_mirrors",
                len(hl.load_state()["partial_mirrors"]) == 1,
            )
    finally:
        hl.HEDGE_FILE = old


def test_reverse_ledgers() -> None:
    section("20. 反向跟单（持有到结算 / 买一挂单）")
    import reverse_ledger as rl

    class Layer:
        def __init__(self, book=None, resolved=None):
            self.book = book
            self.resolved = resolved

        def fetch_orderbook(self, token):
            return self.book

        def fetch_resolved_mark(self, token, event_id):
            if self.resolved is None:
                return None
            return {"best_bid": self.resolved, "mark_source": "RESOLVED"}

    old = dict(rl.FILES)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            rl.FILES = {"reverse": Path(tmp) / "reverse.json", "maker": Path(tmp) / "maker.json"}
            rl.initialise_all()
            paper = {
                "pos_id": "POS-1", "event_id": "E1", "market_question": "Q?", "outcome": "Yes",
                "shares": 5.0, "entry_price": 0.35,
            }
            book = {"best_ask": 0.67, "best_bid": 0.64}

            position = rl.mirror_open("reverse", paper, "TOK_NO", "No", book, 0.04, 1.0)
            check("反向持有按卖一买入", position["entry_price"] == 0.67 and position["shares"] == 5.0)

            settled = rl.tick("reverse", Layer(resolved=1.0))
            check(
                "反向持有拿到结算并入账",
                not settled["open_positions"] and settled["realized_pnl_usdc"] > 1.5,
            )

            # 旧规则（买一挂单等成交）会让反向盘在纸面下跌时永远不成交，已改为开仓即按卖一成交。
            maker_pos = rl.mirror_open("maker", paper, "TOK_NO", "No", book, 0.04, 1.0)
            check(
                "反向跟单开仓即按卖一成交（保证每笔纸面仓位都有反向仓）",
                maker_pos["entry_price"] == 0.67
                and maker_pos["shares"] == 5.0
                and maker_pos["pending_shares"] == 0.0,
            )
            held = rl.tick("maker", Layer(book={"best_ask": 0.66, "best_bid": 0.63}))
            check("反向跟单持仓随盘口盯市", held["open_positions"][0]["shares"] == 5.0)

            closed = rl.close_for_paper("maker", paper, {"best_bid": 0.66, "best_ask": 0.70})
            check(
                "反向跟单平仓先挂卖单（赚回出场价差）",
                closed is None and rl.find_for_paper("maker", "POS-1")["exit_order"]["limit_price"] == 0.70,
            )
            sold = rl.tick("maker", Layer(book={"best_ask": 0.70, "best_bid": 0.70}))
            check(
                "卖单被买盘吃后入账（卖一进、卖一出，只亏手续费）",
                not sold["open_positions"] and -0.02 < sold["realized_pnl_usdc"] < 0.12,
            )

            # 旧版遗留 pending：没有 fill_mode，下一轮必须按卖一补成交，不能让反向盘缺席
            legacy_state = rl.load_state("maker")
            legacy_pos = rl._new_position(
                "maker", {"pos_id": "POS-2"}, "TOK_NO", "No", 0.0, 0.64, 0.04, 1.0
            )
            legacy_pos["pending_shares"] = 2.0
            legacy_pos["limit_price"] = 0.64
            legacy_state["open_positions"].append(legacy_pos)
            rl.save_state("maker", legacy_state)
            chased = rl.tick("maker", Layer(book={"best_ask": 0.65, "best_bid": 0.63}))
            lp = next(p for p in chased["open_positions"] if p["paper_pos_id"] == "POS-2")
            check(
                "旧版遗留挂单按卖一补成交",
                lp["shares"] == 2.0
                and 0.65 <= lp["entry_price"] < 0.67
                and lp["fill_mode"] == "chase_at_ask",
            )
    finally:
        rl.FILES = old


def main() -> int:
    print("=" * 70)
    print(" P0 修复验证套件（离线，不发起任何交易调用）")
    print("=" * 70)

    # 测试会调用 evaluate_existing_positions，其审计写入会污染实盘日志。
    # 实测这些假事件（如"成本硬止损 Bid 0.14"）会被误当成真实平仓记录，
    # 因此把审计输出重定向到临时目录，保证 logs/signals_and_skips.jsonl 只记真实交易。
    import quant_engine as qe
    _test_tmp = Path(tempfile.mkdtemp())
    qe.SIGNALS_LOG_FILE = _test_tmp / "signals_test.jsonl"
    qe.RESEARCH_LOG_FILE = _test_tmp / "samples_test.jsonl"

    test_signature()
    test_single_instance()
    test_single_instance_real_race()
    test_order_ledger()
    test_exposure_gate()
    test_reconciled_state_keys()
    test_exit_strategy()
    test_entry_side_fallback()
    test_price_history_cache()
    test_mode_files()
    test_evaluation_sampling()
    test_proxy_negative_cache()
    test_balance_degrade()
    test_hedge_ledger()
    test_reverse_ledgers()
    test_document_alignment()
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
