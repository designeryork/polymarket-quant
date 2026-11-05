"""跟单纸盘页面：三栏（每位交易员一栏），与主面板同一套深色视觉。

每栏包含：
* KPI —— 账户价值 / 总盈亏 / 可用现金 / 持仓数
* 净值走势折线图（读 state/copy/equity/*.jsonl）
* 当前持仓明细（我们的镜像仓）
* 对手持仓明细（对方真实持仓，用于对照）
"""
from __future__ import annotations

import html as _html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import copy_engine as engine

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "copy.html"
PAGES_URL = os.getenv("PAGES_URL", "https://designeryork.github.io/polymarket-paper-dashboard")


def esc(value: Any) -> str:
    return _html.escape(str(value if value is not None else ""))


def fmt_ts(ts: Any, with_date: bool = True) -> str:
    try:
        value = int(float(ts))
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    dt = datetime.fromtimestamp(value, tz=timezone.utc)
    return dt.strftime("%m-%d %H:%M") if with_date else dt.strftime("%H:%M")


def fmt_signed(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f}"


def pnl_class(value: float) -> str:
    return "pos" if value > 0 else ("neg" if value < 0 else "flat")


# ---------------------------------------------------------------------------
# 折线图
# ---------------------------------------------------------------------------
def equity_chart(points: List[Dict[str, float]], start_value: float) -> str:
    width, height = 360, 110
    pad_l, pad_r, pad_t, pad_b = 8, 8, 10, 16
    values = [float(p.get("e") or 0.0) for p in points]
    if not values:
        return '<div class="chart-empty">暂无净值数据（等待第一轮巡检）</div>'
    lo, hi = min(values + [start_value]), max(values + [start_value])
    if abs(hi - lo) < 1e-6:
        lo -= 0.1
        hi += 0.1
    span = hi - lo
    lo -= span * 0.08
    hi += span * 0.08
    n = len(values)

    def x_at(i: int) -> float:
        if n == 1:
            return (width - pad_l - pad_r) / 2 + pad_l
        return pad_l + i * (width - pad_l - pad_r) / (n - 1)

    def y_at(v: float) -> float:
        return pad_t + (hi - v) / (hi - lo) * (height - pad_t - pad_b)

    coords = [(x_at(i), y_at(v)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"{pad_l},{height - pad_b} " + line + f" {coords[-1][0]:.1f},{height - pad_b}"
    rising = values[-1] >= values[0]
    stroke = "#34d399" if rising else "#fb7185"
    base_y = y_at(start_value)
    return f"""
    <svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="净值走势">
      <defs>
        <linearGradient id="g{id(points) % 100000}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="{stroke}" stop-opacity="0.28"/>
          <stop offset="100%" stop-color="{stroke}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <line x1="{pad_l}" y1="{base_y:.1f}" x2="{width - pad_r}" y2="{base_y:.1f}"
            stroke="#334155" stroke-width="1" stroke-dasharray="3 3"/>
      <polygon points="{area}" fill="url(#g{id(points) % 100000})"/>
      <polyline points="{line}" fill="none" stroke="{stroke}" stroke-width="1.8"
                stroke-linejoin="round" stroke-linecap="round"/>
    </svg>
    <div class="chart-axis"><span>${lo:.2f}</span><span>本金 $10.00</span><span>${hi:.2f}</span></div>
    """


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
def ours_rows(account: Dict[str, Any]) -> str:
    positions = list((account.get("positions") or {}).values())
    if not positions:
        return '<tr><td colspan="7" class="empty">暂无跟单持仓（等待对方开新仓）</td></tr>'
    rows = []
    for p in sorted(positions, key=lambda x: -float(x.get("shares") or 0)):
        shares = float(p.get("shares") or 0.0)
        avg = float(p.get("avg_price") or 0.0)
        mark = float(p.get("last_mark_price") or 0.0)
        value = shares * mark
        cost = float(p.get("cost_usd") or 0.0)
        pnl = value - cost
        pct = (pnl / cost * 100.0) if cost > 0 else 0.0
        rows.append(f"""
        <tr>
          <td class="q-cell" title="{esc(p.get('title'))}">{esc(p.get('title'))}</td>
          <td><span class="badge">{esc(p.get('outcome'))}</span></td>
          <td>{shares:.2f}</td>
          <td>{avg:.3f}</td>
          <td>{mark:.3f}</td>
          <td>${value:.2f}</td>
          <td class="{pnl_class(pnl)}">${fmt_signed(pnl)} <span class="sub">({fmt_signed(pct, 1)}%)</span></td>
        </tr>""")
    return "".join(rows)


def target_rows(account: Dict[str, Any]) -> str:
    positions = list((account.get("target_positions") or {}).values())
    if not positions:
        return '<tr><td colspan="6" class="empty">对方当前无持仓</td></tr>'
    rows = []
    for p in sorted(positions, key=lambda x: -float(x.get("value_usd") or 0)):
        pnl = float(p.get("unrealized_pnl") or 0.0)
        rows.append(f"""
        <tr>
          <td class="q-cell" title="{esc(p.get('title'))}">{esc(p.get('title'))}</td>
          <td><span class="badge">{esc(p.get('outcome'))}</span></td>
          <td>{float(p.get('size') or 0):.2f}</td>
          <td>{float(p.get('avg_price') or 0):.3f}</td>
          <td>${float(p.get('value_usd') or 0):.2f}</td>
          <td class="{pnl_class(pnl)}">${fmt_signed(pnl)}</td>
        </tr>""")
    return "".join(rows)


def trader_column(trader: Dict[str, str], account: Dict[str, Any],
                  history: List[Dict[str, float]]) -> str:
    summary = engine.account_summary(account)
    equity = summary["equity_usd"]
    pnl = summary["total_pnl_usd"]
    pct = summary["total_return_pct"]
    cash = summary["cash_usd"]
    count = summary["position_count"]
    target_count = len(account.get("target_positions") or {})
    last_poll = account.get("last_poll_at") or "—"
    ok = account.get("last_poll_ok")
    status = '<span class="dot ok"></span>正常' if ok else '<span class="dot bad"></span>异常'

    return f"""
  <section class="trader">
    <header class="trader-head">
      <div class="who">
        <h2>{esc(trader['name'])}</h2>
        <a href="{esc(trader['profile'])}" target="_blank" rel="noopener">{esc(trader['address'][:10])}…{esc(trader['address'][-6:])}</a>
      </div>
      <div class="poll">{status}<span>{esc(last_poll)}</span></div>
    </header>

    <div class="kpis">
      <div class="kpi"><label>账户价值</label><b>${equity:.2f}</b><i>本金 $10.00</i></div>
      <div class="kpi"><label>总盈亏</label><b class="{pnl_class(pnl)}">${fmt_signed(pnl)}</b><i class="{pnl_class(pnl)}">{fmt_signed(pct, 1)}%</i></div>
      <div class="kpi"><label>可用现金</label><b>${cash:.2f}</b><i>现金比例 {cash / max(1e-9, equity) * 100:.0f}%</i></div>
      <div class="kpi"><label>持仓</label><b>{count}</b><i>对方 {target_count} 个</i></div>
    </div>

    <div class="card">
      <div class="card-title"><span>净值走势</span><em>最近 3 天</em></div>
      {equity_chart(history, account.get('starting_cash_usd') or 10.0)}
    </div>

    <div class="card">
      <div class="card-title"><span>当前持仓明细</span><em>跟单仓 · 买一计价</em></div>
      <div class="scroll">
        <table>
          <thead><tr><th>市场</th><th>方向</th><th>份额</th><th>均价</th><th>现价</th><th>市值</th><th>浮盈亏</th></tr></thead>
          <tbody>{ours_rows(account)}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><span>对手持仓明细</span><em>{esc(trader['name'])} 实盘</em></div>
      <div class="scroll">
        <table>
          <thead><tr><th>市场</th><th>方向</th><th>份额</th><th>均价</th><th>市值</th><th>浮盈亏</th></tr></thead>
          <tbody>{target_rows(account)}</tbody>
        </table>
      </div>
    </div>
  </section>"""


CSS = """
:root {
  --bg: #0b1119; --panel: #111a25; --raised: #16202d; --line: #232f3e;
  --text: #d7e0ea; --muted: #7f90a5; --faint: #5b6b7f;
  --green: #34d399; --red: #fb7185; --accent: #7dd3fc;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: radial-gradient(1200px 500px at 50% -10%, #16233a 0%, var(--bg) 55%);
  color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
  "Microsoft YaHei", sans-serif; font-size: 12px;
}
a { color: var(--accent); text-decoration: none; }
.topbar {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px 20px; border-bottom: 1px solid var(--line); background: rgba(11,17,25,.9);
  position: sticky; top: 0; z-index: 5; backdrop-filter: blur(8px);
}
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand h1 { margin: 0; font-size: 16px; letter-spacing: .3px; }
.brand span { color: var(--muted); font-size: 11px; }
.tabs { display: inline-flex; gap: 4px; padding: 3px; background: var(--raised); border-radius: 8px; }
.tabs a { padding: 5px 10px; border-radius: 6px; color: var(--muted); font-size: 11px; }
.tabs a.active { background: #1d2b3d; color: var(--text); }
.tabs a:hover { color: var(--text); }
.meta { color: var(--faint); font-size: 10px; text-align: right; line-height: 1.6; }
.grid {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px; padding: 16px 20px 28px; align-items: start;
}
@media (max-width: 1180px) { .grid { grid-template-columns: 1fr; } }
.trader {
  background: linear-gradient(180deg, rgba(22,32,45,.85), rgba(17,26,37,.85));
  border: 1px solid var(--line); border-radius: 12px; padding: 12px; min-width: 0;
}
.trader-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.who h2 { margin: 0 0 3px; font-size: 15px; }
.who a { font-size: 10px; color: var(--faint); }
.poll { color: var(--muted); font-size: 9px; text-align: right; line-height: 1.8; }
.dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px; }
.dot.ok { background: var(--green); }
.dot.bad { background: var(--red); }
.kpis { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px; }
.kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 9px; padding: 8px 10px; min-width: 0; }
.kpi label { display: block; color: var(--muted); font-size: 9px; letter-spacing: .4px; }
.kpi b { display: block; font-size: 17px; font-weight: 700; margin: 2px 0; font-variant-numeric: tabular-nums; }
.kpi i { display: block; color: var(--faint); font-size: 9px; font-style: normal; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; margin-top: 10px; overflow: hidden; }
.card-title { display: flex; justify-content: space-between; align-items: baseline; padding: 9px 10px 7px; }
.card-title span { font-size: 11px; font-weight: 650; }
.card-title em { color: var(--faint); font-size: 9px; font-style: normal; }
.scroll { max-height: 210px; overflow: auto; }
.chart { width: 100%; height: 110px; display: block; padding: 0 8px; }
.chart-empty { padding: 26px 10px; text-align: center; color: var(--faint); font-size: 10px; }
.chart-axis { display: flex; justify-content: space-between; padding: 0 10px 8px; color: var(--faint); font-size: 9px; }
table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 10px; }
th, td { padding: 6px 8px; text-align: left; white-space: nowrap; border-bottom: 1px solid rgba(35,48,66,.7); }
th { position: sticky; top: 0; background: var(--raised); color: var(--muted); font-size: 9px; z-index: 1; }
tbody tr:last-child td { border-bottom: 0; }
.q-cell { max-width: 190px; overflow: hidden; text-overflow: ellipsis; }
td.empty { text-align: center; color: var(--faint); padding: 18px 8px; white-space: normal; }
.badge { display: inline-block; padding: 1px 5px; border-radius: 5px; background: #263447; color: #d3deea; font-size: 9px; }
.pos { color: var(--green); }
.neg { color: var(--red); }
.flat { color: var(--muted); }
.sub { color: var(--faint); font-size: 9px; }
footer { padding: 0 20px 24px; color: var(--faint); font-size: 9px; }
"""


def render(state: Dict[str, Any], history: Optional[Dict[str, List[Dict[str, float]]]] = None,
           note: str = "") -> str:
    history = history if history is not None else engine.load_equity_history()
    columns = "".join(
        trader_column(t, state["accounts"][t["id"]], history.get(t["id"], []))
        for t in engine.TRADERS
    )
    updated = state.get("updated_at") or "—"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>跟单纸盘 · Polymarket</title>
<style>{CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <h1>跟单纸盘</h1><span>COPY PAPER · 24/7 无人值守</span>
    <nav class="tabs">
      <a href="index.html">纸面</a>
      <a href="copy.html" class="active">跟单纸盘</a>
    </nav>
  </div>
  <div class="meta">
    <div>每分钟轮询 · 三账户各 $10</div>
    <div>最后更新 {esc(updated)}</div>
  </div>
</header>
<main class="grid">{columns}</main>
<footer>
  跟单口径：零回填；首次开仓用可用现金 50%；加仓按对方比例；部分减仓同比例；买用卖一、卖用买一。
  {esc(note)}
</footer>
</body>
</html>
"""


def write(state: Dict[str, Any], history: Optional[Dict[str, List[Dict[str, float]]]] = None) -> Path:
    html = render(state, history)
    OUT_FILE.write_text(html, encoding="utf-8")
    return OUT_FILE


if __name__ == "__main__":
    write(engine.load_state())
    print(f"[copy_dashboard] 已生成 {OUT_FILE}")
