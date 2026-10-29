"""
Polymarket 量化交易引擎：从公开成交到无人值守 (10U 专享版)
严格遵循文章《Polymarket 量化交易全过程：从公开成交到无人值守》全套规范：
1. 事实是代码算的，不是模型说的
2. Logit 空间特征工程: z = ln(p / (1-p)), ROC_logit, ATR_z, Aroon
3. 二元凯利 1/4 仓位公式: f* = (p - c) / (1 - c), 0.25 * f*
4. 预授权风控信封 (Risk Gate) + 盘口真实买一卖一深度撮合 (拒绝中间价幻觉)
5. 三重安全开关 + STOP 紧急停机文件 + 结构化审计日志与可视化报表生成
"""
import os
import json
import math
import time
import argparse
import atexit
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import requests
import numpy as np
from dotenv import load_dotenv

from trading_error_monitor import load_trading_errors, record_trading_error
import order_ledger
import single_instance

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

PORTFOLIO_FILE = STATE_DIR / "portfolio.json"
SIGNALS_LOG_FILE = LOGS_DIR / "signals_and_skips.jsonl"
DASHBOARD_HTML_FILE = BASE_DIR / "dashboard.html"
STOP_FILE = BASE_DIR / "STOP"

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
DATA_API_TRADES_URL = "https://data-api.polymarket.com/trades"


# ==========================================================
# 数学与 Logit 空间特征层 (Section 1 & Section 4)
# ==========================================================
def clip_prob(p: float, eps: float = 1e-4) -> float:
    return max(eps, min(1.0 - eps, float(p)))


def to_logit(p: float) -> float:
    p_c = clip_prob(p)
    return math.log(p_c / (1.0 - p_c))


def from_logit(z: float) -> float:
    z_c = max(-20.0, min(20.0, float(z)))
    return 1.0 / (1.0 + math.exp(-z_c))


def compute_features_from_series(prices: List[float]) -> Dict[str, float]:
    """
    在 Logit 空间计算动量 ROC_logit(6)、波动率 ATR_z(14)、EMA(p, 50)、Aroon(14)
    """
    n = len(prices)
    p_curr = clip_prob(prices[-1])
    z_series = [to_logit(p) for p in prices]
    z_curr = z_series[-1]

    # ROC_logit(6)
    lookback_roc = min(6, n - 1)
    roc_logit_6 = z_curr - z_series[-1 - lookback_roc] if lookback_roc > 0 else 0.0

    # ATR_z(14)
    diffs = [abs(z_series[i] - z_series[i - 1]) for i in range(1, n)]
    if diffs:
        window_atr = diffs[-14:]
        atr_z_14 = float(np.mean(window_atr))
    else:
        atr_z_14 = 0.05

    # EMA(p)
    alpha = 2.0 / (min(50, n) + 1.0)
    ema_p = prices[0]
    for p in prices[1:]:
        ema_p = alpha * p + (1.0 - alpha) * ema_p

    # Aroon Up / Down (14)
    w = z_series[-14:] if len(z_series) >= 2 else z_series
    idx_max = int(np.argmax(w))
    idx_min = int(np.argmin(w))
    periods_since_high = (len(w) - 1) - idx_max
    periods_since_low = (len(w) - 1) - idx_min
    span = max(1, len(w) - 1)
    aroon_up = ((span - periods_since_high) / span) * 100.0
    aroon_down = ((span - periods_since_low) / span) * 100.0

    return {
        "p_curr": round(p_curr, 4),
        "z_curr": round(z_curr, 4),
        "roc_logit_6": round(roc_logit_6, 4),
        "atr_z_14": round(max(atr_z_14, 0.01), 4),
        "ema_p": round(ema_p, 4),
        "aroon_up": round(aroon_up, 1),
        "aroon_down": round(aroon_down, 1),
        "sample_len": n,
    }


def binary_kelly_fraction(p_est: float, c_ask: float) -> Tuple[float, float, float]:
    """
    二元凯利公式 (Section 1):
    edge = p - c
    EV = p / c - 1
    f* = (p - c) / (1 - c)
    返回 (edge, ev, quarter_kelly)
    """
    if c_ask <= 0.01 or c_ask >= 0.99 or p_est <= c_ask:
        return round(p_est - c_ask, 4), round((p_est / max(c_ask, 1e-4)) - 1.0, 4), 0.0
    edge = p_est - c_ask
    ev = (p_est / c_ask) - 1.0
    f_star = edge / (1.0 - c_ask)
    quarter_kelly = max(0.0, 0.25 * f_star)
    return round(edge, 4), round(ev, 4), round(quarter_kelly, 4)


# ==========================================================
# 数据层 Gamma + CLOB Orderbook + History (Section 2)
# ==========================================================
class PolymarketDataLayer:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PolymarketQuantAgent/1.0"})

    def fetch_active_markets(self, limit: int = 30) -> List[Dict[str, Any]]:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            resp = self.session.get(GAMMA_MARKETS_URL, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[网络波动跳过] Gamma API 请求异常 (Fail-closed): {e}")
            return []

    def fetch_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self.session.get(
                f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=8
            )
            if resp.status_code != 200:
                return None
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                return None

            # Polymarket CLOB bids 按价格升序或降序排列，明确取最高买价与最低卖价
            parsed_bids = sorted(
                [(float(b["price"]), float(b["size"])) for b in bids],
                key=lambda x: x[0],
                reverse=True,
            )
            parsed_asks = sorted(
                [(float(a["price"]), float(a["size"])) for a in asks],
                key=lambda x: x[0],
            )
            best_bid, bid_size = parsed_bids[0]
            best_ask, ask_size = parsed_asks[0]

            # 前3档累计美元深度
            bid_depth_usdc = sum(p * s for p, s in parsed_bids[:3])
            ask_depth_usdc = sum(p * s for p, s in parsed_asks[:3])
            imbalance = (bid_depth_usdc - ask_depth_usdc) / max(
                1.0, bid_depth_usdc + ask_depth_usdc
            )

            return {
                "best_bid": round(best_bid, 4),
                "best_ask": round(best_ask, 4),
                "mid": round((best_bid + best_ask) / 2.0, 4),
                "spread": round(best_ask - best_bid, 4),
                "bid_depth_usdc": round(bid_depth_usdc, 2),
                "ask_depth_usdc": round(ask_depth_usdc, 2),
                "imbalance": round(imbalance, 4),
                "raw_asks": parsed_asks[:5],
                "raw_bids": parsed_bids[:5],
            }
        except Exception:
            return None

    def fetch_resolved_mark(
        self, token_id: str, event_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """CLOB 盘口消失时，用 Gamma 已结算 outcomePrices 做诚实标记（不假造买一价）。"""
        if not event_id:
            return None
        try:
            markets: List[Dict[str, Any]] = []
            try:
                resp = self.session.get(
                    f"https://gamma-api.polymarket.com/events/{event_id}", timeout=10
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict) and payload.get("markets"):
                        markets = payload["markets"]
            except Exception:
                pass
            if not markets:
                resp = self.session.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"id": event_id},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        markets = data[0].get("markets") or []
                    elif isinstance(data, dict):
                        markets = data.get("markets") or []
            tid = str(token_id)
            for m in markets:
                raw_tokens = m.get("clobTokenIds")
                try:
                    tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else (raw_tokens or [])
                except Exception:
                    tokens = []
                tokens = [str(t) for t in tokens]
                if tid not in tokens:
                    continue
                raw_prices = m.get("outcomePrices")
                try:
                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else (raw_prices or [])
                except Exception:
                    prices = []
                if not prices:
                    continue
                idx = tokens.index(tid)
                if idx >= len(prices):
                    continue
                price = float(prices[idx])
                closed = bool(m.get("closed")) or str(m.get("umaResolutionStatus") or "").lower() == "resolved"
                # 仅在已关闭/已结算，或价格已钉在 0/1 时采用
                if not (closed or price <= 0.001 or price >= 0.999):
                    continue
                price = round(price, 4)
                return {
                    "best_bid": price,
                    "best_ask": price,
                    "mid": price,
                    "spread": 0.0,
                    "bid_depth_usdc": 0.0,
                    "ask_depth_usdc": 0.0,
                    "imbalance": 0.0,
                    "raw_asks": [],
                    "raw_bids": [],
                    "mark_source": "RESOLVED",
                }
        except Exception:
            return None
        return None

    def fetch_price_history(
        self,
        token_id: str,
        fallback_mid: float,
        market_info: Dict[str, Any],
        invert_gamma_chg: bool = False,
    ) -> List[float]:
        """
        优先从 CLOB prices-history 获取真实历史价格序列；
        若接口限时或样本不足，结合 Gamma 提供的 oneDayPriceChange / oneHourPriceChange 重建轨迹。
        当选择 NO 侧 (side_idx==1) 且回退使用 Gamma 涨跌幅时，invert_gamma_chg=True 将涨跌幅取反，
        保证 YES/NO 概率互补对称。
        """
        try:
            resp = self.session.get(
                f"{CLOB_HOST}/prices-history",
                params={"market": token_id, "interval": "1d", "fidelity": 15},
                timeout=6,
            )
            if resp.status_code == 200:
                hist = resp.json().get("history", [])
                pts = [float(item["p"]) for item in hist if "p" in item and 0.001 < float(item["p"]) < 0.999]
                if len(pts) >= 10:
                    return pts[-60:]
        except Exception:
            pass

        # 使用 Gamma 的真实 24h / 1h 价格变化构建保守序列
        day_chg = float(market_info.get("oneDayPriceChange") or 0.0)
        hr_chg = float(market_info.get("oneHourPriceChange") or (day_chg * 0.25))
        if invert_gamma_chg:
            day_chg = -day_chg
            hr_chg = -hr_chg
        p_now = clip_prob(fallback_mid)
        p_24h_ago = clip_prob(p_now - day_chg)
        p_6h_ago = clip_prob(p_now - hr_chg * 2.5)

        series = np.linspace(p_24h_ago, p_6h_ago, 14).tolist() + np.linspace(p_6h_ago, p_now, 7).tolist()
        return [round(clip_prob(x), 4) for x in series]


# ==========================================================
# 策略与风控门层 (Section 5, Section 8, Section 9)
# ==========================================================
class RiskEnvelope:
    def __init__(self):
        self.max_capital = float(os.getenv("MAX_CAPITAL_USDC", "10.0"))
        self.max_single_bet = float(os.getenv("MAX_SINGLE_BET_USDC", "2.0"))
        self.min_single_bet = float(os.getenv("MIN_SINGLE_BET_USDC", "1.0"))
        self.max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS", "4"))
        self.daily_loss_limit = float(os.getenv("DAILY_LOSS_LIMIT_USDC", "2.0"))
        self.paper_mode = os.getenv("PAPER", "true").lower() == "true"
        self.allow_live = os.getenv("ALLOW_LIVE", "false").lower() == "true"


def load_portfolio(risk: RiskEnvelope) -> Dict[str, Any]:
    if PORTFOLIO_FILE.exists():
        try:
            return json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    init_state = {
        "mode": "PAPER (模拟实盘盘口撮合)" if risk.paper_mode else "LIVE (实盘)",
        "wallet_address": os.getenv("WALLET_ADDRESS", ""),
        "initial_capital_usdc": risk.max_capital,
        "cash_usdc": risk.max_capital,
        "realized_pnl_usdc": 0.0,
        "unrealized_pnl_usdc": 0.0,
        "total_equity_usdc": risk.max_capital,
        "total_return_pct": 0.0,
        "open_positions": [],
        "closed_trades": [],
        "recent_decisions": [],
        "equity_curve": [
            {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "equity": risk.max_capital,
            }
        ],
        "stats": {
            "scans_count": 0,
            "markets_evaluated": 0,
            "signals_triggered": 0,
            "skipped_count": 0,
            "skip_reasons": {},
        },
        "daily_stats": {
            "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "start_equity": risk.max_capital,
        },
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    PORTFOLIO_FILE.write_text(json.dumps(init_state, indent=2, ensure_ascii=False), encoding="utf-8")
    return init_state


def save_portfolio(state: Dict[str, Any]) -> None:
    state["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    PORTFOLIO_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")



def ensure_daily_stats(state: Dict[str, Any]) -> None:
    """UTC 自然日亏损熔断统计：跨日自动重置 start_equity。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ds = state.get("daily_stats") or {}
    if ds.get("date_utc") != today:
        state["daily_stats"] = {
            "date_utc": today,
            "start_equity": float(state.get("total_equity_usdc", state.get("initial_capital_usdc", 10.0))),
        }


def get_live_executor():
    """懒加载实盘执行器，避免 PAPER 模式强依赖链上模块。"""
    from clob_executor import PolymarketV2Executor
    return PolymarketV2Executor()


# 单实例锁只在本进程内获取一次，循环期间持续持有
_INSTANCE_LOCK_HELD = False


def ensure_single_instance() -> bool:
    """确保本进程独占交易权。

    旧实现仅在启动器里用命令行匹配判断，既存在 TOCTOU 竞态，也漏掉
    `pythonw.exe` / PyInstaller 打包体等入口，导致本机与云端可能同时实盘下单。
    这里改用原子创建的 PID 锁文件，内核级排他。
    """
    global _INSTANCE_LOCK_HELD
    if _INSTANCE_LOCK_HELD:
        return True
    acquired, message = single_instance.acquire_lock(role="quant_engine")
    print(f"[单实例锁] {message}")
    if not acquired:
        record_trading_error("ENGINE", message, context="检测到重复启动，已拒绝执行任何交易动作")
        return False
    _INSTANCE_LOCK_HELD = True
    atexit.register(single_instance.release_lock)
    return True


def compute_total_exposure(state: Dict[str, Any]) -> float:
    """计算当前总资金敞口。

    = 已持仓成本 + 在途买单名义金额。
    在途挂单必须计入，否则已受理但未成交的买单会让敞口被低估，
    叠加"重复下单"后可能突破 MAX_CAPITAL_USDC 上限。
    """
    position_exposure = sum(
        float(pos.get("cost_usdc") or 0.0) for pos in state.get("open_positions", [])
    )
    return round(position_exposure + order_ledger.open_buy_notional(), 6)


def append_audit_log(record: Dict[str, Any]) -> None:
    record["ts_utc"] = datetime.now(timezone.utc).isoformat()
    with open(SIGNALS_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_hours_to_expiry(market: Dict[str, Any]) -> float:
    end_str = market.get("endDate") or market.get("endDateIso")
    if not end_str:
        return 999.0
    try:
        if end_str.endswith("Z"):
            dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(end_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        diff_sec = (dt - datetime.now(timezone.utc)).total_seconds()
        return round(diff_sec / 3600.0, 2)
    except Exception:
        return 999.0


def evaluate_existing_positions(
    state: Dict[str, Any], data_layer: PolymarketDataLayer
) -> List[Dict[str, Any]]:
    """
    遍历当前模拟/实盘持仓，拉取最新真实买一价 (Best Bid) 计算浮动盈亏与止盈/止损/时间退出信号。
    注意：卖出平仓必须按真实买一价 (Best Bid) 估值，严禁使用中间价自欺欺人！
    """
    remaining_positions = []
    unrealized_sum = 0.0
    events = []

    for pos in state["open_positions"]:
        token_id = pos["token_id"]
        book = data_layer.fetch_orderbook(token_id)
        mark_source = "CLOB"
        if not book:
            # CLOB 无盘口：尝试 Gamma 已结算价；仍失败则 fail-closed 保留上次成功估值
            book = data_layer.fetch_resolved_mark(token_id, pos.get("event_id"))
            if book:
                mark_source = book.get("mark_source") or "RESOLVED"
            else:
                pos["book_status"] = "UNAVAILABLE"
                pos["book_checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                # book_updated_at 保持为「上次成功刷新」时间，不伪造为现在
                unrealized_sum += pos.get("unrealized_pnl_usdc", 0.0)
                remaining_positions.append(pos)
                continue

        best_bid = book["best_bid"]
        best_ask = book["best_ask"]
        shares = pos["shares"]
        entry_price = pos["entry_price"]
        cost_usdc = pos["cost_usdc"]

        # 按真实可成交的买一价计算当前市值（结算价同源：无盘口时用 outcomePrices）
        current_value = round(shares * best_bid, 4)
        pnl_usdc = round(current_value - cost_usdc, 4)
        pnl_pct = round((pnl_usdc / max(0.01, cost_usdc)) * 100.0, 2)

        z_entry = to_logit(entry_price)
        z_now = to_logit(best_bid)
        z_peak = max(pos.get("z_peak", z_entry), z_now)
        pos["z_peak"] = round(z_peak, 4)
        pos["current_bid"] = best_bid
        pos["current_ask"] = best_ask
        pos["current_value_usdc"] = current_value
        pos["unrealized_pnl_usdc"] = pnl_usdc
        pos["unrealized_pnl_pct"] = pnl_pct
        # 每次成功拿到 bid/ask（含已结算标记）都刷新盘口更新时间
        pos["book_updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        pos["book_status"] = mark_source
        pos.pop("book_checked_at", None)

        # 已结算市场：只做诚实盯市，不触发自动下单平仓（避免无盘口时误挂卖单）
        if mark_source == "RESOLVED":
            unrealized_sum += pnl_usdc
            remaining_positions.append(pos)
            continue

        # 检查退出表 (Section 5: 目标止盈 / Logit 追踪止损 / 盘口恶化退出)
        exit_reason = None
        if best_bid >= pos.get("target_prob", 0.90):
            exit_reason = f"达到目标概率止盈 (Bid {best_bid} >= {pos.get('target_prob')})"
        elif (z_now - z_entry) <= -0.38:
            exit_reason = f"Logit 空间止损触发 (Δz={z_now - z_entry:.2f} <= -0.38)"
        elif (z_peak - z_now) >= 0.30 and pnl_usdc > 0:
            exit_reason = f"Logit 追踪止盈回撤触发 (Peak z={z_peak:.2f} -> {z_now:.2f})"
        elif book["spread"] > 0.08:
            exit_reason = f"盘口流动性恶化退出 (Spread={book['spread']:.3f} > 0.08)"

        paper_mode = os.getenv("PAPER", "true").lower() == "true"
        allow_live = os.getenv("ALLOW_LIVE", "false").lower() == "true"

        if not paper_mode:
            # ===== LIVE 分支：所有下单动作都必须先查在途挂单账本 =====
            if allow_live and not STOP_FILE.exists():
                try:
                    ex = get_live_executor()
                    # 该 token 是否已有在途卖单（防止同一仓位挂出多张卖单导致超额卖出）
                    has_open_sell = order_ledger.has_open_sell_for_token(token_id)

                    if exit_reason:
                        if has_open_sell:
                            # 已有在途卖单，本轮不再重复挂单，等待对账确认
                            append_audit_log({
                                "action": "LIVE_EXIT_SKIPPED_DUPLICATE",
                                "token_id": token_id,
                                "reason": exit_reason,
                            })
                        else:
                            # 撤掉此前的止盈挂单（若有），再按当前买一价挂平仓单
                            tp_oid = pos.get("tp_order_id")
                            if tp_oid:
                                try:
                                    ex.cancel_order(tp_oid)
                                    order_ledger.update_status(
                                        tp_oid, order_ledger.STATUS_CANCELLED, notes="平仓前撤销止盈单"
                                    )
                                except Exception as ce:
                                    append_audit_log({"action": "CANCEL_TP_FAILED", "tp_order_id": tp_oid, "error": str(ce)[:200]})
                                    record_trading_error(
                                        "CANCEL_TP", ce, pos.get("market_question", ""), pos.get("outcome", ""), context=f"tp_order_id={tp_oid}"
                                    )
                            sell_payload = ex.build_and_sign_poly1271_order(
                                token_id, "SELL", price=best_bid, size_shares=shares, tick_size=0.01, order_type="GTC"
                            )
                            sell_resp = ex.post_order(sell_payload)
                            exit_order_id = sell_resp.get("orderID") or sell_resp.get("orderId")
                            if not (sell_resp.get("success") in (True, "true", "True") and exit_order_id):
                                raise RuntimeError(f"实盘卖单未被受理: {str(sell_resp)[:300]}")
                            pos["exit_order_id"] = exit_order_id
                            order_ledger.record_order(
                                order_id=exit_order_id,
                                token_id=token_id,
                                side="SELL",
                                intent=order_ledger.INTENT_EXIT,
                                price=best_bid,
                                shares=shares,
                                notional_usdc=round(shares * best_bid, 6),
                                event_id=pos.get("event_id", ""),
                                market_question=pos.get("market_question", ""),
                                outcome=pos.get("outcome", ""),
                                status=order_ledger.STATUS_ACCEPTED,
                                notes=exit_reason[:200],
                            )
                            append_audit_log({"action": "LIVE_EXIT_ORDER_ACCEPTED", "orderID": exit_order_id, "reason": exit_reason})
                    else:
                        # 未触发退出条件：为该仓位挂出止盈限价卖单（此前从未真正实现）
                        tp_oid = pos.get("tp_order_id")
                        if not tp_oid and not has_open_sell:
                            tp_price = float(pos.get("target_prob") or 0.92)
                            if best_bid < tp_price:
                                tp_payload = ex.build_and_sign_poly1271_order(
                                    token_id, "SELL", price=tp_price, size_shares=shares, tick_size=0.01, order_type="GTC"
                                )
                                tp_resp = ex.post_order(tp_payload)
                                tp_order_id = tp_resp.get("orderID") or tp_resp.get("orderId")
                                if tp_resp.get("success") in (True, "true", "True") and tp_order_id:
                                    pos["tp_order_id"] = tp_order_id
                                    order_ledger.record_order(
                                        order_id=tp_order_id,
                                        token_id=token_id,
                                        side="SELL",
                                        intent=order_ledger.INTENT_TAKE_PROFIT,
                                        price=tp_price,
                                        shares=shares,
                                        notional_usdc=round(shares * tp_price, 6),
                                        event_id=pos.get("event_id", ""),
                                        market_question=pos.get("market_question", ""),
                                        outcome=pos.get("outcome", ""),
                                        status=order_ledger.STATUS_ACCEPTED,
                                        notes="止盈限价卖单已挂出",
                                    )
                                    append_audit_log({
                                        "action": "LIVE_TP_ORDER_ACCEPTED",
                                        "orderID": tp_order_id,
                                        "price": tp_price,
                                        "token_id": token_id,
                                    })
                                else:
                                    record_trading_error(
                                        "OPEN", f"止盈卖单未被受理: {str(tp_resp)[:200]}",
                                        pos.get("market_question", ""), pos.get("outcome", ""),
                                        context="LIVE_TP_ORDER_FAILED",
                                    )
                except Exception as le:
                    append_audit_log({"action": "LIVE_EXIT_FAILED", "error": str(le)[:300], "reason": exit_reason})
                    record_trading_error(
                        "CLOSE", le, pos.get("market_question", ""), pos.get("outcome", ""), context=exit_reason or "LIVE_TP"
                    )
            unrealized_sum += pnl_usdc
            remaining_positions.append(pos)
            continue

            state["cash_usdc"] = round(state["cash_usdc"] + current_value, 4)
            state["realized_pnl_usdc"] = round(state["realized_pnl_usdc"] + pnl_usdc, 4)
            closed_item = {
                **pos,
                "exit_price_bid": best_bid,
                "realized_pnl_usdc": pnl_usdc,
                "realized_pnl_pct": pnl_pct,
                "exit_reason": exit_reason,
                "closed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
            state["closed_trades"].insert(0, closed_item)
            events.append({"type": "EXIT", "position": closed_item})
            append_audit_log({"action": "EXIT_POSITION", **closed_item})
        else:
            unrealized_sum += pnl_usdc
            remaining_positions.append(pos)

    state["open_positions"] = remaining_positions
    state["unrealized_pnl_usdc"] = round(unrealized_sum, 4)
    pos_market_value = sum(p["current_value_usdc"] for p in remaining_positions)
    state["total_equity_usdc"] = round(state["cash_usdc"] + pos_market_value, 4)
    state["total_return_pct"] = round(
        ((state["total_equity_usdc"] - state["initial_capital_usdc"]) / state["initial_capital_usdc"]) * 100.0,
        2,
    )
    return events


def run_quant_cycle(max_markets: int = 25) -> Dict[str, Any]:
    """
    执行一轮完整的 Polymarket 扫描 -> 特征计算 -> 假说检验 -> 风险门过滤 -> 模拟/实盘撮合
    """
    if STOP_FILE.exists():
        print("[紧急停机] 检测到 STOP 文件，已拒绝执行任何扫描与下单！")
        return {}

    risk = RiskEnvelope()
    if not risk.paper_mode:
        if not risk.allow_live:
            print("[对账保护] PAPER=false 但 ALLOW_LIVE=false，拒绝使用本地账本继续实盘扫描。")
            return {}
        # 单实例保护：同一钱包绝不允许两个 LIVE 进程并发下单
        if not ensure_single_instance():
            print("[单实例保护] 已检测到其它实盘实例在运行，本轮不执行任何扫描与下单。")
            return {}
        try:
            from portfolio_reconciliation import reconcile
            state = reconcile(apply=True, backup=False)
        except Exception as reconciliation_error:
            record_trading_error("RECONCILIATION", reconciliation_error, context="本轮已跳过，不修改账本或提交订单")
            print(f"[对账失败] 本轮已跳过，不修改账本或提交订单: {reconciliation_error}")
            try:
                refresh_dashboard_from_disk()
            except Exception as dashboard_error:
                print(f"[告警页面刷新失败] {dashboard_error}")
            return {}
    else:
        state = load_portfolio(risk)
    data_layer = PolymarketDataLayer()

    state["stats"]["scans_count"] += 1

    # 1. 先更新已有持仓的真实买一价估值与平仓检查（熔断期间仍允许平仓）
    evaluate_existing_positions(state, data_layer)

    # 2. UTC 自然日亏损熔断（相对当日 start_equity）
    ensure_daily_stats(state)
    start_eq = float(state["daily_stats"]["start_equity"])
    day_pnl = float(state["total_equity_usdc"]) - start_eq
    circuit_breaker = day_pnl <= -risk.daily_loss_limit

    # 3. 抓取活跃市场
    markets = data_layer.fetch_active_markets(limit=max_markets)
    # 在途挂单占用的 token / event 同样要视为"已持有"，防止对同一市场重复下单
    pending_token_ids = order_ledger.blocking_token_ids()
    pending_event_ids = order_ledger.blocking_event_ids()
    held_event_ids = {p["event_id"] for p in state["open_positions"]} | pending_event_ids
    held_token_ids = {p["token_id"] for p in state["open_positions"]} | pending_token_ids

    cycle_decisions = []

    def record_skip(question: str, reason_code: str, detail: str, metrics: Optional[Dict] = None):
        state["stats"]["skipped_count"] += 1
        state["stats"]["skip_reasons"][reason_code] = (
            state["stats"]["skip_reasons"].get(reason_code, 0) + 1
        )
        item = {
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            "question": question[:68],
            "decision": "SKIP",
            "reason_code": reason_code,
            "detail": detail,
            "metrics": metrics or {},
        }
        cycle_decisions.append(item)
        append_audit_log(item)

    for m in markets:
        question = m.get("question", "Unknown Market")
        state["stats"]["markets_evaluated"] += 1

        # 解析 clobTokenIds 與 outcomes
        raw_tokens = m.get("clobTokenIds")
        raw_outcomes = m.get("outcomes")
        try:
            token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else (raw_tokens or [])
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else (raw_outcomes or ["Yes", "No"])
        except Exception:
            record_skip(question, "MISSING_TOKEN_IDS", "无法解析 clobTokenIds")
            continue

        if len(token_ids) < 2:
            record_skip(question, "NON_BINARY", "非标准二元市场")
            continue

        # 规则清晰度检查 (铁律2：结算规则过短或含糊则跳过)
        desc = (m.get("description") or "").strip()
        if len(desc) < 40:
            record_skip(question, "AMBIGUOUS_RULES", "结算规则描述过短或缺失 (<40字符)")
            continue

        # 剩余时间检查 (> 2 hours)
        hours_left = parse_hours_to_expiry(m)
        if hours_left < 2.0:
            record_skip(question, "NEAR_SETTLEMENT", f"距离结算不足2小时 ({hours_left}h)")
            continue

        # 事件相关性去重 (同一 Event 严禁重复下注)
        events_list = m.get("events") or []
        event_id = str(events_list[0].get("id")) if events_list else str(m.get("conditionId") or m.get("id"))
        if event_id in held_event_ids:
            record_skip(question, "CORRELATED_EVENT", f"已持有同事件仓位 (event_id={event_id})")
            continue

        # 检查 YES 与 NO 两个方向，择优分析价格落在 [0.15, 0.85] 的一侧
        picked_side = None
        for idx in (0, 1):
            tid = str(token_ids[idx])
            if tid in held_token_ids:
                continue
            book = data_layer.fetch_orderbook(tid)
            if not book:
                continue
            if 0.15 <= book["best_ask"] <= 0.85:
                picked_side = (idx, tid, outcomes[idx] if idx < len(outcomes) else f"Outcome_{idx}", book)
                break

        if not picked_side:
            record_skip(question, "OUT_OF_PROB_RANGE", "YES/NO 卖一价均不在 [0.15, 0.85] 核心可交易区间")
            continue

        side_idx, token_id, outcome_label, book = picked_side
        best_ask = book["best_ask"]
        best_bid = book["best_bid"]
        spread = book["spread"]
        ask_depth = book["ask_depth_usdc"]

        # 盘口价差与深度过滤 (Section 5 入场表)
        if spread > 0.04:
            record_skip(
                question,
                "WIDE_SPREAD",
                f"[{outcome_label}] 盘口价差过大 Spread={spread:.3f} > 0.04 (Bid={best_bid}, Ask={best_ask})",
                {"ask": best_ask, "bid": best_bid, "spread": spread},
            )
            continue

        if ask_depth < 15.0:
            record_skip(
                question,
                "LOW_DEPTH",
                f"[{outcome_label}] 卖盘深度不足 (${ask_depth:.1f} < $15)",
                {"ask": best_ask, "ask_depth": ask_depth},
            )
            continue

        # 获取历史序列并计算 Logit 空间特征
        # NO 侧 (side_idx==1) 回退 Gamma oneDayPriceChange 时取反，保证 YES/NO 对称
        prices = data_layer.fetch_price_history(
            token_id, book["mid"], m, invert_gamma_chg=(side_idx == 1)
        )
        feats = compute_features_from_series(prices)

        # 定量概率模型估算 p_est (事实由代码在 Logit 空间根据动量、盘口失衡与波动率推导)
        # 当动量方向与盘口买盘失衡同向，且波动突破噪声阈值时，给出保守的 Logit 漂移估计
        roc_z = feats["roc_logit_6"]
        atr_z = feats["atr_z_14"]
        imb = book["imbalance"]

        # 动量信号与盘口失衡合成预期 Logit 增益
        signal_strength = (0.55 * roc_z) + (0.35 * imb * atr_z * 2.5)
        # 严格限制单次最大主观外推不超过 0.45 logit，防止过度自信
        delta_z_est = max(-0.45, min(0.45, signal_strength))
        z_ask = to_logit(best_ask)
        p_est = round(from_logit(z_ask + max(0.08, delta_z_est)), 4) if signal_strength > 0 else round(from_logit(z_ask + delta_z_est), 4)

        edge, ev, quarter_kelly = binary_kelly_fraction(p_est, best_ask)

        metrics_summary = {
            "side": outcome_label,
            "ask": best_ask,
            "bid": best_bid,
            "spread": spread,
            "depth_usdc": ask_depth,
            "z_curr": feats["z_curr"],
            "roc_logit_6": roc_z,
            "atr_z_14": atr_z,
            "imbalance": imb,
            "p_est": p_est,
            "edge": edge,
            "quarter_kelly": quarter_kelly,
        }

        # 入场规则校验 (Section 5: ROC_logit > 0 或显著盘口正失衡，且 edge >= 0.025)
        momentum_ok = (roc_z > 0.02 and roc_z >= 0.5 * atr_z) or (imb >= 0.18 and best_ask >= feats["ema_p"])
        if not momentum_ok or edge < 0.025:
            record_skip(
                question,
                "NO_EDGE_OR_MOMENTUM",
                f"[{outcome_label}] 动量或优势不足 (ROC_z={roc_z:+.3f}, Imb={imb:+.2f}, Edge={edge:+.3f})",
                metrics_summary,
            )
            continue

        # 熔断与持仓上限门禁 (Risk Gate)
        if circuit_breaker:
            record_skip(question, "DAILY_LOSS_BREAKER", "日亏损达到熔断上限，拒绝新开仓", metrics_summary)
            continue

        if len(state["open_positions"]) >= risk.max_open_positions:
            record_skip(
                question,
                "MAX_POSITIONS_REACHED",
                f"已达最大持仓数上限 ({risk.max_open_positions}个)，仅监控不新开",
                metrics_summary,
            )
            continue

        # 总资金敞口门禁：MAX_CAPITAL_USDC 必须真正生效，而不只是初始化时用一次。
        # 敞口 = 已持仓成本 + 在途买单名义金额（否则已受理未成交的买单会被低估）。
        total_exposure = compute_total_exposure(state)
        if total_exposure >= risk.max_capital:
            record_skip(
                question,
                "MAX_CAPITAL_REACHED",
                f"总资金敞口 ${total_exposure:.2f} 已达上限 ${risk.max_capital:.2f}，拒绝新开仓",
                metrics_summary,
            )
            continue

        # 计算仓位 (Section 5 & 8: min(0.25*f* * 权益, 单笔上限 $2.0, 盘口深度*0.2, 可用现金))
        kelly_usdc = quarter_kelly * state["total_equity_usdc"]
        # 针对 10U 账户，若触发高质量信号且 Kelly 算出来在 $0.5~$1.0 之间，允许按最低门槛 $1.00 建仓
        raw_target_usdc = max(risk.min_single_bet, kelly_usdc) if edge >= 0.03 else kelly_usdc
        remaining_capacity = round(risk.max_capital - total_exposure, 4)
        size_usdc = round(
            min(
                raw_target_usdc,
                risk.max_single_bet,
                ask_depth * 0.20,
                state["cash_usdc"],
                remaining_capacity,
            ),
            2,
        )

        if size_usdc < risk.min_single_bet:
            record_skip(
                question,
                "SIZE_BELOW_MIN",
                f"可用资金或凯利仓位不足最小下单额 (${size_usdc:.2f} < ${risk.min_single_bet:.2f})",
                metrics_summary,
            )
            continue

        # 执行撮合 (按真实卖一价 Best Ask 买入，绝不用 Mid price!)
        shares = round(size_usdc / best_ask, 4)
        # Polymarket 多数市场 orderMinSize >= 5 shares
        MIN_SHARES = 5.0
        if shares < MIN_SHARES:
            padded_cost = round(MIN_SHARES * best_ask, 2)
            if padded_cost <= risk.max_single_bet and padded_cost <= state["cash_usdc"]:
                shares = MIN_SHARES
                size_usdc = padded_cost
            else:
                record_skip(
                    question,
                    "SIZE_BELOW_MIN_SHARES",
                    f"份额 {shares:.2f} < 5 且补足至5份名义 ${padded_cost:.2f} 超过单笔上限/现金",
                    metrics_summary,
                )
                continue

        target_prob = round(min(0.92, from_logit(z_ask + 0.45)), 4)
        live_order_id = None
        tp_order_id = None

        # LIVE 只记录订单受理；真实成交、现金和持仓由下一轮只读对账确认。
        if (not risk.paper_mode) and risk.allow_live and not STOP_FILE.exists():
            try:
                ex = get_live_executor()
                buy_payload = ex.build_and_sign_poly1271_order(
                    token_id, "BUY", price=best_ask, size_shares=shares, tick_size=0.01, order_type="GTC"
                )
                buy_resp = ex.post_order(buy_payload)
                live_order_id = buy_resp.get("orderID") or buy_resp.get("orderId")
                if not (buy_resp.get("success") in (True, "true", "True") and live_order_id):
                    record_skip(
                        question,
                        "LIVE_BUY_FAILED",
                        f"实盘买单失败: {str(buy_resp)[:180]}",
                        metrics_summary,
                    )
                    record_trading_error("OPEN", f"实盘买单失败: {buy_resp}", question, outcome_label)
                else:
                    # 订单已受理但未确认成交 —— 必须落盘到在途挂单账本。
                    # 否则下一轮扫描时该 token 不在持仓中，会对同一市场重复下单；
                    # 同时该笔名义金额也必须计入总敞口。
                    order_ledger.record_order(
                        order_id=live_order_id,
                        token_id=token_id,
                        side="BUY",
                        intent=order_ledger.INTENT_ENTRY,
                        price=best_ask,
                        shares=shares,
                        notional_usdc=size_usdc,
                        event_id=event_id,
                        market_question=question,
                        outcome=outcome_label,
                        status=order_ledger.STATUS_ACCEPTED,
                        notes="买单已受理，等待只读对账确认成交",
                    )
                    decision_entry = {
                        "time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                        "question": question[:68],
                        "decision": "LIVE_ORDER_ACCEPTED",
                        "reason_code": "ORDER_ACCEPTED_NOT_FILLED",
                        "detail": f"实盘买单已受理，等待成交对账 | live={live_order_id}",
                        "metrics": metrics_summary,
                    }
                    cycle_decisions.append(decision_entry)
                    append_audit_log(decision_entry)
                    state["stats"]["signals_triggered"] += 1
                continue
            except Exception as live_err:
                record_skip(
                    question,
                    "LIVE_BUY_EXCEPTION",
                    f"实盘下单异常: {str(live_err)[:180]}",
                    metrics_summary,
                )
                record_trading_error("OPEN", live_err, question, outcome_label)
                continue

        immediate_bid_val = round(shares * best_bid, 4)
        immediate_spread_cost = round(immediate_bid_val - size_usdc, 4)

        new_pos = {
            "pos_id": f"POS-{int(time.time()*1000) % 1000000}",
            "event_id": event_id,
            "market_question": question,
            "outcome": outcome_label,
            "token_id": token_id,
            "entry_price": best_ask,
            "current_bid": best_bid,
            "current_ask": best_ask,
            "shares": shares,
            "cost_usdc": size_usdc,
            "current_value_usdc": immediate_bid_val,
            "unrealized_pnl_usdc": immediate_spread_cost,
            "unrealized_pnl_pct": round((immediate_spread_cost / size_usdc) * 100.0, 2),
            "p_est": p_est,
            "edge_at_entry": edge,
            "quarter_kelly": quarter_kelly,
            "target_prob": target_prob,
            "z_entry": round(z_ask, 4),
            "z_peak": round(to_logit(best_bid), 4),
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "book_updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "book_status": "CLOB",
            "live_order_id": live_order_id,
            "tp_order_id": tp_order_id,
        }

        state["cash_usdc"] = round(state["cash_usdc"] - size_usdc, 4)
        state["open_positions"].append(new_pos)
        held_event_ids.add(event_id)
        held_token_ids.add(token_id)
        state["stats"]["signals_triggered"] += 1

        decision_entry = {
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            "question": question[:68],
            "decision": "PAPER_BUY" if risk.paper_mode else "LIVE_BUY",
            "reason_code": "SIGNAL_PASSED_ALL_GATES",
            "detail": f"买入 [{outcome_label}] @ Ask {best_ask:.3f} | 金额 ${size_usdc:.2f} ({shares:.2f} 份) | p_est={p_est:.3f} (Edge +{edge:.3f})"
                      + (f" | live={live_order_id} tp={tp_order_id}" if live_order_id else ""),
            "metrics": metrics_summary,
        }
        cycle_decisions.append(decision_entry)
        append_audit_log(decision_entry)

    # PAPER 可立即模拟成交；LIVE 不在同一轮重复退出或以未确认订单改账。
    if risk.paper_mode:
        evaluate_existing_positions(state, data_layer)

    state["recent_decisions"] = (cycle_decisions + state.get("recent_decisions", []))[:40]
    state["equity_curve"].append(
        {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "equity": state["total_equity_usdc"],
        }
    )
    state["equity_curve"] = state["equity_curve"][-100:]
    save_portfolio(state)
    generate_dashboard_html(state)
    return state


# ==========================================================
# 可视化 HTML 监控面板生成器 (双击即可在浏览器查看盈亏)
# ==========================================================
def generate_dashboard_html(state: Dict[str, Any]) -> None:
    from html import escape

    open_rows = ""
    for p in state.get("open_positions", []):
        pnl = p["unrealized_pnl_usdc"]
        pnl_cls = "pos" if pnl >= 0 else "neg"
        open_rows += f"""
        <tr>
            <td><span class="badge badge-side">{p['outcome']}</span></td>
            <td class="q-cell" title="{p['market_question']}">{p['market_question']}</td>
            <td>{p['entry_price']:.3f}</td>
            <td>{p['current_bid']:.3f} <span class="sub">(Ask {p['current_ask']:.3f})</span></td>
            <td>{p['p_est']:.3f} <span class="sub">(+{p['edge_at_entry']:.3f})</span></td>
            <td>${p['cost_usdc']:.2f} <span class="sub">({p['shares']:.1f}份)</span></td>
            <td>${p['current_value_usdc']:.2f}</td>
            <td class="{pnl_cls}">${pnl:+.2f} ({p['unrealized_pnl_pct']:+.1f}%)</td>
            <td class="sub">{p['opened_at']}</td>
            <td class="sub">{p.get('book_updated_at') or p.get('last_updated') or state.get('last_updated') or ''}{(' · ' + p['book_status']) if p.get('book_status') and p.get('book_status') != 'CLOB' else ''}{(' · 检查于 ' + p['book_checked_at']) if p.get('book_status') == 'UNAVAILABLE' and p.get('book_checked_at') else ''}</td>
        </tr>
        """
    if not open_rows:
        open_rows = '<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:20px;">当前无持仓（风控门正在扫描符合 Logit 动量与优势阈值的盘口）</td></tr>'

    closed_rows = ""
    for c in state.get("closed_trades", [])[:15]:
        pnl = c["realized_pnl_usdc"]
        pnl_cls = "pos" if pnl >= 0 else "neg"
        closed_rows += f"""
        <tr>
            <td><span class="badge">{c['outcome']}</span></td>
            <td class="q-cell">{c['market_question']}</td>
            <td>{c['entry_price']:.3f}</td>
            <td>{c['exit_price_bid']:.3f}</td>
            <td>${c['cost_usdc']:.2f}</td>
            <td class="{pnl_cls}">${pnl:+.2f} ({c['realized_pnl_pct']:+.1f}%)</td>
            <td>{c['exit_reason']}</td>
            <td class="sub">{c['closed_at']}</td>
        </tr>
        """
    if not closed_rows:
        closed_rows = '<tr><td colspan="8" style="text-align:center;color:#94a3b8;padding:16px;">暂无已平仓历史（持仓达到止盈目标或触发 Logit 止损后自动记录在此）</td></tr>'

    decision_rows = ""
    for d in state.get("recent_decisions", [])[:25]:
        is_buy = "BUY" in d["decision"]
        b_cls = "badge-buy" if is_buy else "badge-skip"
        m = d.get("metrics", {})
        m_str = ""
        if m:
            m_str = f"Ask:{m.get('ask','-')} | Spread:{m.get('spread','-')} | ROC_z:{m.get('roc_logit_6','-')} | Edge:{m.get('edge','-')}"
        decision_rows += f"""
        <tr>
            <td class="sub">{d['time']}</td>
            <td><span class="badge {b_cls}">{d['decision']}</span></td>
            <td><code>{d['reason_code']}</code></td>
            <td class="q-cell">{d['question']}</td>
            <td>{d['detail']} <div class="sub">{m_str}</div></td>
        </tr>
        """

    # 净值折线图：内嵌 SVG（不依赖外网 CDN）
    curve = state.get("equity_curve") or []
    chart_points = []
    for pt in curve:
        try:
            eq = float(pt.get("equity", 0))
            ts = str(pt.get("timestamp", ""))
        except Exception:
            continue
        chart_points.append({"t": ts, "e": eq})
    cleaned = []
    for i, pt in enumerate(chart_points):
        eq = pt["e"]
        if i > 0 and i < len(chart_points) - 1:
            prev_e, next_e = chart_points[i - 1]["e"], chart_points[i + 1]["e"]
            if eq < 3 and prev_e >= 8 and next_e >= 8:
                eq = round((prev_e + next_e) / 2, 4)
        cleaned.append({"t": pt["t"], "e": eq})
    if not cleaned:
        cleaned = [{"t": state.get("last_updated", "now"), "e": float(state.get("total_equity_usdc", 0))}]

    def build_equity_svg(points):
        w, h = 900, 260
        pad_l, pad_r, pad_t, pad_b = 48, 16, 16, 36
        ys = [p["e"] for p in points]
        y_min, y_max = min(ys), max(ys)
        if abs(y_max - y_min) < 1e-6:
            y_min -= 0.5
            y_max += 0.5
        span = y_max - y_min
        y_min -= span * 0.08
        y_max += span * 0.08
        n = len(points)

        def x_at(i):
            if n == 1:
                return pad_l + (w - pad_l - pad_r) / 2
            return pad_l + i * (w - pad_l - pad_r) / (n - 1)

        def y_at(v):
            return pad_t + (y_max - v) / (y_max - y_min) * (h - pad_t - pad_b)

        coords = [(x_at(i), y_at(p["e"])) for i, p in enumerate(points)]
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        area = f"{pad_l:.1f},{h - pad_b:.1f} " + line + f" {coords[-1][0]:.1f},{h - pad_b:.1f}"
        stroke = "#10b981" if points[-1]["e"] >= points[0]["e"] else "#f43f5e"
        ticks = []
        for k in range(5):
            val = y_min + (y_max - y_min) * k / 4
            yy = y_at(val)
            ticks.append(
                f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{w - pad_r}" y2="{yy:.1f}" stroke="#334155" stroke-width="1"/>'
                f'<text x="{pad_l - 6}" y="{yy + 4:.1f}" text-anchor="end" fill="#94a3b8" font-size="11">${val:.2f}</text>'
            )
        xlab_idx = sorted(set([0, n // 2, n - 1]))
        xlabels = []
        for i in xlab_idx:
            lab = points[i]["t"].replace(" UTC", "")[-8:]
            xlabels.append(
                f'<text x="{x_at(i):.1f}" y="{h - 10}" text-anchor="middle" fill="#94a3b8" font-size="11">{lab}</text>'
            )
        dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{stroke}" />' for x, y in coords)
        return (
            f'<svg viewBox="0 0 {w} {h}" width="100%" height="260" preserveAspectRatio="none" role="img" aria-label="equity curve">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#0b1220" rx="8"/>'
            + "".join(ticks)
            + f'<polyline fill="{stroke}33" stroke="none" points="{area}"></polyline>'
            + f'<polyline fill="none" stroke="{stroke}" stroke-width="2.5" points="{line}"></polyline>'
            + dots
            + "".join(xlabels)
            + "</svg>"
        )

    equity_svg = build_equity_svg(cleaned)

    total_pnl = round(state["total_equity_usdc"] - state["initial_capital_usdc"], 4)
    pnl_color = "#10b981" if total_pnl >= 0 else "#f43f5e"
    backend_stopped = STOP_FILE.exists()
    backend_notice = ""
    if backend_stopped:
        backend_notice = """
  <div role="alert" style="background:rgba(244,63,94,.16);border:2px solid #f43f5e;border-radius:12px;padding:18px 20px;margin-bottom:24px;">
    <div style="font-size:20px;font-weight:800;color:#fb7185;">后台已断开 · 自动交易已停止</div>
    <div style="font-size:13px;color:#fecdd3;margin-top:6px;">检测到 STOP 文件。当前页面仅展示最后一次对账快照，不会继续扫描、开仓或平仓；链上订单与资产不会因此自动撤销。</div>
  </div>"""

    def parse_event_time(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace(" UTC", ""))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    error_events = load_trading_errors()
    reconciliation = state.get("reconciliation") or {}
    last_success = parse_event_time(reconciliation.get("reconciled_at")) if reconciliation.get("status") == "OK" else None
    latest_reconciliation_error = next((event for event in error_events if event.get("stage") == "RECONCILIATION"), None)
    reconciliation_failed_after_success = bool(
        latest_reconciliation_error
        and (
            last_success is None
            or parse_event_time(latest_reconciliation_error.get("ts_utc")) is None
            or parse_event_time(latest_reconciliation_error.get("ts_utc")) > last_success
        )
    )
    connection_notice = ""
    if not backend_stopped and not reconciliation_failed_after_success:
        success_age = (datetime.now(timezone.utc) - last_success).total_seconds() if last_success else None
        success_time = escape(str(reconciliation.get("reconciled_at") or ""))
        if success_age is not None and 0 <= success_age <= 600:
            connection_notice = f"""
  <div role="status" style="background:rgba(16,185,129,.13);border:2px solid #10b981;border-radius:12px;padding:18px 20px;margin-bottom:24px;">
    <div style="font-size:19px;font-weight:800;color:#6ee7b7;">正常连接</div>
    <div style="font-size:13px;color:#d1fae5;margin-top:6px;">最近一次完整对账成功: {success_time}。Data API、Polygon RPC 与 CLOB 均已通过只读检查。</div>
  </div>"""
        else:
            connection_notice = """
  <div role="status" style="background:rgba(251,191,36,.12);border:1px solid #f59e0b;border-radius:12px;padding:18px 20px;margin-bottom:24px;">
    <div style="font-size:18px;font-weight:800;color:#fcd34d;">连接状态待确认</div>
    <div style="font-size:13px;color:#fef3c7;margin-top:6px;">最近没有足够新的完整对账结果，当前显示的是历史快照，不能据此判断接口正常。</div>
  </div>"""

    error_notice = ""
    error_section = ""
    if error_events:
        latest = error_events[0]
        latest_title = escape(str(latest.get("category_label") or latest.get("category") or "交易异常"))
        latest_time = escape(str(latest.get("ts_utc", "")))
        latest_stage = escape(str(latest.get("stage_label") or latest.get("stage") or "交易"))
        latest_event_time = parse_event_time(latest.get("ts_utc"))
        error_is_current = last_success is None or latest_event_time is None or latest_event_time > last_success
        if error_is_current:
            error_notice = f"""
  <div role="alert" style="background:rgba(251,146,60,.14);border:2px solid #fb923c;border-radius:12px;padding:18px 20px;margin-bottom:24px;">
    <div style="font-size:19px;font-weight:800;color:#fdba74;">交易异常告警 · {latest_stage} · {latest_title}</div>
    <div style="font-size:13px;color:#ffedd5;margin-top:6px;">异常发生时间: {latest_time}。本次操作未确认成功；请查看下方错误详情与处理建议。</div>
  </div>"""
        error_rows = ""
        for event in error_events[:50]:
            question = escape(str(event.get("question") or "—"))
            side = escape(str(event.get("side") or ""))
            source = "历史补录" if event.get("source") == "history" else "实时记录"
            source_record = escape(str(event.get("source_record") or ""))
            context = escape(str(event.get("context") or ""))
            context_html = f'<div class="sub">触发原因: {context}</div>' if context else ""
            error_rows += f"""
        <tr>
          <td class="sub" style="white-space:nowrap;">{escape(str(event.get('ts_utc', '')))}</td>
          <td><span class="badge badge-error">{escape(str(event.get('stage_label') or event.get('stage') or '交易'))}</span></td>
          <td style="color:#fdba74;font-weight:700;">{escape(str(event.get('category_label') or event.get('category') or '交易异常'))}</td>
          <td><div class="error-market">{question}</div><div class="sub">{side} · {source} {source_record}</div></td>
          <td><div class="error-detail">{escape(str(event.get('detail') or ''))}</div>{context_html}</td>
          <td>{escape(str(event.get('handling') or '保持账本不变并检查错误。'))}</td>
        </tr>"""
        error_section = f"""
  <div class="section error-section">
    <h2><span>交易错误与授权告警</span><span class="sub">持久化记录: logs/trading_errors.jsonl · 最近 {min(len(error_events), 50)} / 共 {len(error_events)} 条</span></h2>
    <div class="sub" style="margin-bottom:12px;">这些记录表示订单或操作失败，不代表已经修复，也不代表订单已成交。后台停止期间仅保留和展示历史告警。</div>
    <table>
      <thead><tr><th>发生时间 (UTC)</th><th>阶段</th><th>错误类型</th><th>市场 / 方向</th><th>原始错误摘要</th><th>处理建议</th></tr></thead>
      <tbody>{error_rows}</tbody>
    </table>
  </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="15">
<title>Polymarket 10U 量化实时监控面板</title>
<style>
  :root {{
    --bg: #0f172a; --card: #1e293b; --border: #334155;
    --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8;
    --green: #10b981; --red: #f43f5e;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding: 24px; line-height: 1.5;
  }}
  .header {{
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px;
  }}
  .header h1 {{ font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 12px; }}
  .mode-pill {{
    background: rgba(56, 189, 248, 0.15); color: var(--accent);
    border: 1px solid var(--accent); padding: 4px 12px; border-radius: 999px; font-size: 13px;
  }}
  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }}
  .kpi-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px;
  }}
  .kpi-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }}
  .kpi-val {{ font-size: 26px; font-weight: 700; margin-top: 6px; }}
  .kpi-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .section {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px; margin-bottom: 24px;
  }}
  .section h2 {{ font-size: 16px; margin-bottom: 14px; color: #e2e8f0; display: flex; justify-content: space-between; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 600; background: rgba(15, 23, 42, 0.4); }}
  .q-cell {{ max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .pos {{ color: var(--green); font-weight: 600; }}
  .neg {{ color: var(--red); font-weight: 600; }}
  .sub {{ font-size: 11px; color: var(--muted); }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 11px; font-weight: 600; background: #334155; color: #f8fafc;
  }}
  .badge-side {{ background: rgba(56, 189, 248, 0.2); color: #38bdf8; }}
  .badge-buy {{ background: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid #10b981; }}
  .badge-skip {{ background: rgba(148, 163, 184, 0.15); color: #94a3b8; }}
  .badge-error {{ background: rgba(251, 146, 60, 0.18); color: #fdba74; border: 1px solid #fb923c; }}
  .error-section {{ border-color: #fb923c; }}
  .error-detail {{ max-width: 480px; overflow-wrap: anywhere; white-space: normal; color: #ffedd5; }}
  .error-market {{ max-width: 260px; overflow-wrap: anywhere; }}
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1>Polymarket 量化交易终端 <span class="mode-pill">{state['mode']}</span></h1>
      <div class="sub" style="margin-top:6px;">
        专属钱包: <code>{state['wallet_address']}</code> | 估值准则: 严格按盘口真实买一价 (Best Bid) 计价，已扣除买卖价差成本
      </div>
    </div>
    <div style="text-align:right;">
      <div class="sub">最后更新时间 (每15秒自动刷新)</div>
      <div style="font-size:14px;font-weight:600;">{state['last_updated']}</div>
    </div>
  </div>

  {backend_notice}
  {connection_notice}
  {error_notice}

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">账户总净值 (Total Equity)</div>
      <div class="kpi-val" style="color:{pnl_color};">${state['total_equity_usdc']:.2f}</div>
      <div class="kpi-sub">初始本金: ${state['initial_capital_usdc']:.2f} ({state['total_return_pct']:+.2f}%)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">总盈亏 (Total PnL)</div>
      <div class="kpi-val" style="color:{pnl_color};">${total_pnl:+.2f}</div>
      <div class="kpi-sub">已实现: ${state['realized_pnl_usdc']:+.2f} | 浮动: ${state['unrealized_pnl_usdc']:+.2f}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">可用现金 (Available Cash)</div>
      <div class="kpi-val">${state['cash_usdc']:.2f}</div>
      <div class="kpi-sub">单笔上限: $2.00 | 1/4 凯利动态仓位</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">当前持仓数 (Open Positions)</div>
      <div class="kpi-val">{len(state['open_positions'])} / 4</div>
      <div class="kpi-sub">同一事件互斥锁已开启</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">累计扫描与过滤统计</div>
      <div class="kpi-val">{state['stats']['signals_triggered']} <span style="font-size:14px;color:var(--muted);">笔成交 / {state['stats']['markets_evaluated']} 次评估</span></div>
      <div class="kpi-sub">风控拦截跳过: {state['stats']['skipped_count']} 次</div>
    </div>
  </div>

  {error_section}

  <div class="section">
    <h2><span>账户净值走势 (Equity Curve)</span> <span class="sub">来自 state/portfolio.json · equity_curve</span></h2>
    <div style="position:relative;height:280px;">
      {equity_svg}
    </div>
  </div>

  <div class="section">
    <h2><span>当前持仓明细 (按实盘买一价 Best Bid 实时计价)</span> <span class="sub">刚买入时浮亏等于盘口买卖价差 (Spread)，拒绝中间价幻觉</span></h2>
    <table>
      <thead>
        <tr>
          <th>方向</th><th>预测市场问题</th><th>买入价(Ask)</th><th>现买一价(Bid)</th>
          <th>模型概率(Edge)</th><th>投入本金</th><th>当前清算市值</th><th>实时浮盈亏</th><th>建仓时间</th><th>盘口更新</th>
        </tr>
      </thead>
      <tbody>{open_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <h2><span>已平仓历史记录 (Closed Trades)</span></h2>
    <table>
      <thead>
        <tr>
          <th>方向</th><th>市场</th><th>开仓价</th><th>平仓价(Bid)</th><th>本金</th><th>已实现盈亏</th><th>平仓触发原因</th><th>平仓时间</th>
        </tr>
      </thead>
      <tbody>{closed_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <h2><span>最近风控门与特征扫描日志 (Signals & Skips Audit)</span> <span class="sub">事实由代码在 Logit 空间计算</span></h2>
    <table>
      <thead>
        <tr>
          <th>时间</th><th>动作</th><th>规则代码</th><th>市场</th><th>量化指标与原因明细</th>
        </tr>
      </thead>
      <tbody>{decision_rows}</tbody>
    </table>
  </div>

</body>
</html>
"""
    DASHBOARD_HTML_FILE.write_text(html, encoding="utf-8")


def refresh_dashboard_from_disk() -> None:
    state = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    generate_dashboard_html(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket 10U 量化引擎")
    parser.add_argument("--loop", action="store_true", help="开启无人值守循环扫描模式")
    parser.add_argument("--interval", type=int, default=60, help="扫描间隔秒数 (默认 60s)")
    parser.add_argument("--reset", action="store_true", help="重置 10U 模拟账户状态")
    args = parser.parse_args()

    if args.reset and PORTFOLIO_FILE.exists():
        PORTFOLIO_FILE.unlink()
        print("[重置完成] 已将模拟账户恢复为初始 $10.00 USDC。")

    if args.loop:
        print(f"启动无人值守量化循环 (间隔 {args.interval} 秒)... 按 Ctrl+C 或创建 STOP 文件停止。")
        while True:
            if STOP_FILE.exists():
                print("[STOP] 检测到 STOP 文件，安全退出。")
                break
            try:
                st = run_quant_cycle()
                if st:
                    print(
                        f"[{st['last_updated']}] 净值: ${st['total_equity_usdc']:.2f} "
                        f"(现金: ${st['cash_usdc']:.2f}, 持仓: {len(st['open_positions'])}/4, "
                        f"浮盈亏: ${st['unrealized_pnl_usdc']:+.2f}, 已实现: ${st['realized_pnl_usdc']:+.2f})"
                    )
            except Exception as err:
                record_trading_error("ENGINE", err, context="本轮循环异常；等待下一轮前应先检查告警")
                print(f"[守护容错] 本轮循环发生异常已安全捕获，等待下一轮重试: {err}")
                try:
                    refresh_dashboard_from_disk()
                except Exception as dashboard_error:
                    print(f"[告警页面刷新失败] {dashboard_error}")
            time.sleep(args.interval)
    else:
        st = run_quant_cycle()
        if st:
            print(
                f"单次扫描完成 | 净值: ${st['total_equity_usdc']:.2f} | "
                f"持仓: {len(st['open_positions'])}/4 | "
                f"面板已生成: {DASHBOARD_HTML_FILE}"
            )
