from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.importer import HandHistoryImporter  # noqa: E402
from rivermind_core.coach import explain_leak_report  # noqa: E402
from rivermind_core.coach_evals import run_coach_eval  # noqa: E402
from rivermind_core.gto_matcher import (  # noqa: E402
    MatchStatus,
    extract_decision_game_spec,
    match_game_spec,
)
from rivermind_core.gto_specs import (  # noqa: E402
    RakeSpec,
    SolutionCatalog,
    SolutionQuality,
    SolutionSpec,
    load_solution_catalog,
)
from rivermind_core.quality_gate import (  # noqa: E402
    QUALITY_ATTESTATION_SCHEMA_VERSION,
    QUALITY_GATE_POLICY_VERSION,
    verify_quality_attestation,
)
from rivermind_core.solve_quality import (  # noqa: E402
    SOLVE_QUALITY_REPORT_SCHEMA_VERSION,
)
from rivermind_core.strategy_artifacts import (  # noqa: E402
    STRATEGY_ARTIFACT_SCHEMA_VERSION,
    verify_catalog_artifact,
)
from rivermind_core.strategy_query import build_strategy_evidence  # noqa: E402
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.reports import HandQuery, StatMetric  # noqa: E402
from rivermind_core.storage import SQLiteHandStore  # noqa: E402


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "pokerstars_cash.txt"
STRATEGY_CATALOG = PROJECT_ROOT / "solutions" / "catalog.test_only.json"
STRATEGY_SOLUTION_ID = "test-only.pokerstars-cash.btn-flop-cbet"
STRATEGY_ITERATIONS = 200
GATE_ITERATIONS = 100


def _build_grant(root: Path, node) -> tuple[Path, Path]:
    """Lay out a minimal valid verified grant in a temporary directory.

    Nothing here is committed; the repository still contains zero verified
    strategy. This exists so the gate's cost is measured on the real path.
    """

    solution_id = "benchmark.verified.node"
    report_id = "benchmark.report.1"
    tree = "benchmark.tree/1.0.0"
    solver, solver_version, config = "benchmark.solver", "1.0.0", "benchmark-config"
    solved_from, solved_to, granted = (
        "2026-08-01T00:00:00Z",
        "2026-08-02T00:00:00Z",
        "2026-08-05T00:00:00Z",
    )

    def dump(path: Path, payload: dict) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    artifact_sha = dump(
        root / "strategy" / "node.json",
        {
            "schema_version": STRATEGY_ARTIFACT_SCHEMA_VERSION,
            "solution_id": solution_id,
            "game_spec_fingerprint": node.fingerprint,
            "action_tree_version": tree,
            "node_id": "benchmark.node",
            "ev_unit": "bb",
            "ev_semantics": "action_ev_from_node",
            "actions": [
                {"action_id": "bet_2bb", "kind": "bet", "size_bb": "2"},
                {"action_id": "check", "kind": "check", "size_bb": None},
            ],
            "entries": [
                {
                    "combo": "AhKh",
                    "weight": "1",
                    "policies": [
                        {"action_id": "bet_2bb", "probability": "0.75", "ev": "2.5"},
                        {"action_id": "check", "probability": "0.25", "ev": "2.2"},
                    ],
                }
            ],
            "provenance": {
                "solver_name": solver,
                "solver_version": solver_version,
                "solver_config_id": config,
                "generated_at": solved_to,
                "quality": "verified",
                "quality_report_id": report_id,
                "license": "Benchmark-only synthetic grant.",
            },
        },
    )
    catalog_path = root / "catalog.json"
    dump(
        catalog_path,
        SolutionCatalog(
            catalog_id="benchmark-gate",
            catalog_version="1.0.0",
            solutions=(
                SolutionSpec(
                    solution_id=solution_id,
                    game_spec=node,
                    solver_name=solver,
                    solver_version=solver_version,
                    action_tree_version=tree,
                    quality=SolutionQuality.VERIFIED,
                    artifact_id="strategy/node.json",
                    artifact_sha256=artifact_sha,
                ),
            ),
        ).to_dict(),
    )
    report_sha = dump(
        root / "grants" / "report.json",
        {
            "schema_version": SOLVE_QUALITY_REPORT_SCHEMA_VERSION,
            "report_id": report_id,
            "solution_id": solution_id,
            "game_spec_fingerprint": node.fingerprint,
            "action_tree_version": tree,
            "solver": {
                "name": solver,
                "version": solver_version,
                "config_id": config,
                "config_sha256": "1" * 64,
            },
            "claim_class": "equilibrium_approximation",
            "source": {
                "origin": "in_house",
                "provider": "RiverMind benchmark",
                "license_id": "benchmark-only",
                "obtained_at": "2026-07-01T00:00:00Z",
                "display_allowed": True,
                "redistribution_allowed": False,
            },
            "solve": {
                "started_at": solved_from,
                "completed_at": solved_to,
                "iterations": 1000,
                "convergence_metric": "exploitability",
                "convergence_value": "0.003",
                "convergence_unit": "bb_per_100",
                "convergence_threshold": "0.005",
                "board_abstraction": "none",
                "bet_size_abstraction": "two sizes",
                "card_isomorphism_used": False,
                "rake_model_id": node.rake.model_id,
            },
            "evaluation": {
                "scope": "single_node",
                "board_sample_size": 1,
                "independent_recheck": True,
                "recheck_tool_name": "benchmark.rechecker",
                "recheck_tool_version": "0.1",
            },
            "limits": ["Synthetic benchmark grant; proves nothing about poker."],
        },
    )
    attestation_path = root / "grants" / "attestation.json"
    dump(
        attestation_path,
        {
            "schema_version": QUALITY_ATTESTATION_SCHEMA_VERSION,
            "attestation_id": "benchmark.grant.1",
            "solution_id": solution_id,
            "artifact_sha256": artifact_sha,
            "report": {"path": "report.json", "id": report_id, "sha256": report_sha},
            "granted_quality": "verified",
            "policy_version": QUALITY_GATE_POLICY_VERSION,
            "granted_at": granted,
            "reviewers": [
                {
                    "reviewer_id": "benchmark.owner",
                    "role": "solver_owner",
                    "signed_at": "2026-08-03T00:00:00Z",
                    "statement_sha256": "2" * 64,
                },
                {
                    "reviewer_id": "benchmark.independent",
                    "role": "independent_reviewer",
                    "signed_at": "2026-08-04T00:00:00Z",
                    "statement_sha256": "3" * 64,
                },
            ],
        },
    )
    return catalog_path, attestation_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hands", type=int, default=10_000)
    args = parser.parse_args()
    if args.hands <= 0:
        parser.error("--hands must be positive")

    template = FIXTURE.read_text(encoding="utf-8")
    raw_text = "\n\n".join(
        template.replace("100000000001", str(100_000_000_000 + index), 1)
        for index in range(args.hands)
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        database = Path(temp_dir) / "benchmark.db"
        started = time.perf_counter()
        with SQLiteHandStore(database) as store:
            report = HandHistoryImporter(default_registry(), store).import_text(
                "synthetic-benchmark.txt", raw_text
            )
            import_elapsed = time.perf_counter() - started
            stats_started = time.perf_counter()
            stats = store.query_player_stats(heroes_only=True)
            stats_elapsed = time.perf_counter() - stats_started
            sessions_started = time.perf_counter()
            sessions = store.query_sessions(heroes_only=True)
            sessions_elapsed = time.perf_counter() - sessions_started
            related_started = time.perf_counter()
            related = store.query_hands(
                heroes_only=True,
                query=HandQuery(
                    metric=StatMetric.FLOP_CBET,
                    occurred=True,
                    limit=1000,
                ),
            )
            related_elapsed = time.perf_counter() - related_started
            leaks_started = time.perf_counter()
            leaks = store.query_leaks(heroes_only=True)
            leaks_elapsed = time.perf_counter() - leaks_started
            coach_started = time.perf_counter()
            coach = explain_leak_report(leaks)
            coach_elapsed = time.perf_counter() - coach_started
            coach_eval_started = time.perf_counter()
            coach_eval = run_coach_eval(
                PROJECT_ROOT / "evals" / "coach_candidate_cases.json"
            )
            coach_eval_elapsed = time.perf_counter() - coach_eval_started
            benchmark_hand = store.load_hand(
                "pokerstars", str(100_000_000_000)
            )
            assert benchmark_hand is not None
            benchmark_node = extract_decision_game_spec(
                benchmark_hand,
                before_action=5,
                rake=RakeSpec(
                    model_id="benchmark-rake",
                    percent=Decimal("5"),
                    cap_bb=Decimal("3"),
                ),
            )
            catalog_nodes = 1000
            synthetic_solutions = tuple(
                SolutionSpec(
                    solution_id=f"benchmark-{index}",
                    game_spec=(
                        benchmark_node
                        if index == catalog_nodes - 1
                        else replace(
                            benchmark_node,
                            pot_bb=benchmark_node.pot_bb
                            + Decimal(index + 1) / Decimal("1000"),
                        )
                    ),
                    solver_name="benchmark",
                    solver_version="1",
                    action_tree_version="benchmark/1",
                    quality=SolutionQuality.TEST_ONLY,
                    artifact_id=f"benchmark/{index}",
                    artifact_sha256="b" * 64,
                )
                for index in range(catalog_nodes)
            )
            catalog = SolutionCatalog(
                catalog_id="benchmark",
                catalog_version="1",
                solutions=synthetic_solutions,
            )
            match_started = time.perf_counter()
            match = match_game_spec(benchmark_node, catalog)
            match_elapsed = time.perf_counter() - match_started

        # Strategy artifacts are read from disk, so time them outside the store.
        strategy_catalog = load_solution_catalog(STRATEGY_CATALOG)
        verify_started = time.perf_counter()
        for _ in range(STRATEGY_ITERATIONS):
            verification = verify_catalog_artifact(
                strategy_catalog,
                STRATEGY_SOLUTION_ID,
                root=STRATEGY_CATALOG.parent,
            )
        verify_elapsed = (time.perf_counter() - verify_started) / STRATEGY_ITERATIONS
        query_started = time.perf_counter()
        for _ in range(STRATEGY_ITERATIONS):
            evidence = build_strategy_evidence(verification, combo="AhKh")
        query_elapsed = (time.perf_counter() - query_started) / STRATEGY_ITERATIONS

        with tempfile.TemporaryDirectory() as gate_dir:
            gate_root = Path(gate_dir)
            gate_catalog, gate_attestation = _build_grant(gate_root, benchmark_node)
            loaded_gate_catalog = load_solution_catalog(gate_catalog)
            gate_started = time.perf_counter()
            for _ in range(GATE_ITERATIONS):
                grant = verify_quality_attestation(
                    gate_attestation,
                    catalog=loaded_gate_catalog,
                    catalog_root=gate_catalog.parent,
                )
            gate_elapsed = (time.perf_counter() - gate_started) / GATE_ITERATIONS
            teachable = build_strategy_evidence(
                grant.verification, combo="AhKh", attestation=grant
            )
        if not stats or stats[0].hands != args.hands:
            raise RuntimeError("Stats benchmark did not observe every imported hand")
        if not sessions or sessions[0].hands != args.hands:
            raise RuntimeError("Session benchmark did not observe every imported hand")
        if len(related) != min(args.hands, 1000):
            raise RuntimeError("Related-hand benchmark returned the wrong page size")
        if args.hands >= 30 and not leaks.cards:
            raise RuntimeError("Leak benchmark did not produce the expected review signal")
        if coach.explanation_count != len(leaks.cards):
            raise RuntimeError("Coach benchmark did not explain every leak card")
        if coach_eval.total != 50 or coach_eval.failed:
            raise RuntimeError("Coach candidate eval gate did not pass all 50 cases")
        if match.status != MatchStatus.EXACT:
            raise RuntimeError("GTO matcher benchmark did not find the exact node")
        if verification.quality != SolutionQuality.TEST_ONLY:
            raise RuntimeError("The committed strategy artifact must stay test_only")
        if evidence.usable_for_teaching:
            raise RuntimeError("A test_only artifact must never be teachable")
        if not teachable.usable_for_teaching:
            raise RuntimeError("A fully granted artifact should be teachable")
        if build_strategy_evidence(grant.verification, combo="AhKh").usable_for_teaching:
            raise RuntimeError("Verified bytes without a grant must not be teachable")
        result = {
            "hands": args.hands,
            "imported": report.imported,
            "import_elapsed_seconds": round(import_elapsed, 3),
            "import_hands_per_second": round(args.hands / import_elapsed),
            "stats_elapsed_seconds": round(stats_elapsed, 3),
            "stats_hands_per_second": round(args.hands / stats_elapsed),
            "sessions_elapsed_seconds": round(sessions_elapsed, 3),
            "related_1000_elapsed_seconds": round(related_elapsed, 3),
            "leaks_elapsed_seconds": round(leaks_elapsed, 3),
            "coach_templates_elapsed_seconds": round(coach_elapsed, 3),
            "coach_eval_50_elapsed_seconds": round(coach_eval_elapsed, 3),
            "gto_catalog_nodes": catalog_nodes,
            "gto_match_elapsed_ms": round(match_elapsed * 1000, 3),
            "strategy_artifact_verify_ms": round(verify_elapsed * 1000, 4),
            "strategy_combo_query_ms": round(query_elapsed * 1000, 4),
            "strategy_artifact_combos": len(verification.artifact.entries),
            "strategy_artifact_quality": verification.quality.value,
            "quality_gate_verify_ms": round(gate_elapsed * 1000, 4),
            "quality_gate_policy": QUALITY_GATE_POLICY_VERSION,
            "database_mb": round(database.stat().st_size / 1024 / 1024, 2),
            "fixture": "synthetic variants of the committed golden hand",
        }
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
