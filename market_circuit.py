"""
市场级不可交易熔断 (market_circuit.py)
======================================
Polymarket CLOB 会对某些市场返回 `503 trading is disabled` ——
这通常是该市场被交易所侧临时禁用（UMA 争议、流动性冻结、结算过渡期），
而非账号或代码问题。

旧行为的问题
------------
遇到 503 时，引擎每轮都会重试同一个市场，**16 轮连续硬撞**
（实测 17:58 ~ 18:59，每 4 分钟一次）。每一次都是：
1. 浪费一次完整的下单往返（签名 + 提交 + 失败）；
2. 在告警面板上刷出重复的 critical 错误，淹没其它真实问题；
3. 对交易所产生无意义的请求压力。

设计
----
* `is_not_tradable_error(exc)` —— 识别 503 / trading is disabled / 市场已关闭；
* `MarketCircuitBreaker` —— 按 token 记录连续失败次数与下次可重试时刻；
  达到阈值后进入**冷却**，冷却期内直接跳过该市场（不再发请求）；
  冷却期满后允许一次**试探性**重试，成功则完全复位。
* 状态持久化到 `state/market_circuit.json`，重启后不丢失。

这是 fail-open 于**尝试**、fail-closed 于**下单**的设计：
被熔断的市场不会下单，但也只是暂时不尝试，不会改动账本或持仓。
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
CIRCUIT_FILE = BASE_DIR / "state" / "market_circuit.json"

# 连续失败多少次后进入冷却
FAILURE_THRESHOLD = 2

# 冷却时长（秒）——随连续失败次数递增
BASE_COOLDOWN = 900.0
MAX_COOLDOWN = 6 * 3600.0

# 冷却期内允许的试探间隔
PROBE_INTERVAL = 1800.0


def is_not_tradable_error(error: Any) -> bool:
    """判断异常是否表示"该市场当前不可交易"。

    识别三种表述：
    * `503 trading is disabled`（CLOB 的标准响应）
    * `market ... is closed` / `not accepting orders`
    * `404 No orderbook exists for the requested token id`
    """
    text = str(error).lower()
    if "trading is disabled" in text:
        return True
    if "status_code=503" in text or "503" in text and "error" in text:
        return True
    if "no orderbook exists" in text:
        return True
    if "market is closed" in text or "not accepting orders" in text:
        return True
    if "accepting_orders" in text and "false" in text:
        return True
    return False


def is_balance_error(error: Any) -> bool:
    """判断异常是否表示"余额/授权不足"。

    CLOB 的响应形如：
        not enough balance / allowance: the balance is not enough ->
        balance: 5000000, sum of active orders: 5000000,
        sum of matched orders: 0, order amount (inc. fees): 5000000

    含义：该 token 的链上份额已被**其它活跃卖单**占用，再挂新单必然被拒。
    这不是账户问题，重试无意义 —— 应当等待持仓或挂单状态变化后再处理。
    """
    text = str(error).lower()
    return "not enough balance" in text or "allowance" in text


class MarketCircuitBreaker:
    """按 token 维度的不可交易熔断器。"""

    def __init__(self, path: Path = CIRCUIT_FILE) -> None:
        self.path = path
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._state = data.get("markets", {}) or {}
        except Exception:
            self._state = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"updated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "markets": self._state},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ---------- 核心逻辑 ----------
    def should_skip(self, token_id: str) -> bool:
        """该 token 当前是否应被跳过（处于熔断冷却期）。"""
        entry = self._state.get(str(token_id))
        if not entry:
            return False
        consecutive = int(entry.get("consecutive_failures", 0))
        if consecutive < FAILURE_THRESHOLD:
            return False
        next_retry = float(entry.get("next_retry_at", 0))
        if time.time() >= next_retry:
            # 冷却期满 -> 允许一次试探性重试
            return False
        return True

    def skip_reason(self, token_id: str) -> str:
        entry = self._state.get(str(token_id)) or {}
        wait = float(entry.get("next_retry_at", 0)) - time.time()
        return (
            f"市场处于不可交易熔断冷却中（连续失败 {entry.get('consecutive_failures', 0)} 次，"
            f"约 {max(0, wait) / 60:.0f} 分钟后重试）"
        )

    def record_failure(self, token_id: str, detail: str = "") -> Dict[str, Any]:
        """记录一次不可交易失败。"""
        key = str(token_id)
        entry = self._state.get(key) or {"consecutive_failures": 0}
        entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        entry["last_failure_at"] = time.time()
        entry["last_failure_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry["last_detail"] = str(detail)[:200]

        failures = entry["consecutive_failures"]
        if failures >= FAILURE_THRESHOLD:
            # 冷却时长随失败次数递增
            cooldown = min(BASE_COOLDOWN * (2 ** (failures - FAILURE_THRESHOLD)), MAX_COOLDOWN)
            entry["next_retry_at"] = time.time() + cooldown
            entry["cooldown_seconds"] = cooldown
        else:
            entry["next_retry_at"] = time.time() + PROBE_INTERVAL

        self._state[key] = entry
        self._save()
        return entry

    def record_success(self, token_id: str) -> None:
        """一次成功即完全复位。"""
        key = str(token_id)
        if key in self._state:
            self._state.pop(key, None)
            self._save()

    def record_balance_conflict(self, token_id: str, detail: str = "") -> Dict[str, Any]:
        """记录一次"余额/授权不足"冲突。

        含义：该 token 的份额已被其它活跃卖单占用，再挂新单必然被拒。
        处置：进入一个较短的冷却期，期间不再尝试挂卖单。
        这比每轮重试（每轮都被拒、每次都刷错误）要好得多。
        冷却期过后允许重新尝试 —— 因为挂单成交后份额就释放了。
        """
        key = str(token_id)
        entry = self._state.get(key) or {}
        # 注意：余额冲突不计入"市场不可交易"的连续失败计数，两者语义不同
        entry["balance_conflicts"] = int(entry.get("balance_conflicts", 0)) + 1
        entry["last_balance_conflict_at"] = time.time()
        entry["last_balance_conflict_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry["last_balance_detail"] = str(detail)[:200]
        # 冷却 10 分钟起步，随冲突次数递增
        cooldown = min(BASE_COOLDOWN * 0.67 * (2 ** (entry["balance_conflicts"] - 1)), MAX_COOLDOWN)
        entry["next_retry_at"] = time.time() + cooldown
        entry["cooldown_seconds"] = cooldown
        self._state[key] = entry
        self._save()
        return entry

    def has_balance_conflict(self, token_id: str) -> bool:
        """该 token 是否处于余额冲突冷却期。"""
        entry = self._state.get(str(token_id)) or {}
        if not entry.get("balance_conflicts"):
            return False
        return time.time() < float(entry.get("next_retry_at", 0))

    def status(self, token_id: Optional[str] = None) -> Dict[str, Any]:
        if token_id:
            return self._state.get(str(token_id), {})
        return {
            "markets": len(self._state),
            "details": {
                k: {
                    "failures": v.get("consecutive_failures"),
                    "next_retry_at_utc": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(v.get("next_retry_at", 0))
                    ),
                }
                for k, v in self._state.items()
            },
        }


# 全局单例
_BREAKER = MarketCircuitBreaker()


def get_breaker() -> MarketCircuitBreaker:
    return _BREAKER
