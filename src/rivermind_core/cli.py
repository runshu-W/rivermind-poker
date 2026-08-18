from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Sequence

from rivermind_core.coach import (
    CoachReport,
    CoachSource,
    coach_item_to_dict,
    coach_report_to_dict,
    explain_leak_report,
)
from rivermind_core.coach_evals import (
    coach_eval_report_to_dict,
    run_coach_eval,
)
from rivermind_core.coach_review import (
    build_blind_review_case,
    build_expert_review_document,
    expert_review_report_to_dict,
    load_and_score_expert_reviews,
)
from rivermind_core.coach_runtime import (
    CoachRuntimePolicy,
    coach_call_audit_to_dict,
    run_coach_provider,
)
from rivermind_core.html_report import render_analysis_page
from rivermind_core.gto_matcher import (
    MatchStatus,
    extract_decision_game_spec,
    match_game_spec,
)
from rivermind_core.gto_specs import (
    RakeSpec,
    SolutionObjective,
    SolutionQuality,
    SpecValidationError,
    load_solution_catalog,
)
from rivermind_core.importer import HandHistoryImporter, ImportBatchReport
from rivermind_core.leaks import LeakAssessment, LeakReport, LeakStatus
from rivermind_core.models import GameType, PlayerPosition
from rivermind_core.openai_provider import (
    OpenAIResponsesCoachProvider,
    OpenAIResponsesConfig,
)
from rivermind_core.parsers import default_registry
from rivermind_core.replay import HandReplay
from rivermind_core.reports import HandQuery, PlayerHandReport, StatMetric
from rivermind_core.sessions import SessionSummary
from rivermind_core.stats import METRIC_NAMES, PlayerStats, StatValue, StatsFilter
from rivermind_core.storage import SQLiteHandStore
from rivermind_core.quality_gate import verify_quality_attestation
from rivermind_core.strategy_artifacts import (
    STRATEGY_ARTIFACT_SCHEMA_VERSION,
    ArtifactValidationError,
    load_artifact_document,
    resolve_artifact_path,
    serialize_strategy_artifact,
    strategy_artifact_from_dict,
    verify_catalog_artifact,
)
from rivermind_core.strategy_query import (
    ComboNotCoveredError,
    StrategyQueryError,
    build_strategy_evidence,
)


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

    coach_eval_parser = subparsers.add_parser(
        "coach-eval", help="Run the offline candidate contract and adversarial corpus"
    )
    coach_eval_parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("evals/coach_candidate_cases.json"),
    )
    coach_eval_parser.add_argument("--json", action="store_true")

    coach_openai_parser = subparsers.add_parser(
        "coach-openai",
        help="Explain exactly one leak card through the opt-in OpenAI adapter",
    )
    _add_database_argument(coach_openai_parser)
    _add_scope_arguments(coach_openai_parser)
    _add_dimension_arguments(coach_openai_parser)
    coach_openai_parser.add_argument("--rule-id", required=True)
    coach_openai_parser.add_argument("--model", required=True)
    coach_openai_parser.add_argument(
        "--input-usd-per-million", type=Decimal, required=True
    )
    coach_openai_parser.add_argument(
        "--output-usd-per-million", type=Decimal, required=True
    )
    coach_openai_parser.add_argument("--evidence-limit", type=int, default=5)
    coach_openai_parser.add_argument("--timeout-seconds", type=float, default=10.0)
    coach_openai_parser.add_argument("--max-attempts", type=int, default=1)
    coach_openai_parser.add_argument(
        "--max-cost-microusd", type=int, default=50_000
    )
    coach_openai_parser.add_argument("--allow-external-model", action="store_true")
    coach_openai_parser.add_argument("--review-output", type=Path)
    coach_openai_parser.add_argument(
        "--variant-id",
        choices=list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        default="A",
    )
    coach_openai_parser.add_argument("--json", action="store_true")

    coach_review_parser = subparsers.add_parser(
        "coach-review-score",
        help="Score a blinded expert review document against the quality gate",
    )
    coach_review_parser.add_argument("path", type=Path)
    coach_review_parser.add_argument("--json", action="store_true")

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

    gto_match_parser = subparsers.add_parser(
        "gto-match",
        help="Match one real decision node to a versioned solution catalog",
    )
    _add_gto_node_arguments(gto_match_parser)
    _add_database_argument(gto_match_parser)
    gto_match_parser.add_argument("--json", action="store_true")

    gto_artifact_verify_parser = subparsers.add_parser(
        "gto-artifact-verify",
        help="Verify one catalog solution's strategy artifact end to end",
    )
    gto_artifact_verify_parser.add_argument("catalog", type=Path)
    gto_artifact_verify_parser.add_argument("solution_id")
    gto_artifact_verify_parser.add_argument("--json", action="store_true")

    gto_quality_verify_parser = subparsers.add_parser(
        "gto-quality-verify",
        help="Run the independent quality gate over a signed verified grant",
    )
    gto_quality_verify_parser.add_argument("attestation", type=Path)
    gto_quality_verify_parser.add_argument("--catalog", type=Path, required=True)
    gto_quality_verify_parser.add_argument("--json", action="store_true")

    gto_artifact_package_parser = subparsers.add_parser(
        "gto-artifact-package",
        help="Validate a draft artifact and emit its canonical bytes and SHA-256",
    )
    gto_artifact_package_parser.add_argument("draft", type=Path)
    gto_artifact_package_parser.add_argument("--catalog", type=Path, required=True)
    gto_artifact_package_parser.add_argument(
        "--write",
        action="store_true",
        help="Write the canonical artifact to the path the catalog declares",
    )
    gto_artifact_package_parser.add_argument(
        "--update-catalog",
        action="store_true",
        help="Also rewrite the catalog's recorded SHA-256 for this solution",
    )
    gto_artifact_package_parser.add_argument("--json", action="store_true")

    gto_query_parser = subparsers.add_parser(
        "gto-query",
        help="Return verified strategy facts for one real decision node",
    )
    _add_gto_node_arguments(gto_query_parser)
    gto_query_parser.add_argument(
        "--combo",
        help="Canonical two-card combination such as AhKh; omit for the slice aggregate",
    )
    gto_query_parser.add_argument(
        "--attestation",
        type=Path,
        help="Signed verified grant; required before any result is teachable",
    )
    _add_database_argument(gto_query_parser)
    gto_query_parser.add_argument("--json", action="store_true")

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
        "coach-eval": _run_coach_eval,
        "coach-openai": _run_coach_openai,
        "coach-review-score": _run_coach_review_score,
        "sessions": _run_sessions,
        "hands": _run_hands,
        "replay": _run_replay,
        "gto-match": _run_gto_match,
        "gto-artifact-verify": _run_gto_artifact_verify,
        "gto-quality-verify": _run_gto_quality_verify,
        "gto-artifact-package": _run_gto_artifact_package,
        "gto-query": _run_gto_query,
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


def _run_coach_eval(args: argparse.Namespace) -> int:
    try:
        report = run_coach_eval(args.corpus)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(coach_eval_report_to_dict(report), ensure_ascii=False))
    else:
        print(
            f"Coach candidate eval: {report.passed}/{report.total} passed, "
            f"{report.failed} failed"
        )
        for category, counts in report.categories.items():
            print(f"- {category}: {counts['passed']}/{counts['total']}")
        for item in report.results:
            if not item.passed:
                print(
                    f"- FAIL {item.case_id}: expected {item.expected_status} "
                    f"{list(item.expected_issue_codes)}, got {item.actual_status} "
                    f"{list(item.actual_issue_codes)}"
                )
    return 0 if report.failed == 0 else 2


def _run_coach_openai(args: argparse.Namespace) -> int:
    if not args.allow_external_model:
        return _print_value_error(
            ValueError("--allow-external-model is required before any external request")
        )
    if args.review_output is not None:
        if args.review_output.exists():
            return _print_value_error(
                ValueError(f"review-output already exists: {args.review_output}")
            )
        if not args.review_output.parent.exists():
            return _print_value_error(
                ValueError(
                    f"review-output parent does not exist: {args.review_output.parent}"
                )
            )
    if not _database_exists(args.database):
        return 2
    try:
        stat_filter = _stats_filter(args)
        if not 1 <= args.evidence_limit <= 20:
            raise ValueError("evidence-limit must be between 1 and 20")
        input_rate = _usd_per_million_to_microusd(
            args.input_usd_per_million,
            "input-usd-per-million",
        )
        output_rate = _usd_per_million_to_microusd(
            args.output_usd_per_million,
            "output-usd-per-million",
        )
        config = OpenAIResponsesConfig.from_environment(
            model_id=args.model,
            input_rate_microusd_per_million_tokens=input_rate,
            output_rate_microusd_per_million_tokens=output_rate,
            external_data_consent=True,
        )
        provider = OpenAIResponsesCoachProvider(config)
        policy = CoachRuntimePolicy(
            timeout_seconds=args.timeout_seconds,
            max_attempts=args.max_attempts,
            max_total_cost_microusd=args.max_cost_microusd,
        )
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
    matching_cards = [
        card
        for card in leak_report.cards
        if card.assessment.rule_id == args.rule_id
    ]
    if len(matching_cards) != 1:
        return _print_value_error(
            ValueError(
                f"rule-id must select exactly one leak card; matched {len(matching_cards)}"
            )
        )

    result = asyncio.run(
        run_coach_provider(matching_cards[0], provider, policy=policy)
    )
    review_written = False
    if args.review_output is not None and (
        result.item.explanation.source == CoachSource.LLM_VALIDATED
    ):
        review_document = build_expert_review_document(
            (
                build_blind_review_case(
                    result.item,
                    case_id=(
                        f"{result.item.evidence.evidence_id}:{args.variant_id}"
                    ),
                    variant_id=args.variant_id,
                ),
            )
        )
        try:
            with args.review_output.open("x", encoding="utf-8") as output_file:
                json.dump(review_document, output_file, ensure_ascii=False, indent=2)
                output_file.write("\n")
            review_written = True
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.json:
        print(
            json.dumps(
                {
                    "item": coach_item_to_dict(result.item),
                    "audit": coach_call_audit_to_dict(result.audit),
                    "review_output": (
                        None
                        if not review_written
                        else str(args.review_output.resolve())
                    ),
                },
                ensure_ascii=False,
            )
        )
    else:
        _print_coach(CoachReport((result.item,)))
        print(f"Runtime: {result.audit.status.value}")
        if review_written:
            print(f"Blind review packet: {args.review_output.resolve()}")
    return 0 if result.item.explanation.source == CoachSource.LLM_VALIDATED else 2


def _run_coach_review_score(args: argparse.Namespace) -> int:
    try:
        report = load_and_score_expert_reviews(args.path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(expert_review_report_to_dict(report), ensure_ascii=False))
    else:
        print(
            f"Expert review gate: {'PASS' if report.passed else 'FAIL'} · "
            f"{report.cases} cases · {report.unique_evidence_cases} unique evidence · "
            f"{report.ratings} ratings · "
            f"{report.fatal_errors} fatal errors"
        )
        for dimension, value in report.dimension_means.items():
            print(f"- {dimension}: {value:.3f}")
        for reason in report.failure_reasons:
            print(f"- FAIL {reason}")
    return 0 if report.passed else 2


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


def _add_gto_node_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments shared by every command that pins one real decision node."""

    parser.add_argument("site")
    parser.add_argument("hand_id")
    parser.add_argument("--before-action", type=int, required=True)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("solutions/catalog.json"),
    )
    parser.add_argument(
        "--objective",
        choices=[item.value for item in SolutionObjective],
        default=SolutionObjective.CHIP_EV.value,
    )
    parser.add_argument("--tournament-context-id")
    parser.add_argument("--rake-model")
    parser.add_argument("--rake-percent", type=Decimal)
    parser.add_argument("--rake-cap-bb", type=Decimal)


def _gto_rake(args: argparse.Namespace) -> RakeSpec | None:
    """Cash rake structure must be supplied whole; it is never inferred."""

    values = (args.rake_model, args.rake_percent, args.rake_cap_bb)
    if any(item is not None for item in values) and not all(
        item is not None for item in values
    ):
        raise ValueError(
            "rake-model, rake-percent, and rake-cap-bb must be supplied together"
        )
    if args.rake_model is None:
        return None
    return RakeSpec(
        model_id=args.rake_model,
        percent=args.rake_percent,
        cap_bb=args.rake_cap_bb,
    )


def _observed_gto_node(args: argparse.Namespace):
    """Load the stored hand and extract the de-identified pre-decision node."""

    rake = _gto_rake(args)
    with SQLiteHandStore(args.database) as store:
        hand = store.load_hand(args.site, args.hand_id)
    if hand is None:
        raise ValueError(f"hand not found: {args.site} #{args.hand_id}")
    return extract_decision_game_spec(
        hand,
        before_action=args.before_action,
        objective=SolutionObjective(args.objective),
        rake=rake,
        tournament_context_id=args.tournament_context_id,
    )


def _run_gto_match(args: argparse.Namespace) -> int:
    if not _database_exists(args.database):
        return 2
    try:
        catalog = load_solution_catalog(args.catalog)
        observed = _observed_gto_node(args)
        result = match_game_spec(observed, catalog)
    except (OSError, SpecValidationError, ValueError) as exc:
        return _print_value_error(ValueError(str(exc)))

    payload = {
        "source": {
            "site": args.site,
            "hand_id": args.hand_id,
            "before_action": args.before_action,
        },
        "observed_game_spec": observed.to_dict(),
        "match": result.to_dict(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(
            f"GTO match: {result.status.value} ({result.reason.value}) · "
            f"node {observed.fingerprint[:12]}"
        )
        if result.solution is not None:
            print(
                f"Solution: {result.solution.solution_id} · "
                f"{result.solution.quality.value} · "
                f"distance {result.normalized_distance}"
            )
        for item in result.differences:
            print(
                f"- {item.field}: observed {item.observed}, "
                f"solution {item.solution}, delta {item.absolute_delta}, "
                f"threshold {item.threshold}"
            )
        if result.status == MatchStatus.UNSUPPORTED:
            print("No strategy or EV was returned.")
    return 0


def _run_gto_artifact_verify(args: argparse.Namespace) -> int:
    """Prove a catalog entry's strategy artifact, or refuse to use it."""

    try:
        catalog = load_solution_catalog(args.catalog)
        verification = verify_catalog_artifact(
            catalog,
            args.solution_id,
            root=args.catalog.parent,
        )
    except (OSError, SpecValidationError, ValueError) as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "solution_id": args.solution_id,
                        "catalog": str(args.catalog),
                        "strategy_content_verified": False,
                        "error": _safe_text(exc),
                    },
                    ensure_ascii=False,
                )
            )
        return _print_value_error(ValueError(str(exc)))

    payload = {
        "catalog": str(args.catalog),
        "verification": verification.to_dict(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        artifact = verification.artifact
        print(
            f"Artifact verified: {verification.solution_id} · "
            f"{verification.quality.value} · node {verification.node_id}"
        )
        print(f"sha256 {verification.artifact_sha256}")
        print(
            f"GameSpec {verification.game_spec_fingerprint[:12]} · "
            f"tree {verification.action_tree_version} · "
            f"{len(artifact.actions)} actions · {len(artifact.entries)} combos · "
            f"EV {'present' if artifact.ev_present else 'absent'} "
            f"({artifact.ev_unit.value}, {artifact.ev_semantics.value})"
        )
        if verification.quality is not SolutionQuality.VERIFIED:
            print(
                "Quality label is not 'verified'. This content must not be presented "
                "to a learner as GTO."
            )
    return 0


def _run_gto_quality_verify(args: argparse.Namespace) -> int:
    """Grant nothing. Either the signed grant survives the gate, or it does not."""

    try:
        catalog = load_solution_catalog(args.catalog)
        verification = verify_quality_attestation(
            args.attestation,
            catalog=catalog,
            catalog_root=args.catalog.parent,
        )
    except (OSError, SpecValidationError, ValueError) as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "attestation": str(args.attestation),
                        "catalog": str(args.catalog),
                        "grant_valid": False,
                        "error": _safe_text(exc),
                    },
                    ensure_ascii=False,
                )
            )
        return _print_value_error(ValueError(str(exc)))

    payload = {
        "attestation": str(args.attestation),
        "catalog": str(args.catalog),
        "grant": verification.to_dict(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        report = verification.report
        print(
            f"Grant valid: {verification.solution_id} · "
            f"{verification.attestation.attestation_id} · "
            f"policy {verification.policy_version}"
        )
        print(
            f"Report {report.report_id} · claim {report.claim_class.value} · "
            f"{report.solve.convergence_metric.value} "
            f"{report.solve.convergence_value} <= "
            f"{report.solve.convergence_threshold} "
            f"{report.solve.convergence_unit.value}"
        )
        print(
            f"Source {report.source.origin.value}/{report.source.provider} · "
            f"licence {report.source.license_id} · "
            f"display_allowed={report.source.display_allowed}"
        )
        for reviewer in verification.attestation.reviewers:
            print(f"- signed by {reviewer.reviewer_id} ({reviewer.role.value})")
        for limit in report.limits:
            print(f"! limit: {limit}")
    return 0


def _run_gto_artifact_package(args: argparse.Namespace) -> int:
    """Normalize a draft artifact into the exact bytes the catalog will hash."""

    if args.update_catalog and not args.write:
        return _print_value_error(
            ValueError(
                "--update-catalog requires --write; refusing to record a hash for "
                "bytes that were never written"
            )
        )
    written = False
    catalog_updated = False
    try:
        catalog = load_solution_catalog(args.catalog)
        document = load_artifact_document(args.draft)
        if not isinstance(document, dict):
            raise ValueError("draft artifact must be a JSON object")
        solution_id = document.get("solution_id")
        if not isinstance(solution_id, str):
            raise ValueError("draft artifact must carry a string solution_id")
        solution = next(
            (item for item in catalog.solutions if item.solution_id == solution_id),
            None,
        )
        if solution is None:
            raise ValueError(f"catalog does not contain solution {solution_id!r}")
        artifact = strategy_artifact_from_dict(document, game_spec=solution.game_spec)
        canonical = serialize_strategy_artifact(artifact)
        digest = hashlib.sha256(canonical).hexdigest()
        target = resolve_artifact_path(
            args.catalog.parent, solution.artifact_id, must_exist=False
        )
        _guard_package_target(target, args.catalog, solution_id)

        if args.write:
            _atomic_write(target, canonical)
            written = True
            if args.update_catalog:
                _rewrite_catalog_hash(args.catalog, solution_id, digest)
                catalog_updated = True
                # Postcondition: the pair we just wrote must verify together.
                verify_catalog_artifact(
                    load_solution_catalog(args.catalog),
                    solution_id,
                    root=args.catalog.parent,
                )
    except (OSError, SpecValidationError, ValueError) as exc:
        if written and not catalog_updated:
            print(
                "warning: the artifact was written but the catalog still records "
                "the previous sha256; verification will fail until you re-run",
                file=sys.stderr,
            )
        return _print_value_error(ValueError(str(exc)))

    result = {
        "solution_id": solution_id,
        "artifact_path": solution.artifact_id,
        "canonical_bytes": len(canonical),
        "canonical_sha256": digest,
        "catalog_sha256": solution.artifact_sha256,
        "sha256_matches_catalog": digest == solution.artifact_sha256,
        "draft_was_canonical": args.draft.read_bytes() == canonical,
        "written": written,
        "catalog_updated": catalog_updated,
        "quality": artifact.provenance.quality.value,
        "boundary": (
            "Packaging normalizes formatting and decimals. It validates content "
            "but grants no quality label; 'verified' still requires the quality gate."
        ),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Canonical sha256: {digest} ({len(canonical)} bytes)")
        print(f"Draft already canonical: {result['draft_was_canonical']}")
        print(f"Matches catalog entry: {result['sha256_matches_catalog']}")
        if written:
            print(f"Wrote {solution.artifact_id}")
        if catalog_updated:
            print(f"Updated catalog sha256 for {solution_id}")
            print("Any existing quality attestation for this solution is now void.")
        elif not result["sha256_matches_catalog"]:
            print(
                "Catalog still records a different sha256; re-run with "
                "--write --update-catalog, or edit the catalog deliberately."
            )
    return 0


def _guard_package_target(target: Path, catalog: Path, solution_id: str) -> None:
    """Never let packaging clobber a file that is not this solution's artifact."""

    if target.resolve() == catalog.resolve():
        raise ValueError(
            "the catalog entry points at the catalog file itself; refusing to "
            "overwrite it with an artifact"
        )
    if target.is_dir():
        raise ValueError(f"artifact path is a directory: {target}")
    if not target.exists():
        return
    try:
        existing = json.loads(target.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(
            f"refusing to overwrite {target}: it is not readable as a strategy artifact"
        ) from None
    if (
        not isinstance(existing, dict)
        or existing.get("schema_version") != STRATEGY_ARTIFACT_SCHEMA_VERSION
        or existing.get("solution_id") != solution_id
    ):
        raise ValueError(
            f"refusing to overwrite {target}: it is not the artifact of "
            f"{solution_id!r}"
        )


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write via a sibling temp file so a failure cannot leave a half file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _rewrite_catalog_hash(catalog: Path, solution_id: str, digest: str) -> None:
    payload = json.loads(catalog.read_bytes().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("solutions"), list):
        raise ValueError("catalog does not have the expected shape")
    updated = 0
    for entry in payload["solutions"]:
        if isinstance(entry, dict) and entry.get("solution_id") == solution_id:
            artifact = entry.get("artifact")
            if not isinstance(artifact, dict):
                raise ValueError("catalog entry has no artifact object")
            artifact["sha256"] = digest
            updated += 1
    if updated != 1:
        raise ValueError(
            f"expected exactly one catalog entry for {solution_id!r}, found {updated}"
        )
    _atomic_write(
        catalog, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def _run_gto_query(args: argparse.Namespace) -> int:
    """Return strategy facts only after an exact match and a full artifact check."""

    if not _database_exists(args.database):
        return 2
    try:
        catalog = load_solution_catalog(args.catalog)
        observed = _observed_gto_node(args)
        match = match_game_spec(observed, catalog)
    except (OSError, SpecValidationError, ValueError) as exc:
        return _print_value_error(ValueError(str(exc)))

    payload: dict[str, object] = {
        "source": {
            "site": args.site,
            "hand_id": args.hand_id,
            "before_action": args.before_action,
            "combo": args.combo,
        },
        "observed_game_spec": observed.to_dict(),
        "match": match.to_dict(),
        "strategy_available": False,
        "reason": None,
        "error": None,
        "artifact_verification": None,
        "quality_grant": None,
        "strategy_evidence": None,
        "boundary": (
            "Strategy facts require an exact node match plus a fully verified "
            "artifact. Anything else returns no frequencies and no EV."
        ),
    }

    if match.status is not MatchStatus.EXACT or match.solution is None:
        payload["reason"] = (
            "match_not_exact"
            if match.status is MatchStatus.APPROXIMATE
            else f"match_{match.reason.value}"
        )
        return _emit_gto_query(args, payload, exit_code=0)

    try:
        verification = verify_catalog_artifact(
            catalog,
            match.solution.solution_id,
            root=args.catalog.parent,
        )
    except (OSError, SpecValidationError, ValueError) as exc:
        payload["reason"] = "artifact_verification_failed"
        payload["error"] = _safe_text(exc)
        return _emit_gto_query(args, payload, exit_code=2)

    payload["artifact_verification"] = verification.to_dict()

    attestation = None
    if args.attestation is not None:
        try:
            attestation = verify_quality_attestation(
                args.attestation,
                catalog=catalog,
                catalog_root=args.catalog.parent,
            )
        except (OSError, SpecValidationError, ValueError) as exc:
            payload["reason"] = "attestation_verification_failed"
            payload["error"] = _safe_text(exc)
            return _emit_gto_query(args, payload, exit_code=2)
        payload["quality_grant"] = attestation.to_dict()

    try:
        evidence = build_strategy_evidence(
            verification, combo=args.combo, attestation=attestation
        )
    except ComboNotCoveredError as exc:
        payload["reason"] = "combo_not_covered"
        payload["error"] = _safe_text(exc)
        return _emit_gto_query(args, payload, exit_code=0)
    except StrategyQueryError as exc:
        payload["reason"] = "attestation_scope_mismatch"
        payload["error"] = _safe_text(exc)
        return _emit_gto_query(args, payload, exit_code=2)
    except ArtifactValidationError as exc:
        payload["reason"] = "invalid_combo"
        payload["error"] = _safe_text(exc)
        return _emit_gto_query(args, payload, exit_code=2)

    payload["strategy_available"] = True
    payload["reason"] = "verified_artifact"
    payload["strategy_evidence"] = evidence.to_dict()
    return _emit_gto_query(args, payload, exit_code=0)


def _emit_gto_query(
    args: argparse.Namespace,
    payload: dict[str, object],
    *,
    exit_code: int,
) -> int:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return exit_code
    match = payload["match"]
    assert isinstance(match, dict)
    print(
        f"GTO query: {match['status']} ({match['reason']}) · "
        f"node {str(match['observed_fingerprint'])[:12]}"
    )
    if not payload["strategy_available"]:
        print(f"No strategy returned: {payload['reason']}")
        if payload["error"] is not None:
            print(f"Detail: {payload['error']}")
        return exit_code
    evidence = payload["strategy_evidence"]
    assert isinstance(evidence, dict)
    scope = (
        f"combo {evidence['combo']}"
        if evidence["combo"] is not None
        else f"aggregate over {evidence['combo_count']} stored combos"
    )
    print(
        f"Solution {evidence['solution_id']} · node {evidence['node_id']} · "
        f"quality {evidence['quality']} · {scope}"
    )
    for action in evidence["actions"]:
        size = "" if action["size_bb"] is None else f" {action['size_bb']}bb"
        ev = "" if action["ev"] is None else f" · EV {action['ev']} {evidence['ev_unit']}"
        print(f"- {action['action_id']}{size}: {action['probability']}{ev}")
    if not evidence["usable_for_teaching"]:
        print(
            f"Not teachable ({evidence['teaching_block_reason']}). These numbers "
            "exercise the pipeline only and must not be shown to a learner as GTO."
        )
    return exit_code


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


def _safe_text(value: object) -> str:
    """Strip anything that would crash the UTF-8 encoder on the way out."""

    return str(value).encode("utf-8", "replace").decode("utf-8")


def _print_value_error(error: ValueError) -> int:
    print(f"error: {_safe_text(error)}", file=sys.stderr)
    return 2


def _usd_per_million_to_microusd(value: Decimal, field: str) -> int:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be a positive finite decimal")
    result = int((value * Decimal("1000000")).to_integral_value(ROUND_CEILING))
    if result <= 0:
        raise ValueError(f"{field} is too small to represent safely")
    return result


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
