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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import requests
import numpy as np
from dotenv import load_dotenv

from trading_error_monitor import load_trading_errors, record_trading_error
import order_ledger
import hedge_ledger
import reverse_ledger
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
MARKET_UNIVERSE_FILE = STATE_DIR / "market_universe.json"
SIGNALS_LOG_FILE = LOGS_DIR / "signals_and_skips.jsonl"
STOP_FILE = BASE_DIR / "STOP"

# 实盘与纸面各自独立的运行文件：两个进程可以同时跑，互不覆盖。
# 判断依据是进程启动时的 PAPER 环境变量。
def is_paper_mode() -> bool:
    return os.getenv("PAPER", "true").strip().lower() == "true"


def current_mode_key() -> str:
    """live / paper / hedge。

    hedge 不是独立进程：它由纸面进程在同一次开平仓里同步驱动，
    这里只用于给它的面板与净值历史选文件名。
    """
    if os.getenv("HEDGE", "").strip().lower() in ("1", "true", "yes"):
        return "hedge"
    return "paper" if is_paper_mode() else "live"


def heartbeat_file(key: Optional[str] = None) -> Path:
    key = key or current_mode_key()
    if key == "paper":
        return STATE_DIR / "heartbeat_paper.json"
    if key == "hedge":
        return STATE_DIR / "heartbeat_hedge.json"
    return STATE_DIR / "heartbeat.json"


def dashboard_file(key: Optional[str] = None) -> Path:
    key = key or current_mode_key()
    if key == "live":
        return BASE_DIR / "dashboard.html"
    return BASE_DIR / f"dashboard_{key}.html"


def equity_history_file(key: Optional[str] = None) -> Path:
    """长期净值历史：每轮追加一行，不受 equity_curve 100 点上限约束。

    实盘 / 纸面 / 对冲必须分开存，否则几条净值曲线会混在同一个文件里。
    """
    key = key or current_mode_key()
    if key == "live":
        return STATE_DIR / "equity_history.jsonl"
    return STATE_DIR / f"equity_history_{key}.jsonl"

# 净值图时间窗口：(tab 键, 窗口秒数)。tab 决定"看最近多久"，
# 窗口内再按下面的阶梯自动选聚合粒度，保证每张图都有足够起伏、又不会点太密。
EQUITY_WINDOWS = (
    ("year", 365 * 86400),
    ("month", 30 * 86400),
    ("day", 86400),
    ("hour", 2 * 3600),
)

# 单张图最多绘制的点数：超过就升一级聚合。
# 900px 宽的 SVG 里 150 个点约 6px 间距，再密就只能看到一片色块。
EQUITY_POINT_CAP = 150

# 聚合粒度阶梯（秒）：0 = 原始巡检点，其次是 15 分钟 / 1 小时 / 6 小时 / 1 天 / 7 天 / 30 天
EQUITY_BUCKET_LADDER = (0, 900, 3600, 6 * 3600, 86400, 7 * 86400, 30 * 86400)

# CLOB 最小买单金额（美元）。低于此值交易所直接拒绝：
#   invalid amount for a marketable BUY order ($0.999), min size: $1
MIN_BUY_AMOUNT_USDC = 1.0

# 冷启动预热状态：进程刚启动时代理隧道可能未就绪，首轮对账允许原地重试一次
_PROCESS_START_TS = time.time()
_COLD_START_WARMUP = {"done": False}

# 轮次结束后仍可复用的上下文：空闲补检要复用同一个 data_layer，
# 避免每 60 秒重建一次代理通道（探测本身就要几百毫秒到数秒）。
_CYCLE_CONTEXT: Dict[str, Any] = {}


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
DATA_API_TRADES_URL = "https://data-api.polymarket.com/v2/trades"


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


# ==========================================================
# 退出规则 v2（2026-10-31）
# 1) 远端目标单：入场 × (1 + TAKE_PROFIT_RETURN)，封顶 0.99，常驻链上捕获暴击；
# 2) 成本硬止损：相对成本跌幅达到 HARD_STOP_LOSS_PCT，且距离不低于 HARD_STOP_MIN_DISTANCE；
#    无条件执行，不查历史、不等确认（旧版"单根 K 线暴跌"确认门导致阴跌行情止损失效）；
# 3) 峰值回撤：自持仓期间最高买一起回撤 TRAIL_FROM_PEAK，且触发价不深于硬止损，
#    因此 -10% 永远是地板，追踪只会让出口更早、不会更晚。
# ==========================================================
def exit_config() -> Dict[str, float]:
    """读取退出参数；非法值回落到默认值，避免环境变量写错导致实盘失控。"""

    def env_float(name: str, default: float, low: float, high: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            value = default
        if not math.isfinite(value):
            value = default
        return max(low, min(high, value))

    return {
        "stop_pct": env_float("HARD_STOP_LOSS_PCT", 0.10, 0.02, 0.90),
        "stop_min_distance": env_float("HARD_STOP_MIN_DISTANCE", 0.02, 0.0, 0.30),
        "trail_gap": env_float("TRAIL_FROM_PEAK", 0.05, 0.005, 0.80),
        "tp_return": env_float("TAKE_PROFIT_RETURN", 2.0, 0.05, 20.0),
        "max_exit_slippage": env_float("MAX_EXIT_SLIPPAGE", 0.02, 0.001, 0.20),
        # 平仓单在盘口停留超过该秒数仍未成交，就撤单按现价重挂。
        # 旧实现只要链上有 EXIT 单就永久跳过，实测有仓位挂着 15 分钟不成交、一路跌到 -33%。
        "exit_reprice_timeout": env_float("EXIT_REPRICE_TIMEOUT_SECONDS", 300.0, 30.0, 3600.0),
        # ATR/Logit 止损（文档口径）：止损距离 = k × 入场 ATR_z，并对低价合约兜底一个最小 logit 距离。
        # 固定在 -10% 的百分比止损在 0.15~0.35 的合约上只有 1~2 个 tick，落在买卖价差里，
        # 会被正常报价打掉（纸面 37/40 笔止损、持仓中位数 0 小时），故改回 logit 空间。
        "atr_k": env_float("EXIT_ATR_K", 3.0, 0.5, 10.0),
        "atr_min_drop": env_float("EXIT_ATR_MIN_DROP", 0.45, 0.05, 3.0),
        "catastrophe_pct": env_float("EXIT_CATASTROPHE_PCT", 0.55, 0.20, 0.95),
    }


def seconds_since_iso(value: Any) -> Optional[float]:
    """解析 ISO 时间并返回距现在的秒数；解析失败返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def position_fee_rate(pos: Dict[str, Any]) -> Tuple[float, float]:
    """读取持仓的费率参数。

    优先级：持仓上持久化的 fee_rate（开仓时来自市场 feeSchedule，对账时由真实成交反解）
    → 环境变量 DEFAULT_FEE_RATE → 0。费率缺失时按 0 处理，等价于旧版毛口径，
    不会因为猜错费率而让止损变得异常激进。
    """
    rate = pos.get("fee_rate")
    exponent = pos.get("fee_exponent")
    if isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0:
        exp = float(exponent) if isinstance(exponent, (int, float)) and math.isfinite(exponent) else 1.0
        return float(rate), exp
    try:
        fallback = float(os.getenv("DEFAULT_FEE_RATE", "0"))
    except (TypeError, ValueError):
        fallback = 0.0
    if not math.isfinite(fallback) or fallback < 0:
        fallback = 0.0
    return fallback, 1.0


def net_exit_per_share(price: float, fee_rate: float, fee_exponent: float = 1.0) -> float:
    """按买一价卖出一份的净到手（已扣出场费）。"""
    price = max(0.0, min(1.0, float(price)))
    return price - fee_rate * (price * (1.0 - price)) ** fee_exponent


def hard_stop_price(
    entry_price: float,
    cfg: Dict[str, float],
    fee_rate: float = 0.0,
    fee_exponent: float = 1.0,
) -> float:
    """成本硬止损价：按"净到手 / 含费成本 ≤ -stop_pct"反解毛价。

    净到手随价格单调递增，用二分求解；费率越高，止损位越高（更早卖出）。
    之后再套用 stop_min_distance 距离下限（低价位放宽，用户明确要求不低于 2 分钱）。
    注意：低价位时下限可能比费率修正更严，此时下限生效，净亏损会略深于 stop_pct。
    """
    entry_price = max(0.001, float(entry_price))
    target_net = entry_price * (1.0 - cfg["stop_pct"])
    if fee_rate <= 0:
        net_stop = target_net
    else:
        lo, hi = 0.0001, min(0.9999, entry_price)
        if net_exit_per_share(hi, fee_rate, fee_exponent) < target_net:
            net_stop = hi
        elif net_exit_per_share(lo, fee_rate, fee_exponent) > target_net:
            net_stop = lo
        else:
            for _ in range(40):
                mid = (lo + hi) / 2.0
                if net_exit_per_share(mid, fee_rate, fee_exponent) >= target_net:
                    hi = mid
                else:
                    lo = mid
            net_stop = hi
    by_floor = entry_price - cfg["stop_min_distance"]
    return round(max(0.001, min(net_stop, by_floor)), 4)


def take_profit_price(entry_price: float, cfg: Dict[str, float]) -> float:
    """远端目标价：入场 × (1 + tp_return)，封顶 0.99（CLOB 限价上限）。"""
    entry_price = max(0.001, float(entry_price))
    return round(min(0.99, entry_price * (1.0 + cfg["tp_return"])), 4)


def trailing_exit_price(
    peak_bid: float,
    entry_price: float,
    cfg: Dict[str, float],
    fee_rate: float = 0.0,
    fee_exponent: float = 1.0,
) -> float:
    """峰值回撤触发价；与硬止损取更紧（更高）的一侧，保证亏损上限不被放松。"""
    return round(
        max(hard_stop_price(entry_price, cfg, fee_rate, fee_exponent), float(peak_bid) - cfg["trail_gap"]),
        4,
    )


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


def estimate_taker_fee(shares: float, price: float, rate: float, exponent: float = 1.0, enabled: bool = True) -> float:
    if not enabled or rate <= 0:
        return 0.0
    return shares * rate * (price * (1.0 - price)) ** exponent


def estimate_order_execution(levels: List[Tuple[float, float]], shares: float) -> Dict[str, float]:
    remaining = max(0.0, float(shares))
    filled = 0.0
    notional = 0.0
    worst_price = 0.0
    for price, size in levels:
        if remaining <= 1e-9:
            break
        price = float(price)
        size = max(0.0, float(size))
        if price <= 0 or size <= 0:
            continue
        take = min(remaining, size)
        filled += take
        notional += take * price
        worst_price = price
        remaining -= take
    return {
        "filled_shares": filled,
        "remaining_shares": max(0.0, remaining),
        "notional_usdc": notional,
        "average_price": notional / filled if filled > 0 else 0.0,
        "limit_price": worst_price,
    }


# ==========================================================
# 数据层 Gamma + CLOB Orderbook + History (Section 2)
# ==========================================================
# 价格历史缓存：同一 token 在短时间内的历史序列不变，重复请求只会增加 Data API 压力。
# 单线程使用，普通 dict 即可；命中时完全不发请求。
_PRICE_HISTORY_CACHE: Dict[str, Tuple[float, List[float]]] = {}
_PRICE_HISTORY_CACHE_STATS = {"hit": 0, "miss": 0}


def _price_history_ttl() -> float:
    try:
        value = float(os.getenv("PRICE_HISTORY_CACHE_SECONDS", "300"))
    except (TypeError, ValueError):
        value = 300.0
    return max(0.0, value)


def _price_history_cache_get(token_id: Any) -> Optional[List[float]]:
    key = str(token_id)
    item = _PRICE_HISTORY_CACHE.get(key)
    if not item:
        _PRICE_HISTORY_CACHE_STATS["miss"] += 1
        return None
    ts, points = item
    if time.time() - ts > _price_history_ttl():
        _PRICE_HISTORY_CACHE.pop(key, None)
        _PRICE_HISTORY_CACHE_STATS["miss"] += 1
        return None
    _PRICE_HISTORY_CACHE_STATS["hit"] += 1
    return list(points)


def _price_history_cache_put(token_id: Any, points: List[float]) -> None:
    if len(_PRICE_HISTORY_CACHE) > 3000:
        now = time.time()
        ttl = _price_history_ttl()
        for key, (ts, _pts) in list(_PRICE_HISTORY_CACHE.items()):
            if now - ts > ttl:
                _PRICE_HISTORY_CACHE.pop(key, None)
    _PRICE_HISTORY_CACHE[str(token_id)] = (time.time(), list(points))


def price_history_cache_stats() -> Dict[str, int]:
    """供面板/诊断读取的缓存命中统计（不改变行为）。"""
    return dict(_PRICE_HISTORY_CACHE_STATS)


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
        self._proxy_failed = False
        print(f"[网络通道] 已选定 {self.proxy_url}（来源：{self.proxy_source}，延迟 {proxy_info.get('latency_s', 0)}s）")

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """带限流退避与通道故障回退的请求。

        * 429 —— 遵守 Retry-After 退避重试（限流是突发性的，退避后通常立即恢复）；
        * 代理/连接异常 —— 重新探测可用通道后重试一次；
        * 其它异常直接抛出，交由调用方按 fail-closed 处理。

        超时拆分为 (connect, read)：代理的 CONNECT 隧道偶发卡死时，
        用较小的 connect timeout 快速失败，避免整轮时间被一次卡死吃掉。
        """
        kwargs.setdefault("connect_timeout", 8)
        try:
            return rate_limiter.request_with_retry(self.session, method, url, **kwargs)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout) as exc:
            if self._proxy_failed:
                raise
            self._proxy_failed = True
            print(f"[网络自愈] 通道异常({type(exc).__name__})，重新探测可用通道后重试。")
            try:
                proxy_detector.get_detector().refresh()
                self.session = proxy_detector.build_session()
                self.session.headers.update({"User-Agent": "PolymarketQuantAgent/1.0"})
            except Exception:
                raise
            return rate_limiter.request_with_retry(self.session, method, url, **kwargs)

    def fetch_active_markets(self, limit: int = 30, offset: int = 0, strict: bool = False) -> List[Dict[str, Any]]:
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
                if strict:
                    raise ValueError("Gamma markets 返回结构异常")
                return []
            self._last_market_page_size = len(data)
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
            if strict:
                raise
            print(f"[网络波动跳过] Gamma API 请求异常 (Fail-closed): {e}")
            return []

    def fetch_market_universe(self, refresh_seconds: int = 3600) -> List[Dict[str, Any]]:
        cached = {}
        try:
            cached = json.loads(MARKET_UNIVERSE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        try:
            discovered_at = datetime.fromisoformat(str(cached.get("discovered_at_utc") or ""))
            age = (datetime.now(timezone.utc) - discovered_at).total_seconds()
        except (TypeError, ValueError):
            age = refresh_seconds + 1
        if cached.get("markets") and 0 <= age < refresh_seconds:
            return cached["markets"]

        discovered = []
        for offset in range(0, 10000, 100):
            try:
                page = self.fetch_active_markets(limit=100, offset=offset, strict=True)
            except requests.exceptions.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                if status == 422 and offset >= 2000 and discovered:
                    print(f"[市场宇宙] Gamma 在 offset={offset} 达到分页上限，以已发现的 {len(discovered)} 个市场结束。")
                    break
                raise
            if not page:
                break
            discovered.extend(page)
            if getattr(self, "_last_market_page_size", 0) < 100:
                break
        if not discovered:
            if cached.get("markets"):
                print("[市场宇宙] 完整刷新失败，沿用上次完整快照，不将部分结果当作全量。")
                return cached["markets"]
            return []

        unique = {}
        for market in discovered:
            key = str(market.get("conditionId") or market.get("id") or market.get("question") or "")
            if key:
                unique[key] = market
        ordered = sorted(unique.values(), key=lambda row: str(row.get("conditionId") or row.get("id") or ""))
        payload = {
            "discovered_at_utc": datetime.now(timezone.utc).isoformat(),
            "count": len(ordered),
            "markets": ordered,
        }
        MARKET_UNIVERSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MARKET_UNIVERSE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(MARKET_UNIVERSE_FILE)
        daily = MARKET_UNIVERSE_FILE.with_name(f"market_universe_{datetime.now(timezone.utc):%Y-%m-%d}.json")
        daily.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"[市场宇宙] 完整分页刷新完成：{len(ordered)} 个活跃市场")
        return ordered

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
        """只使用真实 CLOB 历史；样本不足时返回空序列并由入场门禁跳过。"""
        cached = _price_history_cache_get(token_id)
        if cached is not None:
            return cached
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
                    pts = pts[-60:]
                    _price_history_cache_put(token_id, pts)
                    return list(pts)
        except Exception:
            pass
        return []


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
        self.market_whitelist = {
            item.strip() for item in os.getenv("MARKET_WHITELIST", "").split(",") if item.strip()
        }
        self.whitelist_mode = os.getenv("MARKET_WHITELIST_MODE", "AUTO").strip().upper()
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
    """写回账本。

    防误覆盖护栏
    ------------
    如果磁盘上已有一份**有实质内容**的账本（有持仓 / 有已平仓记录），
    而即将写入的新状态却是一份"空白账本"（净值 == 初始资金、无持仓、无平仓），
    那么这次写入一定是误操作 —— 典型场景：云端恢复状态失败后引擎以
    初始 $10 启动，然后把空白账本写回，把真实持仓和净值曲线清零。

    这种清零是不可逆的（快照分支是 --force 覆盖，没有历史可回滚），
    所以宁可**拒绝写入并报错**，也不能让它静默发生。
    """
    target = portfolio_path()
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            existing = None
        if isinstance(existing, dict):
            old_equity = float(existing.get("total_equity_usdc") or 0)
            old_positions = len(existing.get("open_positions") or [])
            old_trades = len(existing.get("closed_trades") or [])
            new_equity = float(state.get("total_equity_usdc") or 0)
            new_positions = len(state.get("open_positions") or [])
            new_trades = len(state.get("closed_trades") or [])
            old_has_content = old_positions > 0 or old_trades > 0
            new_is_blank = new_positions == 0 and new_trades == 0
            if old_has_content and new_is_blank:
                raise RuntimeError(
                    "[护栏] 拒绝用空白账本覆盖已有账本："
                    f"磁盘现有 净值 ${old_equity:.2f} / 持仓 {old_positions} / 平仓 {old_trades}，"
                    f"新状态 净值 ${new_equity:.2f} / 持仓 0 / 平仓 0。"
                    "这通常意味着状态恢复失败，必须先修好恢复逻辑。"
                )
    state["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
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


_INSTANCE_LOCK_HELD = False


class SingleInstanceLost(RuntimeError):
    pass


class ExitDeferred(RuntimeError):
    """本轮不提交平仓单，但**不是故障**。

    两种情况会走到这里：
      1. 链上挂单查询失败——无法确认是否已有卖单，不能凭猜测下单；
      2. 链上查无卖单、但本地账本仍有阻塞记录——多半是那张单刚成交，
         CLOB 立即把它移出挂单列表，而持仓与账本还没对上。
    两种都只跳过本轮，下一轮自动恢复；旧实现第 2 种情况直接抛错，
    实测把止损卡了 4.5 分钟（11:50 → 11:55）。
    """


def existing_engine_processes(paper: bool) -> list:
    """列出同模式的其它引擎进程（不含自己）。用于启动时拒绝重复实例。

    跨部署隔离（重要）：只认工作目录在本 BASE 下的进程。
    旧实现按命令行**全局扫描**，不区分部署目录 ——
    035_polymarket_paper 副本的实例会被误认成 032 自己的进程，
    导致副本起不来（或者反过来，032 被副本挡住）。
    psutil 的 proc.cwd() 在 Windows 上可读，用它做部署归属判定是可靠的。
    """
    try:
        import psutil
    except Exception:
        return []
    base = os.path.normcase(os.path.abspath(str(BASE_DIR)))
    rows = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid == os.getpid():
                continue
            name = (proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmd = proc.info.get("cmdline") or []
            if not any(str(item).endswith("quant_engine.py") for item in cmd):
                continue
            if any("--paper" in str(item) for item in cmd) != paper:
                continue
            # 只有工作目录与本进程一致才是同一份部署
            try:
                cwd = os.path.normcase(os.path.abspath(proc.cwd()))
            except Exception:
                continue
            if cwd != base:
                continue
            rows.append(proc)
        except Exception:
            pass
    return rows


def ensure_single_instance() -> bool:
    """确保本进程仍持有交易锁；锁被替换后必须停止而不是继续交易。"""
    global _INSTANCE_LOCK_HELD
    if _INSTANCE_LOCK_HELD:
        status = single_instance.lock_status()
        if status.get("held") and status.get("alive") and status.get("pid") == os.getpid():
            return True
        _INSTANCE_LOCK_HELD = False
        raise SingleInstanceLost(
            f"PID {os.getpid()} 已失去单实例锁，停止本进程以避免与新实例并发交易。"
        )
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


def fill_paper_order(pos: Dict[str, Any], book: Dict[str, Any]) -> None:
    remaining = float(pos.get("paper_remaining_shares") or 0.0)
    if remaining <= 0:
        return
    limit = float(pos.get("paper_limit_price") or 0.0)
    if book["best_ask"] > limit:
        return
    levels = [(price, size) for price, size in book["raw_asks"] if price <= limit]
    execution = estimate_order_execution(levels, remaining)
    filled = execution["filled_shares"]
    if filled <= 0:
        return
    old_shares = float(pos["shares"])
    old_cost = float(pos["cost_usdc"])
    new_shares = old_shares + filled
    pos["shares"] = new_shares
    pos["cost_usdc"] = round(old_cost + execution["notional_usdc"], 6)
    pos["entry_price"] = round((old_cost + execution["notional_usdc"]) / new_shares, 6)
    pos["paper_remaining_shares"] = round(remaining - filled, 6)
    if pos["paper_remaining_shares"] <= 1e-9:
        pos["paper_remaining_shares"] = 0.0
        pos.pop("paper_order_id", None)
        pos.pop("paper_limit_price", None)


def confirm_probability_move(data_layer: PolymarketDataLayer, pos: Dict[str, Any], current_bid: float) -> bool:
    """【已停用 — 退出规则 v2 起不再调用，保留仅为历史用例与排查】

    缺陷：reference 取的是上一根 15 分钟 K 线，衡量的是"最近 15 分钟跌了多少"，
    而不是"相对入场跌了多少"。阴跌行情下每根 K 线跌幅都小于阈值，该确认恒为 False，
    止损因此长期不触发；只有单根 K 线暴跌（插针）才会放行——等于把止损变成了崩盘探测器。
    现行做法见 hard_stop_price / trailing_exit_price。
    """
    history = data_layer.fetch_price_history(pos["token_id"], current_bid, {})
    if not history:
        return False
    reference = history[-2] if len(history) >= 2 and abs(history[-1] - current_bid) < 1e-9 else history[-1]
    return to_logit(reference) - to_logit(current_bid) >= 0.38


RESEARCH_LOG_FILE = LOGS_DIR / "strategy_research_samples.jsonl"


def append_research_sample(record: Dict[str, Any]) -> None:
    record["ts_utc"] = datetime.now(timezone.utc).isoformat()
    with open(RESEARCH_LOG_FILE, "a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_resolution_sample(pos: Dict[str, Any], resolved_price: float) -> None:
    if resolved_price not in (0.0, 1.0):
        return
    append_research_sample({
        "record_type": "RESOLUTION",
        "token_id": pos["token_id"],
        "event_id": pos.get("event_id", ""),
        "p_est": pos.get("p_est"),
        "outcome": int(resolved_price),
        "entry_price": pos.get("entry_price"),
    })


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

        if os.getenv("PAPER", "true").lower() == "true":
            fill_paper_order(pos, book)
        best_bid = book["best_bid"]
        best_ask = book["best_ask"]
        shares = pos["shares"]
        entry_price = pos["entry_price"]
        cost_usdc = pos["cost_usdc"]

        # 按真实可成交的买一价计算当前市值；出场费按持仓费率预扣，得到"可清算净额"。
        # 结算（RESOLVED）不经过撮合，不计出场费。
        fee_rate, fee_exponent = position_fee_rate(pos)
        exit_fee_usdc = estimate_taker_fee(
            shares, best_bid, fee_rate, fee_exponent, enabled=(mark_source != "RESOLVED")
        )
        gross_value = round(shares * best_bid, 4)
        current_value = round(gross_value - exit_fee_usdc, 4)
        pnl_usdc = round(current_value - cost_usdc, 4)
        pnl_pct = round((pnl_usdc / max(0.01, cost_usdc)) * 100.0, 2)
        pos["estimated_exit_fee_usdc"] = round(exit_fee_usdc, 6)

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
            record_resolution_sample(pos, best_bid)
            unrealized_sum += pnl_usdc
            remaining_positions.append(pos)
            continue

        # ===== 退出规则 v2：成本硬止损 + 峰值回撤 + 远端目标 =====
        # 硬止损无条件执行：近 30 笔里 -48%~-78% 的深亏，全部源于旧版
        # "必须单根 15 分钟 K 线暴跌才确认"的额外门禁。阴跌行情下该门禁恒为 False。
        cfg = exit_config()
        entry_cost = cost_usdc / shares if shares > 0 else float(entry_price)
        # ATR/Logit 止损：以入场时的 ATR_z 为单位，而不是固定百分比。
        # 缺少 atr_z 的存量仓位（旧账本）用最小 logit 距离兜底。
        atr_z_entry = float(pos.get("atr_z") or 0.0)
        if not math.isfinite(atr_z_entry) or atr_z_entry < 0:
            atr_z_entry = 0.0
        stop_drop = max(cfg["atr_k"] * atr_z_entry, cfg["atr_min_drop"])
        stop_price = round(from_logit(z_entry - stop_drop), 4)
        catastrophe_price = round(entry_cost * (1.0 - cfg["catastrophe_pct"]), 4)
        target_prob = take_profit_price(entry_cost, cfg)
        stored_peak = float(pos.get("peak_bid") or 0.0)
        if stored_peak <= 0:
            # 旧账本没有 peak_bid：由 z_peak 反解；排除历史零值污染，失败则退化为入场价
            legacy_z = pos.get("z_peak")
            if (
                isinstance(legacy_z, (int, float))
                and math.isfinite(legacy_z)
                and not (abs(legacy_z) < 1e-9 and abs(z_entry) > 1e-3)
            ):
                stored_peak = from_logit(legacy_z)
        peak_bid = max(stored_peak, entry_cost, float(best_bid))
        pos["peak_bid"] = round(peak_bid, 4)
        # 目标价与峰值一并落盘：存量持仓在首轮即被改写成 v2 口径
        pos["target_prob"] = target_prob
        pos["atr_stop_price"] = stop_price
        pos["atr_stop_drop"] = round(stop_drop, 4)
        pos["exit_rule_version"] = 3
        # 峰值回撤仍按 5 分钱追踪，但地板改为 ATR 止损价（原来是 -10% 成本线）
        trail_price = round(max(stop_price, peak_bid - cfg["trail_gap"]), 4)
        hours_left = parse_hours_to_expiry({"endDate": pos.get("expires_at")}) if pos.get("expires_at") else 999.0

        exit_reason = None
        if best_bid >= target_prob:
            exit_reason = f"达到目标止盈 (Bid {best_bid} >= {target_prob}, 目标 +{cfg['tp_return'] * 100:.0f}%)"
        elif best_bid <= catastrophe_price:
            # 尾部保护：logit 距离再大也不能让一笔仓位接近归零
            exit_reason = (
                f"灾难止损 (Bid {best_bid} <= {catastrophe_price}, "
                f"浮亏 {pnl_pct:.2f}%, 阈值 -{cfg['catastrophe_pct'] * 100:.0f}%)"
            )
        elif best_bid <= stop_price:
            atr_txt = f"{atr_z_entry:.3f}" if atr_z_entry > 0 else "N/A"
            exit_reason = (
                f"ATR 止损 (Bid {best_bid} <= {stop_price}, z {z_now:.3f} <= "
                f"{z_entry - stop_drop:.3f}, 距离 {stop_drop:.2f} logit, ATR_z {atr_txt}, "
                f"浮亏 {pnl_pct:.2f}%)"
            )
        elif peak_bid > entry_cost and best_bid <= trail_price:
            exit_reason = (
                f"峰值回撤退出 (Peak {peak_bid:.3f} -> {best_bid:.3f}, "
                f"回撤 {peak_bid - best_bid:.3f}, 净 {pnl_pct:.2f}%)"
            )
        elif hours_left < 2.0 and best_bid <= entry_cost:
            exit_reason = f"结算前资金效率退出 (剩余 {hours_left:.2f}h < 2h, 未上涨)"

        # 峰值回撤退出后，对同一方向做一次"若重扫会怎样"的评估：
        # 只计算不折单，用来回答"退出之后还有多少比例仍满足入场条件"。
        # 这是重入功能的第一步（纯观测），数据落在研究样本 + 审计日志里。
        reentry_probe_mode = os.getenv("REENTRY_PROBE", "trail").strip().lower()
        if exit_reason and reentry_probe_mode != "off":
            if "峰值回撤" in exit_reason or reentry_probe_mode == "all":
                try:
                    probe = evaluate_entry_side(
                        str(pos.get("market_question") or ""),
                        str(pos.get("outcome") or ""),
                        token_id,
                        book,
                        {},
                        data_layer,
                        lambda *args, **kwargs: None,
                    )
                    append_audit_log({
                        "action": "REENTRY_PROBE",
                        "token_id": token_id,
                        "exit_reason": str(exit_reason)[:80],
                        "gate_result": probe.get("reject_reason") or str(probe.get("status", "")).lower(),
                        "roc_z": probe.get("roc_z"),
                        "edge": probe.get("edge"),
                        "spread": book.get("spread"),
                    })
                except Exception as probe_err:
                    append_audit_log({
                        "action": "REENTRY_PROBE_FAILED",
                        "token_id": token_id,
                        "error": str(probe_err)[:160],
                    })

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
                # 余额冲突冷却：该 token 的份额已被其它卖单占用，
                # 再挂新单必然被拒（not enough balance / allowance）。
                if market_circuit.get_breaker().has_balance_conflict(token_id):
                    append_audit_log({
                        "action": "LIVE_BALANCE_CONFLICT_SKIP",
                        "token_id": token_id,
                        "reason": "余额已被其它活跃卖单占用，冷却期内不重复挂单",
                    })
                    unrealized_sum += pnl_usdc
                    remaining_positions.append(pos)
                    continue
                # 刚释放过残留卖单记录的仓位：等下一轮对账把持仓数量更新后再操作，
                # 否则可能按过时份额提交卖单（超卖会被 CLOB 拒，进而触发余额冷却）。
                deferred_at = pos.get("exit_deferred_at")
                if isinstance(deferred_at, (int, float)) and 0 <= (time.time() - deferred_at) < 600:
                    unrealized_sum += pnl_usdc
                    remaining_positions.append(pos)
                    continue
                try:
                    ex = get_live_executor()
                    has_open_sell = order_ledger.has_open_sell_for_token(token_id)
                    live_sell = ex.find_live_sell_order(token_id, refresh=True)
                    # 查询失败 ≠ 没有挂单：必须区分，否则会把"查不到"当成"不存在"
                    orders_query_error = ""
                    if hasattr(ex, "last_orders_query_error"):
                        orders_query_error = str(ex.last_orders_query_error() or "")
                    chain_order_id = ""
                    live_sell_intent = None
                    if live_sell:
                        chain_order_id = str(live_sell.get("id") or "")
                        previous_order = order_ledger.get_order(chain_order_id) or {}
                        live_sell_intent = previous_order.get("intent") or order_ledger.INTENT_TAKE_PROFIT
                        if chain_order_id and live_sell_intent == order_ledger.INTENT_TAKE_PROFIT:
                            pos["tp_order_id"] = chain_order_id
                        order_ledger.record_order(
                            order_id=chain_order_id or f"chain-{token_id[:12]}",
                            token_id=token_id,
                            side="SELL",
                            intent=live_sell_intent,
                            price=float(live_sell.get("price") or 0),
                            shares=float(live_sell.get("original_size") or 0),
                            notional_usdc=round(
                                float(live_sell.get("price") or 0)
                                * float(live_sell.get("original_size") or 0),
                                6,
                            ),
                            event_id=pos.get("event_id", ""),
                            market_question=pos.get("market_question", ""),
                            outcome=pos.get("outcome", ""),
                            status=order_ledger.STATUS_ACCEPTED,
                            notes="链上已存在活跃卖单，已回填本地记录",
                            # 回填会整行覆盖，必须把原有的触发原因带过去，否则每轮回填都会抹掉它
                            reason=str(previous_order.get("reason") or ""),
                        )

                    if exit_reason:
                        # 平仓单超时未成交：撤单后按当前买一重挂。
                        # 旧实现只要链上有 EXIT 单就永久跳过，实测一个仓位挂 15 分钟不成交，
                        # 期间价格继续下跌，最终在 -33% 成交（设定止损是 -10%）。
                        # 只在"挂单价已高于当前买一、确定成交不了"时才重挂，避免行情上行时来回改价。
                        reprice_remaining = None
                        if live_sell and live_sell_intent == order_ledger.INTENT_EXIT:
                            resting_price = float(live_sell.get("price") or 0)
                            original_size = float(live_sell.get("original_size") or 0)
                            matched_size = float(live_sell.get("size_matched") or 0)
                            remaining_size = (
                                max(0.0, original_size - matched_size) if original_size > 0 else shares
                            )
                            order_age = seconds_since_iso(previous_order.get("created_at_utc"))
                            if (
                                order_age is not None
                                and order_age >= cfg["exit_reprice_timeout"]
                                and remaining_size > 1e-6
                                and best_bid < resting_price
                            ):
                                reprice_remaining = remaining_size

                        if reprice_remaining is not None:
                            try:
                                ex.cancel_order(chain_order_id)
                                order_ledger.update_status(
                                    chain_order_id, order_ledger.STATUS_CANCELLED,
                                    notes="平仓单超时未成交，撤单后按现价重挂",
                                )
                                pos["exit_order_id"] = None
                                append_audit_log({
                                    "action": "LIVE_EXIT_REPRICED",
                                    "orderID": chain_order_id,
                                    "token_id": token_id,
                                    "old_price": resting_price,
                                    "best_bid": best_bid,
                                    "age_seconds": round(order_age, 1),
                                    "remaining_shares": round(reprice_remaining, 6),
                                    "reason": exit_reason,
                                })
                                live_sell = None
                                has_open_sell = False
                            except Exception as ce:
                                # 撤不掉旧单就不发新单，否则份额被自己重复占用必然余额不足。
                                # 不 raise：同一故障不在本轮被记两次，下一轮再试。
                                append_audit_log({
                                    "action": "CANCEL_EXIT_FAILED",
                                    "orderID": chain_order_id,
                                    "error": str(ce)[:200],
                                })
                                record_trading_error(
                                    "CLOSE", ce, pos.get("market_question", ""), pos.get("outcome", ""),
                                    context=f"exit_reprice={chain_order_id}",
                                )
                                reprice_remaining = None

                        if live_sell and live_sell_intent == order_ledger.INTENT_EXIT:
                            append_audit_log({
                                "action": "LIVE_EXIT_SKIPPED_DUPLICATE",
                                "token_id": token_id,
                                "reason": exit_reason,
                            })
                        else:
                            if not live_sell and has_open_sell:
                                if orders_query_error:
                                    # 查询失败：无法确认链上是否已有卖单，本轮不卖
                                    append_audit_log({
                                        "action": "LIVE_EXIT_CHAIN_QUERY_FAILED",
                                        "token_id": token_id,
                                        "error": orders_query_error,
                                        "reason": exit_reason,
                                    })
                                    raise ExitDeferred("链上挂单查询失败，本轮不提交平仓")
                                # 查询成功但没有挂单 → 本地是残留记录（多半刚成交，CLOB 已将其移出列表）。
                                # 先解除本地占位，并等下一轮对账把持仓数量更新后再卖，避免按过时份额超卖。
                                released = order_ledger.release_stale_sell_orders(
                                    token_id, "链上查无此单，先解除本地占位，等对账确认持仓"
                                )
                                pos["exit_deferred_at"] = time.time()
                                append_audit_log({
                                    "action": "LIVE_EXIT_DEFERRED_STALE_LEDGER",
                                    "token_id": token_id,
                                    "released_orders": released,
                                    "reason": exit_reason,
                                })
                                raise ExitDeferred("本地残留卖单已释放，等对账更新持仓后再卖")
                            if live_sell:
                                try:
                                    ex.cancel_order(chain_order_id)
                                    order_ledger.update_status(
                                        chain_order_id, order_ledger.STATUS_CANCELLED, notes="触发退出，撤销旧止盈单"
                                    )
                                    if pos.get("tp_order_id") == chain_order_id:
                                        pos["tp_order_id"] = None
                                except Exception as ce:
                                    append_audit_log({"action": "CANCEL_TP_FAILED", "tp_order_id": chain_order_id, "error": str(ce)[:200]})
                                    record_trading_error(
                                        "CANCEL_TP", ce, pos.get("market_question", ""), pos.get("outcome", ""), context=f"tp_order_id={chain_order_id}"
                                    )
                                    raise
                            exit_request_shares = reprice_remaining if reprice_remaining else shares
                            # 滑点预算：只吃 best_bid - MAX_EXIT_SLIPPAGE 以内的深度。
                            # 旧实现取"吃满全部份额后最深一档"作为限价，薄盘时等于主动放弃价差。
                            floor_price = round(max(0.001, float(best_bid) - cfg["max_exit_slippage"]), 4)
                            budget_levels = [(p, s) for p, s in book["raw_bids"] if p >= floor_price]
                            exit_execution = estimate_order_execution(budget_levels, exit_request_shares)
                            exit_shares = min(exit_request_shares, exit_execution["filled_shares"])
                            if exit_shares <= 0:
                                # 预算内无深度：本轮不挂单（不抛异常，避免每轮刷错误），下一轮重新评估
                                append_audit_log({
                                    "action": "LIVE_EXIT_NO_DEPTH",
                                    "token_id": token_id,
                                    "reason": exit_reason,
                                    "floor_price": floor_price,
                                })
                            else:
                                exit_price = round(max(exit_execution["limit_price"], floor_price), 4)
                                tick_size = float(pos.get("tick_size") or 0.01)
                                exit_intent = order_ledger.make_order_intent(
                                    order_ledger.INTENT_EXIT, token_id,
                                    f"{pos.get('opened_at', '')}:{exit_reason}:{exit_price}",
                                )
                                order_ledger.record_order(
                                    order_id=exit_intent["intent_id"],
                                    token_id=token_id,
                                    side="SELL",
                                    intent=order_ledger.INTENT_EXIT,
                                    price=exit_price,
                                    shares=exit_shares,
                                    notional_usdc=round(exit_shares * exit_price, 6),
                                    event_id=pos.get("event_id", ""),
                                    market_question=pos.get("market_question", ""),
                                    outcome=pos.get("outcome", ""),
                                status=order_ledger.STATUS_SUBMITTING,
                                notes=exit_reason[:200],
                                reason=exit_reason[:200],
                            )
                                sell_payload = ex.build_and_sign_poly1271_order(
                                    token_id, "SELL", price=exit_price, size_shares=exit_shares,
                                    tick_size=tick_size, order_type="GTC", salt=exit_intent["salt"],
                                )
                                sell_resp = ex.post_order(sell_payload)
                                exit_order_id = sell_resp.get("orderID") or sell_resp.get("orderId")
                                if not (sell_resp.get("success") in (True, "true", "True") and exit_order_id):
                                    order_ledger.update_status(exit_intent["intent_id"], order_ledger.STATUS_UNKNOWN)
                                    raise RuntimeError(f"实盘卖单未被受理: {str(sell_resp)[:300]}")
                                order_ledger.attach_clob_order_id(exit_intent["intent_id"], exit_order_id)
                                pos["exit_order_id"] = exit_order_id
                                append_audit_log({"action": "LIVE_EXIT_ORDER_ACCEPTED", "orderID": exit_order_id, "reason": exit_reason})
                    else:
                        # 未触发退出条件：维护远端目标单（止盈限价卖单）
                        # 目标价偏离当前规则即撤单重挂——这同时完成了存量持仓从旧口径到 v2 的迁移。
                        tick_size = float(pos.get("tick_size") or 0.01)
                        tolerance = max(tick_size, 0.005)
                        tp_blocked = False
                        if live_sell and live_sell_intent == order_ledger.INTENT_TAKE_PROFIT and chain_order_id:
                            live_price = float(live_sell.get("price") or 0)
                            if abs(live_price - target_prob) > tolerance:
                                try:
                                    ex.cancel_order(chain_order_id)
                                    order_ledger.update_status(
                                        chain_order_id, order_ledger.STATUS_CANCELLED,
                                        notes="目标价与退出规则 v2 不一致，撤单重挂",
                                    )
                                    if pos.get("tp_order_id") == chain_order_id:
                                        pos["tp_order_id"] = None
                                    has_open_sell = False
                                    append_audit_log({
                                        "action": "LIVE_TP_REPRICED",
                                        "orderID": chain_order_id,
                                        "old_price": live_price,
                                        "new_price": target_prob,
                                        "token_id": token_id,
                                    })
                                except Exception as ce:
                                    # 撤不掉旧单就不挂新单：否则份额被自己重复占用，必然余额不足
                                    tp_blocked = True
                                    append_audit_log({"action": "CANCEL_TP_FAILED", "tp_order_id": chain_order_id, "error": str(ce)[:200]})
                                    record_trading_error(
                                        "CANCEL_TP", ce, pos.get("market_question", ""), pos.get("outcome", ""),
                                        context=f"tp_reprice={chain_order_id}",
                                    )
                            else:
                                pos["tp_order_id"] = chain_order_id

                        tp_oid = pos.get("tp_order_id")
                        if not tp_blocked and not tp_oid and not has_open_sell and best_bid < target_prob:
                            tp_price = target_prob
                            tp_intent = order_ledger.make_order_intent(
                                order_ledger.INTENT_TAKE_PROFIT, token_id,
                                f"{pos.get('opened_at', '')}:{tp_price}",
                            )
                            order_ledger.record_order(
                                order_id=tp_intent["intent_id"],
                                token_id=token_id,
                                side="SELL",
                                intent=order_ledger.INTENT_TAKE_PROFIT,
                                price=tp_price,
                                shares=shares,
                                notional_usdc=round(shares * tp_price, 6),
                                event_id=pos.get("event_id", ""),
                                market_question=pos.get("market_question", ""),
                                outcome=pos.get("outcome", ""),
                                status=order_ledger.STATUS_SUBMITTING,
                                notes="目标限价卖单等待提交（退出规则 v2）",
                            )
                            tp_payload = ex.build_and_sign_poly1271_order(
                                token_id, "SELL", price=tp_price, size_shares=shares,
                                tick_size=tick_size,
                                order_type="GTC", salt=tp_intent["salt"],
                            )
                            tp_resp = ex.post_order(tp_payload)
                            tp_order_id = tp_resp.get("orderID") or tp_resp.get("orderId")
                            if tp_resp.get("success") in (True, "true", "True") and tp_order_id:
                                order_ledger.attach_clob_order_id(tp_intent["intent_id"], tp_order_id)
                                pos["tp_order_id"] = tp_order_id
                                append_audit_log({
                                    "action": "LIVE_TP_ORDER_ACCEPTED",
                                    "orderID": tp_order_id,
                                    "price": tp_price,
                                    "token_id": token_id,
                                })
                            else:
                                order_ledger.update_status(tp_intent["intent_id"], order_ledger.STATUS_UNKNOWN)
                                record_trading_error(
                                    "OPEN", f"止盈卖单未被受理: {str(tp_resp)[:200]}",
                                    pos.get("market_question", ""), pos.get("outcome", ""),
                                    context="LIVE_TP_ORDER_FAILED",
                                )
                except ExitDeferred as deferred:
                    # 本轮跳过是预期行为，不是故障：不记 trading_error，避免面板刷红
                    append_audit_log({
                        "action": "LIVE_EXIT_DEFERRED",
                        "token_id": token_id,
                        "detail": str(deferred)[:160],
                    })
                except Exception as le:
                    append_audit_log({"action": "LIVE_EXIT_FAILED", "error": str(le)[:300], "reason": exit_reason})
                    # 若余额/授权不足：说明该 token 的份额已被其它卖单占用，
                    # 再重试只会继续被拒。标记为卖出阻塞，等持仓变化后再处理。
                    if market_circuit.is_balance_error(le):
                        market_circuit.get_breaker().record_balance_conflict(token_id, str(le)[:200])
                        record_trading_error(
                            "CLOSE", le, pos.get("market_question", ""), pos.get("outcome", ""),
                            context=exit_reason or "LIVE_TP",
                            severity="warning",
                        )
                    # 若是"市场不可交易"，记入熔断器并降低严重度：
                    # 这是交易所侧的市场状态，不是账号或代码故障，
                    # 不该在告警面板上刷成 critical 淹没真实问题。
                    elif market_circuit.is_not_tradable_error(le):
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

        # ===== PAPER 分支：按真实买一价模拟平仓 =====
        # 这段逻辑此前位于上方 continue 之后，属于不可达代码，导致纸面模式永远不会平仓。
        if exit_reason:
            pos["paper_remaining_shares"] = 0.0
            pos.pop("paper_order_id", None)
            pos.pop("paper_limit_price", None)
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

            # 反向类账本：纸面平仓时同步处理
            #   hedge  → 卖出反向仓位
            #   maker  → 卖出（未成交则撤单）
            #   reverse→ 不动（持有到结算，由每轮 tick 处理结算）
            try:
                opp_token = ""
                hedge_position = hedge_ledger.find_for_paper(pos.get("pos_id"))
                if hedge_position:
                    opp_token = str(hedge_position.get("token_id") or "")
                if not opp_token:
                    maker_position = reverse_ledger.find_for_paper("maker", pos.get("pos_id"))
                    opp_token = str((maker_position or {}).get("token_id") or "")
                if opp_token:
                    opp_book = data_layer.fetch_orderbook(opp_token)
                    if opp_book:
                        if hedge_position:
                            hedge_ledger.close_for_paper(pos, opp_book)
                        reverse_ledger.close_for_paper("maker", pos, opp_book)
                    else:
                        append_audit_log({"action": "MIRROR_CLOSE_SKIPPED", "reason": "反向盘口缺失", "pos_id": pos.get("pos_id")})
            except Exception as mirror_err:
                append_audit_log({"action": "MIRROR_CLOSE_FAILED", "error": str(mirror_err)[:160]})
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


def write_heartbeat(status: str) -> None:
    payload = {
        "pid": os.getpid(),
        "status": status,
        "mode": "PAPER" if os.getenv("PAPER", "true").lower() == "true" else "LIVE",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    target = heartbeat_file()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def rotate_jsonl_logs() -> None:
    now = datetime.now(timezone.utc)
    for path in (SIGNALS_LOG_FILE, LOGS_DIR / "trading_errors.jsonl"):
        if path.exists() and path.stat().st_size > 0:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            destination = path.with_name(f"{path.stem}-{modified:%Y-%m-%d}{path.suffix}")
            if destination.exists():
                destination = path.with_name(f"{path.stem}-{modified:%Y-%m-%d-%H%M%S}{path.suffix}")
            try:
                path.replace(destination)
            except OSError:
                pass
        cutoff = now.timestamp() - 30 * 86400
        for old in LOGS_DIR.glob(f"{path.stem}-2*.jsonl"):
            if old.stat().st_mtime < cutoff:
                try:
                    old.unlink()
                except OSError:
                    pass


def run_quant_cycle() -> Dict[str, Any]:
    """
    执行一轮完整的 Polymarket 扫描 -> 特征计算 -> 假说检验 -> 风险门过滤 -> 模拟/实盘撮合
    """
    if STOP_FILE.exists():
        print("[紧急停机] 检测到 STOP 文件，已拒绝执行任何扫描与下单！")
        return {}

    rotate_jsonl_logs()
    if is_paper_mode():
        hedge_ledger.initialise_if_missing()
        reverse_ledger.initialise_all()
    write_heartbeat("RUNNING")
    risk = RiskEnvelope()
    # 本轮请求时间预算：防止单个慢请求吃掉整轮时间，
    # 导致巡检节奏被打乱、后续请求堆积、更易触发上游超时（正反馈）。
    budget_seconds = max(60, int(os.getenv("ROUND_BUDGET_SECONDS", "240")))
    rate_limiter.start_round_budget(budget_seconds)
    round_started = time.time()
    try:
        return _run_quant_cycle_inner(risk, budget_seconds, round_started)
    finally:
        write_heartbeat("WAITING")
        rate_limiter.clear_round_budget()


_EVAL_DEDUPE: Dict[str, float] = {}


def record_evaluation_sample(
    question: str,
    token_id: str,
    label: str,
    market: Dict[str, Any],
    p_est: float,
    best_ask: float,
    metrics: Dict[str, Any],
    gate_result: str,
) -> None:
    """记录**每一次**方向评估（不只是成交的那些）。

    为什么：校准检验的对象是"模型的预测准不准"，不是"我们成交的那几笔准不准"。
    只统计成交样本，一是数量太少，二是带着过滤器偏差。
    记录全部评估后，可以用同一个 p_est 与市场结算结果做校准，
    也能拿被跳过的市场做消融测试。

    去重：同一 token + 方向 + 小时只记一次，避免同一市场在滚动扫描里被反复写入。
    """
    hour_key = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    key = f"{token_id}|{label}|{hour_key}"
    if key in _EVAL_DEDUPE:
        return
    now = time.time()
    _EVAL_DEDUPE[key] = now
    if len(_EVAL_DEDUPE) > 5000:
        cutoff = now - 7200
        for old_key in [k for k, ts in _EVAL_DEDUPE.items() if ts < cutoff]:
            _EVAL_DEDUPE.pop(old_key, None)
    append_research_sample({
        "record_type": "EVAL",
        "token_id": token_id,
        "event_id": str(market.get("conditionId") or market.get("id") or ""),
        "question": str(question)[:80],
        "outcome_label": label,
        "p_est": p_est,
        "signal_price": best_ask,
        "execution_price": best_ask,
        "gate_result": gate_result,
        "metrics": metrics,
    })


def evaluate_entry_side(
    question: str,
    label: str,
    token_id: str,
    book: Dict[str, Any],
    market: Dict[str, Any],
    data_layer: "PolymarketDataLayer",
    record_skip: Any,
) -> Dict[str, Any]:
    """对**单个方向**做入场评估（价差 / 深度 / 历史 / 动量 / edge）。

    为什么拆出来：原实现把两侧评估写在一个 for 循环里，"第一个价格落在区间内的方向"
    直接 break —— 而二元市场 YES/NO 价格为 1 的互补关系，区间判定对称，
    于是永远只会选 outcome #0（通常是 Yes）。结果"YES 在跌、NO 在涨"的市场
    （审计日志里 26.9% 的动量拒绝属于这一类）根本没有被评估过。

    返回 status：
      ACCEPT        —— 可入场，携带下游需要的全部指标
      REJECT_MARKET —— 市场级不合格（价差/深度/历史），换另一侧也没意义
      REJECT_SIDE   —— 本侧方向不对（动量为负或 edge 不足），另一侧可能合格
    """
    best_ask = book["best_ask"]
    best_bid = book["best_bid"]
    spread = book["spread"]
    ask_depth = book["ask_depth_usdc"]

    if spread > 0.04:
        record_skip(
            question,
            "WIDE_SPREAD",
            f"[{label}] 盘口价差过大 Spread={spread:.3f} > 0.04 (Bid={best_bid}, Ask={best_ask})",
            {"ask": best_ask, "bid": best_bid, "spread": spread},
        )
        return {"status": "REJECT_MARKET"}

    # 价差必须相对价格足够小：0.25 的合约上 2 分钱就是 8%，任何按百分比/ATR 的止损都会被它吃掉。
    try:
        max_spread_pct = float(os.getenv("MAX_SPREAD_PCT", "0.05"))
    except (TypeError, ValueError):
        max_spread_pct = 0.05
    spread_pct = spread / best_ask if best_ask > 0 else 1.0
    if spread_pct > max_spread_pct:
        record_skip(
            question,
            "SPREAD_TOO_WIDE_RELATIVE",
            f"[{label}] 价差占价格 {spread_pct:.1%} > {max_spread_pct:.1%} "
            f"(Spread={spread:.3f}, Ask={best_ask:.3f})",
            {"ask": best_ask, "bid": best_bid, "spread": spread,
             "spread_pct": round(spread_pct, 4)},
        )
        return {"status": "REJECT_MARKET"}

    if ask_depth < 15.0:
        record_skip(
            question,
            "LOW_DEPTH",
            f"[{label}] 卖盘深度不足 (${ask_depth:.1f} < $15)",
            {"ask": best_ask, "ask_depth": ask_depth},
        )
        return {"status": "REJECT_MARKET"}

    # 只根据真实历史序列计算 Logit 特征；不足时不得合成数据。
    prices = data_layer.fetch_price_history(token_id, book["mid"], market)
    if len(prices) < 10:
        record_skip(question, "MISSING_PRICE_HISTORY", "真实 CLOB 历史样本不足 10 个，跳过")
        return {"status": "REJECT_MARKET"}

    feats = compute_features_from_series(prices)
    roc_z = feats["roc_logit_6"]
    atr_z = feats["atr_z_14"]
    imb = book["imbalance"]

    # 动量信号与盘口失衡合成预期 Logit 增益；单次外推不超过 0.45 logit
    signal_strength = (0.55 * roc_z) + (0.35 * imb * atr_z * 2.5)
    delta_z_est = max(-0.45, min(0.45, signal_strength))
    z_ask = to_logit(best_ask)
    # 不再给"乐观保底"：旧实现写的是 max(0.08, delta_z_est)，
    # 等于无条件假设每笔至少值 +0.08 logit（约 2 个点），
    # 这正好让 edge 门槛形同虚设。文档要求 p 是可校准的概率估计，不是凑门槛的产物。
    p_est = round(from_logit(z_ask + delta_z_est), 4)
    edge, _ev, quarter_kelly = binary_kelly_fraction(p_est, best_ask)
    # 成本门槛：edge 必须覆盖"卖出时再吃一次价差"。
    # 只要求 edge ≥ 0.025 是不够的——在价差 2~4 分的市场上，回程成本就能把优势吃光。
    try:
        min_edge_over_spread = float(os.getenv("MIN_EDGE_OVER_SPREAD", "1.0"))
    except (TypeError, ValueError):
        min_edge_over_spread = 1.0
    spread_floor = round(max(0.0, spread) * max(0.0, min_edge_over_spread), 4)
    metrics_summary = {
        "side": label,
        "token_id": token_id,
        "ask": best_ask,
        "bid": best_bid,
        "spread": spread,
        "spread_floor": spread_floor,
        "depth_usdc": ask_depth,
        "z_curr": feats["z_curr"],
        "roc_logit_6": roc_z,
        "atr_z_14": atr_z,
        "imbalance": imb,
        "p_est": p_est,
        "edge": edge,
        "quarter_kelly": quarter_kelly,
    }

    # 入场规则校验 (Section 5: ROC_logit > 0 或显著盘口正失衡，且 edge 覆盖成本)
    momentum_ok = (roc_z > 0.02 and roc_z >= 0.5 * atr_z) or (imb >= 0.18 and best_ask >= feats["ema_p"])
    reject_reason = None
    if not momentum_ok:
        reject_reason = "momentum"
    elif edge < 0.025:
        reject_reason = "edge"
    elif edge < spread_floor:
        reject_reason = "spread"

    # 无论最终接不接受，都把这次预测记下来（校准/消融的数据源）
    record_evaluation_sample(
        question, token_id, label, market, p_est, best_ask, metrics_summary,
        reject_reason or "accept",
    )

    if reject_reason:
        return {
            "status": "REJECT_SIDE",
            "label": label,
            "roc_z": roc_z,
            "imb": imb,
            "edge": edge,
            "reject_reason": reject_reason,
            "spread_floor": spread_floor,
            "metrics": metrics_summary,
        }

    return {
        "status": "ACCEPT",
        "best_ask": best_ask,
        "best_bid": best_bid,
        "spread": spread,
        "ask_depth": ask_depth,
        "feats": feats,
        "roc_z": roc_z,
        "atr_z": atr_z,
        "imb": imb,
        "p_est": p_est,
        "edge": edge,
        "quarter_kelly": quarter_kelly,
        "metrics": metrics_summary,
    }


def allow_new_entries() -> bool:
    """开仓总开关。默认开启；显式设为 false 时只做对账与持仓管理。"""
    return os.getenv("ALLOW_NEW_ENTRIES", "true").strip().lower() != "false"


def _run_quant_cycle_inner(risk: RiskEnvelope, budget_seconds: int, round_started: float) -> Dict[str, Any]:
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
        except rate_limiter.RoundBudgetExceeded as budget_error:
            # 预算耗尽不是故障，是"本轮该收手"——不记为错误，避免刷屏
            print(f"[预算耗尽] 对账未完成，本轮提前结束: {budget_error}")
            try:
                refresh_dashboard_from_disk()
            except Exception:
                pass
            return {}
        except Exception as reconciliation_error:
            # 冷启动预热：进程刚起来时，代理隧道可能还没完全就绪，
            # 首轮对账容易撞上 connect 卡死。这里原地重试一次
            # （而不是等 180 秒后的下一轮）——实测首轮失败后立即重试即成功。
            if _COLD_START_WARMUP.get("done") is not True and round_started - _PROCESS_START_TS < 300:
                print(f"[冷启动预热] 首轮对账失败，立即重试一次: {reconciliation_error}")
                time.sleep(3)
                # 必须强制重探代理：负缓存 TTL 是 30 秒，直接重试只会拿到同一个"失败"结论
                try:
                    import proxy_detector

                    proxy_detector.get_detector().refresh()
                except Exception:
                    pass
                try:
                    from portfolio_reconciliation import reconcile as reconcile_again

                    state = reconcile_again(apply=True, backup=False)
                    _COLD_START_WARMUP["done"] = True
                    print("[冷启动预热] 重试成功，对账已完成。")
                except Exception as retry_error:
                    record_trading_error(
                        "RECONCILIATION", reconciliation_error,
                        context="本轮已跳过，不修改账本或提交订单（冷启动重试也失败）",
                    )
                    print(f"[对账失败] 本轮已跳过: {retry_error}")
                    try:
                        refresh_dashboard_from_disk()
                    except Exception:
                        pass
                    return {}
            else:
                record_trading_error(
                    "RECONCILIATION", reconciliation_error,
                    context="本轮已跳过，不修改账本或提交订单",
                )
                print(f"[对账失败] 本轮已跳过，不修改账本或提交订单: {reconciliation_error}")
                try:
                    refresh_dashboard_from_disk()
                except Exception as dashboard_error:
                    print(f"[告警页面刷新失败] {dashboard_error}")
                return {}
    else:
        state = load_portfolio(risk)
    cycle_id = str(int(round_started * 1000))
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

    # 定期完整分页建立市场宇宙，再按实际评估数量递增轮转，避免只在头部 60 个市场循环。
    scan_limit = max(1, min(int(os.getenv("SCAN_LIMIT", "60")), 100))
    health = rate_limiter.network_health()
    adaptive_limit = rate_limiter.adaptive_scan_limit(scan_limit)
    if adaptive_limit < scan_limit:
        scan_limit = adaptive_limit
    refresh_seconds = max(300, int(os.getenv("MARKET_UNIVERSE_REFRESH_SECONDS", "3600")))
    # 开仓总开关。文档要求"概率校准通过之前不放真钱"；
    # 关闭后仍会执行对账、止损/止盈管理与空闲补检，只是不再扫描新市场。
    allow_entries = allow_new_entries()
    if allow_entries:
        universe = data_layer.fetch_market_universe(refresh_seconds=refresh_seconds)
        offset = int(load_scan_rotation().get("next_offset") or 0)
        if universe and offset >= len(universe):
            offset = 0
        markets = universe[offset:offset + scan_limit] if universe else []
        if markets:
            save_scan_rotation((offset + len(markets)) % len(universe))
        print(
            f"[扫描窗口] 全宇宙 {len(universe)} 个 | 本轮 {len(markets)} 个 "
            f"(offset={offset}, 网络={health['status']} 成功率={health['success_rate']})"
        )
    else:
        universe, offset, markets = [], 0, []
        print("[开仓暂停] ALLOW_NEW_ENTRIES=false · 本轮只做对账与持仓管理，不扫描新市场", flush=True)

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
    coverage["universe_size"] = len(universe)
    coverage["network_health"] = health["status"]
    coverage["network_success_rate"] = health["success_rate"]
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
    auto_whitelist = set()

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

    evaluated_before = state["stats"]["markets_evaluated"]
    # 持仓加密检查：在扫描间隙插入，不改并发模型（单线程）。
    # 只读已持仓的 CLOB 盘口（每 token 1 次），不碰 Data API——后者才是 429 的来源；
    # 实测 CLOB /book 连续 60 次全 200（Docs/007 第四节）。
    try:
        position_check_every = int(os.getenv("POSITION_CHECK_EVERY_N_MARKETS", "15"))
    except (TypeError, ValueError):
        position_check_every = 15
    for m in markets:
        # 预算耗尽 -> 立即停止本轮剩余扫描，保留下轮重新开始的机会。
        # 这比"继续硬撞"好得多：硬撞只会让链路更紧张，形成正反馈。
        if rate_limiter.budget_exhausted():
            skipped_by_budget = len(markets) - (state["stats"]["markets_evaluated"] - evaluated_before)
            print(
                f"[预算耗尽] 本轮已用 {time.time() - round_started:.0f}s / {budget_seconds}s，"
                f"剩余 {skipped_by_budget} 个市场留到下轮"
            )
            break

        # 每处理 N 个市场补检一次持仓：把止损发现延迟从"整轮"压到"约 1/N 轮"。
        # 退出逻辑本身是幂等的（链上已有平仓单就跳过），重复调用不会重复下单。
        processed_this_round = state["stats"]["markets_evaluated"] - evaluated_before
        if (
            position_check_every > 0
            and processed_this_round > 0
            and processed_this_round % position_check_every == 0
            and state.get("open_positions")
        ):
            try:
                evaluate_existing_positions(state, data_layer)
                # 后台日志重定向到文件是块缓冲，不 flush 的话要攒满 8KB 才可见
                print(
                    f"[持仓加密检查] 第 {processed_this_round} 个市场后补检 "
                    f"{len(state.get('open_positions') or [])} 个持仓",
                    flush=True,
                )
            except Exception as mid_err:
                # 补检失败不能打断扫描；记审计并在下一轮正常检查时兜底
                append_audit_log({
                    "action": "MIDSCAN_POSITION_CHECK_FAILED",
                    "error": str(mid_err)[:200],
                })

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

        # AUTO 模式由后续全部策略门生成白名单；MANUAL 才要求人工 ID 列表。
        events_list = m.get("events") or []
        event_id = str(events_list[0].get("id")) if events_list else str(m.get("conditionId") or m.get("id"))
        if risk.whitelist_mode == "MANUAL":
            if not risk.market_whitelist:
                record_skip(question, "WHITELIST_EMPTY", "手动白名单为空，禁止新开仓")
                continue
            market_keys = {event_id, str(m.get("conditionId") or ""), str(m.get("id") or "")}
            market_keys.update(str(token_id) for token_id in token_ids)
            if not market_keys.intersection(risk.market_whitelist):
                record_skip(question, "MARKET_NOT_WHITELISTED", f"市场未列入白名单 (event_id={event_id})")
                continue

        # 事件相关性去重 (同一 Event 严禁重复下注)
        if event_id in held_event_ids:
            record_skip(question, "CORRELATED_EVENT", f"已持有同事件仓位 (event_id={event_id})")
            continue

        # 检查 YES 与 NO 两个方向，择优分析价格落在 [0.15, 0.85] 的一侧
        # 说明：二元市场 YES 卖一 + NO 卖一 ≈ 1，因此两侧的区间判定是互补的 ——
        # YES 越界必然导致 NO 也越界。该过滤的真实含义是「市场是否已形成明确共识」。
        # 候选方向评估：先看 outcome #0（二元市场里通常是 Yes）。
        # C 方案：只有当 #0 因为"动量方向不对/edge 不足"被拒、且 #0 本身在下行
        # （ROC_z < 0，说明另一侧在上行）时，才额外加载另一侧重新评估。
        # 这样只补"另一个方向才是对的时候"，请求量增量有限，而不是每轮翻倍。
        side_asks: Dict[str, Any] = {}
        picked_evaluation: Optional[Dict[str, Any]] = None
        rejected_side: Optional[Dict[str, Any]] = None
        for idx in (0, 1):
            if idx >= len(token_ids):
                break
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
            if not (0.15 <= book["best_ask"] <= 0.85):
                continue
            evaluation = evaluate_entry_side(question, label, tid, book, m, data_layer, record_skip)
            if evaluation["status"] == "ACCEPT":
                picked_evaluation = dict(evaluation, side_idx=idx, token_id=tid, label=label, book=book)
                break
            if evaluation["status"] == "REJECT_MARKET":
                rejected_side = evaluation
                break
            rejected_side = dict(evaluation, checked_opposite=(idx == 1))
            # 只有 #0 在下行时才值得补评 #1；否则直接放弃这个市场
            if idx == 0 and evaluation["roc_z"] < 0:
                continue
            break

        if picked_evaluation is None:
            if rejected_side and rejected_side["status"] == "REJECT_SIDE":
                suffix = " · 已补评另一侧" if rejected_side.get("checked_opposite") else ""
                if rejected_side.get("reject_reason") == "spread":
                    suffix = f" · 价差成本 {rejected_side.get('spread_floor', 0):.3f} 未被 edge 覆盖" + suffix
                record_skip(
                    question,
                    "NO_EDGE_OR_MOMENTUM",
                    f"[{rejected_side['label']}] 动量或优势不足 "
                    f"(ROC_z={rejected_side['roc_z']:+.3f}, Imb={rejected_side['imb']:+.2f}, "
                    f"Edge={rejected_side['edge']:+.3f}){suffix}",
                    rejected_side["metrics"],
                )
            elif rejected_side is None:
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
                # 带上 token_id：将来要拿"被跳过的市场"做消融/校准，必须有可回溯的标识
                side_asks["token_ids"] = [str(t) for t in token_ids]
                record_skip(question, "OUT_OF_PROB_RANGE", detail, side_asks)
            # REJECT_MARKET 已在 evaluate_entry_side 内记录过，不重复写
            continue

        side_idx = picked_evaluation["side_idx"]
        token_id = picked_evaluation["token_id"]
        outcome_label = picked_evaluation["label"]
        book = picked_evaluation["book"]
        best_ask = picked_evaluation["best_ask"]
        best_bid = picked_evaluation["best_bid"]
        spread = picked_evaluation["spread"]
        ask_depth = picked_evaluation["ask_depth"]
        feats = picked_evaluation["feats"]
        roc_z = picked_evaluation["roc_z"]
        atr_z = picked_evaluation["atr_z"]
        imb = picked_evaluation["imb"]
        p_est = picked_evaluation["p_est"]
        edge = picked_evaluation["edge"]
        quarter_kelly = picked_evaluation["quarter_kelly"]
        metrics_summary = picked_evaluation["metrics"]

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
            if padded_cost <= risk.max_single_bet and padded_cost <= state["cash_usdc"] and padded_cost <= remaining_capacity:
                shares = MIN_SHARES
                size_usdc = padded_cost
            else:
                record_skip(
                    question,
                    "SIZE_BELOW_MIN_SHARES",
                    f"份额 {shares:.2f} < 5 且补足至5份名义 ${padded_cost:.2f} 超过剩余授权容量",
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
            if needed_cost <= risk.max_single_bet and needed_cost <= state["cash_usdc"] and needed_cost <= remaining_capacity:
                shares = needed_shares
                size_usdc = round(needed_cost, 2)
                metrics_summary["order_amount_adjusted"] = round(needed_cost, 4)
            else:
                record_skip(
                    question,
                    "AMOUNT_BELOW_MIN",
                    f"实际下单金额 ${effective_amount:.4f} 低于最小下单额 ${MIN_BUY_AMOUNT_USDC:.2f}，"
                    f"补足至 ${needed_cost:.2f} 会超过剩余授权容量",
                    metrics_summary,
                )
                continue

        try:
            tick_size = float(m.get("orderPriceMinTickSize") or 0.01)
        except (TypeError, ValueError):
            tick_size = 0.0
        if not 0 < tick_size <= 1:
            record_skip(question, "INVALID_TICK_SIZE", f"市场 tick size 无效: {m.get('orderPriceMinTickSize')}")
            continue

        execution = estimate_order_execution(book["raw_asks"], shares)
        if execution["remaining_shares"] > 1e-9 and not risk.paper_mode:
            record_skip(
                question,
                "INSUFFICIENT_DEPTH_FOR_ORDER",
                f"五档卖盘仅能成交 {execution['filled_shares']:.2f}/{shares:.2f} 份，跳过",
                metrics_summary,
            )
            continue
        shares = execution["filled_shares"]
        if shares < 5.0:
            record_skip(question, "SIZE_BELOW_MIN_SHARES", "真实可成交份额不足 5 份", metrics_summary)
            continue
        final_cost = execution["notional_usdc"]
        reserved_cost = execution["limit_price"] * shares
        execution_price = execution["average_price"]
        order_price = execution["limit_price"]
        if reserved_cost > remaining_capacity + 1e-9 or reserved_cost > risk.max_single_bet + 1e-9 or reserved_cost > state["cash_usdc"] + 1e-9:
            record_skip(
                question,
                "FINAL_AMOUNT_EXCEEDS_CAPACITY",
                f"逐档订单最大成本 ${reserved_cost:.4f} 超过剩余授权容量 ${remaining_capacity:.4f}",
                metrics_summary,
            )
            continue
        fee_schedule = m.get("feeSchedule") or {}
        try:
            fee_rate = float(fee_schedule.get("rate") or os.getenv("TRADING_FEE_RATE", "0"))
            fee_exponent = float(fee_schedule.get("exponent") or 1.0)
        except (TypeError, ValueError):
            fee_rate = -1.0
            fee_exponent = 1.0
        if not 0 <= fee_rate < 1 or fee_exponent <= 0:
            record_skip(question, "INVALID_FEE_RATE", f"市场费率配置无效: {fee_schedule}")
            continue
        try:
            max_fee_pct = float(os.getenv("MAX_FEE_PCT", "0.025"))
        except (TypeError, ValueError):
            max_fee_pct = 0.025
        fee_per_share_entry = (
            fee_rate * (execution_price * (1.0 - execution_price)) ** fee_exponent
            if m.get("feesEnabled") else 0.0
        )
        if execution_price > 0 and fee_per_share_entry / execution_price > max_fee_pct:
            record_skip(
                question,
                "FEE_TOO_HIGH",
                f"入场手续费占价格 {fee_per_share_entry / execution_price:.1%} > {max_fee_pct:.1%} "
                f"(rate={fee_rate}, exponent={fee_exponent})",
                metrics_summary,
            )
            continue
        fee_cost = estimate_taker_fee(
            shares, execution_price, fee_rate, fee_exponent, enabled=bool(m.get("feesEnabled"))
        )
        net_edge = p_est - execution_price - (fee_cost / shares)
        annualized_edge = net_edge * (8760.0 / max(hours_left, 1.0))
        min_annual_edge = float(os.getenv("MIN_ANNUAL_EDGE", "0.025"))
        if net_edge < 0.025 or annualized_edge < min_annual_edge:
            record_skip(
                question,
                "EDGE_AFTER_COSTS",
                f"扣滑点费后 edge={net_edge:.4f}，年化={annualized_edge:.4f}，未达门槛",
                metrics_summary,
            )
            continue
        metrics_summary["fee_rate"] = fee_rate
        metrics_summary["fee_cost_usdc"] = round(fee_cost, 5)
        metrics_summary["net_edge"] = round(net_edge, 6)
        metrics_summary["annualized_edge"] = round(annualized_edge, 6)
        size_usdc = final_cost
        z_execution = to_logit(execution_price)
        # 退出规则 v2：目标 = 入场 × (1 + TAKE_PROFIT_RETURN)，封顶 0.99
        exit_cfg = exit_config()
        target_prob = take_profit_price(execution_price, exit_cfg)
        metrics_summary["execution_price"] = round(execution_price, 6)
        metrics_summary["limit_price"] = order_price
        metrics_summary["filled_shares"] = shares
        if risk.whitelist_mode == "AUTO":
            auto_whitelist.add(event_id)
        append_research_sample({
            "record_type": "SIGNAL",
            "sample_id": f"{cycle_id}:{token_id}",
            "token_id": token_id,
            "event_id": event_id,
            "question": question,
            "outcome_label": outcome_label,
            "p_est": p_est,
            "signal_price": best_ask,
            "execution_price": execution_price,
            "limit_price": order_price,
            "hours_left": hours_left,
            "metrics": metrics_summary,
        })
        live_order_id = None
        tp_order_id = None

        # LIVE 只记录订单受理；真实成交、现金和持仓由下一轮只读对账确认。
        if (not risk.paper_mode) and risk.allow_live and not STOP_FILE.exists():
            order_intent = order_ledger.make_order_intent(order_ledger.INTENT_ENTRY, token_id, cycle_id)
            strategy = {
                "target_prob": target_prob,
                "peak_bid": round(max(execution_price, best_bid), 4),
                "exit_rule_version": 2,
                "fee_rate": round(fee_rate, 6),
                "fee_exponent": fee_exponent,
                "z_entry": round(z_execution, 4),
                "z_peak": round(to_logit(best_bid), 4),
                "atr_z": round(float((metrics_summary or {}).get("atr_z_14") or 0.0), 4),
                "spread_at_entry": round(float((metrics_summary or {}).get("spread") or 0.0), 4),
                "tick_size": tick_size,
                "signal_price": best_ask,
                "estimated_execution_price": execution_price,
                "p_est": p_est,
                "estimated_fee_cost_usdc": round(fee_cost, 5),
                "hours_left": hours_left,
                "intent_id": order_intent["intent_id"],
                "salt": order_intent["salt"],
            }
            order_ledger.record_order(
                order_id=order_intent["intent_id"],
                token_id=token_id,
                side="BUY",
                intent=order_ledger.INTENT_ENTRY,
                price=order_price,
                shares=shares,
                notional_usdc=reserved_cost,
                event_id=event_id,
                market_question=question,
                outcome=outcome_label,
                status=order_ledger.STATUS_SUBMITTING,
                notes="订单意图已持久化，等待签名提交",
                strategy=strategy,
            )
            try:
                ex = get_live_executor()
                buy_payload = ex.build_and_sign_poly1271_order(
                    token_id, "BUY", price=order_price, size_shares=shares, tick_size=tick_size,
                    order_type="GTC", salt=order_intent["salt"],
                )
                buy_resp = ex.post_order(buy_payload)
                live_order_id = buy_resp.get("orderID") or buy_resp.get("orderId")
                if not (buy_resp.get("success") in (True, "true", "True") and live_order_id):
                    order_ledger.update_status(
                        order_intent["intent_id"], order_ledger.STATUS_UNKNOWN,
                        notes="买单响应未确认；保持阻塞并先对账",
                    )
                    record_skip(
                        question,
                        "LIVE_BUY_FAILED",
                        f"实盘买单失败: {str(buy_resp)[:180]}",
                        metrics_summary,
                    )
                    record_trading_error("OPEN", f"实盘买单失败: {buy_resp}", question, outcome_label)
                else:
                    order_ledger.attach_clob_order_id(order_intent["intent_id"], live_order_id)
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
                order_ledger.update_status(
                    order_intent["intent_id"], order_ledger.STATUS_UNKNOWN,
                    notes="提交结果未知；保持订单阻塞并等待对账",
                )
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
            "entry_price": execution_price,
            "current_bid": best_bid,
            "current_ask": best_ask,
            "shares": shares,
            "cost_usdc": size_usdc,
            "current_value_usdc": immediate_bid_val,
            "unrealized_pnl_usdc": immediate_spread_cost,
            "unrealized_pnl_pct": round((immediate_spread_cost / size_usdc) * 100.0, 2),
            "p_est": p_est,
            "edge_at_entry": round(net_edge, 4),
            "quarter_kelly": quarter_kelly,
            "target_prob": target_prob,
            "peak_bid": round(max(execution_price, best_bid), 4),
            "exit_rule_version": 2,
            "fee_rate": round(fee_rate, 6),
            "fee_exponent": fee_exponent,
            "z_entry": round(z_execution, 4),
            "z_peak": round(to_logit(best_bid), 4),
            "atr_z": round(float((metrics_summary or {}).get("atr_z_14") or 0.0), 4),
            "spread_at_entry": round(float((metrics_summary or {}).get("spread") or 0.0), 4),
            "tick_size": tick_size,
            "paper_remaining_shares": execution["remaining_shares"] if risk.paper_mode else 0.0,
            "paper_limit_price": order_price if risk.paper_mode and execution["remaining_shares"] > 0 else None,
            "paper_order_id": f"PAPER-{cycle_id}:{token_id}" if risk.paper_mode and execution["remaining_shares"] > 0 else None,
            "expires_at": m.get("endDate") or m.get("endDateIso"),
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "book_updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "book_status": "CLOB",
            "live_order_id": live_order_id,
            "tp_order_id": tp_order_id,
        }

        state["cash_usdc"] = round(state["cash_usdc"] - size_usdc, 4)
        state["open_positions"].append(new_pos)

        # 对冲纸盘：纸面开仓后立刻在反方向建同份额仓位。
        # 不受 edge / 价差 / 持仓数限制，纯粹跟随；资金不足会按可用现金缩量。
        if risk.paper_mode:
            try:
                opp_idx = 1 - int(side_idx)
                opp_token = str(token_ids[opp_idx])
                opp_label = outcomes[opp_idx] if opp_idx < len(outcomes) else f"Outcome_{opp_idx}"
                opp_book = data_layer.fetch_orderbook(opp_token)
                if opp_book:
                    hedge_ledger.mirror_open(new_pos, opp_token, opp_label, opp_book, fee_rate, fee_exponent)
                    reverse_ledger.mirror_open("reverse", new_pos, opp_token, opp_label, opp_book, fee_rate, fee_exponent)
                    reverse_ledger.mirror_open("maker", new_pos, opp_token, opp_label, opp_book, fee_rate, fee_exponent)
                else:
                    append_audit_log({"action": "HEDGE_OPEN_SKIPPED", "reason": "反向盘口缺失", "token_id": opp_token})
            except Exception as hedge_err:
                append_audit_log({"action": "HEDGE_OPEN_FAILED", "error": str(hedge_err)[:160]})
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

    state["auto_whitelist"] = {
        "mode": risk.whitelist_mode,
        "event_ids": sorted(auto_whitelist),
        "count": len(auto_whitelist),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    state["recent_decisions"] = (cycle_decisions + state.get("recent_decisions", []))[:40]
    curve_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    state["equity_curve"].append(
        {
            "timestamp": curve_timestamp,
            "equity": state["total_equity_usdc"],
        }
    )
    state["equity_curve"] = state["equity_curve"][-100:]
    # 长期历史不受 100 点上限约束，供面板 年/月/日/时 聚合使用
    append_equity_history(curve_timestamp, state["total_equity_usdc"])
    cache_stats = price_history_cache_stats()
    cache_total = cache_stats["hit"] + cache_stats["miss"]
    if cache_total:
        print(
            f"[价格历史缓存] 命中 {cache_stats['hit']}/{cache_total} "
            f"({cache_stats['hit'] / cache_total:.0%})",
            flush=True,
        )
    save_portfolio(state)
    generate_dashboard_html(state)

    # 反向/对冲类账本：每轮盯市 + 独立净值历史 + 独立面板（由纸面进程代为生成）
    if is_paper_mode():
        try:
            for kind in ("reverse", "maker"):
                mirror_state = reverse_ledger.tick(kind, data_layer)
                append_equity_history(curve_timestamp, mirror_state["total_equity_usdc"], key=kind)
                mirror_view = dict(mirror_state)
                for shared in ("stats", "daily_stats", "recent_decisions", "scan_coverage",
                               "auto_whitelist", "reconciliation", "wallet_address", "last_updated"):
                    if shared in state:
                        mirror_view[shared] = state[shared]
                generate_dashboard_html(mirror_view, mode_key=kind)
        except Exception as mirror_err:
            append_audit_log({"action": "MIRROR_REFRESH_FAILED", "error": str(mirror_err)[:160]})
        try:
            hedge_state = hedge_ledger.mark_all(data_layer)
            append_equity_history(curve_timestamp, hedge_state["total_equity_usdc"], key="hedge")
            hedge_view = dict(hedge_state)
            for shared in ("stats", "daily_stats", "recent_decisions", "scan_coverage",
                           "auto_whitelist", "reconciliation", "wallet_address", "last_updated"):
                if shared in state:
                    hedge_view[shared] = state[shared]
            generate_dashboard_html(hedge_view, mode_key="hedge")
        except Exception as hedge_err:
            append_audit_log({"action": "HEDGE_REFRESH_FAILED", "error": str(hedge_err)[:160]})
    # 交给空闲补检复用：同一进程、同一会话、同一代理通道
    _CYCLE_CONTEXT["state"] = state
    _CYCLE_CONTEXT["data_layer"] = data_layer
    return state


def wait_with_position_checks(total_seconds: float) -> None:
    """把轮间空闲等待切成小块，每块之间补检一次持仓。

    为什么需要这一步：一轮的实际结构是「对账(约60s) + 扫描(约90s) + 等待(约180s)」。
    只在扫描里加密检查，最坏情况仍要等到下一轮开头（等待+对账约 4 分钟）才被看到。
    把等待切块后，最坏空窗降到约 2 分钟。

    单线程实现：不新增线程，因此不需要给 rate_limiter 加锁，也不会与扫描/下单并发。
    只读已持仓的 CLOB 盘口，不碰 Data API（429 的来源）。
    """
    try:
        chunk = float(os.getenv("IDLE_CHECK_INTERVAL_SECONDS", "60"))
    except (TypeError, ValueError):
        chunk = 60.0
    if chunk <= 0:
        time.sleep(total_seconds)
        return
    remaining = max(0.0, float(total_seconds))
    while remaining > 0:
        step = min(chunk, remaining)
        time.sleep(step)
        remaining -= step
        if remaining <= 0 or STOP_FILE.exists():
            break
        state = _CYCLE_CONTEXT.get("state")
        data_layer = _CYCLE_CONTEXT.get("data_layer")
        if not state or data_layer is None or not state.get("open_positions"):
            continue
        try:
            evaluate_existing_positions(state, data_layer)
            save_portfolio(state)
            print(
                f"[空闲补检] 剩余 {remaining:.0f}s，已补检 "
                f"{len(state.get('open_positions') or [])} 个持仓",
                flush=True,
            )
        except Exception as err:
            # 补检失败不影响下一轮；记审计并继续等待
            append_audit_log({"action": "IDLE_POSITION_CHECK_FAILED", "error": str(err)[:200]})
            print(f"[空闲补检失败] {str(err)[:120]}", flush=True)


# ==========================================================
# 净值历史与时间粒度聚合（供面板折线图的 年/月/日/时 切换使用）
# ==========================================================
def _parse_curve_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value).replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _curve_points(raw: Any) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        # 同时兼容账本格式 {timestamp, equity} 与历史文件内部格式 {t, e}
        timestamp = str(item.get("timestamp") or item.get("t") or "").strip()
        raw_equity = item.get("equity", item.get("e"))
        try:
            equity = float(raw_equity)
        except (TypeError, ValueError):
            continue
        if not timestamp or not math.isfinite(equity):
            continue
        points.append({"t": timestamp, "e": round(equity, 4)})
    return points


def clean_equity_points(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剔除异常净值点（如对账瞬间写出的 1.0），用邻域中位数替换。

    旧实现只处理"孤立单点"，连续 3 个异常值会整体通过，把纵轴从 1.0 拉到 15.0，
    整条曲线被压平。这里改用滑动窗口中位数做局部离群检测。
    """
    if not points:
        return []
    values = [p["e"] for p in points]
    half = 5
    out: List[Dict[str, Any]] = []
    for i, point in enumerate(points):
        window = sorted(values[max(0, i - half):min(len(values), i + half + 1)])
        local_median = window[len(window) // 2]
        equity = point["e"]
        if local_median > 0 and equity < local_median * 0.5:
            equity = round(local_median, 4)
        out.append({"t": point["t"], "e": equity})
    return out


def load_equity_history(key: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取长期净值历史（每轮一行 JSONL）。

    首次运行时从 `state/portfolio_before_*.json` 与当前账本播种。
    只播种与当前模式一致的账本——纸面账本的净值混进实盘曲线会严重误导。
    """
    rows: List[Dict[str, Any]] = []
    history_file = equity_history_file(key)
    if history_file.exists():
        try:
            with open(history_file, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.extend(_curve_points([json.loads(line)]))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            rows = []
    if not rows and (key or current_mode_key()) == "hedge":
        # 对冲账本从 $20 起算，绝不继承实盘/纸面的历史曲线
        return []
    if not rows:
        wanted = "PAPER" if os.getenv("PAPER", "true").lower() == "true" else "LIVE"
        for path in sorted(STATE_DIR.glob("portfolio_before_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            mode = str(payload.get("mode") or "").upper()
            if mode and wanted not in mode:
                continue
            rows.extend(_curve_points(payload.get("equity_curve")))
        try:
            rows.extend(_curve_points(json.loads(portfolio_path().read_text(encoding="utf-8")).get("equity_curve")))
        except (OSError, json.JSONDecodeError):
            pass
        try:
            seed: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                seed[row["t"]] = row
            with open(history_file, "w", encoding="utf-8") as handle:
                for key in sorted(seed):
                    row = seed[key]
                    handle.write(json.dumps(
                        {"timestamp": row["t"], "equity": row["e"]}, ensure_ascii=False
                    ) + "\n")
        except OSError:
            pass
    deduped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        deduped[row["t"]] = row
    return [deduped[key] for key in sorted(deduped)]


def append_equity_history(timestamp: str, equity: float, key: Optional[str] = None) -> None:
    """追加一轮净值；同一时间戳只写一次，失败静默（图表不该影响交易）。"""
    try:
        history_file = equity_history_file(key)
        if history_file.exists() and history_file.stat().st_size > 0:
            with open(history_file, "rb") as handle:
                handle.seek(max(0, history_file.stat().st_size - 256))
                tail = handle.read().decode("utf-8", "ignore").strip().splitlines()
            if tail:
                try:
                    if str(json.loads(tail[-1]).get("timestamp")) == timestamp:
                        return
                except json.JSONDecodeError:
                    pass
        with open(history_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"timestamp": timestamp, "equity": round(float(equity), 4)}, ensure_ascii=False
            ) + "\n")
    except OSError:
        pass


def _bucket_label(dt: datetime, bucket_seconds: int, same_day: bool) -> str:
    """按聚合粒度给 X 轴刻度：粒度越粗、跨度越大，标签带的信息越多。"""
    if bucket_seconds <= 0 or bucket_seconds < 3600:
        return dt.strftime("%H:%M") if same_day else dt.strftime("%m-%d %H:%M")
    if bucket_seconds < 86400:
        return dt.strftime("%H:00") if same_day else dt.strftime("%m-%d %H:%M")
    if bucket_seconds < 7 * 86400:
        return dt.strftime("%m-%d")
    return dt.strftime("%Y-%m")


def bucket_equity_curve(points: List[Dict[str, Any]], granularity: str) -> List[Dict[str, Any]]:
    """按 tab 选择时间窗口，窗口内自动选最细且点数不超上限的聚合粒度。

    窗口锚定在**最后一个数据点**而不是当前时钟：引擎停机时仍能看到停机前那一段，
    不会因为"最近几小时没有新数据"变成空图。

    自动选粒度是为了在"历史还很短"时不丢细节（原始点就够画），
    在"历史变长"后不画成一片色块（自动升到 15 分钟 / 1 小时 / 1 天）。
    """
    windows = {key: window for key, window in EQUITY_WINDOWS}
    if granularity not in windows:
        return []
    parsed: List[Any] = []
    for point in points:
        dt = _parse_curve_ts(point["t"])
        if dt is not None:
            parsed.append((dt, float(point["e"])))
    if not parsed:
        return []
    parsed.sort(key=lambda item: item[0])
    cutoff = parsed[-1][0] - timedelta(seconds=windows[granularity])
    windowed = [(dt, equity) for dt, equity in parsed if dt >= cutoff]
    if not windowed:
        return []

    def aggregate(seconds: int) -> List[Any]:
        if seconds <= 0:
            return list(windowed)
        buckets: Dict[int, Any] = {}
        for dt, equity in windowed:
            buckets[int(dt.timestamp() // seconds)] = (dt, equity)
        return [buckets[key] for key in sorted(buckets)]

    chosen = EQUITY_BUCKET_LADDER[-1]
    chosen_points = aggregate(chosen)
    for rung in EQUITY_BUCKET_LADDER:
        candidate = aggregate(rung)
        if len(candidate) <= EQUITY_POINT_CAP:
            chosen, chosen_points = rung, candidate
            break

    same_day = chosen_points[0][0].date() == chosen_points[-1][0].date()
    return [
        {
            "t": dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "e": equity,
            "label": _bucket_label(dt, chosen, same_day),
        }
        for dt, equity in chosen_points
    ]


# ==========================================================
# 可视化 HTML 监控面板生成器 (双击即可在浏览器查看盈亏)
# ==========================================================
def generate_dashboard_html(state: Dict[str, Any], mode_key: Optional[str] = None) -> None:
    from html import escape

    shown_key = mode_key or current_mode_key()

    open_rows = ""
    for p in state.get("open_positions", []):
        pending = float(p.get("pending_shares") or 0.0)
        filled = float(p.get("shares") or 0.0)
        is_pending = pending > 0 and filled <= 0
        if is_pending:
            # 还没成交就没有买入价，也不能算浮盈亏；挂单价放在旁边做参考，不冒充 Ask
            limit = float(p.get("limit_price") or 0.0)
            entry_cell = f'<span class="sub">—</span> <span class="sub">挂单 {limit:.3f}</span>'
            cost_cell = f'<span class="sub">—</span> <span class="sub">计划 {pending:.1f} 份</span>'
            value_cell = '<span class="sub">—</span>'
            pnl_cell = '<span class="sub">待成交</span>'
            pnl_cls = ""
        else:
            pnl = p["unrealized_pnl_usdc"]
            pnl_cls = "pos" if pnl >= 0 else "neg"
            entry_cell = f"{p['entry_price']:.3f}"
            cost_cell = f'${p["cost_usdc"]:.2f} <span class="sub">({p["shares"]:.1f}份)</span>'
            if pending > 0:
                cost_cell += f' <span class="sub">另有 {pending:.1f} 份待成交</span>'
            value_cell = f'${p["current_value_usdc"]:.2f}'
            if p.get("exit_order"):
                value_cell += f' <span class="sub">平仓挂单 @{float(p["exit_order"].get("limit_price") or 0):.3f}</span>'
            pnl_cell = f'${pnl:+.2f} ({p["unrealized_pnl_pct"]:+.1f}%)'
        open_rows += f"""
        <tr>
            <td><span class="badge badge-side">{p['outcome']}</span></td>
            <td class="q-cell" title="{p['market_question']}">{p['market_question']}</td>
            <td>{entry_cell}</td>
            <td>{p['current_bid']:.3f} <span class="sub">(Ask {p['current_ask']:.3f})</span></td>
            <td>{p.get('p_est', 0.0):.3f} <span class="sub">(+{p.get('edge_at_entry', 0.0):.3f})</span></td>
            <td>{cost_cell}</td>
            <td>{value_cell}</td>
            <td class="{pnl_cls}">{pnl_cell}</td>
            <td class="sub">{p['opened_at']}</td>
            <td class="sub">{p.get('book_updated_at') or p.get('last_updated') or state.get('last_updated') or ''}{(' · ' + p['book_status']) if p.get('book_status') and p.get('book_status') != 'CLOB' else ''}{(' · 检查于 ' + p['book_checked_at']) if p.get('book_status') == 'UNAVAILABLE' and p.get('book_checked_at') else ''}</td>
        </tr>
        """
    if not open_rows:
        open_rows = '<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:20px;">当前无持仓（风控门正在扫描符合 Logit 动量与优势阈值的盘口）</td></tr>'

    # 挂单盘已改为"开仓即按卖一成交、跟随纸面平仓"；pending 只用于旧版遗留或现金不足。
    entry_head = "买入价 Ask"
    if shown_key == "maker":
        positions_note = ("反向跟单：纸面开仓时立即按反方向卖一价成交，纸面平仓时按买一价跟着卖出。"
                          "「待成交」只会出现在旧版遗留挂单或现金不足时。")
    else:
        positions_note = "按真实买一价估值；初始浮亏反映盘口买卖价差。"

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
    # 数据源换成长期历史文件，这样 年 / 月 / 日 三个粒度才有数据可聚合。
    chart_points = load_equity_history(shown_key)
    if not chart_points:
        chart_points = _curve_points(state.get("equity_curve"))
    cleaned = clean_equity_points(chart_points)
    if not cleaned:
        cleaned = [{
            "t": str(state.get("last_updated") or "now"),
            "e": float(state.get("total_equity_usdc", 0)),
        }]

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
            lab = points[i].get("label") or points[i]["t"].replace(" UTC", "")[-8:]
            xlabels.append(
                f'<text x="{x_at(i):.1f}" y="{h - 10}" text-anchor="middle" fill="#94a3b8" font-size="11">{lab}</text>'
            )
        # 点太多时不再画实心点：150 个 2.6px 的圆会连成一条色块，比线还粗
        if n <= 80:
            dots = "".join(
                f'<circle class="chart-dot" cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{stroke}" pointer-events="none" />'
                for x, y in coords
            )
        else:
            dots = ""
        # 每个点额外画一个透明的命中圆：可见圆点只有 2.6px，直接 hover 很难对准。
        # 命中半径按点间距自适应，点密时自动收窄，避免相邻点互相遮挡。
        plot_w = w - pad_l - pad_r
        hit_r = max(3.0, min(9.0, plot_w / max(1, n - 1) / 2.0))
        hit_parts = []
        for (x, y), point in zip(coords, points):
            tip = str(point["t"]).replace(" UTC", "")
            hit_parts.append(
                f'<circle class="chart-hit" cx="{x:.1f}" cy="{y:.1f}" r="{hit_r:.1f}" '
                f'fill="transparent" pointer-events="all" '
                f'data-value="${point["e"]:.2f}" data-tip="{tip}" />'
            )
        hits = "".join(hit_parts)
        return (
            f'<svg viewBox="0 0 {w} {h}" width="100%" height="260" preserveAspectRatio="none" role="img" aria-label="equity curve">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#0b1220" rx="8"/>'
            + "".join(ticks)
            + f'<polyline fill="{stroke}33" stroke="none" points="{area}"></polyline>'
            + f'<polyline fill="none" stroke="{stroke}" stroke-width="2.5" points="{line}"></polyline>'
            + dots
            + hits
            + "".join(xlabels)
            + "</svg>"
        )

    # 四个时间窗口的 tab 与视图（年 / 月 / 日 / 时）
    gran_labels = {
        "year": ("年", "最近 365 天 · 自动选择粒度，最多 150 点"),
        "month": ("月", "最近 30 天 · 自动选择粒度，最多 150 点"),
        "day": ("日", "最近 24 小时 · 自动选择粒度，最多 150 点"),
        "hour": ("时", "最近 2 小时 · 原始巡检点"),
    }
    gran_tabs = ""
    gran_views = ""
    for gran, _ in EQUITY_WINDOWS:
        tab_label, tab_title = gran_labels[gran]
        selected = "true" if gran == "hour" else "false"
        gran_tabs += (
            f'<button type="button" role="tab" data-gran-tab="{gran}" '
            f'aria-selected="{selected}" title="{tab_title}">{tab_label}</button>'
        )
        gran_points = bucket_equity_curve(cleaned, gran)
        hidden = "" if gran == "hour" else " hidden"
        if len(gran_points) < 2:
            hint = "该时间窗内暂无数据" if not gran_points else "该时间窗内仅有 1 个数据点，运行后自动补齐"
            body = f'<div class="chart-empty">{hint}</div>'
        else:
            body = build_equity_svg(gran_points)
        gran_views += f'<div class="chart-view" data-gran-view="{gran}"{hidden}>{body}</div>'

    total_pnl = round(state["total_equity_usdc"] - state["initial_capital_usdc"], 4)
    generated_at_iso = datetime.now(timezone.utc).isoformat()
    pnl_color = "#10b981" if total_pnl >= 0 else "#f43f5e"
    backend_stopped = STOP_FILE.exists()

    # 实盘 / 纸面 / 对冲 三块面板切换：各自生成自己的 HTML，这里只做导航。
    # 目标面板最近 20 分钟没更新就标灰，避免点过去看到空白页。
    def _tab_class(key: str) -> str:
        if key == shown_key:
            return "active"
        target = dashboard_file(key)
        alive = target.exists() and (time.time() - target.stat().st_mtime) < 1200
        return "" if alive else "dim"

    mode_tabs = (
        '<span class="mode-tabs" role="group" aria-label="面板切换">'
        f'<a class="{_tab_class("live")}" href="dashboard.html">实盘</a>'
        f'<a class="{_tab_class("paper")}" href="dashboard_paper.html">纸面</a>'
        f'<a class="{_tab_class("hedge")}" href="dashboard_hedge.html">对冲</a>'
        f'<a class="{_tab_class("reverse")}" href="dashboard_reverse.html">反向持有</a>'
        f'<a class="{_tab_class("maker")}" href="dashboard_maker.html">反向挂单</a>'
        "</span>"
    )

    # 在途挂单账本 + 单实例锁状态（用于确认没有重复实例、没有悬挂卖单）
    try:
        if shown_key in ("live", "maker"):
            pending_summary = order_ledger.summary()
        else:
            # 对冲/反向持有账本没有链上订单，不能把实盘账本的挂单数挂在它们头上
            pending_summary = {"total": 0, "by_status": {}, "blocking": 0,
                               "open_buy_notional_usdc": 0.0, "open_sell_notional_usdc": 0.0}
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
        else "无可用代理（直连已禁用）"
    )
    proxy_exit_text = (
        f"{proxy_exit.get('ip','')} · {proxy_exit.get('country','')} {proxy_exit.get('city','')}"
        if proxy_exit.get("ip")
        else "出口信息未知"
    )

    pending_rows = ""
    if shown_key == "maker":
        # 反向挂单盘的挂单记在它自己的账本里，不在 order_ledger（那是链上实盘账本的）
        maker_pending = [
            p for p in state.get("open_positions", [])
            if float(p.get("pending_shares") or 0.0) > 0
        ]
        for item in maker_pending[:20]:
            pending_rows += f"""
        <tr>
            <td><code>{str(item.get('paper_pos_id') or '')[:18]}</code></td>
            <td><span class="badge">BUY</span></td>
            <td>反向跟单</td>
            <td class="q-cell">{item.get('market_question','')}</td>
            <td>{float(item.get('limit_price') or 0):.3f}</td>
            <td>{float(item.get('pending_shares') or 0):.2f}</td>
            <td>${float(item.get('pending_shares') or 0) * float(item.get('limit_price') or 0):.2f}</td>
            <td><span class="badge badge-skip">待成交</span></td>
            <td class="sub">{item.get('book_updated_at','')}</td>
        </tr>
            """
        pending_summary = {"blocking": len(maker_pending), "total": len(maker_pending)}
        if not pending_rows:
            pending_rows = '<tr><td colspan="9" style="text-align:center;color:#94a3b8;padding:18px;">当前无挂单（没有等待成交的反向买单）</td></tr>'
    else:
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
            if shown_key == "live":
                pending_rows = '<tr><td colspan="9" style="text-align:center;color:#94a3b8;padding:18px;">当前无在途挂单（所有订单均已离簿并经只读对账确认）</td></tr>'
            else:
                pending_rows = '<tr><td colspan="9" style="text-align:center;color:#94a3b8;padding:18px;">该账本不使用链上挂单（无在途订单）</td></tr>'

    lock_text = (
        "不适用（纸面 / 对冲模式不申请单实例锁）"
        if shown_key != "live"
        else (
            f"已锁定 PID {lock_info.get('pid')}（{lock_info.get('role','')}，启动于 {lock_info.get('started_at_utc','')}）"
            if lock_info.get("held") and lock_info.get("alive")
            else ("锁文件存在但持锁进程已退出（陈旧锁）" if lock_info.get("held") else "未持有锁（当前为一次性运行或后台未启动）")
        )
    )

    # 扫描覆盖统计：量化"评估次数"与"覆盖的不同市场数"的差距
    coverage = state.get("scan_coverage") or {}
    if coverage.get("rounds"):
        distinct = int(coverage.get("distinct_markets_seen", 0))
        evaluations = int(coverage.get("total_evaluations", 0))
        avg = evaluations / max(1, coverage.get("rounds", 1))
        coverage_block = (
            f'<div class="kpi-sub" style="margin-top:6px;">'
            f'全宇宙 {coverage.get("universe_size", 0)} 个 · 累计扫描 {distinct} 个 · 平均每轮 {avg:.0f} 个 · '
            f'窗口 limit={coverage.get("last_limit")} offset={coverage.get("last_offset")}'
            f"</div>"
        )
    else:
        coverage_block = ""

    # 限流与退避状态：让"为什么本轮失败"在面板上一目了然
    try:
        limiter_state = rate_limiter.rate_limit_snapshot()
        health_state = rate_limiter.network_health()
        # 样本为 0 时不要显示"成功率 100%"：那是除零后的默认值，会误导成网络很健康。
        if int(health_state.get("sample") or 0) == 0:
            network_health_text = "尚无请求样本 · 等待下一轮"
        else:
            network_health_text = (
                f"{health_state['status']} · 成功率 {health_state['success_rate']:.0%} "
                f"(最近 {health_state['sample']} 次请求)"
            )
        rate_rows = "".join(
            f"<tr><td>{host}</td><td>{info.get('min_interval')}s</td>"
            f"<td>{info.get('seconds_ago')}s 前</td></tr>"
            for host, info in limiter_state.items()
        )
        rate_block = (
            "<div style='background:rgba(56,138,221,.10);border:1px solid rgba(56,138,221,.35);"
            "border-radius:10px;padding:12px 16px;margin-bottom:14px;font-size:13px;color:#cbd5e1;'>"
            "<strong style='color:#93c5fd;'>限流保护：</strong>"
            "已启用 Retry-After 退避 + 指数退避 + 巡检抖动 + 单轮时间预算"
            + (f"<table style='margin-top:8px;font-size:12px;'>{rate_rows}</table>" if rate_rows else "")
            + "</div>"
        )
    except Exception:
        rate_block = ""
        network_health_text = "未知"

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
    # 顶部状态条：连接状态 + 运行态元信息合并成一行。
    # 原先单实例锁 / 在途名义 / 网络通道 / 限流 / 网络健康 五行占掉了挂单面板约三分之一高度，
    # 合并到顶部后，挂单表格可以直接多显示 3~4 行。
    success_time = escape(str(reconciliation.get("reconciled_at") or ""))
    success_stamp = ""
    if last_success is not None:
        success_stamp = last_success.astimezone(timezone.utc).strftime("%m-%d %H:%M")
    success_age = (datetime.now(timezone.utc) - last_success).total_seconds() if last_success else None
    if backend_stopped:
        status_state, status_title = "danger", "后台已断开 · 自动交易已停止"
        status_detail = "当前仅展示最后一次对账快照；链上订单与资产不会因此自动撤销。"
    elif shown_key == "hedge":
        status_state, status_title = "paper", "对冲纸盘运行中"
        status_detail = "反向跟随纸面 · 模拟撮合 · 不参与链上对账"
    elif shown_key == "reverse":
        status_state, status_title = "paper", "反向持有盘运行中"
        status_detail = "只镜像开仓 · 持有到结算 · 不跟随平仓"
    elif shown_key == "maker":
        status_state, status_title = "paper", "反向跟单盘运行中"
        status_detail = "开仓即按卖一成交 · 纸面平仓时跟随卖出 · 保证每笔都有反向仓"
    elif shown_key == "paper":
        status_state, status_title = "paper", "纸面模式运行中"
        status_detail = "模拟撮合 · 不参与链上对账 · 成交价按真实买一卖一"
    elif reconciliation_failed_after_success:
        status_state, status_title = "danger", "对账失败"
        status_detail = "最近一次完整对账之后出现对账错误，请查看下方告警卡片。"
    elif success_age is not None and 0 <= success_age <= 600:
        status_state, status_title = "success", "系统连接正常"
        status_detail = f"Data API · Polygon RPC · CLOB 只读通过 · 对账 {success_stamp}"
    else:
        status_state, status_title = "warning", "连接状态待确认"
        status_detail = "最近没有足够新的完整对账结果，当前显示历史快照。"

    entry_paused = not allow_new_entries()

    status_hover = status_detail
    if status_state == "success" and success_time:
        status_hover = f"{status_detail}（完整时间 {success_time}）"

    status_meta = f"""
        <div class="status-meta">
          <span><b>单实例锁</b><span class="status-text" title="{escape(lock_text)}">{escape(lock_text)}</span></span>
          <span><b>在途名义</b>买 ${pending_summary.get('open_buy_notional_usdc', 0):.2f} · 卖 ${pending_summary.get('open_sell_notional_usdc', 0):.2f}</span>
          <span><b>网络通道</b><span class="status-text" title="{escape(proxy_text)} · {escape(proxy_exit_text)}">{escape(proxy_text)} · {escape(proxy_exit_text)}</span></span>
          <span><b>限流保护</b>Retry-After 退避 · 指数退避 · 巡检抖动</span>
          <span><b>网络健康</b><span class="status-text" title="最近 20 次请求的成功率；网络不稳时系统会自动收缩每轮扫描量">{escape(network_health_text)}</span></span>
        </div>"""

    status_flags = '<span class="status-flag">开仓已暂停</span>' if entry_paused else ""

    status_strip = f"""
      <div class="status-strip {status_state}" role="status">
        <span class="banner-dot"></span>
        <strong>{status_title}</strong>
        {status_flags}
        <span class="status-headline" title="{escape(status_hover)}">{status_detail}</span>
        {status_meta}
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
            runtime_meta = ""
            if event.get("pid"):
                route = event.get("proxy_url") or event.get("proxy_source") or "通道未选择"
                runtime_meta = escape(f" · PID {event['pid']} · {event.get('code_version') or 'unknown'} · {route}")
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
          <div class="error-record-meta">{side} · {source} {source_record}{runtime_meta}</div>
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
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CjxkZWZzPjxsaW5lYXJHcmFkaWVudCBpZD0iZyIgeDE9IjAiIHkxPSIwIiB4Mj0iMSIgeTI9IjEiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzM0ZDM5OSIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzA0Nzg1NyIvPjwvbGluZWFyR3JhZGllbnQ+PC9kZWZzPgo8cmVjdCB3aWR0aD0iNjQiIGhlaWdodD0iNjQiIHJ4PSIxNCIgZmlsbD0idXJsKCNnKSIvPgo8cG9seWxpbmUgcG9pbnRzPSIxMiw0NCAyNCwzMyAzNCwzOSA1MCwyMSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZjBmZGY0IiBzdHJva2Utd2lkdGg9IjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIvPgo8Y2lyY2xlIGN4PSI1MCIgY3k9IjIxIiByPSI2LjUiIGZpbGw9IiNmYmJmMjQiIHN0cm9rZT0iIzA2NWY0NiIgc3Ryb2tlLXdpZHRoPSIyIi8+Cjwvc3ZnPg==">
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
  .signals-wide {{ grid-column: 2 / -1; }}
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
  .gran-tabs {{ flex: 0 0 auto; display: inline-flex; gap: 2px; padding: 2px; border: 1px solid var(--border); border-radius: 8px; background: #0b1220; }}
  .gran-tabs button {{ appearance: none; border: 0; background: transparent; color: var(--muted); font-family: inherit; font-size: 10px; line-height: 1; padding: 5px 9px; border-radius: 6px; cursor: pointer; transition: background .15s ease, color .15s ease; }}
  .gran-tabs button:hover {{ color: #dce6f1; }}
  .gran-tabs button[aria-selected="true"] {{ background: rgba(56, 189, 248, .16); color: #7dd3fc; }}
  .mode-tabs {{ display: inline-flex; gap: 2px; padding: 2px; border: 1px solid var(--border); border-radius: 8px; background: #0b1220; }}
  .mode-tabs a {{ text-decoration: none; color: var(--muted); font-size: 10px; line-height: 1; padding: 4px 9px; border-radius: 6px; }}
  .mode-tabs a:hover {{ color: #dce6f1; }}
  .mode-tabs a.active {{ background: rgba(56, 189, 248, .16); color: #7dd3fc; }}
  .mode-tabs a.dim {{ opacity: .42; }}
  .chart-view {{ height: 100%; }}
  .chart-view[hidden] {{ display: none; }}
  .chart-empty {{ display: flex; align-items: center; justify-content: center; height: 100%; min-height: 150px; color: var(--muted); font-size: 11px; }}
  .chart-dot {{ opacity: .92; }}
  .data-stale .kpi-card, .data-stale .panel {{ opacity: .78; }}
  .chart-hit {{ cursor: crosshair; }}
  .chart-tip {{
    position: fixed; z-index: 60; pointer-events: none; padding: 6px 9px; border-radius: 8px;
    background: rgba(9, 15, 26, .97); border: 1px solid #2b3a4d; box-shadow: 0 10px 26px rgba(0, 0, 0, .5);
    color: #bac7d5; font-size: 10px; line-height: 1.5; white-space: nowrap;
    transform: translate(-50%, -100%);
  }}
  .chart-tip b {{ display: block; color: #7dd3fc; font-size: 12px; font-weight: 650; }}
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
  .status-strip {{
    display: flex; align-items: center; flex-wrap: wrap; gap: 2px 9px;
    min-height: 34px; padding: 7px 11px; border: 1px solid var(--border); border-radius: 9px;
    background: var(--card); color: var(--muted); font-size: 11px;
  }}
  .status-strip strong {{ color: var(--text); font-size: 11px; white-space: nowrap; }}
  .status-headline {{ flex: 0 1 auto; min-width: 0; max-width: 52ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .status-strip.success {{ border-color: rgba(52, 211, 153, .24); background: rgba(52, 211, 153, .045); }}
  .status-strip.success .banner-dot {{ background: var(--green); box-shadow: 0 0 0 3px rgba(52, 211, 153, .08); }}
  .status-strip.success strong {{ color: #6ee7b7; }}
  .status-strip.paper {{ border-color: rgba(56, 189, 248, .28); background: rgba(56, 189, 248, .05); }}
  .status-strip.paper .banner-dot {{ background: var(--accent); }}
  .status-strip.paper strong {{ color: #7dd3fc; }}
  .status-flag {{ flex: 0 0 auto; padding: 1px 7px; border-radius: 999px; border: 1px solid rgba(251, 191, 36, .35); background: rgba(251, 191, 36, .08); color: #fcd34d; font-size: 9px; white-space: nowrap; }}
  .status-strip.warning {{ border-color: rgba(251, 191, 36, .28); background: rgba(251, 191, 36, .05); }}
  .status-strip.warning .banner-dot {{ background: var(--orange); }}
  .status-strip.warning strong {{ color: #fcd34d; }}
  .status-strip.danger {{ border-color: rgba(251, 113, 133, .32); background: rgba(251, 113, 133, .06); }}
  .status-strip.danger .banner-dot {{ background: var(--red); }}
  .status-strip.danger strong {{ color: #fda4af; }}
  .status-meta {{
    flex: 1 1 auto; min-width: 0; margin-left: auto;
    display: flex; flex-wrap: wrap; align-items: baseline; justify-content: flex-end; gap: 2px 16px;
  }}
  .status-meta > span {{ display: inline-flex; align-items: baseline; gap: 6px; min-width: 0; font-size: 10px; }}
  .status-meta b {{ flex: 0 0 auto; color: #91a5bb; font-weight: 600; white-space: nowrap; }}
  .status-meta .status-text {{ min-width: 0; max-width: 30ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
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
    .signals-wide {{ grid-column: 1 / -1; }}
    .system-banner {{ align-items: flex-start; flex-wrap: wrap; }}
    .system-banner > span:last-child {{ width: 100%; white-space: normal; }}
    .status-strip {{ align-items: flex-start; }}
    .status-headline {{ max-width: 100%; white-space: normal; }}
    .status-meta {{ justify-content: flex-start; }}
  }}
</style>
</head>
<body data-generated-at="{generated_at_iso}">
<div class="app-shell">
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">PM</div>
      <div>
        <div class="title-line"><h1>Polymarket</h1><span class="mode-pill">{state['mode']}</span>{mode_tabs}</div>
        <div class="brand-subtitle">量化交易监控终端 · Best Bid 真实盘口计价</div>
      </div>
    </div>
    <div class="header-meta">
      <div class="wallet"><span class="meta-label">专属钱包</span><code title="{state['wallet_address']}">{state['wallet_address']}</code></div>
      <div><span class="meta-label">最后更新 · 15 秒自动刷新</span><div class="updated-time">{state['last_updated']}</div></div>
    </div>
  </header>

  <div class="notice-stack">{status_strip}{error_notice}</div>

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
        <div><h2>账户净值走势</h2><p>Equity curve · portfolio.json</p></div>
        <div class="gran-tabs" role="tablist" aria-label="净值时间粒度">{gran_tabs}</div>
      </div>
      <div class="chart-wrap">{gran_views}</div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div><h2>当前持仓明细</h2><p>Open positions</p></div>
        <span class="panel-meta">Best Bid 计价</span>
      </div>
      <div class="panel-note">{positions_note}</div>
      <div class="scroll-region" data-scroll-key="positions" tabindex="0" role="region" aria-label="当前持仓明细，可滚动">
        <table class="positions-table">
          <thead><tr><th>方向</th><th>预测市场问题</th><th>{entry_head}</th><th>现买一价 Bid</th><th>模型概率 Edge</th><th>投入本金</th><th>清算市值</th><th>实时浮盈亏</th><th>建仓时间</th><th>盘口更新</th></tr></thead>
          <tbody>{open_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div><h2>在途挂单账本</h2><p>Pending orders</p></div>
        <span class="panel-meta">{pending_summary.get('blocking',0)} 阻塞 / {pending_summary.get('total',0)} 总计</span>
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

    <section class="panel{'' if error_section else ' signals-wide'}">
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
<div id="chart-tip" class="chart-tip" hidden></div>
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

  // 净值图时间粒度切换：年 / 月 / 日 / 时。
  // 页面每 15 秒整页刷新一次，所以选择必须存到 sessionStorage 才能跨刷新保留。
  const granTabs = document.querySelectorAll('[data-gran-tab]');
  const granViews = document.querySelectorAll('[data-gran-view]');
  const granKey = 'pm-dashboard:equity-gran';
  const applyGran = (gran) => {{
    granViews.forEach((view) => {{ view.hidden = view.dataset.granView !== gran; }});
    granTabs.forEach((tab) => {{ tab.setAttribute('aria-selected', String(tab.dataset.granTab === gran)); }});
  }};
  let savedGran = 'hour';
  try {{ savedGran = sessionStorage.getItem(granKey) || 'hour'; }} catch (_) {{}}
  applyGran(savedGran);
  granTabs.forEach((tab) => {{
    tab.addEventListener('click', () => {{
      const gran = tab.dataset.granTab;
      try {{ sessionStorage.setItem(granKey, gran); }} catch (_) {{}}
      applyGran(gran);
    }});
  }});

  // 净值图悬浮提示：hover 命中圆显示该点的时间与净值。
  // 用事件委托挂在 svg 上，避免给每个点单独绑定监听器。
  const chartTip = document.getElementById('chart-tip');
  const hideTip = () => {{ if (chartTip) chartTip.hidden = true; }};
  document.querySelectorAll('[data-gran-view] svg').forEach((svg) => {{
    svg.addEventListener('mouseover', (event) => {{
      const hit = event.target.closest('.chart-hit');
      if (!hit || !chartTip) return;
      const box = hit.getBoundingClientRect();
      const strong = document.createElement('b');
      strong.textContent = hit.dataset.value || '';
      chartTip.textContent = '';
      chartTip.appendChild(strong);
      chartTip.appendChild(document.createTextNode(hit.dataset.tip || ''));
      chartTip.hidden = false;
      // 贴边时把浮层拉回视窗内，避免在小面板里被裁掉
      const left = Math.min(Math.max(box.left + box.width / 2, 76), window.innerWidth - 76);
      const top = Math.max(box.top - 6, 62);
      chartTip.style.left = left + 'px';
      chartTip.style.top = top + 'px';
    }});
    svg.addEventListener('mouseleave', hideTip);
  }});
  window.addEventListener('scroll', hideTip, {{ passive: true }});

  // 页面自检：引擎停止后 HTML 不再生成，但浏览器仍每 15 秒重载同一份文件，
  // 顶栏会一直显示最后一次的状态（例如"系统连接正常"）。这里用生成时间判断过期。
  const generatedAt = document.body.dataset.generatedAt;
  if (generatedAt) {{
    const ageSec = (Date.now() - new Date(generatedAt).getTime()) / 1000;
    if (ageSec > 600) {{
      const strip = document.querySelector('.status-strip');
      if (strip) {{
        strip.className = 'status-strip danger';
        const dot = document.createElement('span');
        dot.className = 'banner-dot';
        const strong = document.createElement('strong');
        strong.textContent = '监控数据已过期 ' + Math.round(ageSec / 60) + ' 分钟';
        const detail = document.createElement('span');
        detail.className = 'status-headline';
        detail.textContent = '引擎可能已停止：页面仍在刷新，但数据不再更新。请检查进程或重新运行启动器。';
        strip.textContent = '';
        strip.appendChild(dot);
        strip.appendChild(strong);
        strip.appendChild(detail);
        document.body.classList.add('data-stale');
      }}
    }}
  }}

}})();
</script>
</body>
</html>
"""
    clean_html = "\n".join(line.rstrip() for line in html.splitlines()) + "\n"
    dashboard_file(shown_key).write_text(clean_html, encoding="utf-8")


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
    parser.add_argument("--paper", action="store_true", help="以纸面模式运行；可与实盘进程同时运行")
    args = parser.parse_args()

    if args.paper:
        # 纸面进程用独立的账本 / 心跳 / 面板 / 净值历史，不会覆盖实盘文件
        os.environ["PAPER"] = "true"

    # 同模式只允许一个实例：纸面没有单实例锁，两个进程会同时写同一本账与面板，
    # 表现为"面板一会儿新格式一会儿旧格式"、持仓被覆盖。这里在启动时直接退出。
    duplicates = existing_engine_processes(is_paper_mode())
    if duplicates:
        pids = ", ".join(str(proc.pid) for proc in duplicates)
        print(f"[单实例保护] 已有同模式引擎在运行（PID {pids}），本进程退出。")
        raise SystemExit(0)

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
            except SingleInstanceLost as err:
                print(f"[单实例锁丢失] {err}")
                break
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
            # 等待期间分块补检持仓：把"最坏 4 分钟才被看到"压到约 2 分钟。
            # 复用本轮同一个 data_layer，不重建代理通道。
            wait_with_position_checks(delay)
    else:
        st = run_quant_cycle()
        if st:
            print(
                f"单次扫描完成 | 净值: ${st['total_equity_usdc']:.2f} | "
                f"持仓: {len(st['open_positions'])}/4 | "
                f"面板已生成: {dashboard_file(current_mode_key())}"
            )
