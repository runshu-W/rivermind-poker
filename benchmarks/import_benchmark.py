from __future__ import annotations

import argparse
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
)
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.reports import HandQuery, StatMetric  # noqa: E402
from rivermind_core.storage import SQLiteHandStore  # noqa: E402


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "pokerstars_cash.txt"


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
            "database_mb": round(database.stat().st_size / 1024 / 1024, 2),
            "fixture": "synthetic variants of the committed golden hand",
        }
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
