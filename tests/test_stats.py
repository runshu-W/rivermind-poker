from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.cli import main  # noqa: E402
from rivermind_core.importer import HandHistoryImporter  # noqa: E402
from rivermind_core.models import ActionType, BettingRound, PlayerPosition  # noqa: E402
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.stats import StatsFilter, calculate_player_stats  # noqa: E402
from rivermind_core.storage import SQLiteHandStore  # noqa: E402


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
        self.assertEqual(
            (hero.fold_to_three_bet.occurrences, hero.fold_to_three_bet.opportunities),
            (0, 1),
        )
        self.assertEqual(
            (hero.flop_cbet.occurrences, hero.flop_cbet.opportunities),
            (1, 1),
        )
        self.assertEqual(
            (
                hero.fold_to_flop_cbet.occurrences,
                hero.fold_to_flop_cbet.opportunities,
            ),
            (0, 1),
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
        self.assertEqual(
            (villain.call_open.occurrences, villain.call_open.opportunities),
            (1, 2),
        )
        self.assertEqual(
            (villain.cold_call.occurrences, villain.cold_call.opportunities),
            (0, 1),
        )

    def test_positive_fold_and_cold_call_branches(self) -> None:
        cash, tournament = self.hands
        call_three_bet_index = next(
            index
            for index, action in enumerate(tournament.actions)
            if action.player == "Hero"
            and action.street == BettingRound.PREFLOP
            and action.action_type == ActionType.CALL
        )
        fold_to_three_bet = replace(
            tournament,
            actions=(
                *tournament.actions[:call_three_bet_index],
                replace(
                    tournament.actions[call_three_bet_index],
                    action_type=ActionType.FOLD,
                    amount=None,
                ),
            ),
            board=(),
        )
        hero = calculate_player_stats(
            [fold_to_three_bet], player_name="Hero"
        )[0]
        self.assertEqual(
            (hero.fold_to_three_bet.occurrences, hero.fold_to_three_bet.opportunities),
            (1, 1),
        )

        flop_call_index = next(
            index
            for index, action in enumerate(cash.actions)
            if action.player == "Villain"
            and action.street == BettingRound.FLOP
            and action.action_type == ActionType.CALL
        )
        fold_to_cbet = replace(
            cash,
            actions=(
                *cash.actions[:flop_call_index],
                replace(
                    cash.actions[flop_call_index],
                    action_type=ActionType.FOLD,
                    amount=None,
                ),
            ),
            board=("2c", "7d", "Ts"),
        )
        villain = calculate_player_stats(
            [fold_to_cbet], player_name="Villain"
        )[0]
        self.assertEqual(
            (
                villain.fold_to_flop_cbet.occurrences,
                villain.fold_to_flop_cbet.opportunities,
            ),
            (1, 1),
        )

        three_bet_index = next(
            index
            for index, action in enumerate(tournament.actions)
            if action.player == "Villain"
            and action.street == BettingRound.PREFLOP
            and action.action_type == ActionType.RAISE
        )
        cold_call = replace(
            tournament,
            actions=(
                *tournament.actions[:three_bet_index],
                replace(
                    tournament.actions[three_bet_index],
                    action_type=ActionType.CALL,
                    amount=Decimal("100"),
                    to_amount=None,
                ),
            ),
            board=(),
        )
        villain = calculate_player_stats([cold_call], player_name="Villain")[0]
        self.assertEqual(
            (villain.cold_call.occurrences, villain.cold_call.opportunities),
            (1, 1),
        )


class StatsWideTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteHandStore(":memory:")
        importer = HandHistoryImporter(default_registry(), self.store)
        importer.import_text("cash.txt", CASH_FIXTURE.read_text(encoding="utf-8"))
        importer.import_text("mtt.txt", MTT_FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.store.close()

    def test_materializes_one_indexed_row_per_player_hand(self) -> None:
        self.assertEqual(self.store.hand_count(), 2)
        self.assertEqual(self.store.stat_row_count(), 8)
        hero = self.store.query_player_stats(heroes_only=True)[0]
        self.assertEqual(hero.hands, 2)
        self.assertEqual(
            (hero.fold_to_three_bet.occurrences, hero.fold_to_three_bet.opportunities),
            (0, 1),
        )

    def test_filters_by_position_game_type_and_effective_stack(self) -> None:
        filtered = self.store.query_player_stats(
            heroes_only=True,
            stat_filter=StatsFilter(
                positions=frozenset({PlayerPosition.UNDER_THE_GUN}),
                max_effective_stack_bb=Decimal("70"),
            ),
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].hands, 1)
        self.assertEqual(filtered[0].vpip.percentage, 100.0)

    def test_version_one_database_backfills_wide_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "migration.db"
            with SQLiteHandStore(database) as store:
                HandHistoryImporter(default_registry(), store).import_text(
                    "cash.txt", CASH_FIXTURE.read_text(encoding="utf-8")
                )
            connection = sqlite3.connect(database)
            payload_json = connection.execute(
                "SELECT payload_json FROM hands LIMIT 1"
            ).fetchone()[0]
            payload = json.loads(payload_json)
            for player in payload["players"]:
                player.pop("position", None)
                player.pop("starting_stack_bb", None)
                player.pop("effective_stack_bb", None)
            connection.execute(
                "UPDATE hands SET payload_json = ?",
                (json.dumps(payload),),
            )
            connection.execute("DELETE FROM player_hand_stats")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
            connection.close()

            with SQLiteHandStore(database) as migrated:
                self.assertEqual(migrated.stat_row_count(), 2)


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
                        "--position",
                        "UTG",
                        "--max-effective-stack-bb",
                        "70",
                        "--json",
                    ]
                )
            tournament_payload = json.loads(tournament_output.getvalue())
            self.assertEqual(tournament_exit, 0)
            self.assertEqual(tournament_payload["players"][0]["hands"], 1)


if __name__ == "__main__":
    unittest.main()
