import json
from datetime import datetime, timezone
from pathlib import Path

from strategy_validation import feature_missing_report

BASE = Path(__file__).resolve().parent
source = BASE / "state" / "market_universe.json"
target = BASE / "logs" / "feature_missing_report.json"

payload = json.loads(source.read_text(encoding="utf-8"))
report = feature_missing_report(payload.get("markets") or [])
report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
report["source"] = source.name
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
