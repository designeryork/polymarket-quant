"""Persistent, sanitized trading-error events for the local monitoring dashboard."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
ERROR_LOG_FILE = BASE_DIR / "logs" / "trading_errors.jsonl"
AUDIT_LOG_FILE = BASE_DIR / "logs" / "signals_and_skips.jsonl"

CATEGORIES = {
    "REGION_RESTRICTED": {
        "title": "区域限制（HTTP 403）",
        "handling": "订单已被拒绝，未确认成交。停止自动重试，先核查 Polymarket 地区与账户合规要求。",
    },
    "SIGNATURE_INVALID": {
        "title": "POLY_1271 签名无效",
        "handling": "订单未成交；保留原持仓。修复并离线验证签名编码后，才可恢复交易。",
    },
    "API_AUTH_FAILED": {
        "title": "API 认证或授权失败",
        "handling": "停止交易并检查 API 凭证与钱包授权；不要反复提交订单。",
    },
    "BALANCE_OR_ALLOWANCE": {
        "title": "余额或授权不足",
        "handling": "卖出未确认成交；先只读对账真实成交份额、可用余额与授权，避免超额卖出。",
    },
    "ORDER_AMOUNT_INVALID": {
        "title": "订单金额或精度不符合规则",
        "handling": "订单未成交；按市场最小金额与精度重新计算，不能将其记为已成交。若为买单金额低于 $1 门槛（invalid amount ... min size: $1），说明份额换算后被截断导致金额缩水，需上调换算精度或加安全边际。",
    },
    "PROXY_ERROR": {
        "title": "本地代理/VPN 通道故障",
        "handling": "请求未能经代理建立隧道，多为本地代理客户端异常（未连接/节点故障）。这是本机网络问题，不是 Polymarket 拒绝交易。修复代理后会自动恢复；不要据此判断账户或地区状态。",
    },
    "RATE_LIMITED": {
        "title": "接口限流（HTTP 429）",
        "handling": "请求被限流。这是突发性的短窗口限流（响应头带 Retry-After，通常 1 秒），退避后立即恢复，不影响资金安全。系统已内置指数退避与巡检抖动；若持续出现，说明巡检间隔过短或单轮请求量过大，应调大 SCAN_INTERVAL / 降低 SCAN_LIMIT。本轮未修改账本也未提交订单。",
    },
    "NETWORK_TIMEOUT": {
        "title": "连接超时",
        "handling": "接口未在超时窗口内响应。本轮已跳过，未修改账本也未提交订单；恢复后自动重试。",
    },
    "CONNECTION_RESET": {
        "title": "连接被重置（10054 等）",
        "handling": "已有连接被中途断开，可能来自本地网络、代理或远端服务。连续发生时应检查本地代理与防火墙。",
    },
    "SSL_ERROR": {
        "title": "TLS/SSL 握手异常",
        "handling": "加密通道建立失败，通常与代理中间人或链路不稳定有关。本轮已跳过，未产生交易动作。",
    },
    "DNS_ERROR": {
        "title": "域名解析失败",
        "handling": "无法解析目标域名，属于本机 DNS 或网络配置问题。未产生任何交易动作。",
    },
    "TRADING_ERROR": {
        "title": "交易或执行异常",
        "handling": "本次操作未确认成功；保持账本不变并检查原始错误，恢复前先只读对账。",
    },
}

STAGE_LABELS = {
    "OPEN": "开仓",
    "CLOSE": "平仓",
    "CANCEL_TP": "撤销止盈单",
    "RECONCILIATION": "账本对账",
    "ENGINE": "交易引擎",
}

# 网络类错误不涉及交易本身，单独标记严重级别，便于面板区分"交易失败"与"网络不通"
NETWORK_CATEGORIES = {
    "PROXY_ERROR",
    "RATE_LIMITED",
    "NETWORK_TIMEOUT",
    "CONNECTION_RESET",
    "SSL_ERROR",
    "DNS_ERROR",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_error(error: Any, limit: int = 800) -> str:
    text = str(error)
    text = re.sub(r"0x[a-fA-F0-9]{64}", "0x[REDACTED]", text)
    text = re.sub(
        r"(?i)(private[_ -]?key|api[_ -]?secret|passphrase)(\s*[:=]\s*)[^\s,;}\]]+",
        r"\1\2[REDACTED]",
        text,
    )
    return text[:limit]


def classify_error(error: Any) -> str:
    """把原始异常映射到分类。

    网络类故障（代理、限流、超时、重置、SSL、DNS）必须与"交易被拒"分开：
    前者是本机链路问题、恢复后自动重试即可；后者涉及账户资格与策略正确性，
    两者混为一谈会让告警失去判断价值。
    """
    text = str(error).lower()

    # --- 网络类（优先判断，避免被泛化的 TRADING_ERROR 吞掉）---
    if "proxyerror" in text or "tunnel connection failed" in text or "unable to connect to proxy" in text:
        return "PROXY_ERROR"
    if "429" in text or "too many requests" in text or "rate limit" in text:
        return "RATE_LIMITED"
    if "read timed out" in text or "connecttimeout" in text or "timed out" in text or "timeout" in text:
        return "NETWORK_TIMEOUT"
    if "10054" in text or "connectionreset" in text or "connection reset" in text or "remote end closed" in text:
        return "CONNECTION_RESET"
    if "ssl" in text and ("eof" in text or "error" in text or "unexpected" in text):
        return "SSL_ERROR"
    if "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
        return "DNS_ERROR"

    # --- 交易与账户类 ---
    if "trading restricted in your region" in text or ("403" in text and "region" in text):
        return "REGION_RESTRICTED"
    if "poly_1271" in text or "invalid signature" in text or "signature too short" in text:
        return "SIGNATURE_INVALID"
    if "not enough balance" in text or "allowance" in text or "insufficient balance" in text:
        return "BALANCE_OR_ALLOWANCE"
    if "401" in text or "unauthorized" in text or "invalid api key" in text or "authentication" in text:
        return "API_AUTH_FAILED"
    if "invalid amount" in text or "min size" in text or "invalid size" in text:
        return "ORDER_AMOUNT_INVALID"
    return "TRADING_ERROR"


def load_trading_errors(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not ERROR_LOG_FILE.exists():
        return []
    events = []
    with ERROR_LOG_FILE.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, dict) and event.get("event_id"):
                events.append(event)
    events.sort(key=lambda item: str(item.get("ts_utc", "")), reverse=True)
    return events[:limit] if limit is not None else events


def record_trading_error(
    stage: str,
    error: Any,
    question: str = "",
    side: str = "",
    occurred_at: Optional[str] = None,
    source: str = "runtime",
    source_record: str = "",
    context: str = "",
) -> Dict[str, Any]:
    timestamp = occurred_at or utc_now()
    detail = sanitize_error(error)
    category = classify_error(detail)
    identity = "\0".join((timestamp, stage, category, question, side, detail, source_record))
    event = {
        "event_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "ts_utc": timestamp,
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "category": category,
        "category_label": CATEGORIES[category]["title"],
        "severity": "warning" if category in NETWORK_CATEGORIES else "critical",
        "question": question,
        "side": side,
        "detail": detail,
        "handling": CATEGORIES[category]["handling"],
        "context": sanitize_error(context, limit=300) if context else "",
        "source": source,
        "source_record": source_record,
    }
    if any(item.get("event_id") == event["event_id"] for item in load_trading_errors()):
        return event
    ERROR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
    return event


def import_history() -> List[Dict[str, Any]]:
    if not AUDIT_LOG_FILE.exists():
        return []
    stage_by_code = {
        "LIVE_BUY_EXCEPTION": "OPEN",
        "LIVE_BUY_FAILED": "OPEN",
        "LIVE_EXIT_FAILED": "CLOSE",
        "CANCEL_TP_FAILED": "CANCEL_TP",
    }
    imported = []
    with AUDIT_LOG_FILE.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            code = entry.get("action") or entry.get("reason_code")
            stage = stage_by_code.get(code)
            if not stage:
                continue
            error = entry.get("error") or entry.get("detail") or code
            metrics = entry.get("metrics") or {}
            imported.append(
                record_trading_error(
                    stage=stage,
                    error=error,
                    question=str(entry.get("question") or ""),
                    side=str(metrics.get("side") or ""),
                    occurred_at=entry.get("ts_utc"),
                    source="history",
                    source_record=f"logs/signals_and_skips.jsonl:{line_number}",
                    context=str(entry.get("reason") or code),
                )
            )
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Record and inspect persistent trading-error events")
    parser.add_argument("--import-history", action="store_true", help="Import known failures from the existing audit log")
    args = parser.parse_args()
    if args.import_history:
        imported = import_history()
        print(f"Processed {len(imported)} historical trading failures")
    events = load_trading_errors()
    counts: Dict[str, int] = {}
    for event in events:
        counts[event["category"]] = counts.get(event["category"], 0) + 1
    print(json.dumps({"total": len(events), "by_category": counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
