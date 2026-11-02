"""结算样本回填 + 概率校准检验。

背景
----
`strategy_validation.py` 已经实现了 Brier、校准曲线、成本压力、基线对照、样本外验证，
但它依赖 `logs/strategy_research_samples.jsonl` 里的 RESOLUTION 记录——而这份文件里
**一条 RESOLUTION 都没有**（仓位大多在结算前就被止损平掉了，`record_resolution_sample`
只在持仓走到已结算状态时才触发）。

本脚本负责补上这块数据：
1. 读 SIGNAL 样本里的 token_id；
2. 用 Gamma 反查这些市场是否已结算、我方方向的结果（0/1）；
3. 把结果作为 RESOLUTION 记录写回研究样本文件（同一 token 只写一次）；
4. 再调用 `strategy_validation` 的现成函数输出 Brier / 校准表 / 成本压力 / 基线对照。

只读链上与公开数据；唯一写操作是追加 RESOLUTION 记录到研究样本文件。

用法
----
    python calibration_report.py              # 反查 + 报告
    python calibration_report.py --no-fetch   # 只跑报告（用已有 RESOLUTION）
"""
import argparse
import json
import math
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

SAMPLES = BASE / "logs" / "strategy_research_samples.jsonl"
GAMMA = "https://gamma-api.polymarket.com"
MIN_SAMPLES = 30


def build_session() -> requests.Session:
    try:
        import proxy_detector

        return proxy_detector.build_session()
    except Exception:
        session = requests.Session()
        session.headers.update({"User-Agent": "PolymarketCalibration/1.0"})
        return session


def read_rows() -> list:
    if not SAMPLES.exists():
        return []
    rows = []
    for line in SAMPLES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def resolve_outcome(session, token_id: str, label: str, cache: dict):
    """返回 (outcome, closed, found)；outcome 为 0/1，未结算或查不到返回 None。"""
    if token_id in cache:
        return cache[token_id]
    result = (None, False, False)
    for params in ({"clob_token_ids": token_id, "closed": "true"}, {"clob_token_ids": token_id}):
        try:
            resp = session.get(f"{GAMMA}/markets", params=params, timeout=20)
            if resp.status_code != 200:
                continue
            data = resp.json()
            rows = data if isinstance(data, list) else data.get("data", [])
            if not rows:
                continue
            row = rows[0]
            closed = bool(row.get("closed"))
            outcomes = row.get("outcomes") or []
            prices = row.get("outcomePrices") or []
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            index = 0
            for i, name in enumerate(outcomes):
                if str(name).strip().lower() == str(label).strip().lower():
                    index = i
                    break
            try:
                price = float(prices[index])
            except (IndexError, TypeError, ValueError):
                price = None
            outcome = None
            if closed and price is not None:
                if price >= 0.99:
                    outcome = 1
                elif price <= 0.01:
                    outcome = 0
            result = (outcome, closed, True)
            break
        except Exception:
            continue
    cache[token_id] = result
    return result


def fetch_resolutions(rows, session, limit: int = 0) -> list:
    """把已结算的 SIGNAL 转成 RESOLUTION 记录；同一 token 只保留一条。"""
    done = {str(r.get("token_id")) for r in rows if r.get("record_type") == "RESOLUTION"}
    resolved = {str(r.get("token_id")) for r in rows if r.get("record_type") == "RESOLUTION" and r.get("outcome") in (0, 1)}
    latest_signal = {}
    for row in rows:
        if row.get("record_type") not in ("SIGNAL", "EVAL"):
            continue
        token = str(row.get("token_id") or "")
        try:
            if not math.isfinite(float(row.get("p_est"))):
                continue
        except (TypeError, ValueError):
            continue
        latest_signal[token] = row

    cache = {}
    new_records = []
    stats = {"closed_resolved": 0, "closed_no_outcome": 0, "open": 0, "not_found": 0, "skipped_by_limit": 0}
    pending = [(token, row) for token, row in latest_signal.items() if token not in resolved]
    pending.sort(key=lambda item: str(item[1].get("ts_utc") or ""))
    budget = limit if limit > 0 else len(pending)
    stats["skipped_by_limit"] = max(0, len(pending) - budget)
    for token, signal in pending[:budget]:
        if token in resolved:
            continue
        outcome, closed, found = resolve_outcome(
            session, token, str(signal.get("outcome_label") or ""), cache
        )
        time.sleep(0.15)
        if outcome is None:
            if found and closed:
                stats["closed_no_outcome"] += 1
            elif not found:
                stats["not_found"] += 1
            else:
                stats["open"] += 1
            continue
        new_records.append({
            "record_type": "RESOLUTION",
            "token_id": token,
            "event_id": signal.get("event_id", ""),
            "question": str(signal.get("question") or "")[:80],
            "outcome_label": signal.get("outcome_label"),
            "p_est": signal.get("p_est"),
            "outcome": outcome,
            "entry_price": signal.get("execution_price"),
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "source": "gamma backfill",
        })
    stats["closed_resolved"] += len(new_records)
    if new_records:
        with open(SAMPLES, "a", encoding="utf-8") as handle:
            for record in new_records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return new_records, stats


def reentry_report() -> int:
    """统计「峰值回撤退出后立即重扫」的结果分布。

    数据来自引擎写的 REENTRY_PROBE 审计记录（只评估、不下单）。
    回答一个问题：退出之后，同方向还有多少比例仍然满足入场条件。
    """
    import glob
    from collections import Counter

    gates = Counter()
    roc_values = []
    edge_values = []
    total = 0
    for path in glob.glob(str(BASE / "logs" / "signals_and_skips*.jsonl")):
        for line in open(path, encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("action") != "REENTRY_PROBE":
                continue
            total += 1
            gates[str(record.get("gate_result") or "unknown")] += 1
            if isinstance(record.get("roc_z"), (int, float)):
                roc_values.append(float(record["roc_z"]))
            if isinstance(record.get("edge"), (int, float)):
                edge_values.append(float(record["edge"]))
    print("=" * 66)
    print(" 峰值回撤退出后的重入探测（只评估，不下单）")
    print("=" * 66)
    print(f"探测次数：{total}")
    if not total:
        print("暂无数据。等下一次「峰值回撤退出」触发后再看。")
        return 0
    print("门禁分布：")
    for name, count in gates.most_common():
        print(f"  {name:12s} {count:>4d}  {count / total:.0%}")
    if roc_values:
        print(f"退出时 ROC_z 均值 {sum(roc_values) / len(roc_values):+.3f}"
              f"（负值说明动量已转下，必然被 momentum 门挡掉）")
    if edge_values:
        print(f"退出时 edge 均值 {sum(edge_values) / len(edge_values):+.4f}")
    accept_rate = gates.get("accept", 0) / total
    print(f"\n若开启重入，理论上可再次满足条件的比例：{accept_rate:.0%}")
    print("注意：满足条件 ≠ 应该重入；还需要扣两次往返价差后仍为正期望。")
    return 0


def brier(pairs):
    if not pairs:
        return None
    return sum((pred - actual) ** 2 for pred, actual in pairs) / len(pairs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fetch", action="store_true", help="跳过 Gamma 反查，只用已有 RESOLUTION")
    parser.add_argument("--limit", type=int, default=200, help="本次最多反查多少个 token（0 = 不限）")
    parser.add_argument("--reentry", action="store_true", help="只看「峰值回撤退出后的重入探测」统计")
    args = parser.parse_args()

    if args.reentry:
        return reentry_report()

    rows = read_rows()
    signals = sum(1 for r in rows if r.get("record_type") in ("SIGNAL", "EVAL"))
    existing = [r for r in rows if r.get("record_type") == "RESOLUTION" and r.get("outcome") in (0, 1)]

    if not args.no_fetch:
        new_records, stats = fetch_resolutions(rows, build_session(), limit=args.limit)
        print(f"Gamma 反查：新增 {len(new_records)} 条 RESOLUTION 记录")
        print(f"  诊断：已结算可用 {stats['closed_resolved']}｜已收盘但取不到结果 {stats['closed_no_outcome']}"
              f"｜未结算 {stats['open']}｜查不到 {stats['not_found']}")
        if stats["skipped_by_limit"]:
            print(f"  本次限额已用完，还有 {stats['skipped_by_limit']} 个 token 待查（下次运行继续）")
        rows = read_rows()

    import strategy_validation as sv

    samples = sv.load_samples(SAMPLES)
    print("=" * 70)
    print(" 概率校准检验（复用 strategy_validation）")
    print("=" * 70)
    print(f"信号 {signals} 条｜已有 RESOLUTION {len(existing)} 条｜可用样本 {len(samples)} 条")
    if len(samples) < MIN_SAMPLES:
        print(f"\n样本 {len(samples)} < {MIN_SAMPLES}：按原文要求**禁止下策略有效结论**。")
        print("下面只输出描述性数字，全部标记为 INSUFFICIENT_DATA。")

    calib = sv.calibration_report(samples)
    print(f"\n校准状态: {calib['status']}（样本 {calib['sample_count']}）")
    if calib["status"] == "OK":
        print(f"Brier(模型) = {calib['brier_score']:.4f}")
        print(f"{'预测区间':12s} {'样本':>4s} {'平均预测':>8s} {'实际胜率':>8s}")
        for b in calib["bins"]:
            print(f"{b['range'][0]:.1f}-{b['range'][1]:.1f}      {b['count']:>4d} "
                  f"{b['mean_prediction']:>8.3f} {b['observed_frequency']:>8.3f}")

    model_pairs = [(float(s["p_est"]), s["outcome"]) for s in samples]
    market_pairs = [(float(s["signal_price"]), s["outcome"]) for s in samples if s.get("signal_price")]
    b_model, b_market = brier(model_pairs), brier(market_pairs)
    if samples:
        print("\n模型 vs 市场（同一批样本）：")
        print(f"  Brier(模型 p_est)   = {b_model:.4f}" if b_model is not None else "  Brier(模型) 无法计算")
        print(f"  Brier(市场价当概率) = {b_market:.4f}" if b_market is not None else "  Brier(市场) 无法计算")
        if b_model is not None and b_market is not None:
            print(f"  结论：{'模型优于市场' if b_model < b_market else '模型不如市场'}（差 {b_model - b_market:+.4f}）")

    baseline = sv.baseline_report(samples)
    print(f"\n基线（持有到结算，不做任何止盈止损）: {baseline['status']}")
    if baseline["status"] == "OK":
        print(f"  平均每份收益 {baseline['mean_pnl_per_share']:+.4f}｜胜率 {baseline['win_rate']:.0%}")

    pressure = sv.cost_pressure_report(samples) if samples else []
    if pressure:
        print("\n成本压力（往返价差倍数 → 仍满足 edge≥0.025 的比例）：")
        for row in pressure:
            print(f"  {row['slippage_multiplier']:>3.1f}x  {row['edge_pass_count']:>3d}/{row['sample_count']:<3d} "
                  f"= {row['edge_pass_rate']:.0%}")

    if samples:
        print("\n消融（仅统计已结算样本；存在选择偏差，样本≥30 才有意义）：")
        variants = {
            "动量 ROC>0.02": lambda s: s.get("roc_logit_6", 0) > 0.02,
            "盘口失衡≥0.18": lambda s: s.get("imbalance", 0) >= 0.18,
            "任一条件": lambda s: s.get("roc_logit_6", 0) > 0.02 or s.get("imbalance", 0) >= 0.18,
        }
        for name, pred in variants.items():
            kept = [s for s in samples if pred(s)]
            if not kept:
                print(f"  {name}: 0 笔")
                continue
            b = brier([(s["p_est"], s["outcome"]) for s in kept])
            print(f"  {name}: 保留 {len(kept)}｜Brier {b:.4f}")

    print("\n提示：统计通过只说明样本表现，不代表未来收益。")
    print("注：样本来自「每一次方向评估」（EVAL 记录），不只统计成交；")
    print("    同 token / 方向 / 小时去重，等这些市场结算后自动进入校准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
