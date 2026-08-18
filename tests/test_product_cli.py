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


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


class ProductCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = self.root / "rivermind.db"
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = main(
                [
                    "import",
                    str(FIXTURES / "pokerstars_cash.txt"),
                    str(FIXTURES / "pokerstars_mtt.txt"),
                    "--database",
                    str(self.database),
                ]
            )
        self.assertEqual(exit_code, 0)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sessions_cli_separates_cash_currency_from_tournament_chips(self) -> None:
        payload = self._json_command("sessions", "--json")

        self.assertEqual(len(payload), 2)
        by_type = {item["game_type"]: item for item in payload}
        self.assertEqual(by_type["cash"]["net_result"], "0.60")
        self.assertEqual(by_type["cash"]["result_unit"], "USD")
        self.assertEqual(by_type["tournament"]["net_result"], "600")
        self.assertEqual(by_type["tournament"]["result_unit"], "chips")

    def test_hands_cli_filters_metric_evidence_and_paginates(self) -> None:
        payload = self._json_command(
            "hands",
            "--metric",
            "flop_cbet",
            "--occurred",
            "--limit",
            "1",
            "--json",
        )

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["hand_id"], "100000000001")
        self.assertEqual(payload[0]["net_result"], "0.60")
        self.assertEqual(
            payload[0]["metrics"]["flop_cbet"],
            {"occurred": True, "opportunity": True},
        )

    def test_replay_cli_returns_running_pot_and_action_investment(self) -> None:
        payload = self._json_command(
            "replay",
            "pokerstars",
            "100000000001",
            "--json",
        )
        hero_raise = next(
            frame
            for frame in payload["frames"]
            if frame["player"] == "Hero" and frame["action"] == "raise"
        )

        self.assertEqual(hero_raise["invested"], "0.25")
        self.assertEqual(hero_raise["pot_after"], "0.40")
        self.assertTrue(payload["accounting_balanced"])

    def test_leaks_cli_exposes_versioned_rules_and_sample_status(self) -> None:
        payload = self._json_command("leaks", "--json")

        self.assertEqual(payload["profile"]["id"], "broad-review-signals")
        self.assertEqual(payload["profile"]["version"], "0.1.0")
        self.assertEqual(payload["summary"]["detected"], 0)
        self.assertEqual(payload["summary"]["insufficient_sample"], 6)
        self.assertEqual(len(payload["assessments"]), 6)
        self.assertEqual(payload["cards"], [])
        self.assertTrue(payload["scope"]["heroes_only"])

    def test_report_cli_generates_escaped_local_analysis_page(self) -> None:
        output = self.root / "analysis.html"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "report",
                    "--database",
                    str(self.database),
                    "--output",
                    str(output),
                    "--title",
                    "<script>alert(1)</script>",
                ]
            )

        html = output.read_text(encoding="utf-8")
        self.assertEqual(exit_code, 0)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("Hero", html)
        self.assertIn("Leak Cards", html)
        self.assertIn("样本不足", html)
        self.assertIn("核心统计", html)

    def _json_command(self, command: str, *arguments: str):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(
                [command, *arguments, "--database", str(self.database)]
            )
        self.assertEqual(exit_code, 0)
        return json.loads(output.getvalue())


if __name__ == "__main__":
    unittest.main()
