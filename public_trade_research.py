"""Read-only public trade ingestion with partial-fill and hedging safeguards."""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import proxy_detector

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "logs" / "public_trade_research.json"


def normalize_public_trades(rows, end_timestamp=None):
    grouped = {}
    discarded_after_settlement = 0
    for row in rows:
        timestamp = int(row.get("timestamp") or 0)
        if end_timestamp is not None and timestamp >= end_timestamp:
            discarded_after_settlement += 1
            continue
        key = (
            str(row.get("transactionHash") or row.get("id") or ""),
            str(row.get("asset") or ""),
            str(row.get("side") or "").upper(),
            str(row.get("price") or ""),
            str(row.get("taker") or row.get("maker") or "UNKNOWN"),
        )
        if key not in grouped:
            grouped[key] = dict(row)
            grouped[key]["size"] = 0.0
        grouped[key]["size"] = float(grouped[key].get("size") or 0) + float(row.get("size") or 0)
    groups = list(grouped.values())
    hedge_keys = set()
    by_hash = defaultdict(set)
    for row in groups:
        by_hash[str(row.get("transactionHash") or "")].add(str(row.get("side") or "").upper())
    for tx_hash, sides in by_hash.items():
        if "BUY" in sides and "SELL" in sides:
            hedge_keys.add(tx_hash)
    known_taker = sum(1 for row in groups if str(row.get("taker") or "").lower() in ("true", "taker"))
    known_maker = sum(1 for row in groups if str(row.get("taker") or "").lower() in ("false", "maker"))
    return {
        "grouped_trades": groups,
        "hedge_transaction_hashes": hedge_keys,
        "stats": {
            "raw_count": len(rows),
            "grouped_count": len(groups),
            "hedge_count": len(hedge_keys),
            "discarded_after_settlement": discarded_after_settlement,
            "known_taker_count": known_taker,
            "known_maker_count": known_maker,
            "unknown_role_count": len(groups) - known_taker - known_maker,
            "volume_shares": sum(float(row.get("size") or 0) for row in groups),
        },
    }


def fetch_public_trades(condition_id="", limit=100):
    session = proxy_detector.build_session()
    params = {"limit": max(1, min(int(limit), 500))}
    if condition_id:
        params["market"] = condition_id
    response = session.get("https://data-api.polymarket.com/v2/trades", params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Public trades response is not a list")
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default="")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    rows = fetch_public_trades(args.market, args.limit)
    result = normalize_public_trades(rows)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "market": args.market or "ALL",
        "stats": result["stats"],
        "note": "Partial fills grouped; hedged transactions flagged; unknown maker/taker roles are not guessed.",
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
