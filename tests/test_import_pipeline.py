from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.cli import main  # noqa: E402
from rivermind_core.importer import (  # noqa: E402
    HandHistoryImporter,
    ImportItemStatus,
    split_hand_histories,
)
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.storage import SQLiteHandStore  # noqa: E402


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "pokerstars_cash.txt"


class ImportPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw_hand = FIXTURE.read_text(encoding="utf-8")

    def setUp(self) -> None:
        self.store = SQLiteHandStore(":memory:")
        self.importer = HandHistoryImporter(default_registry(), self.store)

    def tearDown(self) -> None:
        self.store.close()

    def test_splits_multi_hand_text_and_preserves_line_ranges(self) -> None:
        second_hand = self.raw_hand.replace("100000000001", "100000000002", 1)
        raw = f"ignored export preamble\n{self.raw_hand}\n\n{second_hand}\n"

        segments = split_hand_histories(raw)

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].start_line, 2)
        self.assertGreater(segments[1].start_line, segments[0].end_line)
        self.assertIn("#100000000002", segments[1].raw_text)

    def test_import_is_idempotent_and_hands_are_replayable(self) -> None:
        second_hand = self.raw_hand.replace("100000000001", "100000000002", 1)
        raw = f"{self.raw_hand}\n\n{second_hand}"

        first = self.importer.import_text("two-hands.txt", raw)
        second = self.importer.import_text("two-hands.txt", raw)

        self.assertEqual(first.imported, 2)
        self.assertEqual(second.duplicates, 2)
        self.assertEqual(self.store.hand_count(), 2)
        restored = self.store.load_hand("pokerstars", "100000000001")
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.hero.hole_cards, ("Ah", "Kd"))  # type: ignore[union-attr]
        self.assertEqual(restored.raw_text.strip(), self.raw_hand.strip())

    def test_bad_hand_does_not_block_valid_hand(self) -> None:
        malformed = self.raw_hand.replace(
            "Table 'RiverMind Alpha' 6-max Seat #3 is the button\n", ""
        )
        valid = self.raw_hand.replace("100000000001", "100000000003", 1)

        report = self.importer.import_text(
            "mixed.txt", f"{malformed}\n\n{valid}"
        )

        self.assertEqual(
            [item.status for item in report.items],
            [ImportItemStatus.FAILED, ImportItemStatus.IMPORTED],
        )
        self.assertEqual(report.items[0].error_code, "pokerstars_missing_table")
        self.assertEqual(self.store.hand_count(), 1)

    def test_unknown_and_malformed_tournament_formats_are_visible(self) -> None:
        unknown = self.importer.import_text("unknown.txt", "Unknown room hand #1")
        tournament_text = self.raw_hand.replace(
            "Hold'em No Limit ($0.05/$0.10 USD)",
            "Tournament #1, Hold'em No Limit - Level I (10/20)",
            1,
        )
        tournament = self.importer.import_text("tournament.txt", tournament_text)

        self.assertEqual(unknown.unsupported, 1)
        self.assertEqual(unknown.items[0].error_code, "unknown_format")
        self.assertEqual(tournament.failed, 1)
        self.assertEqual(
            tournament.items[0].error_code,
            "pokerstars_malformed_tournament_header",
        )

    def test_persisted_batch_report_matches_result(self) -> None:
        report = self.importer.import_text("one.txt", self.raw_hand)

        restored = self.store.get_batch_report(report.batch_id)

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.batch_id, report.batch_id)
        self.assertEqual(restored.items, report.items)
        self.assertEqual(restored.imported, 1)

    def test_failed_item_raw_text_can_be_replayed(self) -> None:
        malformed = self.raw_hand.replace(
            "Table 'RiverMind Alpha' 6-max Seat #3 is the button\n", ""
        )
        report = self.importer.import_text("broken.txt", malformed)

        restored_raw = self.store.load_import_item_raw(report.batch_id, 0)

        self.assertEqual(restored_raw, malformed.strip())


class ImportCliTest(unittest.TestCase):
    def test_cli_imports_a_folder_and_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "hands"
            source_dir.mkdir()
            (source_dir / "one.txt").write_text(
                FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
            )
            database = root / "rivermind.db"
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                exit_code = main(
                    [
                        "import",
                        str(source_dir),
                        "--database",
                        str(database),
                        "--json",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload[0]["imported"], 1)
            self.assertTrue(database.exists())


if __name__ == "__main__":
    unittest.main()
