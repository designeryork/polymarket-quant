"""跟单纸盘页面：三栏（每位交易员一栏），与主面板同一套深色视觉。

每栏包含：
* KPI —— 账户价值 / 总盈亏 / 可用现金 / 持仓数
* 净值走势折线图：年 / 月 / 日 / 时四个窗口，鼠标悬停显示时间与数值
* 当前持仓明细（我们的镜像仓）
* 对手持仓明细（对方真实持仓，用于对照）

三栏宽度可拖拽调整（分隔条），宽度存 localStorage；双击分隔条复位。
"""
from __future__ import annotations

import html as _html
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import copy_engine as engine

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "copy.html"
PAGES_URL = os.getenv("PAGES_URL", "https://designeryork.github.io/polymarket-paper-dashboard")

# 时间窗口：(键, 秒数)。与主面板一致：看最近多久，不是把全部历史聚合
WINDOWS: Tuple[Tuple[str, int], ...] = (
    ("year", 365 * 86400),
    ("month", 30 * 86400),
    ("day", 86400),
    ("hour", 2 * 3600),
)
WINDOW_LABEL = {"year": "年", "month": "月", "day": "日", "hour": "时"}
MAX_POINTS = 150


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
# 净值窗口与聚合
# ---------------------------------------------------------------------------
def window_points(points: List[Dict[str, float]], seconds: int,
                  now: Optional[int] = None) -> List[Dict[str, float]]:
    """取最近 seconds 秒内的点；窗口内点数超过 MAX_POINTS 时按时间桶聚合。"""
    now = int(now or time.time())
    cutoff = now - seconds
    subset = [p for p in points if int(p.get("t") or 0) >= cutoff]
    if not subset:
        subset = points[-1:] if points else []
    if len(subset) <= MAX_POINTS:
        return subset
    step = len(subset) / float(MAX_POINTS)
    sampled = []
    for i in range(MAX_POINTS):
        bucket = subset[int(i * step): max(int(i * step) + 1, int((i + 1) * step))]
        if bucket:
            sampled.append(bucket[-1])
    return sampled


# ---------------------------------------------------------------------------
# 折线图（内嵌 SVG，无外部依赖）
# ---------------------------------------------------------------------------
W, H = 400, 130
PAD_L, PAD_R, PAD_T, PAD_B = 10, 10, 12, 18


def _x_at(i: int, n: int) -> float:
    if n <= 1:
        return (W - PAD_L - PAD_R) / 2 + PAD_L
    return PAD_L + i * (W - PAD_L - PAD_R) / (n - 1)


def _y_at(v: float, lo: float, hi: float) -> float:
    return PAD_T + (hi - v) / max(1e-9, hi - lo) * (H - PAD_T - PAD_B)


def chart_view(trader_id: str, gran: str, points: List[Dict[str, float]],
               start_value: float, active: bool) -> str:
    values = [float(p.get("e") or 0.0) for p in points]
    if not values:
        return (f'<div class="chart-view" data-gran="{gran}"'
                f'{" hidden" if not active else ""}><div class="chart-empty">暂无净值数据</div></div>')

    lo = min(values + [start_value])
    hi = max(values + [start_value])
    if abs(hi - lo) < 1e-6:
        lo -= 0.1
        hi += 0.1
    span = hi - lo
    lo -= span * 0.06
    hi += span * 0.06

    n = len(values)
    coords = [(_x_at(i, n), _y_at(v, lo, hi)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"{PAD_L},{H - PAD_B} " + line + f" {coords[-1][0]:.1f},{H - PAD_B}"
    # 颜色以"最后一笔 vs 本金"为准，和 KPI 的总盈亏口径一致
    rising = values[-1] >= start_value
    stroke = "#34d399" if rising else "#fb7185"
    gid = f"g{abs(hash((trader_id, gran))) % 100000}"
    base_y = _y_at(start_value, lo, hi)
    dots = ""
    if n <= 80:
        dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.7" fill="{stroke}" opacity=".85"/>'
                       for x, y in coords)
    ticks = ""
    for k in range(4):
        val = lo + (hi - lo) * (k / 3)
        yy = _y_at(val, lo, hi)
        ticks += (f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" '
                  f'stroke="#334155" stroke-width=".6"/>')

    xs = sorted({0, n // 2, n - 1}) if n > 1 else [0]
    labels = "".join(
        f'<span>{fmt_ts(points[i].get("t"), with_date=(gran in ("year", "month", "day")))}</span>'
        for i in xs
    )
    return f"""
    <div class="chart-view" data-gran="{gran}"{'' if active else ' hidden'}>
      <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-label="净值走势">
        <defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="{stroke}" stop-opacity=".26"/>
          <stop offset="100%" stop-color="{stroke}" stop-opacity="0"/>
        </linearGradient></defs>
        {ticks}
        <line x1="{PAD_L}" y1="{base_y:.1f}" x2="{W - PAD_R}" y2="{base_y:.1f}"
              stroke="#64748b" stroke-width=".8" stroke-dasharray="3 3"/>
        <polygon points="{area}" fill="url(#{gid})"/>
        <polyline points="{line}" fill="none" stroke="{stroke}" stroke-width="1.6"
                  stroke-linejoin="round" stroke-linecap="round"/>
        {dots}
      </svg>
      <div class="chart-axis">{labels}</div>
      <div class="chart-range"><span>${lo:.2f}</span><span>本金 ${start_value:.2f}</span><span>${hi:.2f}</span></div>
    </div>"""


def chart_block(trader_id: str, history: List[Dict[str, float]],
                start_value: float) -> str:
    now = int(time.time())
    views = "".join(
        chart_view(trader_id, gran, window_points(history, seconds, now), start_value, gran == "hour")
        for gran, seconds in WINDOWS
    )
    tabs = "".join(
        f'<button class="gran{" active" if gran == "hour" else ""}" data-gran="{gran}">{WINDOW_LABEL[gran]}</button>'
        for gran, _ in WINDOWS
    )
    return f"""
      <div class="chart-box" data-trader="{esc(trader_id)}">
        <div class="card-title"><span>净值走势</span><span class="gran-tabs">{tabs}</span></div>
        {views}
      </div>"""


def chart_data(histories: Dict[str, List[Dict[str, float]]]) -> Dict[str, List[List[float]]]:
    """给前端悬停用的点集：{"trader|gran": [[ts, value], ...]}"""
    now = int(time.time())
    out: Dict[str, List[List[float]]] = {}
    for trader_id, history in histories.items():
        for gran, seconds in WINDOWS:
            pts = window_points(history, seconds, now)
            out[f"{trader_id}|{gran}"] = [[int(p.get("t") or 0), round(float(p.get("e") or 0.0), 4)]
                                          for p in pts]
    return out


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
def ours_rows(account: Dict[str, Any]) -> str:
    positions = list((account.get("positions") or {}).values())
    if not positions:
        return '<tr><td colspan="8" class="empty">暂无跟单持仓（等待对方开新仓）</td></tr>'
    rows = []
    for p in sorted(positions, key=lambda x: -float(x.get("shares") or 0)):
        shares = float(p.get("shares") or 0.0)
        entry = float(p.get("entry_price") or p.get("avg_price") or 0.0)
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
          <td>{entry:.3f}</td>
          <td>{avg:.3f}</td>
          <td>{mark:.3f}</td>
          <td>${value:.2f}</td>
          <td class="{pnl_class(pnl)}">${fmt_signed(pnl)} <span class="sub">({fmt_signed(pct, 1)}%)</span></td>
        </tr>""")
    return "".join(rows)


def target_rows(account: Dict[str, Any]) -> str:
    positions = list((account.get("target_positions") or {}).values())
    if not positions:
        return '<tr><td colspan="7" class="empty">对方当前无持仓</td></tr>'
    rows = []
    for p in sorted(positions, key=lambda x: -float(x.get("value_usd") or 0)):
        pnl = float(p.get("unrealized_pnl") or 0.0)
        rows.append(f"""
        <tr>
          <td class="q-cell" title="{esc(p.get('title'))}">{esc(p.get('title'))}</td>
          <td><span class="badge">{esc(p.get('outcome'))}</span></td>
          <td>{float(p.get('size') or 0):.2f}</td>
          <td>{float(p.get('avg_price') or 0):.3f}</td>
          <td>{float(p.get('current_price') or 0):.3f}</td>
          <td>${float(p.get('value_usd') or 0):.2f}</td>
          <td class="{pnl_class(pnl)}">${fmt_signed(pnl)}</td>
        </tr>""")
    return "".join(rows)


REASON_LABEL = {"REDUCE": "减仓", "CLOSE": "平仓", "SETTLED": "结算"}


def ours_closed_rows(account: Dict[str, Any]) -> str:
    records = list(account.get("closed_trades") or [])[:15]
    if not records:
        return '<tr><td colspan="8" class="empty">暂无平仓记录</td></tr>'
    rows = []
    for r in records:
        pnl = float(r.get("realized_pnl") or 0.0)
        rows.append(f"""
        <tr>
          <td class="q-cell" title="{esc(r.get('title'))}">{esc(r.get('title'))}</td>
          <td><span class="badge">{esc(r.get('outcome'))}</span></td>
          <td>{float(r.get('shares') or 0):.2f}</td>
          <td>{float(r.get('avg_price') or 0):.3f}</td>
          <td>{float(r.get('exit_price') or 0):.3f}</td>
          <td class="{pnl_class(pnl)}">${fmt_signed(pnl)}</td>
          <td class="sub">{esc(REASON_LABEL.get(str(r.get('reason')), r.get('reason') or '—'))}</td>
          <td class="sub">{fmt_ts(r.get('closed_at'))}</td>
        </tr>""")
    return "".join(rows)


def target_closed_rows(account: Dict[str, Any]) -> str:
    records = list(account.get("target_closed") or [])[:15]
    if not records:
        return '<tr><td colspan="7" class="empty">暂无对方平仓记录</td></tr>'
    rows = []
    for r in records:
        pnl = float(r.get("realized_pnl") or 0.0)
        rows.append(f"""
        <tr>
          <td class="q-cell" title="{esc(r.get('title'))}">{esc(r.get('title'))}</td>
          <td><span class="badge">{esc(r.get('outcome'))}</span></td>
          <td>{float(r.get('size') or 0):.2f}</td>
          <td>{float(r.get('avg_price') or 0):.3f}</td>
          <td>{float(r.get('exit_price') or 0):.3f}</td>
          <td class="{pnl_class(pnl)}">${fmt_signed(pnl)}</td>
          <td class="sub">{fmt_ts(r.get('closed_at'))}</td>
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
    start = float(account.get("starting_cash_usd") or 10.0)

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
      <div class="kpi"><label>账户价值</label><b>${equity:.2f}</b><i>本金 ${start:.2f}</i></div>
      <div class="kpi"><label>总盈亏</label><b class="{pnl_class(pnl)}">${fmt_signed(pnl)}</b><i class="{pnl_class(pnl)}">{fmt_signed(pct, 1)}%</i></div>
      <div class="kpi"><label>可用现金</label><b>${cash:.2f}</b><i>现金比例 {cash / max(1e-9, equity) * 100:.0f}%</i></div>
      <div class="kpi"><label>持仓</label><b>{count}</b><i>对方 {target_count} 个</i></div>
    </div>

    <div class="card">{chart_block(trader['id'], history, start)}</div>

    <div class="card">
      <div class="card-title"><span>当前持仓明细</span><em>跟单仓 · 买一计价</em></div>
      <div class="scroll">
        <table>
          <thead><tr><th>市场</th><th>方向</th><th>份额</th><th>买入价</th><th>均价</th><th>现价</th><th>市值</th><th>浮盈亏</th></tr></thead>
          <tbody>{ours_rows(account)}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><span>对手持仓明细</span><em>{esc(trader['name'])} 实盘</em></div>
      <div class="scroll">
        <table>
          <thead><tr><th>市场</th><th>方向</th><th>份额</th><th>买入均价</th><th>现价</th><th>市值</th><th>浮盈亏</th></tr></thead>
          <tbody>{target_rows(account)}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><span>平仓记录</span><em>我方 · 最近 15 笔</em></div>
      <div class="scroll">
        <table>
          <thead><tr><th>市场</th><th>方向</th><th>份额</th><th>均价</th><th>平仓价</th><th>已实现</th><th>方式</th><th>时间</th></tr></thead>
          <tbody>{ours_closed_rows(account)}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><span>对手平仓记录</span><em>{esc(trader['name'])} 实盘 · 最近 15 笔</em></div>
      <div class="scroll">
        <table>
          <thead><tr><th>市场</th><th>方向</th><th>份额</th><th>均价</th><th>平仓价</th><th>已实现</th><th>时间</th></tr></thead>
          <tbody>{target_closed_rows(account)}</tbody>
        </table>
      </div>
    </div>
  </section>"""


CSS = """
:root {
  --bg: #0b1119; --panel: #111a25; --raised: #16202d; --line: #232f3e;
  --text: #d7e0ea; --muted: #7f90a5; --faint: #5b6b7f;
  --green: #34d399; --red: #fb7185; --accent: #7dd3fc;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: radial-gradient(1200px 500px at 50% -10%, #16233a 0%, var(--bg) 55%);
  color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
  "Microsoft YaHei", sans-serif; font-size: 12px; overflow-x: hidden;
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
.grid { display: flex; gap: 0; padding: 16px 20px 28px; align-items: flex-start; overflow: hidden; }
.trader {
  flex: 1 1 0; min-width: 0;
  background: linear-gradient(180deg, rgba(22,32,45,.85), rgba(17,26,37,.85));
  border: 1px solid var(--line); border-radius: 12px; padding: 12px;
}
.splitter {
  flex: 0 0 12px; align-self: stretch; cursor: col-resize; position: relative;
  border-radius: 6px; margin: 0 2px; touch-action: none;
}
.splitter::after {
  content: ""; position: absolute; left: 5px; top: 50%; transform: translateY(-50%);
  width: 2px; height: 46px; border-radius: 2px;
  background: repeating-linear-gradient(180deg, #3a4b60 0 4px, transparent 4px 8px);
  transition: background .15s ease;
}
.splitter:hover::after { background: repeating-linear-gradient(180deg, var(--accent) 0 4px, transparent 4px 8px); }
@media (max-width: 1180px) {
  .grid { flex-direction: column; gap: 14px; }
  .splitter { display: none; }
  .trader { flex: 1 1 auto !important; width: 100%; }
}
.trader-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.who h2 { margin: 0 0 3px; font-size: 15px; }
.who a { font-size: 10px; color: var(--faint); }
.poll { color: var(--muted); font-size: 9px; text-align: right; line-height: 1.8; white-space: nowrap; }
.dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px; }
.dot.ok { background: var(--green); }
.dot.bad { background: var(--red); }
.kpis { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px; }
.kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 9px; padding: 8px 10px; min-width: 0; }
.kpi label { display: block; color: var(--muted); font-size: 9px; letter-spacing: .4px; }
.kpi b { display: block; font-size: 17px; font-weight: 700; margin: 2px 0; font-variant-numeric: tabular-nums; }
.kpi i { display: block; color: var(--faint); font-size: 9px; font-style: normal; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; margin-top: 10px; overflow: hidden; }
.card-title { display: flex; justify-content: space-between; align-items: center; padding: 9px 10px 7px; gap: 8px; }
.card-title span { font-size: 11px; font-weight: 650; }
.card-title em { color: var(--faint); font-size: 9px; font-style: normal; white-space: nowrap; }
.gran-tabs { display: inline-flex; gap: 2px; padding: 2px; background: var(--raised); border-radius: 7px; }
.gran { appearance: none; border: 0; background: transparent; color: var(--muted); font-family: inherit;
        font-size: 9px; line-height: 1; padding: 4px 7px; border-radius: 5px; cursor: pointer; }
.gran.active { background: #1d2b3d; color: var(--text); }
.chart-view { position: relative; padding: 0 8px 6px; cursor: crosshair; }
.chart-view[hidden] { display: none; }
.chart-view svg { width: 100%; height: 128px; display: block; }
.chart-empty { padding: 30px 10px; text-align: center; color: var(--faint); font-size: 10px; }
.chart-axis { display: flex; justify-content: space-between; color: var(--faint); font-size: 9px; padding: 0 2px; }
.chart-range { display: flex; justify-content: space-between; color: #4d5d70; font-size: 9px; padding: 2px 2px 0; }
.scroll { max-height: 210px; overflow: auto; scrollbar-width: thin; scrollbar-color: #2b3a4d transparent; }
.scroll::-webkit-scrollbar { width: 8px; height: 8px; }
.scroll::-webkit-scrollbar-track { background: transparent; }
.scroll::-webkit-scrollbar-thumb { background: #2b3a4d; border: 2px solid var(--panel); border-radius: 8px; }
.scroll::-webkit-scrollbar-thumb:hover { background: #3b4f66; }
.scroll::-webkit-scrollbar-corner { background: transparent; }
/* 整页滚动条也用同一套深色，避免默认亮灰条 */
html { scrollbar-width: thin; scrollbar-color: #2b3a4d transparent; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #2b3a4d; border-radius: 8px; }
::-webkit-scrollbar-thumb:hover { background: #3b4f66; }
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
.chart-tip {
  position: fixed; z-index: 50; pointer-events: none; padding: 7px 10px;
  background: #0d1724; border: 1px solid #2a3b50; border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,.35); font-size: 11px; line-height: 1.5;
  transform: translate(12px, -50%);
}
.chart-tip b { display: block; color: var(--accent); font-size: 13px; }
.chart-tip span { color: var(--muted); font-size: 10px; }
footer { padding: 0 20px 24px; color: var(--faint); font-size: 9px; }
"""

JS = """
(() => {
  const chartData = JSON.parse(document.getElementById('chart-data').textContent || '{}');
  const tip = document.getElementById('chart-tip');

  document.querySelectorAll('.chart-box').forEach((box) => {
    const trader = box.dataset.trader;
    const buttons = box.querySelectorAll('.gran');
    const views = box.querySelectorAll('.chart-view');
    buttons.forEach((btn) => {
      btn.addEventListener('click', () => {
        buttons.forEach((b) => b.classList.toggle('active', b === btn));
        views.forEach((v) => { v.hidden = v.dataset.gran !== btn.dataset.gran; });
      });
    });

    views.forEach((view) => {
      const pts = chartData[trader + '|' + view.dataset.gran] || [];
      const svg = view.querySelector('svg');
      if (!svg) return;
      const padL = 10, padR = 10, boxW = 400;
      view.addEventListener('mousemove', (ev) => {
        if (!pts.length) return;
        const rect = view.getBoundingClientRect();
        const x = ev.clientX - rect.left;
        let best = 0, bestD = Infinity;
        pts.forEach((p, i) => {
          const frac = pts.length === 1 ? 0.5 :
            (padL + i * (boxW - padL - padR) / (pts.length - 1)) / boxW;
          const px = frac * rect.width;
          const d = Math.abs(px - x);
          if (d < bestD) { bestD = d; best = i; }
        });
        const [ts, val] = pts[best];
        const d = new Date(ts * 1000);
        const pad = (n) => String(n).padStart(2, '0');
        const when = d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate())
          + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + ' UTC';
        tip.innerHTML = '<b>$' + Number(val).toFixed(2) + '</b><span>' + when + '</span>';
        tip.hidden = false;
        tip.style.left = ev.clientX + 'px';
        tip.style.top = ev.clientY + 'px';
      });
      view.addEventListener('mouseleave', () => { tip.hidden = true; });
    });
  });

  // 分栏拖拽：保存的是"比例"而不是像素，三栏宽度之和永远等于容器宽度
  const grid = document.querySelector('.grid');
  const cols = Array.from(grid.querySelectorAll('.trader'));
  const splitters = Array.from(grid.querySelectorAll('.splitter'));
  const KEY = 'copy-dash-grow';
  const applyGrow = (weights) => {
    cols.forEach((c, i) => {
      const w = weights && weights[i] ? weights[i] : 1;
      c.style.flex = w + ' 1 0';
    });
  };
  const saved = JSON.parse(localStorage.getItem(KEY) || 'null');
  applyGrow(Array.isArray(saved) && saved.length === cols.length ? saved : null);
  splitters.forEach((sp, idx) => {
    sp.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      const startX = ev.clientX;
      const base = cols.map((c) => c.getBoundingClientRect().width);
      const pair = base[idx] + base[idx + 1];
      const total = base.reduce((a, b) => a + b, 0);
      const MIN = 200;
      document.body.style.userSelect = 'none';
      const move = (e) => {
        const dx = e.clientX - startX;
        const nl = Math.max(MIN, Math.min(base[idx] + dx, pair - MIN));
        const nr = pair - nl;
        const weights = base.map((w, i) => (i === idx ? nl : i === idx + 1 ? nr : w) / total);
        applyGrow(weights);
      };
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        document.body.style.userSelect = '';
        const widths = cols.map((c) => c.getBoundingClientRect().width);
        const sum = widths.reduce((a, b) => a + b, 0) || 1;
        localStorage.setItem(KEY, JSON.stringify(widths.map((w) => w / sum)));
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
    sp.addEventListener('dblclick', () => {
      localStorage.removeItem(KEY);
      applyGrow(null);
    });
  });
})();
"""


def render(state: Dict[str, Any], history: Optional[Dict[str, List[Dict[str, float]]]] = None,
           note: str = "") -> str:
    history = history if history is not None else engine.load_equity_history(days=7, limit_per_account=None)
    columns = ""
    for index, trader in enumerate(engine.TRADERS):
        if index:
            columns += '<div class="splitter" role="separator" aria-orientation="vertical" title="拖动调整栏宽，双击复位"></div>'
        columns += trader_column(trader, state["accounts"][trader["id"]], history.get(trader["id"], []))
    updated = state.get("updated_at") or "—"
    data_json = json.dumps(chart_data(history), ensure_ascii=False, separators=(",", ":"))
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
    <div>每分钟轮询 · {len(engine.TRADERS)} 个账户各 ${engine.STARTING_CASH_USD:.0f}</div>
    <div>最后更新 {esc(updated)}</div>
  </div>
</header>
<main class="grid">{columns}</main>
<div id="chart-tip" class="chart-tip" hidden></div>
<script type="application/json" id="chart-data">{data_json}</script>
<footer>
  跟单口径：零回填；首次开仓用可用现金 50%；加仓按对方比例；部分减仓同比例；买用卖一、卖用买一。
  {esc(note)}
</footer>
<script>{JS}</script>
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
