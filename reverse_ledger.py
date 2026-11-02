"""反向跟单：把纸面的每一笔开仓，按**另一个方向**单独记一本账。

两种规则（用户要求都跑）：

1. `reverse`（反向持有）
   * 纸面开 A 方向 → 本账买反方向，**不跟随平仓**，一直拿到市场结算；
   * 用途：验证"如果反过来做，到底会不会赚"。纸面错时反向可能赚到 (1−p)；
     纸面对时反向归零。这是真正的反向押注，不是对冲。

2. `maker`（反向跟单 · 跟随平仓）
   * 纸面开 A → 本账在反方向**按卖一价立即成交**，保证每一笔纸面仓位都有对应的反向仓；
   * 纸面平仓时跟着卖出（按买一价），把反向的浮盈兑现；
   * 与 `reverse` 的唯一区别是出场：reverse 拿到结算，maker 跟随纸面平仓。
   * 早期版本用"买一挂限价、等卖一跌下来才成交"，结果纸面一下跌（反向本该赚钱的方向）
     挂单永远不成交，纸面平仓时被撤单，反向盘一次也没参与——已废弃。

两本账互不影响，也与 `hedge_ledger`（对冲盘）独立。
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"

FILES = {
    "reverse": STATE_DIR / "portfolio_reverse.json",
    "maker": STATE_DIR / "portfolio_maker.json",
}
LABELS = {
    "reverse": "REVERSE (反向持有 · 拿到结算)",
    "maker": "MAKER (反向跟单 · 跟随平仓)",
}
DEFAULT_CAPITAL = 12.81  # 2026-11-02 统一为与实盘同口径的起始本金
MIN_SHARES = 0.01
# 反向跟单平仓时先挂卖单（赚回出场价差），超时未成交再按买一市价卖
EXIT_WAIT_SECONDS = 300.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _empty(kind: str) -> Dict[str, Any]:
    return {
        "mode": LABELS[kind],
        "kind": kind,
        "initial_capital_usdc": DEFAULT_CAPITAL,
        "cash_usdc": DEFAULT_CAPITAL,
        "realized_pnl_usdc": 0.0,
        "unrealized_pnl_usdc": 0.0,
        "total_equity_usdc": DEFAULT_CAPITAL,
        "total_return_pct": 0.0,
        "open_positions": [],
        "closed_trades": [],
        "equity_curve": [],
        "notes": [],
        "last_updated": _now(),
    }


def load_state(kind: str) -> Dict[str, Any]:
    path = FILES[kind]
    if not path.exists():
        return _empty(kind)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty(kind)
    base = _empty(kind)
    base.update(data)
    base["mode"] = LABELS[kind]  # 规则改名后，旧状态文件里的标题也要跟着更新
    return base


def save_state(kind: str, state: Dict[str, Any]) -> None:
    state["last_updated"] = _now()
    FILES[kind].parent.mkdir(exist_ok=True)
    FILES[kind].write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def initialise_all() -> None:
    for kind, path in FILES.items():
        if not path.exists():
            save_state(kind, _empty(kind))


def fee_per_share(price: float, rate: float = 0.0, exponent: float = 1.0) -> float:
    if rate <= 0:
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def _recompute(state: Dict[str, Any]) -> None:
    value = sum(float(p.get("current_value_usdc") or 0.0) for p in state["open_positions"])
    state["unrealized_pnl_usdc"] = round(
        sum(float(p.get("unrealized_pnl_usdc") or 0.0) for p in state["open_positions"]), 4
    )
    state["total_equity_usdc"] = round(float(state["cash_usdc"]) + value, 4)
    initial = float(state.get("initial_capital_usdc") or DEFAULT_CAPITAL)
    state["total_return_pct"] = round(((state["total_equity_usdc"] - initial) / initial) * 100.0, 2)


def _new_position(kind: str, paper_pos: Dict[str, Any], token: str, label: str,
                  shares: float, entry: float, fee_rate: float, fee_exp: float) -> Dict[str, Any]:
    cost = round(shares * entry + shares * fee_per_share(entry, fee_rate, fee_exp), 6)
    return {
        "pos_id": f"{kind.upper()}-{str(paper_pos.get('pos_id'))[-12:]}",
        "kind": kind,
        "paper_pos_id": str(paper_pos.get("pos_id") or ""),
        "event_id": paper_pos.get("event_id", ""),
        "market_question": paper_pos.get("market_question", ""),
        "outcome": label,
        "token_id": token,
        "entry_price": round(entry, 6),
        "current_bid": round(entry, 6),
        "current_ask": round(entry, 6),
        "shares": round(shares, 6),
        "cost_usdc": cost,
        "current_value_usdc": round(shares * entry, 6),
        "unrealized_pnl_usdc": round(shares * entry - cost, 6),
        "unrealized_pnl_pct": round(((shares * entry - cost) / cost) * 100.0, 2) if cost else 0.0,
        "p_est": 0.0,
        "edge_at_entry": 0.0,
        "fee_rate": fee_rate,
        "fee_exponent": fee_exp,
        "pending_shares": 0.0,
        "limit_price": None,
        "opened_at": _now(),
        "book_updated_at": _now(),
        "book_status": kind.upper(),
    }


def mirror_open(kind: str, paper_pos: Dict[str, Any], token: str, label: str,
                book: Dict[str, Any], fee_rate: float = 0.0, fee_exp: float = 1.0) -> Optional[Dict[str, Any]]:
    """纸面开仓后，在反方向建立对应仓位。"""
    state = load_state(kind)
    paper_pos_id = str(paper_pos.get("pos_id") or "")
    if any(p.get("paper_pos_id") == paper_pos_id for p in state["open_positions"]):
        return None  # 幂等
    requested = float(paper_pos.get("shares") or 0.0)
    if requested <= 0:
        return None

    if kind == "reverse":
        ask = float(book.get("best_ask") or 0.0)
        if ask <= 0 or ask >= 1:
            return None
        unit = ask + fee_per_share(ask, fee_rate, fee_exp)
        affordable = float(state["cash_usdc"]) / unit if unit > 0 else 0.0
        shares = min(requested, round(affordable, 4))
        if shares < MIN_SHARES:
            state["notes"].append({"ts_utc": _now(), "reason": "现金不足，未建反向仓",
                                   "paper_pos_id": paper_pos_id, "requested": requested})
            save_state(kind, state)
            return None
        position = _new_position(kind, paper_pos, token, label, shares, ask, fee_rate, fee_exp)
        state["cash_usdc"] = round(float(state["cash_usdc"]) - position["cost_usdc"], 6)
        state["open_positions"].append(position)
        _recompute(state)
        save_state(kind, state)
        print(f"[反向持有] 买入 {label} {shares:g}/{requested:g} 份 @ {ask:.3f}", flush=True)
        return position

    # maker：同样按卖一价立即成交。反向跟单必须保证有仓——挂在买一等成交，
    # 在纸面下跌（反向本该赚钱）时价格只会越走越远，最后纸面平仓、挂单被撤，反向盘等于没跟。
    ask = float(book.get("best_ask") or 0.0)
    if ask <= 0 or ask >= 1:
        return None
    unit = ask + fee_per_share(ask, fee_rate, fee_exp)
    affordable = float(state["cash_usdc"]) / unit if unit > 0 else 0.0
    shares = min(requested, round(affordable, 4))
    if shares < MIN_SHARES:
        state["notes"].append({"ts_utc": _now(), "reason": "现金不足，未建反向仓",
                               "paper_pos_id": paper_pos_id, "requested": requested})
        save_state(kind, state)
        return None
    position = _new_position(kind, paper_pos, token, label, shares, ask, fee_rate, fee_exp)
    position["fill_mode"] = "taker_at_ask"
    position["limit_price"] = round(ask, 6)
    state["cash_usdc"] = round(float(state["cash_usdc"]) - position["cost_usdc"], 6)
    state["open_positions"].append(position)
    _recompute(state)
    save_state(kind, state)
    print(f"[反向跟单] 买入 {label} {shares:g}/{requested:g} 份 @ {ask:.3f}（卖一）", flush=True)
    return position


def _fill_pending(kind: str, state: Dict[str, Any], position: Dict[str, Any], book: Dict[str, Any]) -> None:
    pending = float(position.get("pending_shares") or 0.0)
    if pending <= 0:
        return
    limit = float(position.get("limit_price") or 0.0)
    ask = float(book.get("best_ask") or 0.0)
    if ask <= 0 or limit <= 0 or ask > limit:
        return  # 还没轮到成交
    rate = float(position.get("fee_rate") or 0.0)
    exp = float(position.get("fee_exponent") or 1.0)
    unit = limit + fee_per_share(limit, rate, exp)
    affordable = float(state["cash_usdc"]) / unit if unit > 0 else 0.0
    take = min(pending, round(affordable, 4))
    if take < MIN_SHARES:
        return
    add_cost = round(take * unit, 6)
    old_shares = float(position.get("shares") or 0.0)
    old_cost = float(position.get("cost_usdc") or 0.0)
    position["shares"] = round(old_shares + take, 6)
    position["cost_usdc"] = round(old_cost + add_cost, 6)
    position["entry_price"] = round(position["cost_usdc"] / position["shares"], 6)
    position["pending_shares"] = round(pending - take, 6)
    state["cash_usdc"] = round(float(state["cash_usdc"]) - add_cost, 6)
    print(f"[反向挂单] 成交 {take:g} 份 @ {limit:.3f}（余 {position['pending_shares']:g} 份）", flush=True)


def _chase_pending(kind: str, state: Dict[str, Any], position: Dict[str, Any], book: Dict[str, Any]) -> None:
    """旧版"买一挂单"留下的未成交记录：按当前卖一价补成交，避免反向盘缺席。"""
    pending = float(position.get("pending_shares") or 0.0)
    if pending <= 0:
        return
    ask = float(book.get("best_ask") or 0.0)
    if ask <= 0 or ask >= 1:
        return
    rate = float(position.get("fee_rate") or 0.0)
    exp = float(position.get("fee_exponent") or 1.0)
    unit = ask + fee_per_share(ask, rate, exp)
    affordable = float(state["cash_usdc"]) / unit if unit > 0 else 0.0
    take = min(pending, round(affordable, 4))
    if take < MIN_SHARES:
        return
    add_cost = round(take * unit, 6)
    old_shares = float(position.get("shares") or 0.0)
    old_cost = float(position.get("cost_usdc") or 0.0)
    position["shares"] = round(old_shares + take, 6)
    position["cost_usdc"] = round(old_cost + add_cost, 6)
    position["entry_price"] = round(position["cost_usdc"] / position["shares"], 6)
    position["pending_shares"] = round(pending - take, 6)
    position["limit_price"] = round(ask, 6)
    position["fill_mode"] = "chase_at_ask"
    state["cash_usdc"] = round(float(state["cash_usdc"]) - add_cost, 6)
    print(f"[反向跟单] 旧挂单补成交 {take:g} 份 @ {ask:.3f}（卖一）", flush=True)


def _parse_ts(value: Any):
    try:
        return datetime.fromisoformat(str(value).replace(" UTC", "")).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _finalize_exit(kind: str, state: Dict[str, Any], position: Dict[str, Any], price: float, reason: str) -> None:
    shares = float(position.get("shares") or 0.0)
    net = shares * price - shares * fee_per_share(
        price, float(position.get("fee_rate") or 0.0), float(position.get("fee_exponent") or 1.0)
    )
    pnl = round(net - float(position.get("cost_usdc") or 0.0), 6)
    state["open_positions"] = [p for p in state["open_positions"] if p is not position]
    state["cash_usdc"] = round(float(state["cash_usdc"]) + net, 6)
    state["realized_pnl_usdc"] = round(float(state["realized_pnl_usdc"]) + pnl, 6)
    state["closed_trades"].insert(0, {
        **position,
        "exit_order": None,
        "exit_price_bid": round(price, 6),
        "current_value_usdc": round(net, 6),
        "realized_pnl_usdc": pnl,
        "realized_pnl_pct": round((pnl / float(position.get("cost_usdc") or 1)) * 100.0, 2),
        "exit_reason": reason,
        "closed_at": _now(),
    })
    print(f"[{kind}] 平仓 {position.get('outcome')} {shares:g} 份 @ {price:.3f} | {pnl:+.4f}", flush=True)


def tick(kind: str, data_layer) -> Dict[str, Any]:
    """每轮：尝试成交挂单、盯市、检测结算并平掉已结算仓位。"""
    state = load_state(kind)
    for position in list(state["open_positions"]):
        token = str(position.get("token_id") or "")
        book = None
        try:
            book = data_layer.fetch_orderbook(token)
        except Exception:
            book = None
        if book:
            exit_order = position.get("exit_order")
            if exit_order:
                limit = float(exit_order.get("limit_price") or 0.0)
                bid_now = float(book.get("best_bid") or 0.0)
                placed = _parse_ts(exit_order.get("placed_at"))
                elapsed = (datetime.now(timezone.utc) - placed).total_seconds() if placed else EXIT_WAIT_SECONDS
                if limit > 0 and bid_now >= limit:
                    _finalize_exit(kind, state, position, limit, f"跟随纸面平仓（挂卖单成交 @{limit:.3f}）")
                    continue
                if elapsed >= EXIT_WAIT_SECONDS and bid_now > 0:
                    _finalize_exit(
                        kind, state, position, bid_now,
                        f"跟随纸面平仓（挂卖单 {EXIT_WAIT_SECONDS:.0f}s 未成交，按买一 {bid_now:.3f}）",
                    )
                    continue
            if kind == "maker" and float(position.get("pending_shares") or 0.0) > 0:
                # 正常不会再有 pending（开仓即成交）；只处理旧版遗留的挂单
                if position.get("fill_mode") == "taker_at_ask":
                    _fill_pending(kind, state, position, book)
                else:
                    _chase_pending(kind, state, position, book)
            # 挂单未成交时也要刷新盘口，否则面板一直显示建仓那一刻的价格
            position["current_bid"] = round(float(book.get("best_bid") or 0.0), 6)
            position["current_ask"] = round(float(book.get("best_ask") or 0.0), 6)
            position["book_updated_at"] = _now()
            bid = float(book.get("best_bid") or 0.0)
            if bid > 0 and float(position.get("shares") or 0) > 0:
                shares = float(position["shares"])
                cost = float(position.get("cost_usdc") or 0.0)
                net = shares * bid - shares * fee_per_share(
                    bid, float(position.get("fee_rate") or 0.0), float(position.get("fee_exponent") or 1.0)
                )
                position["current_bid"] = round(bid, 6)
                position["book_updated_at"] = _now()
                position["current_value_usdc"] = round(net, 6)
                position["unrealized_pnl_usdc"] = round(net - cost, 6)
                position["unrealized_pnl_pct"] = round(((net - cost) / cost) * 100.0, 2) if cost else 0.0
            continue
        # 无盘口：尝试结算价
        try:
            mark = data_layer.fetch_resolved_mark(token, position.get("event_id"))
        except Exception:
            mark = None
        settled = float((mark or {}).get("best_bid") or -1.0)
        if settled not in (0.0, 1.0):
            continue
        shares = float(position.get("shares") or 0.0)
        if shares <= 0:
            state["open_positions"].remove(position)
            continue
        cost = float(position.get("cost_usdc") or 0.0)
        proceeds = round(shares * settled, 6)
        pnl = round(proceeds - cost, 6)
        state["open_positions"].remove(position)
        state["cash_usdc"] = round(float(state["cash_usdc"]) + proceeds, 6)
        state["realized_pnl_usdc"] = round(float(state["realized_pnl_usdc"]) + pnl, 6)
        state["closed_trades"].insert(0, {
            **position,
            "exit_price_bid": settled,
            "current_value_usdc": proceeds,
            "realized_pnl_usdc": pnl,
            "realized_pnl_pct": round((pnl / cost) * 100.0, 2) if cost else 0.0,
            "exit_reason": "到结算价",
            "closed_at": _now(),
        })
        print(f"[{kind}] 结算 {position.get('outcome')} {shares:g} 份 @ {settled:.2f} | {pnl:+.4f}", flush=True)
    _recompute(state)
    save_state(kind, state)
    return state


def find_for_paper(kind: str, paper_pos_id: Any) -> Optional[Dict[str, Any]]:
    target = str(paper_pos_id)
    for position in load_state(kind)["open_positions"]:
        if position.get("paper_pos_id") == target:
            return position
    return None


def close_for_paper(kind: str, paper_pos: Dict[str, Any], book: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """纸面平仓时的处理：reverse 持有不动；maker 卖出（未成交则撤单）。"""
    if kind == "reverse":
        return None  # 持有到结算
    state = load_state(kind)
    target = next((p for p in state["open_positions"]
                   if p.get("paper_pos_id") == str(paper_pos.get("pos_id"))), None)
    if target is None:
        return None
    shares = float(target.get("shares") or 0.0)
    if shares <= 0:
        # 未成交：撤单
        state["open_positions"].remove(target)
        _recompute(state)
        save_state(kind, state)
        print("[反向挂单] 纸面已平仓但挂单未成交，撤单", flush=True)
        return None
    if target.get("exit_order"):
        return None
    bid = float(book.get("best_bid") or 0.0)
    ask = float(book.get("best_ask") or 0.0)
    limit = ask if 0 < ask < 1 else bid
    if limit <= 0:
        return None
    # 不再直接吃买一：挂在卖一，等买盘来吃；超时（下一轮）再按买一兜底
    target["exit_order"] = {
        "limit_price": round(limit, 6),
        "shares": shares,
        "placed_at": _now(),
    }
    _recompute(state)
    save_state(kind, state)
    print(
        f"[反向跟单] 纸面平仓 → 挂卖单 {shares:g} 份 @ {limit:.3f}"
        f"（买一 {bid:.3f}，{EXIT_WAIT_SECONDS:.0f}s 未成交转市价）",
        flush=True,
    )
    return None
