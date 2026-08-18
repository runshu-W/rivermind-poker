from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from rivermind_core.importer import HandHistoryImporter, ImportBatchReport
from rivermind_core.parsers import default_registry
from rivermind_core.storage import SQLiteHandStore


HAND_HISTORY_SUFFIXES = {".txt", ".log", ".hh"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rivermind")
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser(
        "import", help="Import hand-history files or folders"
    )
    import_parser.add_argument("paths", nargs="+", type=Path)
    import_parser.add_argument(
        "--database", type=Path, default=Path("data/rivermind.db")
    )
    import_parser.add_argument(
        "--json", action="store_true", help="Print a machine-readable report"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "import":
        return 2

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
            raw_text = _read_text(file_path)
            reports.append(importer.import_text(str(file_path), raw_text))

    if args.json:
        print(json.dumps([_report_dict(report) for report in reports], ensure_ascii=False))
    else:
        _print_human_report(reports, args.database)

    has_errors = any(report.failed or report.unsupported for report in reports)
    return 2 if has_errors else 0


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
            if candidate.is_file()
            and candidate.suffix.lower() in HAND_HISTORY_SUFFIXES
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


def _report_dict(report: ImportBatchReport) -> dict[str, object]:
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


def _print_human_report(reports: Sequence[ImportBatchReport], database: Path) -> None:
    totals = {
        "detected": sum(report.detected for report in reports),
        "imported": sum(report.imported for report in reports),
        "duplicates": sum(report.duplicates for report in reports),
        "failed": sum(report.failed for report in reports),
        "unsupported": sum(report.unsupported for report in reports),
    }
    print(
        "Import complete: "
        f"{totals['detected']} detected, {totals['imported']} imported, "
        f"{totals['duplicates']} duplicate, {totals['failed']} failed, "
        f"{totals['unsupported']} unsupported"
    )
    print(f"Database: {database}")
    for report in reports:
        for item in report.items:
            if item.error_code:
                print(
                    f"- {report.source_name}:{item.start_line}-{item.end_line} "
                    f"[{item.status.value}/{item.error_code}] {item.message}"
                )
