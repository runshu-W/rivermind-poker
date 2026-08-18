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
        elapsed = time.perf_counter() - started
        result = {
            "hands": args.hands,
            "imported": report.imported,
            "elapsed_seconds": round(elapsed, 3),
            "hands_per_second": round(args.hands / elapsed),
            "database_mb": round(database.stat().st_size / 1024 / 1024, 2),
            "fixture": "synthetic variants of the committed golden hand",
        }
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
