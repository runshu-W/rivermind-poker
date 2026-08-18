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
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.stats import calculate_player_stats  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
CASH_FIXTURE = FIXTURES / "pokerstars_cash.txt"
MTT_FIXTURE = FIXTURES / "pokerstars_mtt.txt"


class PreflopStatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = default_registry()
        cls.hands = (
            registry.parse(CASH_FIXTURE.read_text(encoding="utf-8")),
            registry.parse(MTT_FIXTURE.read_text(encoding="utf-8")),
        )

    def test_hero_stats_use_hand_and_opportunity_denominators(self) -> None:
        stats = calculate_player_stats(self.hands, heroes_only=True)

        self.assertEqual(len(stats), 1)
        hero = stats[0]
        self.assertEqual(hero.player_name, "Hero")
        self.assertEqual(hero.hands, 2)
        self.assertEqual((hero.vpip.occurrences, hero.vpip.opportunities), (2, 2))
        self.assertEqual((hero.pfr.occurrences, hero.pfr.opportunities), (2, 2))
        self.assertEqual((hero.rfi.occurrences, hero.rfi.opportunities), (2, 2))
        self.assertEqual(
            (hero.three_bet.occurrences, hero.three_bet.opportunities),
            (0, 0),
        )

    def test_three_bet_counts_only_first_decision_facing_one_raise(self) -> None:
        stats = calculate_player_stats(self.hands, player_name="Villain")

        villain = stats[0]
        self.assertEqual(villain.hands, 2)
        self.assertEqual((villain.vpip.occurrences, villain.vpip.opportunities), (2, 2))
        self.assertEqual((villain.pfr.occurrences, villain.pfr.opportunities), (1, 2))
        self.assertEqual((villain.rfi.occurrences, villain.rfi.opportunities), (0, 0))
        self.assertEqual(
            (villain.three_bet.occurrences, villain.three_bet.opportunities),
            (1, 2),
        )
        self.assertEqual(villain.three_bet.percentage, 50.0)


class StatsCliTest(unittest.TestCase):
    def test_stats_cli_reads_imported_hands_and_emits_evidence_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "rivermind.db"
            with contextlib.redirect_stdout(io.StringIO()):
                import_exit = main(
                    [
                        "import",
                        str(CASH_FIXTURE),
                        str(MTT_FIXTURE),
                        "--database",
                        str(database),
                    ]
                )
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                stats_exit = main(
                    [
                        "stats",
                        "--database",
                        str(database),
                        "--player",
                        "Hero",
                        "--json",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(import_exit, 0)
            self.assertEqual(stats_exit, 0)
            self.assertEqual(payload["players"][0]["hands"], 2)
            self.assertEqual(
                payload["players"][0]["rfi"],
                {
                    "occurrences": 2,
                    "opportunities": 2,
                    "percentage": 100.0,
                },
            )

            tournament_output = io.StringIO()
            with contextlib.redirect_stdout(tournament_output):
                tournament_exit = main(
                    [
                        "stats",
                        "--database",
                        str(database),
                        "--player",
                        "Hero",
                        "--game-type",
                        "tournament",
                        "--json",
                    ]
                )
            tournament_payload = json.loads(tournament_output.getvalue())
            self.assertEqual(tournament_exit, 0)
            self.assertEqual(tournament_payload["players"][0]["hands"], 1)


if __name__ == "__main__":
    unittest.main()
