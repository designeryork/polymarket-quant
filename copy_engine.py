"""跟单纸盘引擎：盯 3 位交易员的持仓，按比例镜像到各自的 $10 纸面账户。

规则（2026-11-05 与用户确认）
--------------------------------
* 初始资金：每位交易员一个独立账户，起始 $10；
* 零回填：上线时把他们已有持仓记为「基线」，只跟之后的变化；
* 首次开仓：用该账户**当前可用现金的 50%** 按卖一价买入；
* 加仓：按对方加仓比例跟 —— 维持「我方份额 / 对方份额」的比例不变，
  即新增份额 = 对方新增份额 × 当前比例（现金不足时按可用现金买满并记录事件）；
* 部分减仓：同比例减仓（对方卖掉 30%，我们也卖 30%）；
* 清仓：对方该 token 归零 → 按买一价卖出；若市场已结算（盘口消失）→ 按结算价 1/0 结算；
* 成交价：买按卖一、卖按买一（和主引擎纸盘一致，真实价差计入成本）。

数据来源（2026-11-03 起 Data API v1 已 410，只能用 v2）
--------------------------------
* `/v2/positions?user=...&status=OPEN|closed` —— 持仓，含 title/outcome/current_size/
  avg_price/current_price/first_entry_at，cursor 分页；
* `CLOB /book` —— 买卖一价；
* `CLOB /markets/{condition_id}` —— 市场是否结算、结算价（1/0）。
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state" / "copy"
STATE_FILE = STATE_DIR / "state.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
EQUITY_DIR = STATE_DIR / "equity"
HEARTBEAT_FILE = STATE_DIR / "heartbeat.json"

DATA_API = "https://data-api.polymarket.com"
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

STARTING_CASH_USD = 10.0
CASH_FRACTION = 0.5          # 首次开仓用可用现金的 50%
MIN_ORDER_USD = 1.0          # Polymarket 最小下单金额
EPS = 1e-6
MAX_COPY_LAG_SEC = 900       # 断线超过 15 分钟，新开仓不再补跟，只记 MISSED_STALE

# 执行硬约束（不是策略择时）：单边挂单会造成"买入即腰斩"。
# 实测三位交易员 42 个持仓：价差中位数 2.9%，≤10% 占 70%，但存在 85%~99% 的假盘口。
MAX_SPREAD_REL = 0.10        # 入场：价差 / 卖一 > 10% 不买
MAX_EXIT_SPREAD_REL = 0.15   # 出场：价差 / 卖一 > 15% 暂缓（下轮再试，结算走结算价）
DEPTH_BAND = 0.02            # 深度统计范围：最优价 ±2%

# 接盘保护（2026-11-05 复盘得出）
# 财报类市场里，对方买入后 1~3 分钟价格就从 0.87 跳到 0.999；
# 我们在 0.999 接盘只剩 0.1% 空间，而 1 个 tick 的价差就约 0.9% —— 结构性负期望。
# 实测 Warby / Wendy's / Frontdoor 三笔：对方均价 0.873~0.964，我们全部买在 0.999。
MAX_ENTRY_PRICE = 0.97       # 入场价上限：剩余空间不足 3% 不追
MAX_ENTRY_SLIPPAGE = 0.05    # 相对对方持仓均价的滑点上限：贵 5 分以上视为接盘太晚

TRADERS: Tuple[Dict[str, str], ...] = (
    {
        "id": "makeit99",
        "name": "makeit99",
        "address": "0x2f633efb75256a2f2445110c8978684ab8936643",
        "profile": "https://polymarket.com/@makeit99",
    },
    {
        "id": "supersmard",
        "name": "supersmard",
        "address": "0xe3f2ecfd3e648bdabcd3fb701ccf294f13afe1c1",
        "profile": "https://polymarket.com/@supersmard",
    },
    {
        "id": "robbb",
        "name": "Robbb",
        "address": "0x9cdda449d7cedf1072d74982a5dc2349df3d3e97",
        "profile": "https://polymarket.com/@Robbb",
    },
)


# ==========================================================
# 时间 / 数值工具
# ==========================================================
def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ts_now() -> int:
    return int(time.time())


def floor_shares(value: float, digits: int = 2) -> float:
    factor = 10 ** digits
    return math.floor(value * factor + 1e-9) / factor


def money(value: float, digits: int = 6) -> float:
    return round(float(value) + 1e-12, digits)


# ==========================================================
# 状态读写
# ==========================================================
def empty_account(trader: Dict[str, str]) -> Dict[str, Any]:
    return {
        "id": trader["id"],
        "name": trader["name"],
        "address": trader["address"],
        "profile": trader["profile"],
        "starting_cash_usd": STARTING_CASH_USD,
        "cash_usd": STARTING_CASH_USD,
        "realized_pnl_usd": 0.0,
        "positions": {},
        "baseline_sizes": {},
        "baseline_at": None,
        "last_poll_at": None,
        "last_poll_ok": False,
        "last_error": "",
        "target_equity_usd": 0.0,
    }


def empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "started_at": iso_now(),
        "updated_at": iso_now(),
        "accounts": {t["id"]: empty_account(t) for t in TRADERS},
    }


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return empty_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return empty_state()
    if not isinstance(data, dict) or "accounts" not in data:
        return empty_state()
    # 新增交易员时自动补齐，不破坏已有账本
    for trader in TRADERS:
        data["accounts"].setdefault(trader["id"], empty_account(trader))
    return data


def save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = iso_now()
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def append_event(event: Dict[str, Any]) -> None:
    """事件流：只追加，不覆盖，供事后分析。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    event.setdefault("ts", ts_now())
    event.setdefault("time", utc_now())
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_equity(state: Dict[str, Any], marks: Dict[str, float]) -> None:
    """每分钟一条净值快照，按天分文件，只追加。"""
    EQUITY_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    record = {"ts": ts_now(), "accounts": {}}
    for trader_id, account in state["accounts"].items():
        summary = account_summary(account, marks.get(trader_id, {}))
        record["accounts"][trader_id] = {
            "e": round(summary["equity_usd"], 4),
            "c": round(summary["cash_usd"], 4),
            "p": round(summary["total_pnl_usd"], 4),
        }
    with (EQUITY_DIR / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_equity_history(days: int = 3, limit_per_account: int = 150) -> Dict[str, List[Dict[str, float]]]:
    """读取最近若干天的净值曲线，按账户返回 [{"t":..., "e":...}]。"""
    out: Dict[str, List[Dict[str, float]]] = {t["id"]: [] for t in TRADERS}
    if not EQUITY_DIR.exists():
        return out
    files = sorted(EQUITY_DIR.glob("*.jsonl"))[-days:]
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                for trader_id, values in (rec.get("accounts") or {}).items():
                    out.setdefault(trader_id, []).append({"t": rec.get("ts", 0), "e": values.get("e", 0.0)})
        except Exception:
            continue
    for trader_id, points in out.items():
        if len(points) > limit_per_account:
            step = len(points) / float(limit_per_account)
            out[trader_id] = [points[min(int(i * step), len(points) - 1)] for i in range(limit_per_account)]
    return out


def save_heartbeat(payload: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HEARTBEAT_FILE)


# ==========================================================
# HTTP
# ==========================================================
def build_session() -> requests.Session:
    """复用主项目的通道探测（云端 ALLOW_DIRECT=true 时走直连）。"""
    try:
        from proxy_detector import build_session as _build

        return _build()
    except Exception:
        return requests.Session()


def _get(session: requests.Session, url: str, params: Optional[Dict[str, Any]] = None,
         timeout: Tuple[float, float] = (8.0, 20.0)) -> Any:
    from rate_limiter import request_with_retry

    resp = request_with_retry(
        session, "GET", url, params=params, timeout=timeout, throttle=True, respect_budget=False
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} {url}")
    return resp.json()


def fetch_positions(session: requests.Session, wallet: str, status: str = "OPEN",
                    max_pages: int = 20) -> Dict[str, Dict[str, Any]]:
    """拉取某钱包的持仓（cursor 分页），返回 {token_id: 原始记录}。"""
    result: Dict[str, Dict[str, Any]] = {}
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: Dict[str, Any] = {"user": wallet, "limit": 100, "status": status}
        if cursor:
            params["cursor"] = cursor
        payload = _get(session, f"{DATA_API}/v2/positions", params)
        for item in payload.get("data") or []:
            token = str(item.get("token_id") or "")
            if token:
                result[token] = item
        pagination = payload.get("pagination") or {}
        cursor = pagination.get("next_cursor")
        if not pagination.get("has_more") or not cursor:
            break
    return result


def fetch_book(session: requests.Session, token_id: str) -> Optional[Dict[str, float]]:
    """返回最优买卖价；盘口缺失（已结算/停牌）返回 None。"""
    try:
        book = _get(session, f"{CLOB_HOST}/book", {"token_id": token_id}, timeout=(6.0, 12.0))
    except Exception:
        return None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    parsed_bids = sorted(((float(b["price"]), float(b["size"])) for b in bids), reverse=True)
    parsed_asks = sorted((float(a["price"]), float(a["size"])) for a in asks)
    best_bid, best_ask = parsed_bids[0][0], parsed_asks[0][0]
    if best_ask <= 0:
        return None
    bid_depth = sum(size for price, size in parsed_bids if price >= best_bid * (1 - DEPTH_BAND))
    ask_depth = sum(size for price, size in parsed_asks if price <= best_ask * (1 + DEPTH_BAND))
    spread_rel = (best_ask - best_bid) / best_ask
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_rel": spread_rel,
        "bid_depth_shares": bid_depth,
        "ask_depth_shares": ask_depth,
        "bid_depth_usdc": bid_depth * best_bid,
        "ask_depth_usdc": ask_depth * best_ask,
        "mid": (best_bid + best_ask) / 2.0,
    }


def fetch_settlement_price(session: requests.Session, condition_id: str,
                           outcome_index: int, outcome: str = "") -> Optional[float]:
    """市场结算后取结算价（1 或 0）。

    为什么不用 Gamma：实测 `gamma-api/markets?condition_ids=<hex>` 返回 0 条，
    而 `?condition_id=` 会忽略参数、返回 20 个无关市场（差点拿错结算价）。
    正确来源是 CLOB `GET /markets/<condition_id>`：closed=True，
    tokens 里带 winner/price，且顺序与 outcome_index 一致。
    """
    if not condition_id:
        return None
    try:
        market = _get(session, f"{CLOB_HOST}/markets/{condition_id}", timeout=(6.0, 15.0))
    except Exception:
        return None
    if not isinstance(market, dict) or not market.get("closed"):
        return None
    tokens = market.get("tokens") or []
    token: Optional[Dict[str, Any]] = None
    if outcome:
        for item in tokens:
            if str(item.get("outcome") or "").lower() == outcome.lower():
                token = item
                break
    if token is None and 0 <= outcome_index < len(tokens):
        token = tokens[outcome_index]
    if not token:
        return None
    winner = token.get("winner")
    if winner is True:
        return 1.0
    if winner is False:
        return 0.0
    try:
        return float(token.get("price"))
    except (TypeError, ValueError):
        return None


# ==========================================================
# 账户估值
# ==========================================================
def account_summary(account: Dict[str, Any], marks: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    marks = marks or {}
    value = 0.0
    unrealized = 0.0
    for token, position in (account.get("positions") or {}).items():
        price = marks.get(token, position.get("last_mark_price") or 0.0)
        mark_value = float(position.get("shares") or 0.0) * float(price or 0.0)
        value += mark_value
        unrealized += mark_value - float(position.get("cost_usd") or 0.0)
    cash = float(account.get("cash_usd") or 0.0)
    equity = cash + value
    total_pnl = equity - float(account.get("starting_cash_usd") or STARTING_CASH_USD)
    return {
        "cash_usd": cash,
        "positions_value_usd": value,
        "equity_usd": equity,
        "unrealized_pnl_usd": unrealized,
        "realized_pnl_usd": float(account.get("realized_pnl_usd") or 0.0),
        "total_pnl_usd": total_pnl,
        "total_return_pct": total_pnl / max(1e-9, float(account.get("starting_cash_usd") or STARTING_CASH_USD)) * 100.0,
        "position_count": len(account.get("positions") or {}),
    }


# ==========================================================
# 镜像逻辑
# ==========================================================
def _record_skip(trader_id: str, token: str, target: Dict[str, Any], reason: str, **extra: Any) -> None:
    append_event({
        "type": "COPY_SKIP",
        "trader": trader_id,
        "token": token,
        "title": target.get("title") or "",
        "outcome": target.get("outcome") or "",
        "reason": reason,
        **extra,
    })


def _open_position(account: Dict[str, Any], trader_id: str,
                   token: str, target: Dict[str, Any], book: Optional[Dict[str, float]]) -> None:
    if not book:
        _record_skip(trader_id, token, target, "NO_BOOK")
        return
    ask = book["best_ask"]
    if not (0.0 < ask < 1.0):
        _record_skip(trader_id, token, target, "BAD_PRICE", price=ask)
        return
    if book["spread_rel"] > MAX_SPREAD_REL:
        _record_skip(trader_id, token, target, "SPREAD_TOO_WIDE",
                     spread_pct=round(book["spread_rel"] * 100, 1))
        return
    if ask >= MAX_ENTRY_PRICE:
        _record_skip(trader_id, token, target, "PRICE_TOO_HIGH", price=round(ask, 4))
        return
    target_avg = float(target.get("avg_price") or 0.0)
    if target_avg > 0 and ask - target_avg > MAX_ENTRY_SLIPPAGE:
        _record_skip(trader_id, token, target, "LATE_ENTRY",
                     price=round(ask, 4), target_avg=round(target_avg, 4),
                     slippage=round(ask - target_avg, 4))
        return
    cash = float(account.get("cash_usd") or 0.0)
    spend = min(cash * CASH_FRACTION, book["ask_depth_usdc"])
    if spend < MIN_ORDER_USD:
        reason = "INSUFFICIENT_CASH" if cash * CASH_FRACTION < MIN_ORDER_USD else "DEPTH_TOO_THIN"
        _record_skip(trader_id, token, target, reason,
                     cash=round(cash, 4), depth_usd=round(book["ask_depth_usdc"], 2))
        return
    shares = floor_shares(spend / ask)
    if shares <= 0:
        _record_skip(trader_id, token, target, "TOO_SMALL")
        return
    cost = money(shares * ask)
    target_size = float(target.get("current_size") or 0.0)
    account["cash_usd"] = money(cash - cost)
    account["positions"][token] = {
        "token_id": token,
        "condition_id": target.get("condition_id") or "",
        "title": target.get("title") or "",
        "outcome": target.get("outcome") or "",
        "outcome_index": int(target.get("outcome_index") or 0),
        "shares": shares,
        "cost_usd": cost,
        "avg_price": money(ask),
        "opened_at": utc_now(),
        "target_size_at_entry": target_size,
        "ratio": shares / target_size if target_size > 0 else 0.0,
        "last_mark_price": book["best_bid"],
        "last_mark_at": utc_now(),
        "book_ok": True,
    }
    append_event({
        "type": "COPY_BUY", "trader": trader_id, "token": token,
        "title": target.get("title") or "", "outcome": target.get("outcome") or "",
        "shares": shares, "price": money(ask), "usd": cost,
        "target_size": target_size, "cash_after": account["cash_usd"],
        "reason": "OPEN",
    })


def _add_position(account: Dict[str, Any], trader_id: str,
                  token: str, position: Dict[str, Any], target: Dict[str, Any],
                  delta_target: float, book: Optional[Dict[str, float]]) -> None:
    if not book:
        _record_skip(trader_id, token, position, "NO_BOOK_ADD")
        return
    ratio = float(position.get("ratio") or 0.0)
    if ratio <= 0:
        _record_skip(trader_id, token, position, "NO_RATIO")
        return
    if book["spread_rel"] > MAX_SPREAD_REL:
        _record_skip(trader_id, token, position, "SPREAD_TOO_WIDE_ADD",
                     spread_pct=round(book["spread_rel"] * 100, 1))
        return
    ask = book["best_ask"]
    if ask >= MAX_ENTRY_PRICE:
        _record_skip(trader_id, token, position, "PRICE_TOO_HIGH_ADD", price=round(ask, 4))
        return
    target_avg = float(target.get("avg_price") or 0.0)
    if target_avg > 0 and ask - target_avg > MAX_ENTRY_SLIPPAGE:
        _record_skip(trader_id, token, position, "LATE_ENTRY_ADD",
                     price=round(ask, 4), target_avg=round(target_avg, 4))
        return
    want_shares = floor_shares(delta_target * ratio)
    if want_shares <= 0:
        return
    cash = float(account.get("cash_usd") or 0.0)
    max_by_cash = floor_shares(cash / ask) if ask > 0 else 0.0
    max_by_depth = floor_shares(book["ask_depth_shares"])
    shares = min(want_shares, max_by_cash, max_by_depth)
    if shares <= 0:
        _record_skip(trader_id, token, position, "INSUFFICIENT_CASH_ADD", cash=round(cash, 4))
        return
    cost = money(shares * ask)
    if cost < MIN_ORDER_USD:
        _record_skip(trader_id, token, position, "TOO_SMALL_ADD", usd=cost)
        return
    old_shares = float(position["shares"])
    old_cost = float(position["cost_usd"])
    account["cash_usd"] = money(cash - cost)
    position["shares"] = floor_shares(old_shares + shares)
    position["cost_usd"] = money(old_cost + cost)
    position["avg_price"] = money(position["cost_usd"] / max(1e-9, position["shares"]))
    position["last_mark_price"] = book["best_bid"]
    position["last_mark_at"] = utc_now()
    # 现金不足导致没跟够时，重算比例，避免下一轮继续"追补"
    target_size = float(target.get("current_size") or 0.0)
    position["target_size_at_entry"] = target_size
    if target_size > 0:
        position["ratio"] = position["shares"] / target_size
    append_event({
        "type": "COPY_BUY", "trader": trader_id, "token": token,
        "title": position.get("title") or "", "outcome": position.get("outcome") or "",
        "shares": shares, "price": money(ask), "usd": cost,
        "target_size": target_size, "cash_after": account["cash_usd"],
        "partial": shares < want_shares,
        "reason": "ADD",
    })


def _reduce_position(account: Dict[str, Any], trader_id: str, token: str,
                     position: Dict[str, Any], target: Dict[str, Any],
                     target_after: float, book: Optional[Dict[str, float]]) -> None:
    target_before = float(position.get("target_size_at_entry") or 0.0)
    if target_before <= 0:
        position["target_size_at_entry"] = target_after
        return
    keep_ratio = max(0.0, min(1.0, target_after / target_before))
    sell_shares = floor_shares(float(position["shares"]) * (1.0 - keep_ratio))
    position["target_size_at_entry"] = target_after
    if sell_shares <= 0:
        return
    if not book:
        _record_skip(trader_id, token, position, "NO_BOOK_REDUCE", shares=sell_shares)
        return
    if book["spread_rel"] > MAX_EXIT_SPREAD_REL:
        _record_skip(trader_id, token, position, "EXIT_SPREAD_TOO_WIDE",
                     spread_pct=round(book["spread_rel"] * 100, 1))
        return
    bid = book["best_bid"]
    sell_shares = min(sell_shares, floor_shares(book["bid_depth_shares"]))
    if sell_shares <= 0:
        _record_skip(trader_id, token, position, "BID_DEPTH_TOO_THIN")
        return
    proceeds = money(sell_shares * bid)
    cost_part = money(float(position["cost_usd"]) * (sell_shares / max(1e-9, float(position["shares"]))))
    position["shares"] = floor_shares(float(position["shares"]) - sell_shares)
    position["cost_usd"] = money(float(position["cost_usd"]) - cost_part)
    position["avg_price"] = money(position["cost_usd"] / position["shares"]) if position["shares"] > 0 else 0.0
    account["cash_usd"] = money(float(account["cash_usd"]) + proceeds)
    account["realized_pnl_usd"] = money(float(account["realized_pnl_usd"]) + proceeds - cost_part)
    position["last_mark_price"] = bid
    position["last_mark_at"] = utc_now()
    if position["shares"] <= 0:
        account["positions"].pop(token, None)
    append_event({
        "type": "COPY_SELL", "trader": trader_id, "token": token,
        "title": position.get("title") or "", "outcome": position.get("outcome") or "",
        "shares": sell_shares, "price": money(bid), "usd": proceeds,
        "realized_pnl": money(proceeds - cost_part),
        "reason": "REDUCE",
    })


def _close_position(account: Dict[str, Any], trader_id: str, token: str,
                    position: Dict[str, Any], session: requests.Session) -> None:
    shares = float(position.get("shares") or 0.0)
    cost = float(position.get("cost_usd") or 0.0)
    if shares <= 0:
        account["positions"].pop(token, None)
        return
    book = fetch_book(session, token)
    if book and book["spread_rel"] <= MAX_EXIT_SPREAD_REL:
        price = book["best_bid"]
        kind = "COPY_SELL"
        reason = "CLOSE"
        # 买盘深度不够时这次先卖能卖的部分，余量下轮继续
        shares = min(shares, floor_shares(book["bid_depth_shares"]))
        if shares <= 0:
            _record_skip(trader_id, token, position, "BID_DEPTH_TOO_THIN_CLOSE")
            return
        cost = money(float(position["cost_usd"]) * (shares / max(1e-9, float(position["shares"] or shares))))
    else:
        # 盘口缺失（已结算）或双边价差过大：只有市场确实结算才按 1/0 结算
        settled = fetch_settlement_price(
            session, position.get("condition_id") or "",
            int(position.get("outcome_index") or 0), position.get("outcome") or "",
        )
        if settled is None:
            reason = "CLOSE_SPREAD_TOO_WIDE" if book else "CLOSE_PRICE_UNAVAILABLE"
            _record_skip(trader_id, token, position, reason,
                         spread_pct=round(book["spread_rel"] * 100, 1) if book else None)
            return
        price = settled
        kind = "COPY_SETTLE"
        reason = "SETTLED"
    proceeds = money(shares * price)
    account["cash_usd"] = money(float(account["cash_usd"]) + proceeds)
    account["realized_pnl_usd"] = money(float(account["realized_pnl_usd"]) + proceeds - cost)
    remaining = floor_shares(float(position["shares"]) - shares)
    if remaining <= 0:
        account["positions"].pop(token, None)
    else:
        position["shares"] = remaining
        position["cost_usd"] = money(float(position["cost_usd"]) - cost)
    append_event({
        "type": kind, "trader": trader_id, "token": token,
        "title": position.get("title") or "", "outcome": position.get("outcome") or "",
        "shares": shares, "price": money(price), "usd": proceeds,
        "realized_pnl": money(proceeds - cost), "reason": reason,
    })


def _target_snapshot(targets: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """对方持仓的精简快照，存进账本供页面展示与事后分析。"""
    snapshot: Dict[str, Dict[str, Any]] = {}
    for token, item in targets.items():
        snapshot[token] = {
            "token_id": token,
            "title": item.get("title") or "",
            "outcome": item.get("outcome") or "",
            "size": round(float(item.get("current_size") or 0.0), 4),
            "avg_price": round(float(item.get("avg_price") or 0.0), 4),
            "current_price": round(float(item.get("current_price") or 0.0), 4),
            "value_usd": round(float(item.get("current_value") or 0.0), 2),
            "unrealized_pnl": round(float(item.get("unrealized_pnl") or 0.0), 2),
            "first_entry_at": item.get("first_entry_at") or 0,
        }
    return snapshot


def reconcile_account(session: requests.Session, account: Dict[str, Any],
                      targets: Dict[str, Dict[str, Any]], lag_sec: int) -> Dict[str, float]:
    """把账户对齐到目标持仓，返回 {token: 标记价}。"""
    trader_id = account["id"]
    marks: Dict[str, float] = {}
    now = utc_now()

    # 首次运行：只记录基线，不跟
    if account.get("baseline_at") is None:
        account["baseline_sizes"] = {
            token: float(item.get("current_size") or 0.0) for token, item in targets.items()
        }
        account["baseline_at"] = now
        account["target_positions"] = _target_snapshot(targets)
        append_event({
            "type": "BASELINE", "trader": trader_id,
            "positions": len(targets),
            "titles": [item.get("title") for item in list(targets.values())[:10]],
        })
        for token, item in targets.items():
            marks[token] = float(item.get("current_price") or 0.0)
        account["last_poll_at"] = now
        account["last_poll_ok"] = True
        return marks

    # 1) 目标里新增 / 加仓 / 减仓
    baseline = account.get("baseline_sizes") or {}
    for token, target in targets.items():
        target_size = float(target.get("current_size") or 0.0)
        if target_size <= EPS:
            continue
        position = (account.get("positions") or {}).get(token)
        if position is None:
            # 零回填：上线时已存在的仓位永远不跟（即使对方之后加仓，也没有基准比例）
            if token in baseline:
                continue
            if lag_sec > MAX_COPY_LAG_SEC:
                _record_skip(trader_id, token, target, "MISSED_STALE", lag_sec=lag_sec)
                continue
            _open_position(account, trader_id, token, target, fetch_book(session, token))
        else:
            before = float(position.get("target_size_at_entry") or 0.0)
            if target_size > before + EPS:
                _add_position(account, trader_id, token, position, target,
                              target_size - before, fetch_book(session, token))
            elif target_size < before - EPS:
                _reduce_position(account, trader_id, token, position, target,
                                 target_size, fetch_book(session, token))

    # 2) 我们持有但目标已清空的仓位
    for token in list((account.get("positions") or {}).keys()):
        if token in targets:
            continue
        position = account["positions"][token]
        _close_position(account, trader_id, token, position, session)

    # 3) 标记价：优先盘口买一；价差过大时用中间价（避免单边挂单把净值打穿），缺失则沿用上次
    for token, position in (account.get("positions") or {}).items():
        book = fetch_book(session, token)
        if book:
            mark = book["best_bid"] if book["spread_rel"] <= MAX_SPREAD_REL else book["mid"]
            position["last_mark_price"] = mark
            position["last_mark_at"] = utc_now()
            position["book_ok"] = True
            marks[token] = mark
        else:
            position["book_ok"] = False
            marks[token] = float(position.get("last_mark_price") or 0.0)

    for token, target in targets.items():
        if token not in marks:
            marks[token] = float(target.get("current_price") or 0.0)

    # 对方的真实持仓快照：页面「对手持仓明细」用它，也方便事后分析
    account["target_positions"] = _target_snapshot(targets)

    account["last_poll_at"] = now
    account["last_poll_ok"] = True
    account["target_equity_usd"] = round(
        sum(float(t.get("current_value") or 0.0) for t in targets.values()), 4
    )
    return marks


def run_cycle(session: requests.Session, state: Dict[str, Any]) -> Dict[str, Any]:
    """跑一轮：三个账户各自拉持仓并镜像。任何单账户失败不影响其他账户。"""
    cycle_marks: Dict[str, Dict[str, float]] = {}
    for trader in TRADERS:
        account = state["accounts"][trader["id"]]
        try:
            targets = fetch_positions(session, trader["address"], "OPEN")
        except Exception as exc:
            account["last_poll_ok"] = False
            account["last_error"] = f"{type(exc).__name__}: {exc}"
            append_event({"type": "POLL_ERROR", "trader": trader["id"], "error": account["last_error"]})
            continue
        last_ok = account.get("last_poll_at")
        lag_sec = 0
        if last_ok:
            try:
                lag_sec = int(time.time() - datetime.strptime(last_ok, "%Y-%m-%d %H:%M:%S UTC")
                              .replace(tzinfo=timezone.utc).timestamp())
            except Exception:
                lag_sec = 0
        try:
            marks = reconcile_account(session, account, targets, lag_sec)
        except Exception as exc:
            account["last_poll_ok"] = False
            account["last_error"] = f"{type(exc).__name__}: {exc}"
            append_event({"type": "ENGINE_ERROR", "trader": trader["id"], "error": account["last_error"]})
            continue
        account["last_error"] = ""
        cycle_marks[trader["id"]] = marks

    state["updated_at"] = iso_now()
    save_state(state)
    append_equity(state, cycle_marks)
    save_heartbeat({
        "pid": os.getpid(),
        "updated_at": utc_now(),
        "accounts": {
            tid: {
                "ok": acc.get("last_poll_ok"),
                "error": acc.get("last_error", ""),
                "positions": len(acc.get("positions") or {}),
                "cash_usd": round(float(acc.get("cash_usd") or 0.0), 4),
            }
            for tid, acc in state["accounts"].items()
        },
    })
    return cycle_marks


def main() -> int:
    """本地调试：跑一轮或常驻。"""
    import argparse

    parser = argparse.ArgumentParser(description="跟单纸盘引擎")
    parser.add_argument("--loop", action="store_true", help="常驻轮询")
    parser.add_argument("--interval", type=int, default=60, help="轮询间隔秒")
    args = parser.parse_args()

    session = build_session()
    state = load_state()
    while True:
        started = time.time()
        run_cycle(session, state)
        summary = {
            tid: account_summary(acc)["equity_usd"] for tid, acc in state["accounts"].items()
        }
        print(f"[{utc_now()}] 轮询完成 净值: " +
              " | ".join(f"{k} ${v:.2f}" for k, v in summary.items()))
        if not args.loop:
            return 0
        elapsed = time.time() - started
        time.sleep(max(1.0, args.interval - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
