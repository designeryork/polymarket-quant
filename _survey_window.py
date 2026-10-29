"""Verify: how much opportunity is lost because the engine only scans the top-25 markets?

Compares the engine's current top-25 window against a larger window (up to 100),
checking how many extra markets would qualify past the price-range filter.
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv

load_dotenv(BASE / ".env")

import proxy_detector

session = proxy_detector.build_session()
session.headers.update({"User-Agent": "PolymarketQuantAgent/1.0"})

r = session.get(
    "https://gamma-api.polymarket.com/markets",
    params={
        "active": "true",
        "closed": "false",
        "limit": 100,
        "order": "volume24hr",
        "ascending": "false",
    },
    timeout=25,
)
markets = r.json()
print(f"Gamma 返回 {len(markets)} 个活跃市场（引擎当前只取前 25 个）\n")

books = {}
qualified_25 = 0
qualified_100 = 0
checked = 0

for idx, m in enumerate(markets):
    try:
        raw = m.get("clobTokenIds")
        tokens = json.loads(raw) if isinstance(raw, str) else (raw or [])
        if len(tokens) < 2:
            continue
        asks = []
        for tid in tokens[:2]:
            book = session.get(
                "https://clob.polymarket.com/book", params={"token_id": tid}, timeout=12
            ).json()
            a = book.get("asks") or []
            asks.append(min(float(x["price"]) for x in a) if a else None)
        checked += 1
        ok = any(x is not None and 0.15 <= x <= 0.85 for x in asks)
        if ok:
            qualified_100 += 1
            if idx < 25:
                qualified_25 += 1
    except Exception:
        pass

print(f"已检查 {checked} 个市场的价格区间")
print()
print(f"  前 25 个（引擎当前范围）内可交易: {qualified_25}")
print(f"  前 100 个内可交易           : {qualified_100}")
print()
extra = qualified_100 - qualified_25
print(f"  -> 扩大扫描范围可多覆盖 {extra} 个可交易市场 "
      f"({100*extra/max(1,qualified_100):.0f}% 的机会当前完全看不到)")
print()
print("=== 前 25 名之后、但价格落在区间内的市场（当前完全被忽略）===")
shown = 0
for idx, m in enumerate(markets[25:], start=25):
    if shown >= 12:
        break
    try:
        raw = m.get("clobTokenIds")
        tokens = json.loads(raw) if isinstance(raw, str) else (raw or [])
        if len(tokens) < 2:
            continue
        asks = []
        for tid in tokens[:2]:
            book = session.get(
                "https://clob.polymarket.com/book", params={"token_id": tid}, timeout=12
            ).json()
            a = book.get("asks") or []
            asks.append(min(float(x["price"]) for x in a) if a else None)
        if any(x is not None and 0.15 <= x <= 0.85 for x in asks):
            ys = f"{asks[0]:.3f}" if asks[0] is not None else "  -  "
            ns = f"{asks[1]:.3f}" if asks[1] is not None else "  -  "
            print(f"  #{idx+1:3d} YES={ys} NO={ns}  {m.get('question','')[:52]}")
            shown += 1
    except Exception:
        pass
