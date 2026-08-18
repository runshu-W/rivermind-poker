from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Sequence

from rivermind_core.reports import PlayerHandReport
from rivermind_core.sessions import SessionSummary
from rivermind_core.stats import METRIC_NAMES, PlayerStats, StatValue


METRIC_LABELS = {
    "vpip": "VPIP",
    "pfr": "PFR",
    "rfi": "RFI",
    "three_bet": "3Bet",
    "call_open": "Call Open",
    "cold_call": "Cold Call",
    "fold_to_three_bet": "Fold to 3Bet",
    "flop_cbet": "Flop CBet",
    "fold_to_flop_cbet": "Fold to Flop CBet",
}


def render_analysis_page(
    stats: Sequence[PlayerStats],
    sessions: Sequence[SessionSummary],
    recent_hands: Sequence[PlayerHandReport],
    *,
    title: str = "RiverMind Poker Analysis",
) -> str:
    total_hands = sum(item.hands for item in stats)
    balanced = all(item.accounting_balanced for item in sessions)
    player_names = ", ".join(item.player_name for item in stats) or "No data"
    generated_at = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    stats_rows = "".join(_stats_row(item) for item in stats)
    session_rows = "".join(_session_row(item) for item in sessions)
    hand_rows = "".join(_hand_row(item) for item in recent_hands)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b1020; --panel:#141b2d; --line:#26324d;
      --text:#edf2ff; --muted:#96a3bd; --green:#54d39a; --red:#ff7b86; --blue:#76a9ff; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 Inter,Segoe UI,sans-serif; }}
    main {{ max-width:1280px; margin:auto; padding:32px 22px 60px; }}
    h1 {{ margin:0 0 6px; font-size:28px; }} h2 {{ margin:32px 0 12px; font-size:18px; }}
    .muted {{ color:var(--muted); }} .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:22px; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }}
    .value {{ font-size:25px; font-weight:700; margin-top:5px; }}
    .table-wrap {{ overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
    table {{ width:100%; border-collapse:collapse; white-space:nowrap; }}
    th,td {{ padding:11px 13px; border-bottom:1px solid var(--line); text-align:right; }}
    th:first-child,td:first-child {{ text-align:left; }} th {{ color:var(--muted); font-weight:600; }}
    tr:last-child td {{ border-bottom:0; }} .positive {{ color:var(--green); }} .negative {{ color:var(--red); }}
    .pill {{ display:inline-block; padding:2px 8px; border-radius:999px; background:#1d2942; color:var(--blue); }}
    footer {{ margin-top:28px; color:var(--muted); font-size:12px; }}
    @media (max-width:760px) {{ .grid {{ grid-template-columns:1fr; }} main {{ padding:22px 12px 40px; }} }}
  </style>
</head>
<body><main>
  <h1>{escape(title)}</h1>
  <div class="muted">玩家：{escape(player_names)} · 生成时间：{generated_at}</div>
  <section class="grid">
    <div class="card"><div class="muted">统计手数</div><div class="value">{total_hands}</div></div>
    <div class="card"><div class="muted">Session</div><div class="value">{len(sessions)}</div></div>
    <div class="card"><div class="muted">账本校验</div><div class="value">{"通过" if balanced else "需检查"}</div></div>
  </section>
  <h2>核心统计</h2>
  <div class="table-wrap"><table><thead><tr><th>玩家</th><th>手数</th>{''.join(f'<th>{METRIC_LABELS[name]}</th>' for name in METRIC_NAMES)}</tr></thead>
  <tbody>{stats_rows or '<tr><td colspan="11">暂无数据</td></tr>'}</tbody></table></div>
  <h2>Session</h2>
  <div class="table-wrap"><table><thead><tr><th>玩家</th><th>开始</th><th>类型</th><th>手数</th><th>结果</th><th>BB</th><th>账本</th></tr></thead>
  <tbody>{session_rows or '<tr><td colspan="7">暂无数据</td></tr>'}</tbody></table></div>
  <h2>最近手牌</h2>
  <div class="table-wrap"><table><thead><tr><th>玩家</th><th>时间 / 手牌</th><th>类型</th><th>位置</th><th>有效筹码</th><th>结果</th><th>BB</th></tr></thead>
  <tbody>{hand_rows or '<tr><td colspan="7">暂无数据</td></tr>'}</tbody></table></div>
  <footer>所有数值由确定性代码从规范化牌谱计算。MTT 结果单位为筹码，不代表奖金或 ROI。此页面不包含实时行动建议。</footer>
</main></body></html>"""


def _stats_row(stats: PlayerStats) -> str:
    values = "".join(
        f"<td>{_format_stat(getattr(stats, name))}</td>" for name in METRIC_NAMES
    )
    return f"<tr><td>{escape(stats.player_name)}</td><td>{stats.hands}</td>{values}</tr>"


def _session_row(session: SessionSummary) -> str:
    result_class = _result_class(session.net_result)
    started = session.started_at.isoformat(sep=" ") if session.started_at else "未知"
    return (
        f"<tr><td>{escape(session.player_name)}</td><td>{escape(started)}</td>"
        f"<td><span class='pill'>{session.game_type.value}</span></td>"
        f"<td>{session.hands}</td><td class='{result_class}'>{session.net_result} {escape(session.result_unit)}</td>"
        f"<td class='{result_class}'>{session.net_result_bb}</td>"
        f"<td>{'通过' if session.accounting_balanced else '需检查'}</td></tr>"
    )


def _hand_row(report: PlayerHandReport) -> str:
    result_class = _result_class(report.net_result)
    played_at = report.played_at.isoformat(sep=" ") if report.played_at else "未知时间"
    hand_label = f"{played_at} / {report.stats.site} #{report.stats.hand_id}"
    return (
        f"<tr><td>{escape(report.stats.player_name)}</td><td>{escape(hand_label)}</td>"
        f"<td>{report.stats.game_type.value}</td>"
        f"<td>{report.stats.position.value}</td><td>{report.stats.effective_stack_bb} BB</td>"
        f"<td class='{result_class}'>{report.net_result} {escape(report.result_unit)}</td>"
        f"<td class='{result_class}'>{report.net_result_bb}</td></tr>"
    )


def _format_stat(value: StatValue) -> str:
    if value.percentage is None:
        return "n/a"
    return f"{value.percentage:.1f}% <span class='muted'>({value.occurrences}/{value.opportunities})</span>"


def _result_class(value: Decimal) -> str:
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return ""
