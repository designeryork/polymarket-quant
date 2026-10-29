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
    **kwargs: Any,
) -> requests.Response:
    """带限流感知重试的请求。

    * 429 —— 遵守 `Retry-After` 等待后重试；
    * 5xx —— 指数退避 + 抖动后重试；
    * 网络异常 —— 指数退避 + 抖动后重试；
    * 4xx（除 429）—— 立即抛出（重试无意义）。
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_attempts):
        if throttle:
            _sleep_until_allowed(url)
        try:
            response = session.request(method, url, **kwargs)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt >= max_attempts - 1:
                raise
            delay = min(base_backoff * (2 ** attempt), max_backoff) * (0.7 + random.random() * 0.6)
            time.sleep(delay)
            continue

        if response.status_code == 429:
            if attempt >= max_attempts - 1:
                return response  # 交给调用方按 fail-closed 处理
            delay = _retry_after_seconds(response) * (2 ** attempt)
            delay = min(delay, max_backoff)
            time.sleep(delay)
            continue

        if response.status_code >= 500:
            if attempt >= max_attempts - 1:
                return response
            delay = min(base_backoff * (2 ** attempt), max_backoff) * (0.7 + random.random() * 0.6)
            time.sleep(delay)
            continue

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
