from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from rivermind_core.coach import (
    CoachReport,
    coach_report_to_dict,
    explain_leak_report,
)
from rivermind_core.html_report import render_analysis_page
from rivermind_core.importer import HandHistoryImporter, ImportBatchReport
from rivermind_core.leaks import LeakAssessment, LeakReport, LeakStatus
from rivermind_core.models import GameType, PlayerPosition
from rivermind_core.parsers import default_registry
from rivermind_core.replay import HandReplay
from rivermind_core.reports import HandQuery, PlayerHandReport, StatMetric
from rivermind_core.sessions import SessionSummary
from rivermind_core.stats import METRIC_NAMES, PlayerStats, StatValue, StatsFilter
from rivermind_core.storage import SQLiteHandStore


HAND_HISTORY_SUFFIXES = {".txt", ".log", ".hh"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rivermind")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import", help="Import hand-history files or folders"
    )
    import_parser.add_argument("paths", nargs="+", type=Path)
    _add_database_argument(import_parser)
    import_parser.add_argument("--json", action="store_true")

    stats_parser = subparsers.add_parser(
        "stats", help="Calculate deterministic player stats"
    )
    _add_database_argument(stats_parser)
    _add_scope_arguments(stats_parser)
    _add_dimension_arguments(stats_parser)
    stats_parser.add_argument("--json", action="store_true")

    leaks_parser = subparsers.add_parser(
        "leaks", help="Evaluate deterministic review signals with hand evidence"
    )
    _add_database_argument(leaks_parser)
    _add_scope_arguments(leaks_parser)
    _add_dimension_arguments(leaks_parser)
    leaks_parser.add_argument("--evidence-limit", type=int, default=5)
    leaks_parser.add_argument("--json", action="store_true")

    coach_parser = subparsers.add_parser(
        "coach", help="Generate evidence-bound Chinese coaching explanations"
    )
    _add_database_argument(coach_parser)
    _add_scope_arguments(coach_parser)
    _add_dimension_arguments(coach_parser)
    coach_parser.add_argument("--evidence-limit", type=int, default=5)
    coach_parser.add_argument("--json", action="store_true")

    sessions_parser = subparsers.add_parser(
        "sessions", help="Summarize cash sessions and tournaments"
    )
    _add_database_argument(sessions_parser)
    _add_scope_arguments(sessions_parser)
    _add_dimension_arguments(sessions_parser)
    sessions_parser.add_argument("--gap-minutes", type=int, default=30)
    sessions_parser.add_argument("--json", action="store_true")

    hands_parser = subparsers.add_parser(
        "hands", help="Find related hands with metric evidence"
    )
    _add_database_argument(hands_parser)
    _add_scope_arguments(hands_parser)
    _add_dimension_arguments(hands_parser)
    hands_parser.add_argument("--metric", choices=[item.value for item in StatMetric])
    occurrence = hands_parser.add_mutually_exclusive_group()
    occurrence.add_argument(
        "--occurred", dest="occurred", action="store_const", const=True
    )
    occurrence.add_argument(
        "--missed", dest="occurred", action="store_const", const=False
    )
    hands_parser.add_argument("--start", type=_parse_datetime)
    hands_parser.add_argument("--end", type=_parse_datetime)
    hands_parser.add_argument("--limit", type=int, default=100)
    hands_parser.add_argument("--offset", type=int, default=0)
    hands_parser.add_argument("--json", action="store_true")

    replay_parser = subparsers.add_parser(
        "replay", help="Return structured action-by-action replay data"
    )
    replay_parser.add_argument("site")
    replay_parser.add_argument("hand_id")
    _add_database_argument(replay_parser)
    replay_parser.add_argument("--json", action="store_true")

    report_parser = subparsers.add_parser(
        "report", help="Generate the local H2N-lite analysis page"
    )
    _add_database_argument(report_parser)
    _add_scope_arguments(report_parser)
    _add_dimension_arguments(report_parser)
    report_parser.add_argument(
        "--output", type=Path, default=Path("rivermind-report.html")
    )
    report_parser.add_argument("--title", default="RiverMind Poker Analysis")
    report_parser.add_argument("--recent-limit", type=int, default=50)
    report_parser.add_argument("--evidence-limit", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runners = {
        "import": _run_import,
        "stats": _run_stats,
        "leaks": _run_leaks,
        "coach": _run_coach,
        "sessions": _run_sessions,
        "hands": _run_hands,
        "replay": _run_replay,
        "report": _run_report,
    }
    return runners[args.command](args)


def _run_import(args: argparse.Namespace) -> int:
    try:
        files = _expand_paths(args.paths)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not files:
        print("error: no hand-history files found", file=sys.stderr)
        return 2

    reports: list[ImportBatchReport] = []
    with SQLiteHandStore(args.database) as store:
        importer = HandHistoryImporter(default_registry(), store)
        for file_path in files:
            reports.append(
                importer.import_text(str(file_path), _read_text(file_path))
            )

    if args.json:
        print(json.dumps([_import_report_dict(item) for item in reports], ensure_ascii=False))
    else:
        _print_import_reports(reports, args.database)
    return 2 if any(item.failed or item.unsupported for item in reports) else 0


def _run_stats(args: argparse.Namespace) -> int:
    if not _database_exists(args.database):
        return 2
    try:
        stat_filter = _stats_filter(args)
    except ValueError as exc:
        return _print_value_error(exc)
    player_name, heroes_only = _scope(args)
    with SQLiteHandStore(args.database) as store:
        stats = store.query_player_stats(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter,
        )
    if args.json:
        print(json.dumps({"scope": _scope_dict(args), "players": [_player_stats_dict(item) for item in stats]}, ensure_ascii=False))
    else:
        _print_stats(stats)
    return 0


def _run_leaks(args: argparse.Namespace) -> int:
    if not _database_exists(args.database):
        return 2
    try:
        stat_filter = _stats_filter(args)
        if not 1 <= args.evidence_limit <= 20:
            raise ValueError("evidence-limit must be between 1 and 20")
    except ValueError as exc:
        return _print_value_error(exc)
    player_name, heroes_only = _scope(args)
    with SQLiteHandStore(args.database) as store:
        report = store.query_leaks(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter,
            evidence_limit=args.evidence_limit,
        )
    if args.json:
        payload = _leak_report_dict(report)
        payload["scope"] = _scope_dict(args)
        print(json.dumps(payload, ensure_ascii=False))
    else:
        _print_leaks(report)
    return 0


def _run_coach(args: argparse.Namespace) -> int:
    if not _database_exists(args.database):
        return 2
    try:
        stat_filter = _stats_filter(args)
        if not 1 <= args.evidence_limit <= 20:
            raise ValueError("evidence-limit must be between 1 and 20")
    except ValueError as exc:
        return _print_value_error(exc)
    player_name, heroes_only = _scope(args)
    with SQLiteHandStore(args.database) as store:
        leak_report = store.query_leaks(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter,
            evidence_limit=args.evidence_limit,
        )
    report = explain_leak_report(leak_report)
    if args.json:
        payload = coach_report_to_dict(report)
        payload["scope"] = _scope_dict(args)
        print(json.dumps(payload, ensure_ascii=False))
    else:
        _print_coach(report)
    return 0


def _run_sessions(args: argparse.Namespace) -> int:
    if not _database_exists(args.database):
        return 2
    if args.gap_minutes < 0:
        return _print_value_error(ValueError("gap-minutes cannot be negative"))
    try:
        stat_filter = _stats_filter(args)
    except ValueError as exc:
        return _print_value_error(exc)
    player_name, heroes_only = _scope(args)
    with SQLiteHandStore(args.database) as store:
        sessions = store.query_sessions(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter,
            cash_gap=timedelta(minutes=args.gap_minutes),
        )
    if args.json:
        print(json.dumps([_session_dict(item) for item in sessions], ensure_ascii=False))
    else:
        _print_sessions(sessions)
    return 0


def _run_hands(args: argparse.Namespace) -> int:
    if not _database_exists(args.database):
        return 2
    try:
        query = HandQuery(
            stat_filter=_stats_filter(args),
            metric=None if args.metric is None else StatMetric(args.metric),
            occurred=args.occurred,
            started_at=args.start,
            ended_at=args.end,
            limit=args.limit,
            offset=args.offset,
        )
    except ValueError as exc:
        return _print_value_error(exc)
    player_name, heroes_only = _scope(args)
    with SQLiteHandStore(args.database) as store:
        hands = store.query_hands(
            player_name=player_name,
            heroes_only=heroes_only,
            query=query,
        )
    if args.json:
        print(json.dumps([_hand_report_dict(item) for item in hands], ensure_ascii=False))
    else:
        _print_hands(hands)
    return 0


def _run_replay(args: argparse.Namespace) -> int:
    if not _database_exists(args.database):
        return 2
    with SQLiteHandStore(args.database) as store:
        replay = store.load_replay(args.site, args.hand_id)
    if replay is None:
        print(f"error: hand not found: {args.site} #{args.hand_id}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_replay_dict(replay), ensure_ascii=False))
    else:
        _print_replay(replay)
    return 0


def _run_report(args: argparse.Namespace) -> int:
    if not _database_exists(args.database):
        return 2
    try:
        stat_filter = _stats_filter(args)
        recent_query = HandQuery(stat_filter=stat_filter, limit=args.recent_limit)
        if not 1 <= args.evidence_limit <= 20:
            raise ValueError("evidence-limit must be between 1 and 20")
    except ValueError as exc:
        return _print_value_error(exc)
    player_name, heroes_only = _scope(args)
    with SQLiteHandStore(args.database) as store:
        stats = store.query_player_stats(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter,
        )
        sessions = store.query_sessions(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter,
        )
        recent_hands = store.query_hands(
            player_name=player_name,
            heroes_only=heroes_only,
            query=recent_query,
        )
        leak_report = store.query_leaks(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter,
            evidence_limit=args.evidence_limit,
        )
        coach_report = explain_leak_report(leak_report)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_analysis_page(
            stats,
            sessions,
            recent_hands,
            leak_report=leak_report,
            coach_report=coach_report,
            title=args.title,
        ),
        encoding="utf-8",
    )
    print(f"Report written: {output}")
    return 0


def _add_database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, default=Path("data/rivermind.db"))


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--player", help="Exact player name; defaults to hero hands")
    scope.add_argument("--all-players", action="store_true")


def _add_dimension_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--game-type", choices=[item.value for item in GameType])
    parser.add_argument(
        "--position",
        nargs="+",
        choices=[item.value for item in PlayerPosition],
    )
    parser.add_argument("--min-effective-stack-bb", type=Decimal)
    parser.add_argument("--max-effective-stack-bb", type=Decimal)


def _stats_filter(args: argparse.Namespace) -> StatsFilter:
    return StatsFilter(
        game_types=(
            frozenset({GameType(args.game_type)})
            if args.game_type is not None
            else frozenset()
        ),
        positions=(
            frozenset(PlayerPosition(item) for item in args.position)
            if args.position
            else frozenset()
        ),
        min_effective_stack_bb=args.min_effective_stack_bb,
        max_effective_stack_bb=args.max_effective_stack_bb,
    )


def _scope(args: argparse.Namespace) -> tuple[str | None, bool]:
    return args.player, args.player is None and not args.all_players


def _scope_dict(args: argparse.Namespace) -> dict[str, object]:
    return {
        "player": args.player,
        "heroes_only": args.player is None and not args.all_players,
        "game_type": args.game_type,
        "positions": args.position,
        "min_effective_stack_bb": (
            None
            if args.min_effective_stack_bb is None
            else str(args.min_effective_stack_bb)
        ),
        "max_effective_stack_bb": (
            None
            if args.max_effective_stack_bb is None
            else str(args.max_effective_stack_bb)
        ),
    }


def _database_exists(path: Path) -> bool:
    if path.exists():
        return True
    print(f"error: database does not exist: {path}", file=sys.stderr)
    return False


def _print_value_error(error: ValueError) -> int:
    print(f"error: {error}", file=sys.stderr)
    return 2


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use ISO date/time, e.g. 2026-08-18T12:00:00") from exc


def _expand_paths(paths: Sequence[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_file():
            files.add(path.resolve())
            continue
        files.update(
            candidate.resolve()
            for candidate in path.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in HAND_HISTORY_SUFFIXES
        )
    return sorted(files)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"Cannot decode hand-history file: {path}")


def _import_report_dict(report: ImportBatchReport) -> dict[str, object]:
    return {
        "batch_id": report.batch_id,
        "source_name": report.source_name,
        "detected": report.detected,
        "imported": report.imported,
        "duplicates": report.duplicates,
        "failed": report.failed,
        "unsupported": report.unsupported,
        "elapsed_ms": report.elapsed_ms,
        "items": [
            {
                "index": item.index,
                "lines": [item.start_line, item.end_line],
                "status": item.status.value,
                "site": item.source_site,
                "parser": item.parser_name,
                "hand_id": item.hand_id,
                "error_code": item.error_code,
                "message": item.message,
            }
            for item in report.items
        ],
    }


def _player_stats_dict(stats: PlayerStats) -> dict[str, object]:
    return {
        "player": stats.player_name,
        "hands": stats.hands,
        **{
            name: _stat_value_dict(getattr(stats, name))
            for name in METRIC_NAMES
        },
    }


def _stat_value_dict(value: StatValue) -> dict[str, int | float | None]:
    return {
        "occurrences": value.occurrences,
        "opportunities": value.opportunities,
        "percentage": None if value.percentage is None else round(value.percentage, 2),
    }


def _session_dict(session: SessionSummary) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "player": session.player_name,
        "game_type": session.game_type.value,
        "currency": session.currency,
        "tournament_id": session.tournament_id,
        "started_at": None if session.started_at is None else session.started_at.isoformat(),
        "ended_at": None if session.ended_at is None else session.ended_at.isoformat(),
        "hands": session.hands,
        "net_result": str(session.net_result),
        "result_unit": session.result_unit,
        "net_result_bb": str(session.net_result_bb),
        "accounting_balanced": session.accounting_balanced,
        "hand_keys": [list(item) for item in session.hand_keys],
    }


def _hand_report_dict(report: PlayerHandReport) -> dict[str, object]:
    row = report.stats
    metrics = {
        name: {
            "occurred": bool(getattr(row, name)),
            "opportunity": bool(getattr(row, f"{name}_opportunity", True)),
        }
        for name in METRIC_NAMES
    }
    return {
        "site": row.site,
        "hand_id": row.hand_id,
        "player": row.player_name,
        "played_at": None if report.played_at is None else report.played_at.isoformat(),
        "game_type": row.game_type.value,
        "tournament_id": row.tournament_id,
        "table_name": report.table_name,
        "currency": report.currency,
        "position": row.position.value,
        "starting_stack_bb": str(row.starting_stack_bb),
        "effective_stack_bb": str(row.effective_stack_bb),
        "invested": str(report.invested),
        "returned": str(report.returned),
        "collected": str(report.collected),
        "net_result": str(report.net_result),
        "result_unit": report.result_unit,
        "net_result_bb": str(report.net_result_bb),
        "accounting_balanced": report.accounting_balanced,
        "metrics": metrics,
    }


def _leak_report_dict(report: LeakReport) -> dict[str, object]:
    return {
        "profile": {
            "id": report.profile_id,
            "version": report.profile_version,
            "method": "95% Wilson interval must fully cross the review threshold",
        },
        "summary": {
            "detected": report.detected_count,
            "clear": report.clear_count,
            "insufficient_sample": report.insufficient_sample_count,
        },
        "assessments": [_leak_assessment_dict(item) for item in report.assessments],
        "cards": [
            {
                **_leak_assessment_dict(card.assessment),
                "evidence_hands": [
                    _hand_report_dict(hand) for hand in card.evidence_hands
                ],
            }
            for card in report.cards
        ],
    }


def _leak_assessment_dict(assessment: LeakAssessment) -> dict[str, object]:
    confidence_interval = None
    if assessment.confidence_low is not None and assessment.confidence_high is not None:
        confidence_interval = [
            round(assessment.confidence_low, 2),
            round(assessment.confidence_high, 2),
        ]
    return {
        "rule_id": assessment.rule_id,
        "rule_version": assessment.profile_version,
        "player": assessment.player_name,
        "title": assessment.title,
        "status": assessment.status.value,
        "severity": None if assessment.severity is None else assessment.severity.value,
        "metric": assessment.metric.value,
        "direction": assessment.direction.value,
        "occurrences": assessment.occurrences,
        "opportunities": assessment.opportunities,
        "observed_percentage": (
            None
            if assessment.observed_percentage is None
            else round(assessment.observed_percentage, 2)
        ),
        "confidence_interval_95": confidence_interval,
        "threshold": {
            "trigger_percentage": assessment.trigger_percentage,
            "priority_percentage": assessment.priority_percentage,
            "min_opportunities": assessment.min_opportunities,
        },
        "sample_shortfall": assessment.sample_shortfall,
        "evidence_selector": {
            "metric": assessment.metric.value,
            "occurred": assessment.evidence_occurred,
        },
        "rationale": assessment.rationale,
        "review_prompt": assessment.review_prompt,
    }


def _replay_dict(replay: HandReplay) -> dict[str, object]:
    return {
        "site": replay.site,
        "hand_id": replay.hand_id,
        "table_name": replay.table_name,
        "played_at_raw": replay.played_at_raw,
        "button_seat": replay.button_seat,
        "blinds": [str(replay.small_blind), str(replay.big_blind)],
        "board": list(replay.board),
        "accounting_balanced": replay.accounting_balanced,
        "players": [
            {
                "seat": item.seat,
                "name": item.name,
                "position": None if item.position is None else item.position.value,
                "starting_stack": str(item.starting_stack),
                "starting_stack_bb": None if item.starting_stack_bb is None else str(item.starting_stack_bb),
                "is_hero": item.is_hero,
                "hole_cards": list(item.hole_cards),
            }
            for item in replay.players
        ],
        "frames": [
            {
                "sequence": item.sequence,
                "street": item.street.value,
                "board": list(item.visible_board),
                "player": item.player_name,
                "action": item.action_type.value,
                "amount": None if item.amount is None else str(item.amount),
                "to_amount": None if item.to_amount is None else str(item.to_amount),
                "is_all_in": item.is_all_in,
                "invested": str(item.invested),
                "returned": str(item.returned),
                "collected": str(item.collected),
                "pot_after": str(item.pot_after),
            }
            for item in replay.frames
        ],
        "results": [
            {
                "player": item.player_name,
                "invested": str(item.invested),
                "returned": str(item.returned),
                "collected": str(item.collected),
                "net_result": str(item.net_result),
                "net_result_bb": str(item.net_result_bb),
            }
            for item in replay.results
        ],
    }


def _print_import_reports(reports: Sequence[ImportBatchReport], database: Path) -> None:
    print(
        "Import complete: "
        f"{sum(item.detected for item in reports)} detected, "
        f"{sum(item.imported for item in reports)} imported, "
        f"{sum(item.duplicates for item in reports)} duplicate, "
        f"{sum(item.failed for item in reports)} failed, "
        f"{sum(item.unsupported for item in reports)} unsupported"
    )
    print(f"Database: {database}")
    for report in reports:
        for item in report.items:
            if item.error_code:
                print(f"- {report.source_name}:{item.start_line}-{item.end_line} [{item.status.value}/{item.error_code}] {item.message}")


def _print_stats(stats: Sequence[PlayerStats]) -> None:
    if not stats:
        print("No matching hands found")
        return
    print("Player                 Hands    VPIP     PFR     RFI    3Bet   CallOp  ColdCall   F3Bet   FCBet  FoldFCB")
    for item in stats:
        values = " ".join(f"{_format_stat(getattr(item, name)):>8}" for name in METRIC_NAMES)
        print(f"{item.player_name[:20]:20} {item.hands:5d} {values}")


def _print_sessions(sessions: Sequence[SessionSummary]) -> None:
    if not sessions:
        print("No matching sessions found")
        return
    print("Started              Type        Hands       Result          BB  Player")
    for item in sessions:
        started = item.started_at.isoformat(sep=" ") if item.started_at else "unknown"
        print(f"{started:20} {item.game_type.value:10} {item.hands:5d} {str(item.net_result):>10} {item.result_unit:5} {str(item.net_result_bb):>9}  {item.player_name}")


def _print_leaks(report: LeakReport) -> None:
    print(
        f"Leak profile {report.profile_id} v{report.profile_version}: "
        f"{report.detected_count} detected, {report.clear_count} clear, "
        f"{report.insufficient_sample_count} insufficient sample"
    )
    if not report.cards:
        print("No qualified review signals found")
        return
    for card in report.cards:
        item = card.assessment
        assert item.severity is not None
        assert item.observed_percentage is not None
        assert item.confidence_low is not None
        assert item.confidence_high is not None
        interval = f"{item.confidence_low:.1f}-{item.confidence_high:.1f}%"
        evidence = ", ".join(
            f"{hand.stats.site}#{hand.stats.hand_id}" for hand in card.evidence_hands
        )
        print(
            f"[{item.severity.value}] {item.player_name} · {item.title}: "
            f"{item.observed_percentage:.1f}% ({item.occurrences}/{item.opportunities}), "
            f"95% CI {interval}; evidence: {evidence or 'none'}"
        )


def _print_coach(report: CoachReport) -> None:
    if not report.items:
        print("No qualified review signals found")
        return
    for item in report.items:
        explanation = item.explanation
        print(f"{explanation.player_name} · {explanation.headline}")
        print(explanation.observation)
        print(explanation.teaching_point)
        for index, step in enumerate(explanation.review_plan, start=1):
            print(f"  {index}. {step}")
        print(f"Boundary: {explanation.uncertainty}")
        print(
            f"Source: {explanation.source.value} · evidence {explanation.evidence_hash[:12]}"
        )


def _print_hands(hands: Sequence[PlayerHandReport]) -> None:
    if not hands:
        print("No matching hands found")
        return
    print("Played at           Hand                    Pos   Eff BB     Result       BB")
    for item in hands:
        played_at = item.played_at.isoformat(sep=" ") if item.played_at else "unknown"
        hand_key = f"{item.stats.site}#{item.stats.hand_id}"
        print(f"{played_at:19} {hand_key:23} {item.stats.position.value:5} {str(item.stats.effective_stack_bb):>7} {str(item.net_result):>10} {str(item.net_result_bb):>8}")


def _print_replay(replay: HandReplay) -> None:
    print(f"{replay.site} #{replay.hand_id} · {replay.table_name}")
    for frame in replay.frames:
        amount = "" if frame.amount is None else f" {frame.amount}"
        print(f"{frame.sequence:02d} {frame.street.value:8} {frame.player_name:18} {frame.action_type.value}{amount} · pot {frame.pot_after}")


def _format_stat(value: StatValue) -> str:
    return "n/a" if value.percentage is None else f"{value.percentage:.1f}%"
