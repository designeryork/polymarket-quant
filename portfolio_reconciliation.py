"""Reconcile the local LIVE ledger from read-only chain, Data API and CLOB data."""
import argparse
import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from web3 import Web3

import rate_limiter


class RateLimitError(RuntimeError):
    """Data API 返回 429 且重试已耗尽。

    单独成类，便于上层区分"限流"（稍后重试即可）与"真实故障"（需要人工介入）。
    """


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
PORTFOLIO_FILE = BASE_DIR / "state" / "portfolio.json"
SNAPSHOT_FILE = BASE_DIR / "state" / "reconciliation_latest.json"
DATA_API = "https://data-api.polymarket.com"
PUSD_ADDRESS = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
ERC20_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_timestamp(value: Any) -> str:
    return datetime.fromtimestamp(int(value), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def finite_number(value: Any, field: str) -> float:
    number = float(value or 0)
    if not math.isfinite(number):
        raise ValueError(f"Invalid numeric field: {field}")
    return number


def build_session() -> requests.Session:
    """构建使用探测到的可用通道的会话。

    不直接信任环境变量：环境变量可能指向与浏览器不同的代理端口
    （实测事故：环境变量指向工具链自带的转发通道，对 Polymarket 返回 502，
    而浏览器使用的代理一切正常）。探测逻辑见 proxy_detector.py。
    """
    import proxy_detector

    session = proxy_detector.build_session()
    session.headers.update({"User-Agent": "PolymarketPortfolioReconciliation/1.0"})
    return session


def build_direct_session() -> Optional[requests.Session]:
    """直连回退会话。"""
    direct = requests.Session()
    direct.headers.update({"User-Agent": "PolymarketPortfolioReconciliation/1.0"})
    direct.trust_env = False
    return direct


def fetch_paginated(session: requests.Session, endpoint: str, wallet: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    limit = 500
    direct = build_direct_session()
    proxy_failed = False
    for offset in range(0, 10000, limit):
        params = {"user": wallet, "limit": limit, "offset": offset, "sizeThreshold": 0}
        url = f"{DATA_API}/{endpoint}"
        try:
            # connect timeout 给小值（8s）：代理隧道偶发卡死时快速失败并重试，
            # 而不是把整轮 25 秒耗在一次卡住的 CONNECT 上。
            response = rate_limiter.request_with_retry(
                session, "GET", url, params=params,
                timeout=(8, 25), connect_timeout=8, read_timeout=25,
            )
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout) as exc:
            # 代理通道不可用（含 CONNECT 卡死）-> 重新探测可用通道后重试。
            # 注意：直连在多数地区不可用（实测 data-api 直连 12s 超时），
            # 所以真正的回退是"换一条代理通道"，而不是"不走代理"。
            if proxy_failed:
                raise
            proxy_failed = True
            try:
                import proxy_detector

                info = proxy_detector.get_detector().refresh()
                new_session = proxy_detector.build_session()
                new_session.headers.update({"User-Agent": "PolymarketPortfolioReconciliation/1.0"})
                print(
                    f"[网络自愈] 原通道异常({type(exc).__name__})，"
                    f"重新探测到 {info.get('proxy_url') or '直连'}，重试本请求"
                )
                response = rate_limiter.request_with_retry(
                    new_session, "GET", url, params=params,
                    timeout=(8, 25), connect_timeout=8, read_timeout=25,
                )
            except Exception as heal_error:
                raise exc from heal_error
        if response.status_code == 429:
            # 重试已耗尽：明确记录限流事实，而不是笼统的"请求失败"
            raise RateLimitError(
                f"Data API 限流 (HTTP 429) on {endpoint}: "
                f"Retry-After={response.headers.get('Retry-After') or 'N/A'}"
            )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError(f"Unexpected {endpoint} response shape")
        rows.extend(batch)
        if len(batch) < limit:
            return rows
    raise ValueError(f"Too many {endpoint} rows; refusing an incomplete reconciliation")


def read_pusd_balances(eoa: str, deposit_wallet: str) -> Dict[str, float]:
    errors = []
    rpcs = [
        os.getenv("POLYGON_RPC", ""),
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.drpc.org",
        "https://1rpc.io/matic",
    ]
    for rpc in dict.fromkeys(x for x in rpcs if x):
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            contract = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_BALANCE_ABI)
            return {
                "eoa": contract.functions.balanceOf(Web3.to_checksum_address(eoa)).call() / 1e6,
                "deposit_wallet": contract.functions.balanceOf(Web3.to_checksum_address(deposit_wallet)).call() / 1e6,
            }
        except Exception as exc:
            errors.append(f"{rpc}: {type(exc).__name__}")
    raise RuntimeError(f"Unable to read pUSD balances: {', '.join(errors)}")


def read_open_orders() -> List[Dict[str, Any]]:
    from clob_executor import PolymarketV2Executor

    executor = PolymarketV2Executor()
    for attempt in range(3):
        try:
            response = executor.get_open_orders()
            rows = response.get("data", []) if isinstance(response, dict) else response
            if not isinstance(rows, list):
                raise ValueError("Unexpected CLOB open-order response shape")
            fields = ("id", "status", "market", "asset_id", "side", "price", "original_size", "size_matched")
            return [{key: row.get(key) for key in fields} for row in rows]
        except Exception:
            if attempt == 2:
                raise
    raise RuntimeError("Unreachable open-order retry state")


def fetch_sources() -> Dict[str, Any]:
    eoa = Web3.to_checksum_address(os.environ["WALLET_ADDRESS"])
    deposit_wallet = Web3.to_checksum_address(os.environ["DEPOSIT_WALLET"])
    session = build_session()
    activity = fetch_paginated(session, "activity", deposit_wallet)
    positions = fetch_paginated(session, "positions", deposit_wallet)
    balances = read_pusd_balances(eoa, deposit_wallet)
    open_orders = read_open_orders()
    return {
        "eoa": eoa,
        "deposit_wallet": deposit_wallet,
        "activity": activity,
        "positions": positions,
        "balances": balances,
        "open_orders": open_orders,
    }


def rebuild_trade_ledger(activity: List[Dict[str, Any]]) -> Dict[str, Any]:
    lots: Dict[str, Dict[str, Any]] = {}
    closed_trades = []
    non_trade_activity = 0
    buys = sells = 0.0
    trades = [row for row in activity if row.get("type") == "TRADE"]
    non_trade_activity = len(activity) - len(trades)
    trades.sort(key=lambda row: (int(row.get("timestamp") or 0), str(row.get("transactionHash") or "")))

    for trade in trades:
        token_id = str(trade.get("asset") or "")
        side = str(trade.get("side") or "").upper()
        size = finite_number(trade.get("size"), "trade.size")
        price = finite_number(trade.get("price"), "trade.price")
        value = finite_number(trade.get("usdcSize"), "trade.usdcSize") or size * price
        if not token_id or side not in ("BUY", "SELL") or size <= 0 or value < 0:
            raise ValueError("Invalid or unsupported trade activity")
        position = lots.setdefault(
            token_id,
            {
                "token_id": token_id,
                "condition_id": str(trade.get("conditionId") or ""),
                "title": str(trade.get("title") or "Unknown market"),
                "outcome": str(trade.get("outcome") or ""),
                "shares": 0.0,
                "cost": 0.0,
                "opened_at": int(trade.get("timestamp") or 0),
            },
        )
        if side == "BUY":
            position["shares"] += size
            position["cost"] += value
            buys += value
            continue

        if size - position["shares"] > 1e-5:
            raise ValueError(f"SELL exceeds reconstructed holdings for {position['title']}")
        size = min(size, position["shares"])
        average_cost = position["cost"] / position["shares"] if position["shares"] else 0.0
        cost_sold = average_cost * size
        proceeds = value
        pnl = proceeds - cost_sold
        position["shares"] = max(0.0, position["shares"] - size)
        position["cost"] = max(0.0, position["cost"] - cost_sold)
        sells += proceeds
        closed_trades.append(
            {
                "pos_id": f"CHAIN-{trade.get('transactionHash') or len(closed_trades)}",
                "event_id": position["condition_id"],
                "market_question": position["title"],
                "outcome": position["outcome"],
                "token_id": token_id,
                "entry_price": round(average_cost, 6),
                "exit_price_bid": price,
                "shares": size,
                "cost_usdc": round(cost_sold, 6),
                "current_value_usdc": round(proceeds, 6),
                "realized_pnl_usdc": round(pnl, 6),
                "realized_pnl_pct": round((pnl / cost_sold) * 100, 2) if cost_sold else 0.0,
                "exit_reason": "CLOB 真实 SELL 成交（移动平均成本对账）",
                "closed_at": utc_timestamp(trade["timestamp"]),
                "transaction_hash": trade.get("transactionHash"),
                "data_source": "Polymarket Data API activity",
            }
        )

    closed_trades.sort(key=lambda row: row["closed_at"], reverse=True)
    return {
        "lots": lots,
        "closed_trades": closed_trades,
        "realized_pnl": sum(row["realized_pnl_usdc"] for row in closed_trades),
        "buy_value": buys,
        "sell_value": sells,
        "trade_count": len(trades),
        "non_trade_activity": non_trade_activity,
    }


def resolve_pending_orders(
    lots: Dict[str, Dict[str, Any]],
    open_positions: List[Dict[str, Any]],
    open_orders: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """用只读事实核销在途挂单账本。

    规则（保守优先，宁可少交易也不误判）：
    * 仍在 CLOB 挂单列表中 -> 保持阻塞（可能部分成交）；
    * 已不在挂单列表，且该 token 的持仓数量与重建账本一致 -> 视为已成交（FILLED）；
    * 已不在挂单列表，且持仓已清零 -> 视为已离簿（CONFIRMED，成交与否由账本判定）。
    """
    import order_ledger

    stats = order_ledger.reconcile_with_open_orders(open_orders)
    live_tokens = {str(pos.get("token_id") or "") for pos in open_positions}

    ledger = order_ledger.load_ledger()
    for order_id, order in list(ledger.get("orders", {}).items()):
        if order.get("status") != order_ledger.STATUS_CONFIRMED:
            continue
        token_id = str(order.get("token_id") or "")
        side = str(order.get("side") or "").upper()
        if side == "BUY":
            # 买单：链上已出现该 token 的持仓 -> 确认成交
            if token_id in live_tokens:
                order["status"] = order_ledger.STATUS_FILLED
                order["updated_at_utc"] = utc_now()
        else:
            # 卖单：链上已无该 token 持仓 -> 确认成交离场
            if token_id and token_id not in live_tokens:
                order["status"] = order_ledger.STATUS_FILLED
                order["updated_at_utc"] = utc_now()
    order_ledger.save_ledger(ledger)
    stats["resolved"] = len(ledger.get("orders", {}))
    return stats


def build_reconciled_state(previous: Dict[str, Any], sources: Dict[str, Any]) -> Dict[str, Any]:
    ledger = rebuild_trade_ledger(sources["activity"])
    lots = ledger["lots"]
    old_positions = {str(row.get("token_id")): row for row in previous.get("open_positions", [])}
    actual_tokens = set()
    open_positions = []
    position_value = 0.0

    for raw in sources["positions"]:
        token_id = str(raw.get("asset") or "")
        shares = finite_number(raw.get("size"), "position.size")
        if shares <= 1e-8:
            continue
        if token_id not in lots:
            raise ValueError(f"On-chain position has no reconstructed trade history: {token_id}")
        lot = lots[token_id]
        if abs(shares - lot["shares"]) > 1e-5:
            raise ValueError(f"Position size mismatch for {lot['title']}: API={shares}, ledger={lot['shares']}")
        actual_tokens.add(token_id)
        current_value = finite_number(raw.get("currentValue"), "position.currentValue")
        current_price = finite_number(raw.get("curPrice"), "position.curPrice")
        cost = lot["cost"]
        entry_price = cost / shares if shares else 0.0
        old = old_positions.get(token_id, {})
        redeemable = bool(raw.get("redeemable"))
        open_positions.append(
            {
                "pos_id": old.get("pos_id") or f"CHAIN-{token_id[-12:]}",
                "event_id": str(raw.get("conditionId") or lot["condition_id"]),
                "market_question": str(raw.get("title") or lot["title"]),
                "outcome": str(raw.get("outcome") or lot["outcome"]),
                "token_id": token_id,
                "entry_price": round(entry_price, 6),
                "current_bid": current_price,
                "current_ask": current_price,
                "shares": shares,
                "cost_usdc": round(cost, 6),
                "current_value_usdc": round(current_value, 6),
                "unrealized_pnl_usdc": round(current_value - cost, 6),
                "unrealized_pnl_pct": round(((current_value - cost) / cost) * 100, 2) if cost else 0.0,
                "p_est": old.get("p_est", entry_price),
                "edge_at_entry": old.get("edge_at_entry", 0.0),
                "quarter_kelly": old.get("quarter_kelly", 0.0),
                "target_prob": old.get("target_prob", 0.92),
                "z_entry": old.get("z_entry", 0.0),
                "z_peak": old.get("z_peak", 0.0),
                "opened_at": old.get("opened_at") or utc_timestamp(lot["opened_at"]),
                "book_updated_at": utc_now(),
                "book_status": "RESOLVED" if redeemable else "DATA_API",
                "live_order_id": old.get("live_order_id"),
                "tp_order_id": old.get("tp_order_id"),
                "exit_order_id": old.get("exit_order_id"),
                "redeemable": redeemable,
                "data_source": "Polymarket Data API positions + reconstructed trades",
            }
        )
        position_value += current_value

    missing = set(lots) - actual_tokens
    for token_id in missing:
        if lots[token_id]["shares"] > 1e-5:
            raise ValueError(f"Reconstructed position missing from Data API: {lots[token_id]['title']}")

    # 用真实持仓与 CLOB 挂单核销在途挂单账本（防止重复下单与超额卖出）
    try:
        pending_stats = resolve_pending_orders(lots, open_positions, sources["open_orders"])
    except Exception as exc:
        pending_stats = {"error": str(exc)}

    cash = sources["balances"]["deposit_wallet"]
    eoa_idle = sources["balances"]["eoa"]
    if (previous.get("reconciliation") or {}).get("status") == "OK":
        initial_capital = finite_number(previous.get("initial_capital_usdc"), "previous.initial_capital")
    else:
        initial_capital = cash - (ledger["sell_value"] - ledger["buy_value"])
    if initial_capital <= 0:
        raise ValueError("Unable to establish a positive initial capital baseline")
    total_equity = cash + position_value
    unrealized = sum(row["unrealized_pnl_usdc"] for row in open_positions)
    previous_cash = finite_number(previous.get("cash_usdc"), "previous.cash")
    reconciled_at = utc_now()
    state = dict(previous)
    # 对账重建的账本必须保留引擎运行所需的统计与熔断字段。
    # 旧实现直接 state.update(...) 覆盖，导致全新账本缺少 stats/daily_stats，
    # 引擎下一行 state["stats"]["scans_count"] += 1 会抛 KeyError 并中止整轮。
    state.setdefault("stats", {
        "scans_count": 0,
        "markets_evaluated": 0,
        "signals_triggered": 0,
        "skipped_count": 0,
        "skip_reasons": {},
    })
    state.setdefault("daily_stats", {
        "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "start_equity": round(cash + position_value, 6),
    })
    state.setdefault("equity_curve", [])
    state.setdefault("recent_decisions", [])
    state.update(
        {
            "mode": "LIVE (实盘 · 已对账)",
            "wallet_address": sources["eoa"],
            "initial_capital_usdc": round(initial_capital, 6),
            "cash_usdc": round(cash, 6),
            "eoa_idle_pusd": round(eoa_idle, 6),
            "realized_pnl_usdc": round(ledger["realized_pnl"], 6),
            "unrealized_pnl_usdc": round(unrealized, 6),
            "total_equity_usdc": round(total_equity, 6),
            "total_return_pct": round(((total_equity - initial_capital) / initial_capital) * 100, 2),
            "open_positions": open_positions,
            "closed_trades": ledger["closed_trades"],
            "last_updated": reconciled_at,
            "reconciliation": {
                "status": "OK",
                "reconciled_at": reconciled_at,
                "deposit_wallet": sources["deposit_wallet"],
                "accounting_method": "CLOB activity moving-average cost; Data API position marks; Polygon pUSD balance",
                "activity_count": len(sources["activity"]),
                "trade_count": ledger["trade_count"],
                "non_trade_activity": ledger["non_trade_activity"],
                "open_order_count": len(sources["open_orders"]),
                "open_orders": sources["open_orders"],
                "pending_orders": pending_stats,
                "previous_cash_usdc": round(previous_cash, 6),
                "cash_adjustment_usdc": round(cash - previous_cash, 6),
                "buy_value_usdc": round(ledger["buy_value"], 6),
                "sell_value_usdc": round(ledger["sell_value"], 6),
            },
        }
    )
    return state


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def reconcile(apply: bool = False, backup: bool = False) -> Dict[str, Any]:
    previous = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8")) if PORTFOLIO_FILE.exists() else {}
    sources = fetch_sources()
    state = build_reconciled_state(previous, sources)
    if apply:
        if backup and PORTFOLIO_FILE.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = PORTFOLIO_FILE.with_name(f"portfolio_before_reconcile_{stamp}.json")
            shutil.copy2(PORTFOLIO_FILE, backup_path)
            state["reconciliation"]["backup_file"] = backup_path.name
        atomic_write_json(PORTFOLIO_FILE, state)
        atomic_write_json(SNAPSHOT_FILE, state["reconciliation"])
    return state


def print_summary(state: Dict[str, Any], applied: bool) -> None:
    reconciliation = state["reconciliation"]
    print(f"Mode: {'APPLIED' if applied else 'DRY-RUN'}")
    print(f"Activity: {reconciliation['activity_count']} ({reconciliation['trade_count']} trades)")
    print(f"Open orders: {reconciliation['open_order_count']}")
    print(f"Deposit Wallet cash: {state['cash_usdc']:.6f} pUSD")
    print(f"EOA idle: {state['eoa_idle_pusd']:.6f} pUSD")
    print(f"Equity: {state['total_equity_usdc']:.6f} pUSD")
    print(f"Realized PnL: {state['realized_pnl_usdc']:+.6f} | Unrealized PnL: {state['unrealized_pnl_usdc']:+.6f}")
    print(f"Positions: {len(state['open_positions'])}")
    for position in state["open_positions"]:
        print(
            f"  - {position['market_question']} [{position['outcome']}]: "
            f"{position['shares']:g} shares, value={position['current_value_usdc']:.6f}, "
            f"redeemable={position['redeemable']}"
        )
    print(f"Cash adjustment: {reconciliation['cash_adjustment_usdc']:+.6f} pUSD")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only chain/CLOB reconciliation; --apply updates only local JSON")
    parser.add_argument("--apply", action="store_true", help="Back up and atomically update the local portfolio")
    args = parser.parse_args()

    # 本工具始终读取链上真实余额与 CLOB 真实成交，因此它是一个"实盘账本"工具。
    # 即便环境变量 PAPER=true，--apply 写入的仍是实盘账本 —— 这里明确提示，避免误用。
    if args.apply and os.getenv("PAPER", "true").lower() == "true":
        print("[提示] 当前环境为 PAPER=true，但本工具始终依据链上真实数据重建**实盘账本**。")
        print("       模拟盘账本请使用 state/portfolio_paper.json，与实盘账本相互隔离。")

    state = reconcile(apply=args.apply, backup=args.apply)
    print_summary(state, args.apply)
    if args.apply:
        from quant_engine import generate_dashboard_html

        generate_dashboard_html(state)
        print(f"Updated: {PORTFOLIO_FILE}")


if __name__ == "__main__":
    main()
