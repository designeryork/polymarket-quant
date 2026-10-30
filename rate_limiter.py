"""
限流感知的 HTTP 重试层 (rate_limiter.py)
======================================
Polymarket 的 Data API 存在**突发型限流**：短时间内重复请求会返回
HTTP 429 + `Retry-After: 1`，几秒后即恢复。

实测（2026-10-29）：
  * 连续 30 次 positions 请求 → 首次即 429（Retry-After=1），其余 29 次 200；
  * 429 响应**没有** X-RateLimit-* 头，只有一个 Retry-After；
  * CLOB 的 orderbook 接口连续 60 次全部 200，限流主要发生在 Data API。

旧行为的问题
------------
对账代码对 429 **零处理**：不读 Retry-After、不退避、不 sleep。
撞上限流窗口时，本轮直接失败（fail-closed 是对的），
但下一轮又以完全相同的节奏重来，于是反复撞同一堵墙。
更糟的是：撞墙 → 本轮跳过 → 跳过时点集中在固定间隔上 → 更容易形成规律性流量。

设计
----
* `request_with_retry()` —— 统一入口，遇 429 读 `Retry-After` 等待后重试；
  5xx / 网络错误走指数退避（含抖动）；
* `throttle_guard()` —— 进程内最小请求间隔，防止单轮内密集打同一接口；
* `jittered_sleep()` —— 巡检间隔加随机抖动，打散规律性流量。
"""
import random
import time
from typing import Any, Callable, Dict, Optional, Tuple

import requests

# 全局节流状态（按 host 维度记录下次可发起请求的时刻）
_LAST_REQUEST_AT: Dict[str, float] = {}

# 各接口的最小请求间隔（秒）—— 依据实测速率反推，留出安全余量
MIN_INTERVAL = {
    "data-api.polymarket.com": 0.35,
    "clob.polymarket.com": 0.05,
    "gamma-api.polymarket.com": 0.20,
}

# ==========================================================
# 轮次时间预算
# ==========================================================
# 实测：单轮巡检含 180+ 请求（对账 3 + Gamma 1 + 盘口 120 + 历史 60）。
# 正常情况下需 43~96s。但一旦某个请求超时：
#   盘口 timeout=8s x 4 次重试 = 最坏 45.7s
#   对账 timeout=25s x 4 次重试 = 最坏 113.7s
# 单个慢请求就能吃掉整轮时间，导致巡检节奏被打乱、
# 后续请求挤在一起，反而更容易触发上游限流与超时（正反馈）。
#
# 因此引入**轮次预算**：预算耗尽后立即停止重试并快速失败，
# 让本轮剩余部分跳过，保留下轮重新开始的机会。
_DEADLINE: Optional[float] = None
_ROUND_BUDGET_SECONDS = 240.0


def start_round_budget(budget_seconds: float = _ROUND_BUDGET_SECONDS) -> None:
    """开启新一轮的请求时间预算。"""
    global _DEADLINE
    _DEADLINE = time.time() + budget_seconds


def clear_round_budget() -> None:
    """结束本轮预算（下一轮会重新开启）。"""
    global _DEADLINE
    _DEADLINE = None


def round_budget_remaining() -> Optional[float]:
    """返回本轮剩余预算秒数；None 表示未设预算。"""
    if _DEADLINE is None:
        return None
    return max(0.0, _DEADLINE - time.time())


def budget_exhausted() -> bool:
    """预算是否已耗尽。"""
    remaining = round_budget_remaining()
    return remaining is not None and remaining <= 0


class RoundBudgetExceeded(RuntimeError):
    """本轮请求时间预算耗尽。

    与普通网络异常区分开：这不是"请求失败"，而是"本轮该收手了"。
    捕获它时应当跳过剩余工作，而不是重试。
    """


def _host_of(url: str) -> str:
    try:
        return url.split("/")[2].lower()
    except Exception:
        return url.lower()


def _sleep_until_allowed(url: str) -> None:
    """按 host 维度的最小间隔节流。"""
    host = _host_of(url)
    gap = MIN_INTERVAL.get(host, 0.1)
    last = _LAST_REQUEST_AT.get(host, 0.0)
    wait = gap - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST_AT[host] = time.time()


def _retry_after_seconds(response: Optional[requests.Response], default: float = 2.0) -> float:
    """从响应头读取 Retry-After（秒）。缺失或非法时返回默认值。"""
    if response is None:
        return default
    raw = response.headers.get("Retry-After")
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    # 上限 60 秒，避免被异常头值卡死
    return max(0.2, min(value, 60.0))


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_attempts: int = 4,
    base_backoff: float = 1.5,
    max_backoff: float = 30.0,
    throttle: bool = True,
    respect_budget: bool = True,
    connect_timeout: Optional[float] = None,
    read_timeout: Optional[float] = None,
    **kwargs: Any,
) -> requests.Response:
    """带限流感知重试的请求。

    * 429 —— 遵守 `Retry-After` 等待后重试；
    * 5xx —— 指数退避 + 抖动后重试；
    * 网络异常 —— 指数退避 + 抖动后重试；
    * 4xx（除 429）—— 立即抛出（重试无意义）；
    * **轮次预算耗尽 —— 立即抛 RoundBudgetExceeded，不再重试**
      （这是"本轮该收手"的信号，不是"再试一次也许能成"）。

    connect_timeout / read_timeout
    --------------------------------
    实测（2026-10-30）：唯一可用通道是本地代理（直连 data-api 在国内超时），
    而代理的 CONNECT 隧道**偶发卡死**——正常建立只需 0.15s，
    卡住时则整整耗尽 connect timeout（原先 25s）。
    一次卡死就吃掉 25 秒，一轮 180+ 请求中若有几个卡死，
    整轮节奏就被彻底打乱。

    因此把超时拆开：
    * connect_timeout —— 隧道建立用，给小值（如 8s）快速失败并换节点/换通道；
    * read_timeout    —— 等待响应用，给较大值（如 25s）。
    requests 的 timeout 参数支持 (connect, read) 元组。
    """
    last_error: Optional[Exception] = None

    # 拆分连接与读取超时
    if connect_timeout is not None or read_timeout is not None:
        original = kwargs.get("timeout")
        if isinstance(original, tuple):
            base_connect, base_read = original
        elif original is None:
            base_connect = base_read = None
        else:
            base_connect = base_read = original
        kwargs["timeout"] = (
            connect_timeout if connect_timeout is not None else base_connect,
            read_timeout if read_timeout is not None else base_read,
        )

    for attempt in range(max_attempts):
        # 预算检查：本轮时间不够了就别再重试，快速失败让本轮收尾
        if respect_budget and budget_exhausted():
            raise RoundBudgetExceeded(
                f"本轮请求时间预算已耗尽（剩余 {round_budget_remaining() or 0:.0f}s），跳过剩余请求"
            )
        if throttle:
            _sleep_until_allowed(url)
        try:
            response = session.request(method, url, **kwargs)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as exc:
            record_request_result(False)
            last_error = exc
            if attempt >= max_attempts - 1:
                raise
            # 退避也要看预算：退避到预算耗尽就放弃
            delay = min(base_backoff * (2 ** attempt), max_backoff) * (0.7 + random.random() * 0.6)
            if respect_budget:
                remaining = round_budget_remaining()
                if remaining is not None and delay >= remaining:
                    raise RoundBudgetExceeded(
                        f"退避 {delay:.0f}s 将超出本轮剩余预算 {remaining:.0f}s，放弃重试"
                    ) from exc
            time.sleep(delay)
            continue

        if response.status_code == 429:
            record_request_result(False)
            if attempt >= max_attempts - 1:
                return response  # 交给调用方按 fail-closed 处理
            delay = _retry_after_seconds(response) * (2 ** attempt)
            delay = min(delay, max_backoff)
            if respect_budget:
                remaining = round_budget_remaining()
                if remaining is not None and delay >= remaining:
                    raise RoundBudgetExceeded(
                        f"限流退避 {delay:.0f}s 将超出本轮剩余预算 {remaining:.0f}s，放弃重试"
                    )
            time.sleep(delay)
            continue

        if response.status_code >= 500:
            record_request_result(False)
            if attempt >= max_attempts - 1:
                return response
            delay = min(base_backoff * (2 ** attempt), max_backoff) * (0.7 + random.random() * 0.6)
            if respect_budget:
                remaining = round_budget_remaining()
                if remaining is not None and delay >= remaining:
                    raise RoundBudgetExceeded(
                        f"5xx 退避 {delay:.0f}s 将超出本轮剩余预算 {remaining:.0f}s，放弃重试"
                    )
            time.sleep(delay)
            continue

        record_request_result(True)
        return response

    if last_error:
        raise last_error
    return session.request(method, url, **kwargs)


def jittered_sleep(base_seconds: int, jitter_ratio: float = 0.25) -> float:
    """带抖动的等待，打散规律性请求节奏。

    固定间隔（如每 180 秒一次）会形成规律性流量特征，
    更容易与限流窗口对齐。加上 ±25% 的随机抖动后，
    请求在时间轴上散开，撞限流的概率显著下降。
    """
    if base_seconds <= 0:
        return 0.0
    spread = base_seconds * jitter_ratio
    delay = base_seconds + random.uniform(-spread, spread)
    return max(5.0, delay)


# ==========================================================
# 网络健康度与自适应扫描量
# ==========================================================
# 网络不稳时，固定 60 个市场的扫描量会持续给上游加压，
# 让本就紧张的链路更容易超时（正反馈）。
# 这里记录最近的网络失败率，据此自动收缩本轮扫描量。
_HEALTH_WINDOW = 20          # 保留最近多少次请求的结果
_success_streak = 0
_health: list = []           # True=成功 / False=失败


def record_request_result(ok: bool) -> None:
    """记录一次请求结果，用于计算网络健康度。"""
    global _success_streak
    _health.append(bool(ok))
    if len(_health) > _HEALTH_WINDOW:
        del _health[: len(_health) - _HEALTH_WINDOW]
    _success_streak = _success_streak + 1 if ok else 0


def network_health() -> Dict[str, Any]:
    """返回网络健康度摘要。"""
    if not _health:
        return {"success_rate": 1.0, "sample": 0, "status": "unknown"}
    rate = sum(_health) / len(_health)
    if rate >= 0.95:
        status = "healthy"
    elif rate >= 0.7:
        status = "degraded"
    else:
        status = "poor"
    return {"success_rate": round(rate, 3), "sample": len(_health), "status": status}


def adaptive_scan_limit(base_limit: int, min_limit: int = 15) -> int:
    """按网络健康度收缩本轮扫描量。

    * healthy —— 用满 base_limit（默认 60）；
    * degraded —— 降到 60%；
    * poor    —— 降到 min_limit（默认 15）。

    目的是让网络不稳时的请求压力自动下降，给链路喘息空间。
    网络恢复后会自动回到满额。
    """
    health = network_health()
    status = health["status"]
    if status == "healthy":
        return base_limit
    if status == "degraded":
        return max(min_limit, int(base_limit * 0.6))
    return min_limit


def rate_limit_snapshot() -> Dict[str, Any]:
    """返回节流状态快照，供面板展示。"""
    now = time.time()
    return {
        host: {
            "last_request_at": round(last, 2),
            "seconds_ago": round(now - last, 1),
            "min_interval": MIN_INTERVAL.get(host, 0.1),
        }
        for host, last in _LAST_REQUEST_AT.items()
    }
