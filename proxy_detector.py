"""
代理自动探测 (proxy_detector.py)
==============================
自动找出当前可用的网络出口通道，避免因环境变量被无关进程污染而全军覆没。

问题背景
--------
Windows 上通常同时存在两类本地代理：
1. 用户自己的代理客户端（如 FlClash / Clash / v2rayN）——浏览器读注册表用它；
2. 工具链注入的环境变量（`http_proxy` / `https_proxy`）——可能指向完全不同的端口。

实测事故：环境变量指向 `127.0.0.1:2166`（某工具链自带的 HTTP 转发通道），
该端口对 Polymarket 返回 `502 Bad Gateway`，而浏览器实际使用的
`127.0.0.1:7890`（FlClash）一切正常。结果是浏览器飞快、脚本全面超时。

探测顺序
--------
1. Windows 注册表 `ProxyServer`（浏览器实际使用的设置，最可信）
2. 环境变量中的代理地址
3. 常见本地代理端口（7890 / 7897 / 10809 / 1080 / 10808 / 8888 / 8118 ...）
4. 直连（trust_env=False）

每个候选都用一个**轻量只读**请求验证（只取 1 条数据的 positions 接口），
选定第一个能真正连通 Polymarket 的通道。
"""
import os
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

# 验证用的只读接口：只取 1 条数据，负载极小，且不会产生任何交易动作
PROBE_URL = "https://data-api.polymarket.com/positions?user=0x0000000000000000000000000000000000000000&limit=1"

# 常见本地代理端口（按国内用户使用频率排序）
COMMON_PROXY_PORTS = [7890, 7897, 7891, 10809, 1080, 10808, 8888, 8118, 1081, 33210]

# 出口信息查询（用于展示当前出口 IP 与地区）
IPINFO_URL = "https://ipinfo.io/json"


def _read_windows_registry_proxy() -> List[str]:
    """读取 Windows 注册表中的系统代理设置（浏览器实际使用的那个）。"""
    proxies: List[str] = []
    try:
        import winreg  # type: ignore

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            try:
                enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            except OSError:
                enabled = 0
            if enabled:
                try:
                    raw = str(winreg.QueryValueEx(key, "ProxyServer")[0])
                except OSError:
                    raw = ""
                # 形如 "127.0.0.1:7890" 或 "http=127.0.0.1:7890;https=127.0.0.1:7890"
                for part in re.split(r"[;,]", raw):
                    part = part.strip()
                    if not part:
                        continue
                    if "=" in part:
                        part = part.split("=", 1)[1].strip()
                    if part.startswith("http"):
                        part = re.sub(r"^https?://", "", part)
                    if part and ":" in part:
                        proxies.append(f"http://{part}")
    except Exception:
        pass
    return proxies


def _read_env_proxies() -> List[str]:
    """读取环境变量中的代理（可能来自无关进程，需验证）。"""
    found: List[str] = []
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.getenv(name, "").strip()
        if value and value not in found:
            found.append(value)
    return found


def _port_is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def candidate_proxies() -> List[Dict[str, str]]:
    """列出候选通道（按优先级），并标注来源便于排查。"""
    candidates: List[Dict[str, str]] = []
    seen = set()

    def add(url: str, source: str) -> None:
        if not url:
            return
        normalized = url.rstrip("/")
        if normalized in seen:
            return
        seen.add(normalized)
        candidates.append({"url": normalized, "source": source})

    for url in _read_windows_registry_proxy():
        add(url, "系统注册表(浏览器所用)")
    for url in _read_env_proxies():
        add(url, "环境变量")
    for port in COMMON_PROXY_PORTS:
        if _port_is_open("127.0.0.1", port):
            add(f"http://127.0.0.1:{port}", "常见端口扫描")

    return candidates


def _probe_url(url: Optional[str], timeout: float = 12.0) -> Tuple[bool, str, float]:
    """用只读请求验证通道是否真正连通 Polymarket。"""
    session = requests.Session()
    session.trust_env = False
    if url:
        session.proxies = {"http": url, "https": url}
    started = time.time()
    try:
        response = session.get(PROBE_URL, timeout=timeout)
        elapsed = time.time() - started
        if response.status_code == 200:
            return True, f"HTTP {response.status_code}", elapsed
        return False, f"HTTP {response.status_code}", elapsed
    except requests.exceptions.ProxyError as exc:
        return False, f"ProxyError: {str(exc)[:80]}", time.time() - started
    except requests.exceptions.ConnectTimeout:
        return False, "ConnectTimeout", time.time() - started
    except requests.exceptions.ReadTimeout:
        return False, "ReadTimeout", time.time() - started
    except requests.exceptions.ConnectionError as exc:
        return False, f"ConnectionError: {str(exc)[:80]}", time.time() - started
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:80]}", time.time() - started
    finally:
        try:
            session.close()
        except Exception:
            pass


def fetch_exit_info(url: Optional[str], timeout: float = 15.0) -> Dict[str, Any]:
    """查询当前出口 IP 与地区（仅用于面板展示，不影响交易逻辑）。"""
    session = requests.Session()
    session.trust_env = False
    if url:
        session.proxies = {"http": url, "https": url}
    try:
        response = session.get(IPINFO_URL, timeout=timeout)
        if response.status_code != 200:
            return {}
        data = response.json()
        return {
            "ip": data.get("ip", ""),
            "country": data.get("country", ""),
            "region": data.get("region", ""),
            "city": data.get("city", ""),
            "org": data.get("org", ""),
        }
    except Exception:
        return {}
    finally:
        try:
            session.close()
        except Exception:
            pass


class ProxyDetector:
    """探测并缓存可用的网络通道。"""

    def __init__(self) -> None:
        self.selected_url: Optional[str] = None
        self.source: str = ""
        self.latency: float = 0.0
        self.checked_at: Optional[str] = None
        self.attempts: List[Dict[str, Any]] = []
        self.exit_info: Dict[str, Any] = {}
        self._resolved = False

    def detect(self, force: bool = False, probe_timeout: float = 12.0) -> Dict[str, Any]:
        """执行一次探测，返回结果摘要。

        顺序：注册表 -> 环境变量 -> 常见端口 -> 直连。
        直连放在最后，因为很多地区直连 Polymarket 会被限速或阻断。
        """
        if self._resolved and not force:
            return self.summary()

        self.attempts = []
        for candidate in candidate_proxies():
            ok, detail, elapsed = _probe_url(candidate["url"], timeout=probe_timeout)
            self.attempts.append(
                {
                    "url": candidate["url"],
                    "source": candidate["source"],
                    "ok": ok,
                    "detail": detail,
                    "latency_s": round(elapsed, 2),
                }
            )
            if ok:
                self.selected_url = candidate["url"]
                self.source = candidate["source"]
                self.latency = elapsed
                break

        if self.selected_url is None:
            ok, detail, elapsed = _probe_url(None, timeout=probe_timeout)
            self.attempts.append(
                {"url": "(直连)", "source": "直连回退", "ok": ok, "detail": detail, "latency_s": round(elapsed, 2)}
            )
            if ok:
                self.selected_url = None
                self.source = "直连(未使用代理)"
                self.latency = elapsed

        self._resolved = True
        from datetime import datetime, timezone

        self.checked_at = datetime.now(timezone.utc).isoformat()
        if self.selected_url:
            self.exit_info = fetch_exit_info(self.selected_url)
        return self.summary()

    def refresh(self, probe_timeout: float = 8.0) -> Dict[str, Any]:
        """强制重新探测可用通道。

        用于通道卡死时的自愈：代理节点可能已经切换、端口可能已变，
        或者先前选中的通道本身失效了。此时重新走一遍候选列表，
        有机会落到一个仍然可用的通道上。

        直连在多数地区不可用（实测 data-api 直连 12s 超时），
        因此这里不把直连当作有效回退 —— 回退的真实价值在于
        "换一条代理通道"，而不是"不走代理"。
        """
        previous = self.selected_url
        self._resolved = False
        self.selected_url = None
        self.source = ""
        result = self.detect(force=True, probe_timeout=probe_timeout)
        if self.selected_url != previous:
            print(
                f"[通道自愈] 代理通道已切换: {previous or '无'} -> {self.selected_url or '直连'}"
            )
        return result

    def session(self) -> requests.Session:
        """构建使用已选通道的会话（trust_env=False，避免被污染的环境变量干扰）。"""
        session = requests.Session()
        session.trust_env = False
        if self.selected_url:
            session.proxies = {"http": self.selected_url, "https": self.selected_url}
        return session

    def summary(self) -> Dict[str, Any]:
        return {
            "proxy_url": self.selected_url,
            "source": self.source,
            "latency_s": round(self.latency, 2),
            "checked_at": self.checked_at,
            "exit": self.exit_info,
            "attempts": self.attempts,
            "resolved": self._resolved,
        }


# 全局单例：探测一次，全项目复用
_DETECTOR = ProxyDetector()


def get_detector() -> ProxyDetector:
    return _DETECTOR


def detect_proxy(force: bool = False) -> Dict[str, Any]:
    """执行探测并返回摘要。"""
    return _DETECTOR.detect(force=force)


def build_session() -> requests.Session:
    """构建使用已探测通道的会话（未探测时自动探测一次）。"""
    if not _DETECTOR._resolved:
        _DETECTOR.detect()
    return _DETECTOR.session()


def current_proxy_url() -> Optional[str]:
    if not _DETECTOR._resolved:
        _DETECTOR.detect()
    return _DETECTOR.selected_url


def apply_to_clob_sdk() -> Dict[str, Any]:
    """把探测到的通道应用到 py_clob_client 的底层 httpx 客户端。

    为什么必须做这件事：
    py_clob_client 在模块加载时创建了一个全局 `_http_client = httpx.Client(http2=True)`，
    其 `trust_env=True` 意味着它会读取 `http_proxy`/`https_proxy` 环境变量。
    如果这些环境变量指向错误的端口，SDK 的所有下单/查询请求都会失败 ——
    而我们自己用 requests 建的会话是好的，于是出现"能查账、不能下单"的诡异现象。

    这里用显式 proxy 重建该客户端，使 SDK 与其余代码走同一条通道。
    """
    if not _DETECTOR._resolved:
        _DETECTOR.detect()

    url = _DETECTOR.selected_url
    applied = {"ok": False, "proxy": url, "detail": ""}
    try:
        import httpx
        from py_clob_client.http_helpers import helpers

        kwargs = {"http2": True, "timeout": httpx.Timeout(20.0, connect=12.0)}
        if url:
            kwargs["proxy"] = url
        else:
            kwargs["trust_env"] = False

        old_client = getattr(helpers, "_http_client", None)
        helpers._http_client = httpx.Client(**kwargs)
        applied["ok"] = True
        applied["detail"] = (
            f"已重建 SDK 客户端，代理={url}" if url else "已重建 SDK 客户端，使用直连"
        )
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass
    except Exception as exc:
        applied["detail"] = f"应用失败: {type(exc).__name__}: {str(exc)[:120]}"
    return applied


if __name__ == "__main__":
    import json

    result = detect_proxy(force=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(apply_to_clob_sdk(), ensure_ascii=False, indent=2))
