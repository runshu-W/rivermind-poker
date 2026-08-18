from __future__ import annotations

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

from rivermind_core import ActionType, GameType  # noqa: E402
from rivermind_core.accounting import calculate_hand_ledger  # noqa: E402
from rivermind_core.importer import HandHistoryImporter  # noqa: E402
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.reports import HandQuery, StatMetric  # noqa: E402
from rivermind_core.replay import build_hand_replay  # noqa: E402
from rivermind_core.sessions import (  # noqa: E402
    build_player_hand_outcomes,
    summarize_sessions,
)
from rivermind_core.storage import SQLiteHandStore  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


class AccountingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = default_registry()
        cls.cash = registry.parse(
            (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
        )
        cls.tournament = registry.parse(
            (FIXTURES / "pokerstars_mtt.txt").read_text(encoding="utf-8")
        )

    def test_cash_ledger_uses_raise_target_and_conserves_rake(self) -> None:
        ledger = calculate_hand_ledger(self.cash)
        hero = ledger.result_for("Hero")
        villain = ledger.result_for("Villain")

        assert hero is not None and villain is not None
        self.assertEqual(hero.invested, Decimal("1.38"))
        self.assertEqual(hero.returned, Decimal("0.75"))
        self.assertEqual(hero.collected, Decimal("1.23"))
        self.assertEqual(hero.net_result, Decimal("0.60"))
        self.assertEqual(hero.net_result_bb, Decimal("6"))
        self.assertEqual(villain.net_result, Decimal("-0.63"))
        self.assertEqual(ledger.accounted_total_pot, Decimal("1.26"))
        self.assertEqual(ledger.balance_delta, Decimal("0.00"))
        self.assertTrue(ledger.is_balanced)

    def test_tournament_ledger_reports_chips_and_bb_without_claiming_cash_profit(self) -> None:
        ledger = calculate_hand_ledger(self.tournament)
        hero = ledger.result_for("Hero")

        assert hero is not None
        self.assertEqual(hero.invested, Decimal("505"))
        self.assertEqual(hero.collected, Decimal("1105"))
        self.assertEqual(hero.net_result, Decimal("600"))
        self.assertEqual(hero.net_result_bb, Decimal("12"))
        self.assertTrue(ledger.is_balanced)

    def test_replay_exposes_action_deltas_visible_board_and_running_pot(self) -> None:
        replay = build_hand_replay(self.cash)
        hero_raise = next(
            frame
            for frame in replay.frames
            if frame.player_name == "Hero"
            and frame.action_type == ActionType.RAISE
        )

        self.assertEqual(hero_raise.invested, Decimal("0.25"))
        self.assertEqual(hero_raise.pot_after, Decimal("0.40"))
        self.assertEqual(hero_raise.visible_board, ())
        self.assertEqual(replay.frames[-1].pot_after, Decimal("0.03"))
        self.assertTrue(replay.accounting_balanced)


class SessionSummaryTest(unittest.TestCase):
    def test_cash_gap_and_tournament_id_create_stable_sessions(self) -> None:
        registry = default_registry()
        cash = registry.parse(
            (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
        )
        tournament = registry.parse(
            (FIXTURES / "pokerstars_mtt.txt").read_text(encoding="utf-8")
        )
        cash_20_minutes_later = replace(
            cash,
            hand_id="100000000002",
            played_at_raw="2026/08/18 12:20:00 ET",
        )
        cash_60_minutes_later = replace(
            cash,
            hand_id="100000000003",
            played_at_raw="2026/08/18 13:00:00 ET",
        )

        outcomes = build_player_hand_outcomes(
            [cash, cash_20_minutes_later, cash_60_minutes_later, tournament],
            heroes_only=True,
        )
        sessions = summarize_sessions(outcomes)

        self.assertEqual(len(outcomes), 4)
        self.assertEqual(len(sessions), 3)
        cash_sessions = [
            session for session in sessions if session.game_type == GameType.CASH
        ]
        tournament_session = next(
            session
            for session in sessions
            if session.game_type == GameType.TOURNAMENT
        )
        self.assertEqual(sorted(session.hands for session in cash_sessions), [1, 2])
        self.assertEqual(
            sorted(session.net_result for session in cash_sessions),
            [Decimal("0.60"), Decimal("1.20")],
        )
        self.assertEqual(tournament_session.tournament_id, "900000001")
        self.assertEqual(tournament_session.net_result, Decimal("600"))
        self.assertEqual(tournament_session.result_unit, "chips")


class StoredReportsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteHandStore(":memory:")
        importer = HandHistoryImporter(default_registry(), self.store)
        importer.import_text(
            "cash.txt",
            (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8"),
        )
        importer.import_text(
            "mtt.txt",
            (FIXTURES / "pokerstars_mtt.txt").read_text(encoding="utf-8"),
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_materializes_results_and_filters_related_metric_hands(self) -> None:
        cbet_hands = self.store.query_hands(
            heroes_only=True,
            query=HandQuery(metric=StatMetric.FLOP_CBET, occurred=True),
        )
        missed_fold_to_three_bet = self.store.query_hands(
            heroes_only=True,
            query=HandQuery(
                metric=StatMetric.FOLD_TO_THREE_BET,
                occurred=False,
            ),
        )

        self.assertEqual(self.store.result_row_count(), 8)
        self.assertEqual(len(cbet_hands), 1)
        self.assertEqual(cbet_hands[0].stats.hand_id, "100000000001")
        self.assertEqual(cbet_hands[0].net_result, Decimal("0.60"))
        self.assertEqual(len(missed_fold_to_three_bet), 1)
        self.assertEqual(
            missed_fold_to_three_bet[0].stats.hand_id,
            "200000000001",
        )

    def test_queries_sessions_and_structured_replay_from_store(self) -> None:
        sessions = self.store.query_sessions(heroes_only=True)
        replay = self.store.load_replay("pokerstars", "100000000001")

        self.assertEqual(len(sessions), 2)
        self.assertEqual(
            {session.game_type for session in sessions},
            {GameType.CASH, GameType.TOURNAMENT},
        )
        self.assertIsNotNone(replay)
        assert replay is not None
        self.assertEqual(replay.frames[-1].pot_after, Decimal("0.03"))

    def test_version_two_database_backfills_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "migration.db"
            with SQLiteHandStore(database) as store:
                HandHistoryImporter(default_registry(), store).import_text(
                    "cash.txt",
                    (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8"),
                )
            connection = sqlite3.connect(database)
            connection.execute("DELETE FROM player_hand_results")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
            connection.close()

            with SQLiteHandStore(database) as migrated:
                self.assertEqual(migrated.result_row_count(), 2)
                report = migrated.query_hands(heroes_only=True)[0]
                self.assertEqual(report.net_result, Decimal("0.60"))


if __name__ == "__main__":
    unittest.main()
