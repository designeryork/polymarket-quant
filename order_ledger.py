"""
在途挂单账本 (order_ledger.py)
==============================
持久化记录所有已提交但尚未确认成交的订单，使订单生命周期可跨轮次追踪。

背景与风险
----------
旧实现中：
* LIVE 买单被受理后仅在当轮内存中记录 `live_order_id`，本轮即 `continue`，
  下一轮对账重建持仓时该字段被丢弃；
* `exit_order_id` 写入持仓后同样不被对账器保留；
* `tp_order_id` 从未被赋真实值（止盈单从未真正挂出）。

后果：未成交的 GTC 挂单（`expiration="0"` 永不过期）长期留在链上订单簿，
下一轮扫描时该 token 不在 `held_token_ids` 中 → 对同一市场重复下单；
已发出的卖单不去重、不撤销 → 同仓位挂出多张卖单 → 超额卖出。

设计
----
`state/pending_orders.json` 保存每笔在途订单的完整快照：

    {
      "version": 1,
      "updated_at_utc": "...",
      "orders": {
        "<order_id>": {
          "order_id": "...",
          "token_id": "...",
          "side": "BUY" | "SELL",
          "intent": "ENTRY" | "TAKE_PROFIT" | "EXIT",
          "price": 0.35,
          "shares": 5.0,
          "notional_usdc": 1.75,
          "event_id": "...",
          "market_question": "...",
          "outcome": "...",
          "status": "ACCEPTED" | "CONFIRMED" | "CANCELLED" | "FILLED" | "UNKNOWN",
          "created_at_utc": "...",
          "updated_at_utc": "...",
          "notes": "..."
        }
      }
    }

关键语义
--------
* `ACCEPTED` —— CLOB 已受理但成交状态未知（这是最危险的中间态）；
* `CONFIRMED` —— 已通过只读对账或 CLOB 查询确认成交；
* `CANCELLED` —— 已撤销；
* `FILLED`   —— 已被对账器确认为真实成交；
* `UNKNOWN`  —— 状态查询失败，保守视为"可能仍在链上"，继续阻止重复下单。

保守原则
--------
无法确认状态时，一律按"订单可能仍然存在"处理 —— 宁可少开一单，也不重复下单或超额卖出。
"""
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
LEDGER_FILE = BASE_DIR / "state" / "pending_orders.json"

LEDGER_VERSION = 1

STATUS_SUBMITTING = "SUBMITTING"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_CANCELLED = "CANCELLED"
STATUS_FILLED = "FILLED"
STATUS_UNKNOWN = "UNKNOWN"

# 这些状态代表"订单可能仍在链上"，必须阻止同一 token 的重复下单
BLOCKING_STATUSES = (STATUS_SUBMITTING, STATUS_ACCEPTED, STATUS_UNKNOWN)

INTENT_ENTRY = "ENTRY"
INTENT_TAKE_PROFIT = "TAKE_PROFIT"
INTENT_EXIT = "EXIT"


def make_order_intent(kind: str, token_id: str, cycle_id: str) -> Dict[str, Any]:
    intent_id = hashlib.sha256(f"{kind}|{token_id}|{cycle_id}".encode("utf-8")).hexdigest()
    return {"intent_id": f"intent-{intent_id}", "salt": int(intent_id[:13], 16)}


def attach_clob_order_id(intent_id: str, clob_order_id: str) -> Optional[Dict[str, Any]]:
    ledger = load_ledger()
    order = ledger["orders"].pop(str(intent_id), None)
    if order is None:
        return None
    order["order_id"] = str(clob_order_id)
    order["status"] = STATUS_ACCEPTED
    order["updated_at_utc"] = utc_now()
    ledger["orders"][str(clob_order_id)] = order
    save_ledger(ledger)
    return order


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_ledger() -> Dict[str, Any]:
    return {"version": LEDGER_VERSION, "updated_at_utc": utc_now(), "orders": {}}


def load_ledger() -> Dict[str, Any]:
    """读取账本；文件缺失或损坏时返回空账本（fail-open 于读取，但写入时保持完整结构）。"""
    if not LEDGER_FILE.exists():
        return _empty_ledger()
    try:
        data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_ledger()
    if not isinstance(data, dict):
        return _empty_ledger()
    orders = data.get("orders")
    if not isinstance(orders, dict):
        data["orders"] = {}
    data.setdefault("version", LEDGER_VERSION)
    return data


def save_ledger(ledger: Dict[str, Any]) -> None:
    """原子写入账本。"""
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ledger["version"] = LEDGER_VERSION
    ledger["updated_at_utc"] = utc_now()
    tmp = LEDGER_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LEDGER_FILE)


def record_order(
    order_id: str,
    token_id: str,
    side: str,
    intent: str,
    price: float,
    shares: float,
    notional_usdc: float,
    event_id: str = "",
    market_question: str = "",
    outcome: str = "",
    status: str = STATUS_ACCEPTED,
    notes: str = "",
    strategy: Optional[Dict[str, Any]] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """登记一笔在途订单并持久化。

    `reason` 专用于记录"这笔单是为什么下的"（如平仓触发规则）。
    它不会被 update_status / 对账状态机改写，因此面板能长期还原真实触发原因；
    `notes` 会被状态机覆盖，不适合承载原因。
    """
    ledger = load_ledger()
    existing = ledger["orders"].get(str(order_id)) or {}
    ledger["orders"][str(order_id)] = {
        "order_id": str(order_id),
        "token_id": str(token_id),
        "side": str(side).upper(),
        "intent": intent,
        "price": round(float(price), 6),
        "shares": round(float(shares), 6),
        "notional_usdc": round(float(notional_usdc), 6),
        "event_id": str(event_id or ""),
        "market_question": str(market_question or "")[:120],
        "outcome": str(outcome or "")[:40],
        "status": status,
        # created_at 必须保持首次登记时间：链上回填每轮都会重写这一行，
        # 若在这里重置时间，"平仓单挂了多久"就永远算成 0，超时重挂永远不触发。
        "created_at_utc": existing.get("created_at_utc") or utc_now(),
        "updated_at_utc": utc_now(),
        "notes": str(notes or "")[:200],
        "reason": str(reason or "")[:200],
        "strategy": dict(strategy) if isinstance(strategy, dict) else {},
    }
    save_ledger(ledger)
    return ledger["orders"][str(order_id)]


def update_status(order_id: str, status: str, notes: str = "") -> Optional[Dict[str, Any]]:
    """更新某笔订单的状态。"""
    ledger = load_ledger()
    order = ledger["orders"].get(str(order_id))
    if not order:
        return None
    order["status"] = status
    order["updated_at_utc"] = utc_now()
    if notes:
        order["notes"] = str(notes)[:200]
    save_ledger(ledger)
    return order


def remove_order(order_id: str) -> None:
    ledger = load_ledger()
    if ledger["orders"].pop(str(order_id), None) is not None:
        save_ledger(ledger)


def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    return load_ledger()["orders"].get(str(order_id))


def blocking_orders() -> List[Dict[str, Any]]:
    """返回所有"可能仍在链上"的订单。"""
    ledger = load_ledger()
    return [
        order
        for order in ledger["orders"].values()
        if order.get("status") in BLOCKING_STATUSES
    ]


def blocking_token_ids(side: Optional[str] = None) -> set:
    """返回被在途订单占用的 token_id 集合。

    side=None 表示不限方向；side="BUY" 表示仅考虑买单。
    """
    result = set()
    for order in blocking_orders():
        if side and str(order.get("side", "")).upper() != side.upper():
            continue
        token_id = str(order.get("token_id") or "")
        if token_id:
            result.add(token_id)
    return result


def blocking_event_ids() -> set:
    """返回被在途订单占用的 event_id 集合（用于同一事件禁止重复建仓）。"""
    result = set()
    for order in blocking_orders():
        event_id = str(order.get("event_id") or "")
        if event_id:
            result.add(event_id)
    return result


def open_sell_notional(token_id: str = "") -> float:
    """统计在途卖单的名义金额（用于阻止超额卖出）。"""
    total = 0.0
    for order in blocking_orders():
        if str(order.get("side", "")).upper() != "SELL":
            continue
        if token_id and str(order.get("token_id") or "") != token_id:
            continue
        total += float(order.get("notional_usdc") or 0.0)
    return round(total, 6)


def open_buy_notional() -> float:
    """统计在途买单的名义金额（计入总敞口）。"""
    total = 0.0
    for order in blocking_orders():
        if str(order.get("side", "")).upper() != "BUY":
            continue
        total += float(order.get("notional_usdc") or 0.0)
    return round(total, 6)


def has_open_buy_for_token(token_id: str) -> bool:
    """该 token 是否已有在途买单（防止对同一市场重复下单）。"""
    target = str(token_id)
    for order in blocking_orders():
        if str(order.get("side", "")).upper() != "BUY":
            continue
        if str(order.get("token_id") or "") == target:
            return True
    return False


def has_open_sell_for_token(token_id: str) -> bool:
    """该 token 是否已有在途卖单（防止超额卖出）。"""
    target = str(token_id)
    for order in blocking_orders():
        if str(order.get("side", "")).upper() != "SELL":
            continue
        if str(order.get("token_id") or "") == target:
            return True
    return False


def release_stale_sell_orders(token_id: str, reason: str = "") -> int:
    """把该 token 下仍处于阻塞状态的卖单标记为 CONFIRMED。

    使用场景：链上已查无此单（多半是刚成交，CLOB 立即从挂单列表移除），
    但持仓与账本还没对上。此时本地记录若继续阻塞，
    退出逻辑会一直判定"本地有卖单、链上未确认"而无法止损。

    链上是权威来源；这里只解除占位，不代表成交或撤单的最终判定
    （成交与否仍由持仓对账决定）。
    """
    ledger = load_ledger()
    released = 0
    for order in ledger.get("orders", {}).values():
        if str(order.get("side", "")).upper() != "SELL":
            continue
        if str(order.get("token_id") or "") != str(token_id):
            continue
        if order.get("status") not in BLOCKING_STATUSES:
            continue
        order["status"] = STATUS_CONFIRMED
        order["updated_at_utc"] = utc_now()
        order["resolved_at_utc"] = utc_now()
        order["notes"] = str(reason or "链上查无此单，本地记录判定为过期")[:200]
        released += 1
    if released:
        save_ledger(ledger)
    return released


def reconcile_with_open_orders(open_orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """用 CLOB 当前挂单列表对账本地账本。

    * CLOB 中仍存在 → 维持阻塞状态（可能部分成交，仍在链上）；
    * CLOB 中不存在 → 视为已离簿。若该单是买单，其成交与否交由持仓对账器判定；
      若是卖单，同样交由持仓对账器判定。这里只负责解除"占位"。

    返回统计信息，便于写入审计日志。
    """
    ledger = load_ledger()
    live_ids = set()
    for row in open_orders or []:
        for key in ("id", "orderID", "orderId"):
            value = row.get(key)
            if value:
                live_ids.add(str(value))
                break

    stats = {"total": len(ledger["orders"]), "still_live": 0, "released": 0}
    for order_id, order in list(ledger["orders"].items()):
        if order.get("status") not in BLOCKING_STATUSES:
            continue
        if order_id in live_ids:
            order["status"] = STATUS_ACCEPTED
            order["updated_at_utc"] = utc_now()
            order["last_seen_on_clob_at_utc"] = utc_now()
            stats["still_live"] += 1
        else:
            order["status"] = STATUS_CONFIRMED
            order["updated_at_utc"] = utc_now()
            order["resolved_at_utc"] = utc_now()
            order["notes"] = "已不在 CLOB 挂单列表，状态待持仓对账确认"
            stats["released"] += 1
    save_ledger(ledger)
    return stats


def clear_confirmed() -> int:
    """清理已确认/已撤销的历史订单，仅保留在途订单。返回清理条数。"""
    ledger = load_ledger()
    before = len(ledger["orders"])
    ledger["orders"] = {
        order_id: order
        for order_id, order in ledger["orders"].items()
        if order.get("status") in BLOCKING_STATUSES
    }
    removed = before - len(ledger["orders"])
    if removed:
        save_ledger(ledger)
    return removed


def summary() -> Dict[str, Any]:
    """返回账本摘要，供状态查看器与面板使用。"""
    ledger = load_ledger()
    orders = list(ledger["orders"].values())
    by_status: Dict[str, int] = {}
    for order in orders:
        by_status[order.get("status", "UNKNOWN")] = by_status.get(order.get("status", "UNKNOWN"), 0) + 1
    return {
        "total": len(orders),
        "by_status": by_status,
        "blocking": len(blocking_orders()),
        "open_buy_notional_usdc": open_buy_notional(),
        "open_sell_notional_usdc": open_sell_notional(),
        "updated_at_utc": ledger.get("updated_at_utc"),
    }


if __name__ == "__main__":
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
