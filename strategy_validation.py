"""Calibration and cost-pressure checks for recorded strategy signals; no synthetic performance claims."""
import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEFAULT_SAMPLES = BASE / "logs" / "strategy_research_samples.jsonl"


def load_samples(path=DEFAULT_SAMPLES):
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as file:
        for line in file:
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    signals = defaultdict(list)
    resolutions = []
    for row in rows:
        if row.get("record_type") in ("SIGNAL", "EVAL") and row.get("p_est") is not None:
            signals[str(row.get("token_id"))].append(row)
        elif row.get("record_type") == "RESOLUTION" and row.get("outcome") in (0, 1):
            resolutions.append(row)
    samples = []
    for resolution in resolutions:
        resolved_at = datetime.fromisoformat(resolution["ts_utc"])
        candidates = [
            row for row in signals.get(str(resolution.get("token_id")), [])
            if datetime.fromisoformat(row["ts_utc"]) <= resolved_at
        ]
        if not candidates:
            continue
        signal = candidates[-1]
        metrics = signal.get("metrics") or {}
        samples.append({
            "p_est": float(signal["p_est"]),
            "outcome": int(resolution["outcome"]),
            "signal_price": float(signal.get("signal_price") or 0),
            "execution_price": float(signal.get("execution_price") or signal.get("signal_price") or 0),
            # 信号时刻的盘口价差：往返成本的主要来源（卖出时还要再吃一次价差）
            "spread": float(metrics.get("spread") or 0.0),
            "roc_logit_6": float(metrics.get("roc_logit_6") or 0.0),
            "imbalance": float(metrics.get("imbalance") or 0.0),
            "hours_left": float(signal.get("hours_left") or 0),
            "signal_ts": signal.get("ts_utc"),
            "sample_id": signal.get("sample_id"),
        })
    return samples


def calibration_report(samples, minimum=30):
    if len(samples) < minimum:
        return {
            "status": "INSUFFICIENT_DATA",
            "sample_count": len(samples),
            "minimum_required": minimum,
            "valid_for_strategy_claims": False,
        }
    bins = defaultdict(list)
    for row in samples:
        index = min(9, int(row["p_est"] * 10))
        bins[index].append(row)
    brier = sum((row["p_est"] - row["outcome"]) ** 2 for row in samples) / len(samples)
    return {
        "status": "OK",
        "sample_count": len(samples),
        "valid_for_strategy_claims": True,
        "brier_score": brier,
        "bins": [
            {
                "range": [index / 10, (index + 1) / 10],
                "count": len(rows),
                "mean_prediction": sum(row["p_est"] for row in rows) / len(rows),
                "observed_frequency": sum(row["outcome"] for row in rows) / len(rows),
            }
            for index, rows in sorted(bins.items())
        ],
    }


def cost_pressure_report(samples):
    """成本压力：把**往返价差**按倍数放大，看还剩多少交易满足 edge 门槛。

    旧实现用 `(execution_price − signal_price) × 倍数` 模拟滑点，但实盘是市价吃卖一，
    成交价等于信号价，滑点恒为 0 —— 于是 8x 压力下依然 100% 通过，等于没做压力测试。

    现在的口径：
      入场成本 = execution_price（已经吃掉了卖一）
      出场成本 = spread（卖出时再吃一次价差）
      压力成本 = execution_price + spread × multiplier
    这才是"成本翻倍"对这类策略的真实含义。
    """
    reports = []
    for multiplier in (1.0, 2.0, 8.0):
        adjusted = []
        for row in samples:
            spread = float(row.get("spread") or 0.0)
            cost = row["execution_price"] + spread * multiplier
            edge = row["p_est"] - cost
            adjusted.append({**row, "cost": cost, "edge": edge})
        passing = [row for row in adjusted if row["edge"] >= 0.025]
        reports.append({
            "slippage_multiplier": multiplier,
            "cost_model": "entry price + spread × multiplier",
            "sample_count": len(adjusted),
            "edge_pass_count": len(passing),
            "edge_pass_rate": len(passing) / len(adjusted) if adjusted else 0.0,
            "calibration_of_passing": calibration_report(passing, minimum=30),
        })
    return reports


def ablation_report(samples, evaluator, variants):
    if not variants:
        raise ValueError("At least one feature variant is required")
    return {
        "status": "OK" if samples else "INSUFFICIENT_DATA",
        "sample_count": len(samples),
        "variants": {name: [evaluator(row, name) for row in samples] for name in variants},
        "valid_for_strategy_claims": len(samples) >= 30,
    }


def baseline_report(samples):
    if len(samples) < 30:
        return {"status": "INSUFFICIENT_DATA", "sample_count": len(samples), "valid_for_strategy_claims": False}
    pnl = [row["outcome"] - row["execution_price"] for row in samples]
    return {
        "status": "OK",
        "sample_count": len(samples),
        "strategy": "每份持有到结算，不做动量或止盈止损",
        "total_pnl_per_share": sum(pnl),
        "mean_pnl_per_share": sum(pnl) / len(pnl),
        "win_rate": sum(value > 0 for value in pnl) / len(pnl),
    }


def threshold_sensitivity_report(samples):
    return [
        {
            "edge_threshold": threshold,
            "selected_count": len(selected := [row for row in samples if row["p_est"] - row["execution_price"] >= threshold]),
            "calibration": calibration_report(selected, minimum=30),
        }
        for threshold in (0.02, 0.025, 0.03)
    ]


def stratified_report(samples):
    groups = defaultdict(list)
    for row in samples:
        hours = row.get("hours_left") or 0
        label = "<24h" if hours < 24 else "24-168h" if hours <= 168 else ">168h"
        groups[label].append(row)
    return {label: calibration_report(rows, minimum=30) for label, rows in sorted(groups.items())}


def out_of_sample_report(samples):
    ordered = sorted(samples, key=lambda row: row.get("signal_ts") or row.get("sample_id") or "")
    split = int(len(ordered) * 0.7)
    training, testing = ordered[:split], ordered[split:]
    if len(training) < 30 or len(testing) < 30:
        return {
            "status": "INSUFFICIENT_DATA",
            "training_count": len(training),
            "testing_count": len(testing),
            "minimum_each": 30,
            "valid_for_strategy_claims": False,
        }
    return {
        "status": "OK",
        "training": calibration_report(training),
        "out_of_sample": calibration_report(testing),
    }


def execution_quality_report(positions):
    rows = [row for row in positions if row.get("signal_price") is not None and row.get("realized_slippage") is not None]
    if not rows:
        return {"status": "INSUFFICIENT_DATA", "sample_count": 0, "valid_for_strategy_claims": False}
    slippage = [float(row["realized_slippage"]) for row in rows]
    edges = [float(row["edge_at_entry"]) for row in rows]
    return {
        "status": "OK",
        "sample_count": len(rows),
        "mean_realized_slippage": sum(slippage) / len(slippage),
        "max_realized_slippage": max(slippage),
        "mean_realized_edge_at_entry": sum(edges) / len(edges),
        "negative_edge_count": sum(edge < 0 for edge in edges),
    }


def feature_missing_report(markets):
    fields = ("clobTokenIds", "outcomes", "description", "endDate", "feeSchedule", "orderPriceMinTickSize")
    total = len(markets)
    report = {}
    for field in fields:
        missing = sum(1 for row in markets if row.get(field) in (None, "", [], {}))
        report[field] = {"missing": missing, "missing_rate": missing / total if total else 0.0}
    return {"market_count": total, "fields": report}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--portfolio", type=Path, default=BASE / "state" / "portfolio.json")
    args = parser.parse_args()
    samples = load_samples(args.samples)
    positions = []
    if args.portfolio.exists():
        positions = json.loads(args.portfolio.read_text(encoding="utf-8")).get("open_positions") or []
    print(json.dumps({
        "execution_quality": execution_quality_report(positions),
        "calibration": calibration_report(samples),
        "baseline": baseline_report(samples),
        "threshold_sensitivity": threshold_sensitivity_report(samples),
        "stratified": stratified_report(samples),
        "out_of_sample": out_of_sample_report(samples),
        "cost_pressure": cost_pressure_report(samples),
        "note": "统计通过只说明样本表现，不代表未来收益；不足 30 个真实结算样本时禁止下策略有效结论。",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
