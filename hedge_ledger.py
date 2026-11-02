"""对冲纸盘：反向跟随纸面账户的每一笔开仓与平仓。

规则（由用户确认）：
* 纸面在方向 A 买入 N 份 → 本账本在同一市场买入**另一个方向**，同样 N 份；
* 纸面平仓 → 本账本卖出同样 N 份；
* 不扫描、不做独立决策、不受 edge / 价差 / 持仓数门槛约束；
* 初始资金 12.81 USDC（2026-11-02 起与实盘统一口径；只对冲从现在起的新仓，不回补历史仓位）。

资金约束：纸面永远买便宜的一侧，所以反向总是更贵的一侧。
现金不够一整笔时**按可用现金缩量买入**，并记录 partial_mirrors，
面板外可通过 `state["partial_mirrors"]` 查看哪些仓没有被完整对冲。
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
HEDGE_FILE = STATE_DIR / "portfolio_hedge.json"

DEFAULT_CAPITAL = 12.81  # 2026-11-02 统一为与实盘同口径的起始本金
MIN_SHARES = 0.01
# 纸面平仓时先按卖一挂卖单（赚回出场价差），超过这个时间没成交再按买一市价卖出
EXIT_WAIT_SECONDS = 300.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _empty_state() -> Dict[str, Any]:
    return {
        "mode": "HEDGE (对冲纸盘 · 反向跟随)",
        "initial_capital_usdc": DEFAULT_CAPITAL,
        "cash_usdc": DEFAULT_CAPITAL,
        "realized_pnl_usdc": 0.0,
        "unrealized_pnl_usdc": 0.0,
        "total_equity_usdc": DEFAULT_CAPITAL,
        "total_return_pct": 0.0,
        "open_positions": [],
        "closed_trades": [],
        "equity_curve": [],
        "partial_mirrors": [],
        "last_updated": _now(),
    }


def load_state() -> Dict[str, Any]:
    if not HEDGE_FILE.exists():
        return _empty_state()
    try:
        data = json.loads(HEDGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    base = _empty_state()
    base.update(data)
    return base


def save_state(state: Dict[str, Any]) -> None:
    state["last_updated"] = _now()
    HEDGE_FILE.parent.mkdir(exist_ok=True)
    HEDGE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def initialise_if_missing() -> Dict[str, Any]:
    """首次运行时建立 $20 账本；已存在则原样返回。"""
    state = load_state()
    if not HEDGE_FILE.exists():
        save_state(state)
    return state


def fee_per_share(price: float, rate: float = 0.0, exponent: float = 1.0) -> float:
    if rate <= 0:
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def _recompute_equity(state: Dict[str, Any]) -> None:
    value = sum(float(p.get("current_value_usdc") or 0.0) for p in state["open_positions"])
    state["unrealized_pnl_usdc"] = round(
        sum(float(p.get("unrealized_pnl_usdc") or 0.0) for p in state["open_positions"]), 4
    )
    state["total_equity_usdc"] = round(float(state["cash_usdc"]) + value, 4)
    initial = float(state.get("initial_capital_usdc") or DEFAULT_CAPITAL)
    state["total_return_pct"] = round(
        ((state["total_equity_usdc"] - initial) / initial) * 100.0, 2
    )


def mirror_open(
    paper_pos: Dict[str, Any],
    opposite_token_id: str,
    opposite_label: str,
    book: Dict[str, Any],
    fee_rate: float = 0.0,
    fee_exponent: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """纸面开仓后，在反方向建立同份额对冲。现金不足时自动缩量。"""
    state = load_state()
    paper_pos_id = str(paper_pos.get("pos_id") or "")
    for existing in state["open_positions"]:
        if existing.get("paper_pos_id") == paper_pos_id:
            return None  # 已镜像过，幂等

    ask = float(book.get("best_ask") or 0.0)
    if ask <= 0.0 or ask >= 1.0:
        print(f"[对冲] 反向无有效卖一，跳过 {paper_pos.get('market_question', '')[:30]}", flush=True)
        return None
    requested = float(paper_pos.get("shares") or 0.0)
    unit_cost = ask + fee_per_share(ask, fee_rate, fee_exponent)
    affordable = round(float(state["cash_usdc"]) / unit_cost, 4) if unit_cost > 0 else 0.0
    shares = min(requested, affordable)
    if shares < MIN_SHARES:
        state["partial_mirrors"].append({
            "paper_pos_id": paper_pos_id,
            "market_question": paper_pos.get("market_question"),
            "requested_shares": requested,
            "filled_shares": 0.0,
            "reason": "现金不足，未对冲",
            "ts_utc": _now(),
        })
        save_state(state)
        print(f"[对冲] 现金不足，未对冲 {paper_pos.get('market_question', '')[:30]}", flush=True)
        return None

    cost = round(shares * unit_cost, 6)
    position = {
        "pos_id": f"HEDGE-{paper_pos_id[-12:]}",
        "paper_pos_id": paper_pos_id,
        "event_id": paper_pos.get("event_id", ""),
        "market_question": paper_pos.get("market_question", ""),
        "outcome": opposite_label,
        "token_id": opposite_token_id,
        "entry_price": round(ask, 6),
        "current_bid": round(ask, 6),
        "current_ask": round(ask, 6),
        "shares": round(shares, 6),
        "cost_usdc": cost,
        "current_value_usdc": round(shares * ask, 6),
        "unrealized_pnl_usdc": round(shares * ask - cost, 6),
        "unrealized_pnl_pct": round(((shares * ask - cost) / cost) * 100.0, 2) if cost else 0.0,
        "p_est": 0.0,          # 对冲盘不做概率估计，仅用于面板列展示
        "edge_at_entry": 0.0,
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "opened_at": _now(),
        "book_updated_at": _now(),
        "book_status": "HEDGE",
    }
    state["cash_usdc"] = round(float(state["cash_usdc"]) - cost, 6)
    state["open_positions"].append(position)
    if shares < requested - 1e-6:
        state["partial_mirrors"].append({
            "paper_pos_id": paper_pos_id,
            "market_question": paper_pos.get("market_question"),
            "requested_shares": requested,
            "filled_shares": shares,
            "reason": "现金不足，按可用资金缩量",
            "ts_utc": _now(),
        })
    _recompute_equity(state)
    save_state(state)
    print(
        f"[对冲] 开仓 {opposite_label} {shares:g}/{requested:g} 份 @ {ask:.3f} "
        f"（纸面 {paper_pos.get('outcome')} {paper_pos.get('entry_price')}）",
        flush=True,
    )
    return position


def find_for_paper(paper_pos_id: Any) -> Optional[Dict[str, Any]]:
    """找到某笔纸面持仓对应的对冲仓位（没有则返回 None）。"""
    target = str(paper_pos_id)
    for position in load_state()["open_positions"]:
        if position.get("paper_pos_id") == target:
            return position
    return None


def close_for_paper(paper_pos: Dict[str, Any], book: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """纸面平仓后，卖出对应的对冲仓位。"""
    state = load_state()
    paper_pos_id = str(paper_pos.get("pos_id") or "")
    target = None
    for position in state["open_positions"]:
        if position.get("paper_pos_id") == paper_pos_id:
            target = position
            break
    if target is None:
        return None

    if target.get("exit_order"):
        return None  # 已经在挂卖单，等 mark_all 处理成交/超时
    shares = float(target.get("shares") or 0.0)
    if shares <= 0:
        return None
    bid = float(book.get("best_bid") or 0.0)
    ask = float(book.get("best_ask") or 0.0)
    limit = ask if 0 < ask < 1 else bid
    if limit <= 0.0:
        print(f"[对冲] 反向无有效盘口，暂不平仓 {target.get('market_question', '')[:30]}", flush=True)
        return None
    # 直接吃买一 = 白付出场价差；改成挂在卖一等买盘来吃，成交不了再按买一兜底
    target["exit_order"] = {
        "limit_price": round(limit, 6),
        "shares": shares,
        "placed_at": _now(),
    }
    save_state(state)
    print(
        f"[对冲] 纸面平仓 → 挂卖单 {shares:g} 份 @ {limit:.3f}"
        f"（买一 {bid:.3f}，{EXIT_WAIT_SECONDS:.0f}s 未成交转市价）",
        flush=True,
    )
    return None


def _parse_ts(value: Any):
    try:
        return datetime.fromisoformat(str(value).replace(" UTC", "")).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _finalize_exit(state: Dict[str, Any], position: Dict[str, Any], price: float, reason: str) -> Dict[str, Any]:
    """按 price 卖出并落账（出场手续费按该价格计）。"""
    shares = float(position.get("shares") or 0.0)
    net = shares * price - shares * fee_per_share(
        price, float(position.get("fee_rate") or 0.0), float(position.get("fee_exponent") or 1.0)
    )
    pnl = round(net - float(position.get("cost_usdc") or 0.0), 6)
    state["open_positions"] = [p for p in state["open_positions"] if p is not position]
    state["cash_usdc"] = round(float(state["cash_usdc"]) + net, 6)
    state["realized_pnl_usdc"] = round(float(state["realized_pnl_usdc"]) + pnl, 6)
    closed = {
        **position,
        "exit_order": None,
        "exit_price_bid": round(price, 6),
        "current_value_usdc": round(net, 6),
        "realized_pnl_usdc": pnl,
        "realized_pnl_pct": round((pnl / float(position.get("cost_usdc") or 1)) * 100.0, 2),
        "exit_reason": reason,
        "closed_at": _now(),
    }
    state["closed_trades"].insert(0, closed)
    print(f"[对冲] 平仓 {position.get('outcome')} {shares:g} 份 @ {price:.3f} | {pnl:+.4f}", flush=True)
    return closed


def mark_all(data_layer) -> Dict[str, Any]:
    """按真实盘口刷新市值；有平仓挂单的仓位在这里决定成交或超时市价卖出。"""
    state = load_state()
    for position in list(state["open_positions"]):
        token = str(position.get("token_id") or "")
        try:
            book = data_layer.fetch_orderbook(token)
        except Exception:
            book = None
        if not book:
            continue
        bid = float(book.get("best_bid") or 0.0)
        exit_order = position.get("exit_order")
        if exit_order:
            limit = float(exit_order.get("limit_price") or 0.0)
            placed = _parse_ts(exit_order.get("placed_at"))
            elapsed = (datetime.now(timezone.utc) - placed).total_seconds() if placed else EXIT_WAIT_SECONDS
            if limit > 0 and bid >= limit:
                _finalize_exit(state, position, limit, f"跟随纸面平仓（挂卖单成交 @{limit:.3f}）")
                continue
            if elapsed >= EXIT_WAIT_SECONDS and bid > 0:
                _finalize_exit(
                    state, position, bid,
                    f"跟随纸面平仓（挂卖单 {EXIT_WAIT_SECONDS:.0f}s 未成交，按买一 {bid:.3f}）",
                )
                continue
        if bid <= 0:
            continue
        shares = float(position.get("shares") or 0.0)
        cost = float(position.get("cost_usdc") or 0.0)
        net = shares * bid - shares * fee_per_share(
            bid, float(position.get("fee_rate") or 0.0), float(position.get("fee_exponent") or 1.0)
        )
        position["current_bid"] = round(bid, 6)
        position["book_updated_at"] = _now()
        position["current_value_usdc"] = round(net, 6)
        position["unrealized_pnl_usdc"] = round(net - cost, 6)
        position["unrealized_pnl_pct"] = round(((net - cost) / cost) * 100.0, 2) if cost else 0.0
    _recompute_equity(state)
    save_state(state)
    return state
