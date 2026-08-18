from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.importer import HandHistoryImporter  # noqa: E402
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
        if not stats or stats[0].hands != args.hands:
            raise RuntimeError("Stats benchmark did not observe every imported hand")
        if not sessions or sessions[0].hands != args.hands:
            raise RuntimeError("Session benchmark did not observe every imported hand")
        if len(related) != min(args.hands, 1000):
            raise RuntimeError("Related-hand benchmark returned the wrong page size")
        if args.hands >= 30 and not leaks.cards:
            raise RuntimeError("Leak benchmark did not produce the expected review signal")
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
            "database_mb": round(database.stat().st_size / 1024 / 1024, 2),
            "fixture": "synthetic variants of the committed golden hand",
        }
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
