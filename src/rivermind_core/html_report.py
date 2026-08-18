from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Sequence

from rivermind_core.coach import (
    CoachExplanation,
    CoachReport,
    CoachSource,
)
from rivermind_core.leaks import LeakCard, LeakDirection, LeakReport, LeakSeverity
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
    leak_report: LeakReport | None = None,
    coach_report: CoachReport | None = None,
    title: str = "RiverMind Poker Analysis",
) -> str:
    total_hands = sum(item.hands for item in stats)
    balanced = all(item.accounting_balanced for item in sessions)
    player_names = ", ".join(item.player_name for item in stats) or "No data"
    generated_at = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    stats_rows = "".join(_stats_row(item) for item in stats)
    session_rows = "".join(_session_row(item) for item in sessions)
    hand_rows = "".join(_hand_row(item) for item in recent_hands)
    leak_section = _leak_section(leak_report, coach_report)
    detected_leaks = 0 if leak_report is None else leak_report.detected_count

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
    .muted {{ color:var(--muted); }} .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:22px; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }}
    .value {{ font-size:25px; font-weight:700; margin-top:5px; }}
    .leak-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
    .leak-card {{ background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--blue); border-radius:12px; padding:16px; }}
    .leak-card.priority {{ border-left-color:var(--red); }}
    .leak-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
    .leak-title {{ font-size:17px; font-weight:700; }} .leak-metric {{ font-size:23px; font-weight:700; margin:12px 0 2px; }}
    .severity {{ font-size:12px; padding:2px 8px; border-radius:999px; background:#1d2942; color:var(--blue); white-space:nowrap; }}
    .severity.priority {{ background:#3b202b; color:var(--red); }}
    .review-prompt {{ margin:12px 0; padding:10px 12px; background:#0f1628; border-radius:8px; }}
    .coach-box {{ margin-top:14px; padding:13px; background:#10182a; border:1px solid #263b61; border-radius:10px; }}
    .coach-head {{ display:flex; justify-content:space-between; gap:10px; color:var(--blue); font-weight:700; }}
    .coach-source {{ color:var(--muted); font-size:11px; font-weight:400; }}
    .coach-box h3 {{ margin:10px 0 6px; font-size:15px; }} .coach-box p {{ margin:7px 0; }}
    .coach-plan {{ margin:8px 0; padding-left:22px; }} .boundary {{ color:var(--muted); font-size:12px; }}
    details {{ margin-top:10px; }} summary {{ cursor:pointer; color:var(--blue); }}
    .evidence {{ margin:8px 0 0; padding-left:20px; color:var(--muted); }}
    .empty {{ background:var(--panel); border:1px dashed var(--line); border-radius:12px; padding:18px; color:var(--muted); }}
    .table-wrap {{ overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
    table {{ width:100%; border-collapse:collapse; white-space:nowrap; }}
    th,td {{ padding:11px 13px; border-bottom:1px solid var(--line); text-align:right; }}
    th:first-child,td:first-child {{ text-align:left; }} th {{ color:var(--muted); font-weight:600; }}
    tr:last-child td {{ border-bottom:0; }} .positive {{ color:var(--green); }} .negative {{ color:var(--red); }}
    .pill {{ display:inline-block; padding:2px 8px; border-radius:999px; background:#1d2942; color:var(--blue); }}
    footer {{ margin-top:28px; color:var(--muted); font-size:12px; }}
    @media (max-width:760px) {{ .grid,.leak-grid {{ grid-template-columns:1fr; }} main {{ padding:22px 12px 40px; }} }}
  </style>
</head>
<body><main>
  <h1>{escape(title)}</h1>
  <div class="muted">玩家：{escape(player_names)} · 生成时间：{generated_at}</div>
  <section class="grid">
    <div class="card"><div class="muted">统计手数</div><div class="value">{total_hands}</div></div>
    <div class="card"><div class="muted">Session</div><div class="value">{len(sessions)}</div></div>
    <div class="card"><div class="muted">复盘信号</div><div class="value">{detected_leaks}</div></div>
    <div class="card"><div class="muted">账本校验</div><div class="value">{"通过" if balanced else "需检查"}</div></div>
  </section>
  <h2>Leak Cards</h2>
  {leak_section}
  <h2>核心统计</h2>
  <div class="table-wrap"><table><thead><tr><th>玩家</th><th>手数</th>{''.join(f'<th>{METRIC_LABELS[name]}</th>' for name in METRIC_NAMES)}</tr></thead>
  <tbody>{stats_rows or '<tr><td colspan="11">暂无数据</td></tr>'}</tbody></table></div>
  <h2>Session</h2>
  <div class="table-wrap"><table><thead><tr><th>玩家</th><th>开始</th><th>类型</th><th>手数</th><th>结果</th><th>BB</th><th>账本</th></tr></thead>
  <tbody>{session_rows or '<tr><td colspan="7">暂无数据</td></tr>'}</tbody></table></div>
  <h2>最近手牌</h2>
  <div class="table-wrap"><table><thead><tr><th>玩家</th><th>时间 / 手牌</th><th>类型</th><th>位置</th><th>有效筹码</th><th>结果</th><th>BB</th></tr></thead>
  <tbody>{hand_rows or '<tr><td colspan="7">暂无数据</td></tr>'}</tbody></table></div>
  <footer>所有数值由确定性代码从规范化牌谱计算。AI 教练默认使用确定性模板；候选模型文本只有通过证据哈希、字段引用、数值和隐私校验后才能展示。Leak Cards 不代表 GTO 定论。MTT 结果单位为筹码，不代表奖金或 ROI。此页面不包含实时行动建议。</footer>
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


def _leak_section(
    report: LeakReport | None,
    coach_report: CoachReport | None,
) -> str:
    if report is None:
        return '<div class="empty">本次报告未执行漏洞评估。</div>'
    if not report.cards:
        return (
            '<div class="empty">当前没有达到保守触发条件的复盘信号。'
            f"已通过 {report.clear_count} 项，另有 {report.insufficient_sample_count} 项样本不足。"
            f"规则配置：{escape(report.profile_id)} v{escape(report.profile_version)}。</div>"
        )
    coach_by_key = {
        (item.explanation.player_name, item.explanation.rule_id): item.explanation
        for item in (() if coach_report is None else coach_report.items)
    }
    cards = "".join(
        _leak_card(
            card,
            coach_by_key.get(
                (card.assessment.player_name, card.assessment.rule_id)
            ),
        )
        for card in report.cards
    )
    return (
        f'<div class="muted" style="margin-bottom:10px">规则配置：{escape(report.profile_id)} '
        f"v{escape(report.profile_version)}；仅在 95% Wilson 区间整体越过阈值时触发。</div>"
        f'<section class="leak-grid">{cards}</section>'
    )


def _leak_card(
    card: LeakCard,
    explanation: CoachExplanation | None,
) -> str:
    item = card.assessment
    assert item.severity is not None
    assert item.observed_percentage is not None
    assert item.confidence_low is not None
    assert item.confidence_high is not None
    severity_class = (
        "priority" if item.severity == LeakSeverity.PRIORITY else "review"
    )
    severity_label = "优先复盘" if item.severity == LeakSeverity.PRIORITY else "建议复盘"
    direction = "≥" if item.direction == LeakDirection.ABOVE else "≤"
    interval = f"95% 区间 {item.confidence_low:.1f}%–{item.confidence_high:.1f}%"
    observed = f"{item.observed_percentage:.1f}%"
    evidence = "".join(_leak_evidence_hand(hand) for hand in card.evidence_hands)
    if not evidence:
        evidence = "<li>当前筛选范围内没有可展示的证据手牌。</li>"
    coach = "" if explanation is None else _coach_explanation(explanation)
    rule_context = (
        f'<p>{escape(item.rationale)}</p>'
        f'<div class="review-prompt">{escape(item.review_prompt)}</div>'
        if explanation is None
        else ""
    )
    return (
        f'<article class="leak-card {severity_class}">'
        '<div class="leak-head"><div>'
        f'<div class="muted">{escape(item.player_name)} · {escape(item.metric.value)}</div>'
        f'<div class="leak-title">{escape(item.title)}</div></div>'
        f'<span class="severity {severity_class}">{severity_label}</span></div>'
        f'<div class="leak-metric">{observed}</div>'
        f'<div class="muted">{item.occurrences}/{item.opportunities} · {interval} · '
        f"复盘阈值 {direction}{item.trigger_percentage:g}%</div>"
        f"{rule_context}"
        f"{coach}"
        f'<details><summary>查看证据手牌（{len(card.evidence_hands)}）</summary>'
        f'<ul class="evidence">{evidence}</ul></details></article>'
    )


def _coach_explanation(explanation: CoachExplanation) -> str:
    source_labels = {
        CoachSource.TEMPLATE: "确定性模板",
        CoachSource.LLM_VALIDATED: "已校验模型草稿",
        CoachSource.TEMPLATE_FALLBACK: "校验失败·模板回退",
    }
    plan = "".join(
        f"<li>{escape(step)}</li>" for step in explanation.review_plan
    )
    return (
        '<section class="coach-box">'
        '<div class="coach-head"><span>AI 教练</span>'
        f'<span class="coach-source">{source_labels[explanation.source]} · '
        f"证据 {escape(explanation.evidence_hash[:12])}</span></div>"
        f"<h3>{escape(explanation.headline)}</h3>"
        f"<p>{escape(explanation.observation)}</p>"
        f"<p>{escape(explanation.teaching_point)}</p>"
        f'<ol class="coach-plan">{plan}</ol>'
        f'<p class="boundary">边界：{escape(explanation.uncertainty)}</p>'
        "</section>"
    )


def _leak_evidence_hand(report: PlayerHandReport) -> str:
    row = report.stats
    played_at = report.played_at.isoformat(sep=" ") if report.played_at else "未知时间"
    hand_key = f"{row.site} #{row.hand_id}"
    result = f"{report.net_result} {report.result_unit}"
    return (
        f"<li>{escape(played_at)} · {escape(hand_key)} · {escape(row.position.value)} · "
        f"{escape(result)}</li>"
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
