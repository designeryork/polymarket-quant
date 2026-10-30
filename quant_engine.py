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
import proxy_detector
import rate_limiter
import market_circuit

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

PORTFOLIO_FILE = STATE_DIR / "portfolio.json"
PAPER_PORTFOLIO_FILE = STATE_DIR / "portfolio_paper.json"
SCAN_ROTATION_FILE = STATE_DIR / "scan_rotation.json"
SIGNALS_LOG_FILE = LOGS_DIR / "signals_and_skips.jsonl"
DASHBOARD_HTML_FILE = BASE_DIR / "dashboard.html"
STOP_FILE = BASE_DIR / "STOP"

# CLOB 最小买单金额（美元）。低于此值交易所直接拒绝：
#   invalid amount for a marketable BUY order ($0.999), min size: $1
MIN_BUY_AMOUNT_USDC = 1.0


def portfolio_path() -> Path:
    """返回当前模式对应的账本文件路径。

    PAPER 与 LIVE 必须使用不同的账本文件。
    旧实现两者共用 `state/portfolio.json`，导致任何一次模拟运行都会覆盖实盘账本
    （持仓、已实现盈亏、对账状态全部被纸面数据污染）。
    """
    if os.getenv("PAPER", "true").lower() == "true":
        return PAPER_PORTFOLIO_FILE
    return PORTFOLIO_FILE

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
        # 使用探测到的可用通道，而不是直接信任环境变量。
        # 原因：环境变量可能指向与浏览器不同的代理端口（例如被工具链注入的转发通道），
        # 导致浏览器正常但脚本全面超时。探测逻辑见 proxy_detector.py。
        proxy_info = proxy_detector.detect_proxy()
        self.proxy_url = proxy_info.get("proxy_url")
        self.proxy_source = proxy_info.get("source", "")
        self.session = proxy_detector.build_session()
        self.session.headers.update({"User-Agent": "PolymarketQuantAgent/1.0"})
        # 探测失败时保留一条直连通道作为兜底
        self._direct_session = requests.Session()
        self._direct_session.headers.update({"User-Agent": "PolymarketQuantAgent/1.0"})
        self._direct_session.trust_env = False
        self._proxy_failed = False
        if self.proxy_url:
            print(f"[网络通道] 已选定 {self.proxy_url}（来源：{self.proxy_source}，延迟 {proxy_info.get('latency_s', 0)}s）")
        else:
            print("[网络通道] 未找到可用代理，改用直连。")

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """带限流退避与通道故障回退的请求。

        * 429 —— 遵守 Retry-After 退避重试（限流是突发性的，退避后通常立即恢复）；
        * 代理/连接异常 —— 改用直连重试一次；
        * 其它异常直接抛出，交由调用方按 fail-closed 处理。
        """
        try:
            return rate_limiter.request_with_retry(self.session, method, url, **kwargs)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
            if self._proxy_failed:
                raise
            self._proxy_failed = True
            print("[网络回退] 代理通道异常，切换为直连重试一次。")
            return rate_limiter.request_with_retry(self._direct_session, method, url, **kwargs)

    def fetch_active_markets(self, limit: int = 30, offset: int = 0) -> List[Dict[str, Any]]:
        """拉取活跃市场列表。

        `offset` 用于轮转覆盖：Gamma 单次最多返回 100 个市场，若每轮都只取
        成交量前 N 个，相邻轮次的候选集合高度重叠（实测 Jaccard 重合度 0.669），
        大量市场永远得不到评估机会。通过 offset 轮转，让整个池子被逐步覆盖。

        额外过滤已结束市场：Gamma 的 `active=true` 并不严格排除已结算盘口，
        实测返回结果中包含已过期 148 天的市场（endDate 早已过去）。
        这些市场白白占用扫描配额（历史数据显示占扫描量的 24.6%，
        其中 83.9% 实际已过期），这里在拉取阶段就剔除。
        """
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }
        if offset:
            params["offset"] = offset
        try:
            resp = self._request("GET", GAMMA_MARKETS_URL, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return []
            now = datetime.now(timezone.utc)
            alive = []
            expired = 0
            for market in data:
                hours_left = parse_hours_to_expiry(market)
                if hours_left <= 0:
                    expired += 1
                    continue
                alive.append(market)
            if expired:
                print(f"[扫描窗口] 剔除已结束市场 {expired} 个（Gamma active 过滤不彻底）")
            return alive
        except Exception as e:
            print(f"[网络波动跳过] Gamma API 请求异常 (Fail-closed): {e}")
            return []

    def fetch_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self._request(
                "GET", f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=8
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
                resp = self._request(
                    "GET", f"https://gamma-api.polymarket.com/events/{event_id}", timeout=10
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict) and payload.get("markets"):
                        markets = payload["markets"]
            except Exception:
                pass
            if not markets:
                resp = self._request(
                    "GET",
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
            resp = self._request(
                "GET",
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
    target = portfolio_path()
    if target.exists():
        try:
            return json.loads(target.read_text(encoding="utf-8"))
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(init_state, indent=2, ensure_ascii=False), encoding="utf-8")
    return init_state


def save_portfolio(state: Dict[str, Any]) -> None:
    state["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    target = portfolio_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")



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


def load_scan_rotation() -> Dict[str, Any]:
    """读取扫描轮转状态。

    独立文件存储，不放进 portfolio.json —— 因为 LIVE 模式下 portfolio.json
    会被对账器整份重建，放在里面会被覆盖。
    """
    try:
        return json.loads(SCAN_ROTATION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_scan_rotation(next_offset: int) -> None:
    try:
        SCAN_ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCAN_ROTATION_FILE.write_text(
            json.dumps(
                {
                    "next_offset": int(next_offset),
                    "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def cleanup_dangling_sell_orders(state: Dict[str, Any]) -> int:
    """清理已无对应持仓的挂卖单（止盈单/平仓单）。

    背景：仓位可能因市场结算、被赎回或人工干预而消失，但挂在 CLOB 上的 GTC 卖单
    （`expiration="0"` 永不过期）会继续留在链上订单簿里。之后一旦有其它逻辑尝试
    卖出同一 token，就会触发 `not enough balance / allowance`，或让卖单在错误的价格上成交。

    只处理"链上已无该 token 持仓"的卖单；部分成交的仓位由对账器负责核销。
    返回成功撤销的单数。
    """
    live_token_ids = {str(pos.get("token_id") or "") for pos in state.get("open_positions", [])}
    live_token_ids.discard("")
    if not live_token_ids:
        # 没有任何持仓时，所有卖单都是悬挂单
        pass

    cancelled = 0
    try:
        executor = get_live_executor()
    except Exception:
        return 0

    for order in order_ledger.blocking_orders():
        if str(order.get("side", "")).upper() != "SELL":
            continue
        token_id = str(order.get("token_id") or "")
        if token_id and token_id in live_token_ids:
            continue
        order_id = str(order.get("order_id") or "")
        if not order_id:
            continue
        try:
            executor.cancel_order(order_id)
            order_ledger.update_status(
                order_id, order_ledger.STATUS_CANCELLED, notes="对应持仓已消失，撤销悬挂卖单"
            )
            append_audit_log({
                "action": "DANGLING_SELL_CANCELLED",
                "orderID": order_id,
                "token_id": token_id,
                "intent": order.get("intent"),
            })
            cancelled += 1
        except Exception as exc:
            append_audit_log({
                "action": "DANGLING_SELL_CANCEL_FAILED",
                "orderID": order_id,
                "error": str(exc)[:200],
            })
            record_trading_error(
                "CANCEL_TP", exc, str(order.get("market_question") or ""),
                str(order.get("outcome") or ""), context=f"撤销悬挂卖单 {order_id}",
            )
    return cancelled


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
                # 市场级熔断：该市场若被交易所禁用（如 503 trading is disabled），
                # 冷却期内直接跳过，不再每轮硬撞（实测曾连续 16 轮重复失败）。
                if market_circuit.get_breaker().should_skip(token_id):
                    append_audit_log({
                        "action": "LIVE_MARKET_CIRCUIT_OPEN",
                        "token_id": token_id,
                        "reason": market_circuit.get_breaker().skip_reason(token_id),
                    })
                    unrealized_sum += pnl_usdc
                    remaining_positions.append(pos)
                    continue
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
                    # 若是"市场不可交易"，记入熔断器并降低严重度：
                    # 这是交易所侧的市场状态，不是账号或代码故障，
                    # 不该在告警面板上刷成 critical 淹没真实问题。
                    if market_circuit.is_not_tradable_error(le):
                        market_circuit.get_breaker().record_failure(token_id, str(le)[:200])
                        record_trading_error(
                            "CLOSE", le, pos.get("market_question", ""), pos.get("outcome", ""),
                            context=exit_reason or "LIVE_TP",
                            severity="warning",
                        )
                    else:
                        record_trading_error(
                            "CLOSE", le, pos.get("market_question", ""), pos.get("outcome", ""),
                            context=exit_reason or "LIVE_TP",
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


def run_quant_cycle() -> Dict[str, Any]:
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

    # 1b. 清理已无对应持仓的悬挂卖单（止盈/平仓单），避免误成交或余额不足错误
    if not risk.paper_mode and risk.allow_live:
        try:
            cleaned = cleanup_dangling_sell_orders(state)
            if cleaned:
                print(f"[悬挂单清理] 已撤销 {cleaned} 笔无对应持仓的卖单。")
        except Exception as cleanup_error:
            print(f"[悬挂单清理] 跳过（不影响本轮其它逻辑）: {cleanup_error}")

    # 2. UTC 自然日亏损熔断（相对当日 start_equity）
    ensure_daily_stats(state)
    start_eq = float(state["daily_stats"]["start_equity"])
    day_pnl = float(state["total_equity_usdc"]) - start_eq
    circuit_breaker = day_pnl <= -risk.daily_loss_limit

    # 3. 抓取活跃市场
    # 扫描窗口配置：
    #   SCAN_LIMIT           —— 每轮从 Gamma 拉取的候选数量（上限 100）
    #   SCAN_OFFSET_ROTATION —— 轮转步长，让相邻轮次覆盖不同区间，
    #                           避免"每轮都看同一批头部市场"导致的机会遗漏
    scan_limit = max(1, min(int(os.getenv("SCAN_LIMIT", "60")), 100))
    rotation = max(0, int(os.getenv("SCAN_OFFSET_ROTATION", "20")))
    offset = 0
    if rotation:
        offset = int(load_scan_rotation().get("next_offset") or 0)
        if offset >= scan_limit:
            offset = 0
    markets = data_layer.fetch_active_markets(limit=scan_limit, offset=offset)
    if rotation and markets:
        save_scan_rotation((offset + rotation) % max(1, scan_limit))
    print(f"[扫描窗口] 拉取 {len(markets)} 个市场 (limit={scan_limit}, offset={offset})")

    # 扫描覆盖统计：记录本轮评估过的不同市场，用于量化"是否真的在覆盖整个池子"
    state.setdefault("scan_coverage", {})
    coverage = state["scan_coverage"]
    seen = coverage.setdefault("markets_seen", {})
    for m in markets:
        q = m.get("question", "")[:120]
        if q:
            seen[q] = int(seen.get(q, 0)) + 1
    coverage["distinct_markets_seen"] = len(seen)
    coverage["total_evaluations"] = int(coverage.get("total_evaluations", 0)) + len(markets)
    coverage["rounds"] = int(coverage.get("rounds", 0)) + 1
    coverage["last_limit"] = scan_limit
    coverage["last_offset"] = offset
    coverage["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    if coverage["rounds"] % 10 == 0:
        print(
            f"[扫描覆盖] 累计 {coverage['rounds']} 轮 | 评估 {coverage['total_evaluations']} 次 | "
            f"覆盖 {coverage['distinct_markets_seen']} 个不同市场"
        )
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
        # 说明：二元市场 YES 卖一 + NO 卖一 ≈ 1，因此两侧的区间判定是互补的 ——
        # YES 越界必然导致 NO 也越界。该过滤的真实含义是「市场是否已形成明确共识」。
        picked_side = None
        side_asks: Dict[str, Any] = {}
        for idx in (0, 1):
            tid = str(token_ids[idx])
            if tid in held_token_ids:
                continue
            book = data_layer.fetch_orderbook(tid)
            if not book:
                side_asks[f"side{idx}_book"] = None
                continue
            label = outcomes[idx] if idx < len(outcomes) else f"Outcome_{idx}"
            side_asks[f"side{idx}_ask"] = book["best_ask"]
            side_asks[f"side{idx}_bid"] = book["best_bid"]
            side_asks[f"side{idx}_label"] = label
            if 0.15 <= book["best_ask"] <= 0.85:
                picked_side = (idx, tid, label, book)
                break

        if not picked_side:
            # 记录两侧实际价格，便于从审计日志直接判断是「共识已形成」还是「盘口缺失」
            detail = "YES/NO 卖一价均不在 [0.15, 0.85] 核心可交易区间"
            yes_ask = side_asks.get("side0_ask")
            no_ask = side_asks.get("side1_ask")
            if yes_ask is None and no_ask is None:
                detail = "YES/NO 两侧盘口均缺失，无法取得卖一价"
            else:
                parts = []
                for idx, key in ((0, "YES"), (1, "NO")):
                    ask = side_asks.get(f"side{idx}_ask")
                    if ask is None:
                        parts.append(f"{key}=无盘口")
                    elif ask > 0.85:
                        parts.append(f"{key}={ask:.3f}(>0.85)")
                    else:
                        parts.append(f"{key}={ask:.3f}(<0.15)")
                detail = f"YES/NO 卖一价均不在 [0.15, 0.85] 核心可交易区间 | {' '.join(parts)}"
            record_skip(question, "OUT_OF_PROB_RANGE", detail, side_asks)
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
        # 份额换算必须**向上取整**：CLOB 要求买单金额 >= $1，
        # 而执行器会把份额截断到 2 位小数（floor），截断会让金额缩水。
        # 例：$1.00 @ best_ask=0.199 -> 5.0251 份，截断成 5.02 份
        #     -> 5.02*0.199 = $0.99898 < $1，被 CLOB 以 HTTP 400 拒绝。
        # 用 ceil 换算 + 向上对齐到 2 位小数，保证截断后仍达标。
        shares = math.ceil(size_usdc / best_ask * 100.0 - 1e-9) / 100.0
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

        # 二次校验：实际下单金额（按截断后的份额计）必须达到 $1 最小门槛。
        # 这里模拟执行器的 floor 截断，用与真实下单完全一致的算法预检，
        # 避免把注定失败的订单送到交易所。
        #
        # 注意 floor 的浮点陷阱：4.77 * 100 = 476.99999999999994，
        # 直接 floor 会得到 476 而非 477，凭空少掉 0.01 份（$0.0021）。
        # 必须加 epsilon 修正，这与执行器里的处理保持一致。
        effective_shares = math.floor(shares * 100.0 + 1e-6) / 100.0
        effective_amount = effective_shares * best_ask
        if effective_amount < MIN_BUY_AMOUNT_USDC - 1e-9:
            # 向上补足到刚好达标（多付不足 1 tick，可忽略）
            needed_shares = math.ceil((MIN_BUY_AMOUNT_USDC + 1e-9) / best_ask * 100.0 - 1e-6) / 100.0
            needed_cost = needed_shares * best_ask
            if needed_cost <= risk.max_single_bet and needed_cost <= state["cash_usdc"]:
                shares = needed_shares
                size_usdc = round(needed_cost, 2)
                metrics_summary["order_amount_adjusted"] = round(needed_cost, 4)
            else:
                record_skip(
                    question,
                    "AMOUNT_BELOW_MIN",
                    f"实际下单金额 ${effective_amount:.4f} 低于最小下单额 ${MIN_BUY_AMOUNT_USDC:.2f}，"
                    f"补足至 ${needed_cost:.2f} 会超过单笔上限/现金",
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
      <div class="system-banner danger" role="alert">
        <span class="banner-dot"></span><strong>后台已断开 · 自动交易已停止</strong>
        <span>当前仅展示最后一次对账快照；链上订单与资产不会因此自动撤销。</span>
      </div>"""

    # 在途挂单账本 + 单实例锁状态（用于确认没有重复实例、没有悬挂卖单）
    try:
        pending_summary = order_ledger.summary()
    except Exception:
        pending_summary = {"total": 0, "by_status": {}, "blocking": 0}
    try:
        lock_info = single_instance.lock_status()
    except Exception:
        lock_info = {"held": False}
    try:
        proxy_info = proxy_detector.detect_proxy()
    except Exception:
        proxy_info = {"proxy_url": None, "source": "", "latency_s": 0, "exit": {}}
    proxy_exit = proxy_info.get("exit") or {}
    proxy_text = (
        f"{proxy_info.get('proxy_url')}（来源：{proxy_info.get('source','')}，延迟 {proxy_info.get('latency_s',0)}s）"
        if proxy_info.get("proxy_url")
        else "直连（未使用代理）"
    )
    proxy_exit_text = (
        f"{proxy_exit.get('ip','')} · {proxy_exit.get('country','')} {proxy_exit.get('city','')}"
        if proxy_exit.get("ip")
        else "出口信息未知"
    )

    pending_rows = ""
    for order in order_ledger.blocking_orders()[:20]:
        pending_rows += f"""
        <tr>
            <td><code>{order.get('order_id','')[:18]}…</code></td>
            <td><span class="badge">{order.get('side','')}</span></td>
            <td>{order.get('intent','')}</td>
            <td class="q-cell">{order.get('market_question','')}</td>
            <td>{order.get('price',0):.3f}</td>
            <td>{order.get('shares',0):.2f}</td>
            <td>${order.get('notional_usdc',0):.2f}</td>
            <td><span class="badge badge-skip">{order.get('status','')}</span></td>
            <td class="sub">{order.get('updated_at_utc','')}</td>
        </tr>
        """
    if not pending_rows:
        pending_rows = '<tr><td colspan="9" style="text-align:center;color:#94a3b8;padding:18px;">当前无在途挂单（所有订单均已离簿并经只读对账确认）</td></tr>'

    lock_text = (
        f"已锁定 PID {lock_info.get('pid')}（{lock_info.get('role','')}，启动于 {lock_info.get('started_at_utc','')}）"
        if lock_info.get("held") and lock_info.get("alive")
        else ("锁文件存在但持锁进程已退出（陈旧锁）" if lock_info.get("held") else "未持有锁（当前为一次性运行或后台未启动）")
    )

    # 扫描覆盖统计：量化"评估次数"与"覆盖的不同市场数"的差距
    coverage = state.get("scan_coverage") or {}
    if coverage.get("rounds"):
        distinct = int(coverage.get("distinct_markets_seen", 0))
        evaluations = int(coverage.get("total_evaluations", 0))
        avg = evaluations / max(1, coverage.get("rounds", 1))
        coverage_block = (
            f'<div class="kpi-sub" style="margin-top:6px;">'
            f'覆盖 {distinct} 个不同市场 · 平均每轮 {avg:.0f} 个 · '
            f'窗口 limit={coverage.get("last_limit")} offset={coverage.get("last_offset")}'
            f"</div>"
        )
    else:
        coverage_block = ""

    # 限流与退避状态：让"为什么本轮失败"在面板上一目了然
    try:
        limiter_state = rate_limiter.rate_limit_snapshot()
        rate_rows = "".join(
            f"<tr><td>{host}</td><td>{info.get('min_interval')}s</td>"
            f"<td>{info.get('seconds_ago')}s 前</td></tr>"
            for host, info in limiter_state.items()
        )
        rate_block = (
            "<div style='background:rgba(56,138,221,.10);border:1px solid rgba(56,138,221,.35);"
            "border-radius:10px;padding:12px 16px;margin-bottom:14px;font-size:13px;color:#cbd5e1;'>"
            "<strong style='color:#93c5fd;'>限流保护：</strong>"
            "已启用 Retry-After 退避 + 指数退避 + 巡检抖动"
            + (f"<table style='margin-top:8px;font-size:12px;'>{rate_rows}</table>" if rate_rows else "")
            + "</div>"
        )
    except Exception:
        rate_block = ""

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
      <div class="system-banner success" role="status">
        <span class="banner-dot"></span><strong>系统连接正常</strong>
        <span>Data API、Polygon RPC 与 CLOB 只读检查通过 · 最近对账 {success_time}</span>
      </div>"""
        else:
            connection_notice = """
      <div class="system-banner warning" role="status">
        <span class="banner-dot"></span><strong>连接状态待确认</strong>
        <span>最近没有足够新的完整对账结果，当前显示历史快照。</span>
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
      <div class="system-banner warning" role="alert">
        <span class="banner-dot"></span><strong>交易异常 · {latest_stage} · {latest_title}</strong>
        <span>{latest_time} · 本次操作未确认成功，请查看错误卡片中的处理建议。</span>
      </div>"""
        error_rows = ""
        for event in error_events[:50]:
            question = escape(str(event.get("question") or "—"))
            side = escape(str(event.get("side") or ""))
            source = "历史补录" if event.get("source") == "history" else "实时记录"
            source_record = escape(str(event.get("source_record") or ""))
            context = escape(str(event.get("context") or ""))
            context_html = f'<div class="error-context"><strong>触发原因</strong>{context}</div>' if context else ""
            error_rows += f"""
      <details class="error-item">
        <summary>
          <span class="badge badge-error">{escape(str(event.get('stage_label') or event.get('stage') or '交易'))}</span>
          <span class="error-summary">
            <strong>{escape(str(event.get('category_label') or event.get('category') or '交易异常'))}</strong>
            <span>{question}</span>
          </span>
          <time>{escape(str(event.get('ts_utc', '')))}</time>
        </summary>
        <div class="error-body">
          <div class="error-record-meta">{side} · {source} {source_record}</div>
          <div class="error-detail">{escape(str(event.get('detail') or ''))}</div>
          {context_html}
          <div class="error-handling"><strong>处理建议</strong>{escape(str(event.get('handling') or '保持账本不变并检查错误。'))}</div>
        </div>
      </details>"""
        error_section = f"""
    <section class="panel error-panel">
      <div class="panel-header">
        <div><h2>交易错误与授权告警</h2><p>Errors & authorization</p></div>
        <span class="panel-meta">最近 {min(len(error_events), 50)} / {len(error_events)}</span>
      </div>
      <div class="panel-note">失败记录不代表已成交或已修复；点击记录可查看原始错误与处理建议。</div>
      <div class="scroll-region error-list" data-scroll-key="errors" tabindex="0" role="region" aria-label="交易错误记录，可滚动">
        {error_rows}
      </div>
    </section>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Polymarket · 量化交易监控</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #090d14; --card: #111823; --card-raised: #151e2b; --border: #233042;
    --text: #edf3fa; --muted: #8492a6; --faint: #59687c; --accent: #7dd3fc;
    --green: #34d399; --red: #fb7185; --orange: #fbbf24;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ height: 100%; }}
  body {{
    font-family: Inter, "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text); overflow: hidden; line-height: 1.45;
    font-size: 13px; -webkit-font-smoothing: antialiased;
  }}
  button, input, table {{ font: inherit; }}
  code, .kpi-val, td, .updated-time {{ font-variant-numeric: tabular-nums; }}
  .app-shell {{
    height: 100dvh; max-width: 2200px; margin: 0 auto; padding: 16px 18px;
    display: grid; grid-template-rows: auto auto auto minmax(0, 1fr); gap: 12px;
  }}
  .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 46px; }}
  .brand {{ display: flex; align-items: center; gap: 11px; min-width: 0; }}
  .brand-mark {{
    width: 38px; height: 38px; flex: 0 0 auto; display: grid; place-items: center;
    border: 1px solid #38536c; border-radius: 11px; background: #132333;
    color: var(--accent); font-size: 12px; font-weight: 800; letter-spacing: -.03em;
  }}
  .title-line {{ display: flex; align-items: center; gap: 10px; }}
  h1 {{ font-size: 18px; line-height: 1.2; letter-spacing: -.025em; font-weight: 700; }}
  .brand-subtitle {{ color: var(--muted); font-size: 11px; margin-top: 4px; }}
  .mode-pill {{
    display: inline-flex; align-items: center; gap: 6px; max-width: 260px;
    padding: 3px 8px; border: 1px solid #285846; border-radius: 999px;
    background: rgba(52, 211, 153, .07); color: #6ee7b7;
    font-size: 10px; font-weight: 700; letter-spacing: .03em;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .mode-pill::before {{ content: ""; width: 5px; height: 5px; flex: 0 0 auto; border-radius: 50%; background: var(--green); }}
  .header-meta {{ display: flex; align-items: center; gap: 22px; text-align: right; }}
  .wallet {{ max-width: 210px; }}
  .meta-label {{ display: block; color: var(--faint); font-size: 10px; margin-bottom: 3px; }}
  .wallet code {{ display: block; color: var(--muted); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .updated-time {{ color: #c9d4e2; font-size: 12px; font-weight: 600; white-space: nowrap; }}
  .notice-stack {{ display: grid; gap: 6px; }}
  .notice-stack:empty {{ display: none; }}
  .system-banner {{
    min-height: 34px; display: flex; align-items: center; gap: 9px; padding: 7px 11px;
    border: 1px solid var(--border); border-radius: 9px; background: var(--card);
    color: var(--muted); font-size: 11px;
  }}
  .system-banner strong {{ color: var(--text); font-size: 11px; white-space: nowrap; }}
  .system-banner > span:last-child {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .banner-dot {{ width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: var(--muted); }}
  .system-banner.success {{ border-color: rgba(52, 211, 153, .24); background: rgba(52, 211, 153, .045); }}
  .system-banner.success .banner-dot {{ background: var(--green); box-shadow: 0 0 0 3px rgba(52, 211, 153, .08); }}
  .system-banner.success strong {{ color: #6ee7b7; }}
  .system-banner.warning {{ border-color: rgba(251, 191, 36, .28); background: rgba(251, 191, 36, .05); }}
  .system-banner.warning .banner-dot {{ background: var(--orange); }}
  .system-banner.warning strong {{ color: #fcd34d; }}
  .system-banner.danger {{ border-color: rgba(251, 113, 133, .32); background: rgba(251, 113, 133, .06); }}
  .system-banner.danger .banner-dot {{ background: var(--red); }}
  .system-banner.danger strong {{ color: #fda4af; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }}
  .kpi-card {{
    position: relative; min-width: 0; padding: 12px 14px;
    border: 1px solid var(--border); border-radius: 12px; background: var(--card);
    box-shadow: 0 8px 24px rgba(0, 0, 0, .08);
  }}
  .kpi-card::before {{ content: ""; position: absolute; inset: 0 auto 0 0; width: 2px; border-radius: 12px 0 0 12px; background: #2c3d50; }}
  .kpi-card:first-child::before {{ background: var(--green); }}
  .kpi-label {{ display: flex; justify-content: space-between; gap: 8px; color: #a6b3c3; font-size: 11px; }}
  .kpi-english {{ color: var(--faint); font-size: 9px; letter-spacing: .08em; text-transform: uppercase; }}
  .kpi-val {{ margin-top: 4px; color: var(--text); font-size: 25px; line-height: 1.15; font-weight: 700; letter-spacing: -.035em; white-space: nowrap; }}
  .kpi-val small {{ color: var(--muted); font-size: 11px; font-weight: 500; letter-spacing: 0; }}
  .kpi-sub {{ margin-top: 5px; color: var(--muted); font-size: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .workspace {{
    min-height: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
    grid-template-rows: repeat(2, minmax(0, 1fr)); gap: 12px;
  }}
  .panel {{
    min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden;
    border: 1px solid var(--border); border-radius: 13px; background: var(--card);
    box-shadow: 0 12px 32px rgba(0, 0, 0, .09);
  }}
  .panel-header {{
    flex: 0 0 auto; display: flex; align-items: flex-start; justify-content: space-between; gap: 10px;
    padding: 12px 14px 10px; border-bottom: 1px solid rgba(35, 48, 66, .7);
  }}
  .panel-header h2 {{ color: #dce6f1; font-size: 12px; font-weight: 650; letter-spacing: .01em; }}
  .panel-header p {{ margin-top: 2px; color: var(--faint); font-size: 9px; letter-spacing: .06em; text-transform: uppercase; }}
  .panel-meta {{ flex: 0 0 auto; color: var(--muted); font-size: 10px; white-space: nowrap; }}
  .panel-note {{ flex: 0 0 auto; padding: 8px 14px; color: var(--muted); font-size: 10px; border-bottom: 1px solid rgba(35, 48, 66, .55); }}
  .scroll-region {{ flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }}
  .scroll-region:focus-visible {{ outline: 2px solid var(--accent); outline-offset: -2px; }}
  .scroll-region::-webkit-scrollbar {{ width: 8px; height: 8px; }}
  .scroll-region::-webkit-scrollbar-track {{ background: transparent; }}
  .scroll-region::-webkit-scrollbar-thumb {{ background: #2b3a4d; border: 2px solid var(--card); border-radius: 8px; }}
  .scroll-region::-webkit-scrollbar-thumb:hover {{ background: #3b4f66; }}
  .chart-wrap {{ flex: 1; min-height: 0; padding: 10px 12px 12px; }}
  .chart-wrap svg {{ display: block; width: 100%; height: 100%; min-height: 150px; }}
  table {{ width: 100%; border-collapse: separate; border-spacing: 0; color: #bac7d5; font-size: 11px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid rgba(35, 48, 66, .75); text-align: left; vertical-align: middle; white-space: nowrap; }}
  th {{ position: sticky; top: 0; z-index: 2; background: var(--card-raised); color: #7f90a5; font-size: 10px; font-weight: 600; }}
  tbody tr:last-child td {{ border-bottom: 0; }}
  td .sub {{ display: inline; }}
  .positions-table {{ min-width: 1180px; }}
  .orders-table {{ min-width: 900px; }}
  .closed-table {{ min-width: 920px; }}
  .signals-table {{ min-width: 760px; }}
  .q-cell {{ max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .pos {{ color: var(--green); font-weight: 650; }}
  .neg {{ color: var(--red); font-weight: 650; }}
  .sub {{ color: var(--muted); font-size: 10px; }}
  code {{ color: #9fb2c8; font-family: "SFMono-Regular", Consolas, monospace; font-size: 10px; }}
  .badge {{
    display: inline-flex; align-items: center; padding: 2px 6px; border-radius: 5px;
    background: #263447; color: #d3deea; font-size: 9px; font-weight: 700; line-height: 1.5;
  }}
  .badge-side {{ background: rgba(125, 211, 252, .1); color: var(--accent); }}
  .badge-buy {{ background: rgba(52, 211, 153, .1); color: #6ee7b7; }}
  .badge-skip {{ background: rgba(132, 146, 166, .1); color: #9aaabc; }}
  .badge-error {{ background: rgba(251, 191, 36, .1); color: #fcd34d; }}
  .error-panel {{ border-color: rgba(251, 191, 36, .25); }}
  .error-list {{ padding: 0 12px 10px; }}
  .error-item {{ border-bottom: 1px solid rgba(35, 48, 66, .7); }}
  .error-item summary {{
    display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px;
    padding: 10px 2px; cursor: pointer; list-style: none;
  }}
  .error-item summary::-webkit-details-marker {{ display: none; }}
  .error-item summary::after {{ content: "+"; grid-column: 3; grid-row: 1 / span 2; color: var(--faint); font-size: 15px; }}
  .error-item[open] summary::after {{ content: "−"; }}
  .error-summary {{ min-width: 0; display: flex; align-items: baseline; gap: 7px; }}
  .error-summary strong {{ flex: 0 0 auto; color: #fcd34d; font-size: 10px; }}
  .error-summary > span {{ color: #aebdce; font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .error-item time {{ grid-column: 2; color: var(--faint); font-size: 9px; }}
  .error-body {{ margin: 0 0 10px; padding: 10px; border-radius: 8px; background: #0c121b; color: #aebdce; font-size: 10px; overflow-wrap: anywhere; }}
  .error-record-meta {{ margin-bottom: 8px; color: var(--faint); }}
  .error-detail {{ color: #d2dce8; white-space: normal; }}
  .error-context, .error-handling {{ margin-top: 8px; }}
  .error-context strong, .error-handling strong {{ display: block; margin-bottom: 2px; color: #8492a6; }}
  .order-status {{ flex: 0 0 auto; display: grid; gap: 5px; padding: 9px 12px; border-bottom: 1px solid rgba(35, 48, 66, .7); }}
  .status-row {{ display: flex; align-items: baseline; gap: 7px; min-width: 0; color: var(--muted); font-size: 9px; }}
  .status-row strong {{ flex: 0 0 auto; color: #91a5bb; font-weight: 600; }}
  .status-row span {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  @media (hover: hover) and (pointer: fine) {{
    tbody tr:hover td {{ background: rgba(125, 211, 252, .025); }}
    .error-item summary:hover .error-summary > span {{ color: var(--text); }}
  }}
  @media (min-width: 1001px) and (max-height: 800px) {{
    .app-shell {{ padding: 10px 12px; gap: 8px; }}
    .topbar {{ min-height: 38px; }}
    .brand-mark {{ width: 34px; height: 34px; }}
    .brand-subtitle {{ display: none; }}
    .kpi-grid, .workspace {{ gap: 8px; }}
    .kpi-card {{ padding: 9px 12px; }}
    .kpi-val {{ font-size: 22px; }}
    .panel-header {{ padding: 9px 12px 8px; }}
    .panel-note {{ padding: 6px 12px; }}
  }}
  @media (max-width: 1180px) {{
    .kpi-english {{ display: none; }}
    .wallet {{ display: none; }}
  }}
  @media (max-width: 1000px) {{
    body {{ overflow: auto; }}
    .app-shell {{ height: auto; min-height: 100dvh; }}
    .workspace {{ grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-rows: none; grid-auto-rows: 360px; }}
    .kpi-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
  }}
  @media (max-width: 680px) {{
    .app-shell {{ padding: 12px; gap: 10px; }}
    .topbar {{ align-items: flex-start; gap: 12px; }}
    .header-meta {{ gap: 0; }}
    .updated-time {{ font-size: 10px; white-space: normal; }}
    .mode-pill {{ max-width: 140px; }}
    .kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
    .kpi-card:last-child {{ grid-column: 1 / -1; }}
    .workspace {{ grid-template-columns: 1fr; grid-auto-rows: 380px; gap: 10px; }}
    .system-banner {{ align-items: flex-start; flex-wrap: wrap; }}
    .system-banner > span:last-child {{ width: 100%; white-space: normal; }}
  }}
</style>
</head>
<body>
<div class="app-shell">
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">PM</div>
      <div>
        <div class="title-line"><h1>Polymarket</h1><span class="mode-pill">{state['mode']}</span></div>
        <div class="brand-subtitle">量化交易监控终端 · Best Bid 真实盘口计价</div>
      </div>
    </div>
    <div class="header-meta">
      <div class="wallet"><span class="meta-label">专属钱包</span><code title="{state['wallet_address']}">{state['wallet_address']}</code></div>
      <div><span class="meta-label">最后更新 · 15 秒自动刷新</span><div class="updated-time">{state['last_updated']}</div></div>
    </div>
  </header>

  <div class="notice-stack">{backend_notice}{connection_notice}{error_notice}</div>

  <section class="kpi-grid" aria-label="账户概览">
    <div class="kpi-card">
      <div class="kpi-label"><span>账户总净值</span><span class="kpi-english">Equity</span></div>
      <div class="kpi-val" style="color:{pnl_color};">${state['total_equity_usdc']:.2f}</div>
      <div class="kpi-sub">本金 ${state['initial_capital_usdc']:.2f} · 回报 {state['total_return_pct']:+.2f}%</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label"><span>总盈亏</span><span class="kpi-english">Total PnL</span></div>
      <div class="kpi-val" style="color:{pnl_color};">${total_pnl:+.2f}</div>
      <div class="kpi-sub">已实现 ${state['realized_pnl_usdc']:+.2f} · 浮动 ${state['unrealized_pnl_usdc']:+.2f}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label"><span>可用现金</span><span class="kpi-english">Cash</span></div>
      <div class="kpi-val">${state['cash_usdc']:.2f}</div>
      <div class="kpi-sub">单笔上限 $2.00 · 1/4 凯利 · 最小 $1.00</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label"><span>当前持仓</span><span class="kpi-english">Positions</span></div>
      <div class="kpi-val">{len(state['open_positions'])} <small>/ 4</small></div>
      <div class="kpi-sub">同一事件互斥锁已开启</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label"><span>扫描与过滤</span><span class="kpi-english">Activity</span></div>
      <div class="kpi-val">{state['stats']['signals_triggered']} <small>笔成交</small></div>
      <div class="kpi-sub">{state['stats']['markets_evaluated']:,} 次评估 · {state['stats']['skipped_count']:,} 次跳过</div>
      {coverage_block}
    </div>
  </section>

  <main class="workspace">
    <section class="panel">
      <div class="panel-header">
        <div><h2>账户净值走势</h2><p>Equity curve</p></div>
        <span class="panel-meta">portfolio.json</span>
      </div>
      <div class="chart-wrap">{equity_svg}</div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div><h2>当前持仓明细</h2><p>Open positions</p></div>
        <span class="panel-meta">Best Bid 计价</span>
      </div>
      <div class="panel-note">按真实买一价估值；初始浮亏反映盘口买卖价差。</div>
      <div class="scroll-region" data-scroll-key="positions" tabindex="0" role="region" aria-label="当前持仓明细，可滚动">
        <table class="positions-table">
          <thead><tr><th>方向</th><th>预测市场问题</th><th>买入价 Ask</th><th>现买一价 Bid</th><th>模型概率 Edge</th><th>投入本金</th><th>清算市值</th><th>实时浮盈亏</th><th>建仓时间</th><th>盘口更新</th></tr></thead>
          <tbody>{open_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div><h2>在途挂单账本</h2><p>Pending orders</p></div>
        <span class="panel-meta">{pending_summary.get('blocking',0)} 阻塞 / {pending_summary.get('total',0)} 总计</span>
      </div>
      <div class="order-status">
        <div class="status-row"><strong>单实例锁</strong><span title="{lock_text}">{lock_text}</span></div>
        <div class="status-row"><strong>在途名义</strong><span>买 ${pending_summary.get('open_buy_notional_usdc',0):.2f} · 卖 ${pending_summary.get('open_sell_notional_usdc',0):.2f}</span></div>
        <div class="status-row"><strong>网络通道</strong><span title="{proxy_text} · {proxy_exit_text}">{proxy_text} · {proxy_exit_text}</span></div>
        <div class="status-row"><strong>限流保护</strong><span>Retry-After 退避 · 指数退避 · 巡检抖动</span></div>
      </div>
      <div class="scroll-region" data-scroll-key="orders" tabindex="0" role="region" aria-label="在途挂单账本，可滚动">
        <table class="orders-table">
          <thead><tr><th>订单ID</th><th>方向</th><th>用途</th><th>市场</th><th>价格</th><th>份额</th><th>名义金额</th><th>状态</th><th>更新时间</th></tr></thead>
          <tbody>{pending_rows}</tbody>
        </table>
      </div>
    </section>

    {error_section}

    <section class="panel">
      <div class="panel-header">
        <div><h2>已平仓历史记录</h2><p>Closed trades</p></div>
        <span class="panel-meta">最近 {min(len(state.get('closed_trades', [])), 15)} 笔</span>
      </div>
      <div class="scroll-region" data-scroll-key="closed" tabindex="0" role="region" aria-label="已平仓历史记录，可滚动">
        <table class="closed-table">
          <thead><tr><th>方向</th><th>市场</th><th>开仓价</th><th>平仓价 Bid</th><th>本金</th><th>已实现盈亏</th><th>平仓触发原因</th><th>平仓时间</th></tr></thead>
          <tbody>{closed_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div><h2>风控门与特征扫描</h2><p>Signals & skips</p></div>
        <span class="panel-meta">Logit 空间</span>
      </div>
      <div class="scroll-region" data-scroll-key="signals" tabindex="0" role="region" aria-label="风控与扫描日志，可滚动">
        <table class="signals-table">
          <thead><tr><th>时间</th><th>动作</th><th>规则代码</th><th>市场</th><th>量化指标与原因明细</th></tr></thead>
          <tbody>{decision_rows}</tbody>
        </table>
      </div>
    </section>
  </main>
</div>
<script>
(() => {{
  const regions = document.querySelectorAll('[data-scroll-key]');
  regions.forEach((region) => {{
    const key = 'pm-dashboard:' + region.dataset.scrollKey;
    try {{
      const saved = JSON.parse(sessionStorage.getItem(key) || '[0,0]');
      region.scrollTop = Number(saved[0]) || 0;
      region.scrollLeft = Number(saved[1]) || 0;
    }} catch (_) {{}}
    let frame = null;
    region.addEventListener('scroll', () => {{
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {{
        try {{ sessionStorage.setItem(key, JSON.stringify([region.scrollTop, region.scrollLeft])); }} catch (_) {{}}
        frame = null;
      }});
    }}, {{ passive: true }});
  }});
}})();
</script>
</body>
</html>
"""
    clean_html = "\n".join(line.rstrip() for line in html.splitlines()) + "\n"
    DASHBOARD_HTML_FILE.write_text(clean_html, encoding="utf-8")


def refresh_dashboard_from_disk() -> None:
    target = portfolio_path()
    if not target.exists():
        return
    state = json.loads(target.read_text(encoding="utf-8"))
    generate_dashboard_html(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket 10U 量化引擎")
    parser.add_argument("--loop", action="store_true", help="开启无人值守循环扫描模式")
    parser.add_argument("--interval", type=int, default=60, help="扫描间隔秒数 (默认 60s)")
    parser.add_argument("--reset", action="store_true", help="重置 10U 模拟账户状态")
    args = parser.parse_args()

    if args.reset:
        target = portfolio_path()
        if target.exists():
            target.unlink()
            print(f"[重置完成] 已将{('模拟' if target == PAPER_PORTFOLIO_FILE else '实盘')}账本重置为初始 $10.00 USDC：{target.name}")

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

            # 巡检间隔加随机抖动：固定节奏的请求更容易与限流窗口对齐，
            # 实测 Data API 在突发高频下会返回 429 + Retry-After: 1。
            delay = rate_limiter.jittered_sleep(args.interval)
            print(f"[等待] 下一轮在 {delay:.0f}s 后开始（含随机抖动，打散请求节奏）")
            time.sleep(delay)
    else:
        st = run_quant_cycle()
        if st:
            print(
                f"单次扫描完成 | 净值: ${st['total_equity_usdc']:.2f} | "
                f"持仓: {len(st['open_positions'])}/4 | "
                f"面板已生成: {DASHBOARD_HTML_FILE}"
            )
