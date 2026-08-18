from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.accounting import calculate_hand_ledger  # noqa: E402
from rivermind_core.models import ActionType, BettingRound, GameType  # noqa: E402
from rivermind_core.parsers import GGPokerCashParser, default_registry  # noqa: E402
from rivermind_core.parsers.base import (  # noqa: E402
    HandHistoryParseError,
    UnsupportedHandHistoryError,
)


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "ggpoker_cash.txt"


def _raw() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class GGPokerCashParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hand = default_registry().parse(_raw())

    def test_the_registry_routes_ggpoker_to_its_own_parser(self) -> None:
        parser, hand = default_registry().parse_with_parser(_raw())
        self.assertIsInstance(parser, GGPokerCashParser)
        self.assertEqual(parser.name, "ggpoker_cash_v1")
        self.assertEqual(hand.site, "ggpoker")

    def test_reads_the_header_including_the_lettered_hand_id(self) -> None:
        self.assertEqual(self.hand.hand_id, "RC837124540")
        self.assertEqual(self.hand.game_type, GameType.CASH)
        self.assertEqual(self.hand.game_name, "Hold'em No Limit")
        # GGPoker writes "$0.1", not "$0.10", and omits the currency code.
        self.assertEqual(self.hand.small_blind, Decimal("0.1"))
        self.assertEqual(self.hand.big_blind, Decimal("0.25"))
        self.assertIsNone(self.hand.currency)

    def test_reads_the_table_seats_and_button(self) -> None:
        self.assertEqual(self.hand.table_name, "RushAndCash689800")
        self.assertEqual(self.hand.max_seats, 6)
        self.assertEqual(self.hand.button_seat, 1)
        self.assertEqual(len(self.hand.players), 6)

    def test_assigns_positions_around_the_button(self) -> None:
        positions = {
            player.name: player.position.value for player in self.hand.players
        }
        self.assertEqual(positions["Hero"], "BTN")
        self.assertEqual(positions["bf27d3a"], "SB")
        self.assertEqual(positions["5320473f"], "BB")

    def test_identifies_the_hero_among_anonymous_opponents(self) -> None:
        heroes = [player for player in self.hand.players if player.is_hero]
        self.assertEqual(len(heroes), 1)
        self.assertEqual(heroes[0].name, "Hero")
        self.assertEqual(heroes[0].hole_cards, ("Ah", "Kd"))
        for player in self.hand.players:
            if not player.is_hero:
                self.assertEqual(player.hole_cards, ())

    def test_reads_the_board_and_the_summary(self) -> None:
        self.assertEqual(self.hand.board, ("2c", "7d", "Ts", "As"))
        # The summary carries Jackpot and Bingo columns PokerStars never emits.
        self.assertEqual(self.hand.total_pot, Decimal("3.6"))
        self.assertEqual(self.hand.rake, Decimal("0.15"))

    def test_reads_every_action_in_order(self) -> None:
        kinds = [
            (action.street, action.action_type) for action in self.hand.actions
        ]
        self.assertEqual(kinds[0], (BettingRound.PREFLOP, ActionType.POST_SMALL_BLIND))
        self.assertEqual(kinds[1], (BettingRound.PREFLOP, ActionType.POST_BIG_BLIND))
        self.assertIn((BettingRound.FLOP, ActionType.BET), kinds)
        self.assertIn((BettingRound.TURN, ActionType.RETURN), kinds)
        self.assertIn((BettingRound.TURN, ActionType.COLLECT), kinds)
        self.assertEqual(
            [action.sequence for action in self.hand.actions],
            list(range(len(self.hand.actions))),
        )

    def test_the_chip_ledger_balances(self) -> None:
        ledger = calculate_hand_ledger(self.hand)
        by_name = {item.player_name: item for item in ledger.results}
        self.assertEqual(by_name["Hero"].net_result, Decimal("1.65"))
        self.assertEqual(by_name["bf27d3a"].net_result, Decimal("-0.1"))
        self.assertEqual(by_name["5320473f"].net_result, Decimal("-1.75"))
        # Nets come from actual invested/returned/collected amounts, so they are
        # right regardless of how the house labels its cut.
        self.assertEqual(
            sum(item.invested - item.returned for item in ledger.results),
            self.hand.total_pot,
        )

    def test_the_rake_field_understates_what_the_house_took(self) -> None:
        """GGPoker deducts Jackpot and Bingo on top of Rake. Known limitation.

        ``HandHistory.rake`` holds the Rake column only, because the canonical
        model has one field. The players' combined loss is therefore larger than
        ``rake``, and the difference is the jackpot drop. Anything that models
        cash-game cost — the GTO layer's ``RakeSpec`` in particular — has to
        account for that separately.
        """

        ledger = calculate_hand_ledger(self.hand)
        house_take = -sum(item.net_result for item in ledger.results)
        self.assertEqual(house_take, Decimal("0.20"))
        self.assertEqual(self.hand.rake, Decimal("0.15"))
        self.assertEqual(house_take - self.hand.rake, Decimal("0.05"))


class GGPokerRefusalTest(unittest.TestCase):
    """GGPoker ships features the canonical model cannot hold. Refuse, never guess."""

    def _parse(self, text: str):
        return default_registry().parse(text)

    def _refuses(self, text: str, code: str) -> None:
        with self.assertRaises(HandHistoryParseError) as caught:
            self._parse(text)
        self.assertEqual(caught.exception.code, code)

    def test_refuses_a_hand_run_more_than_once(self) -> None:
        """Two boards cannot be reported as one result."""

        self._refuses(
            _raw().replace("*** FLOP *** [2c 7d Ts]", "*** FIRST FLOP *** [2c 7d Ts]"),
            "ggpoker_run_it_multiple_times",
        )
        self._refuses(
            _raw().replace("Board [2c 7d Ts As]", "Hand was run two times\nBoard [2c 7d Ts As]"),
            "ggpoker_run_it_multiple_times",
        )
        self._refuses(
            _raw().replace("*** TURN *** [2c 7d Ts] [As]", "*** THIRD RIVER *** [2c 7d Ts] [As]"),
            "ggpoker_run_it_multiple_times",
        )

    def test_refuses_ev_cashout(self) -> None:
        """Cashout settles money outside the pot, so the ledger would not balance."""

        for line in (
            "Hero: Chooses to EV Cashout",
            "Hero: Pays Cashout Risk ($1.09)",
            "Hero: Receives Cashout ($2.31)",
        ):
            with self.subTest(line=line):
                self._refuses(
                    _raw().replace("Hero: doesn't show hand", line),
                    "ggpoker_ev_cashout",
                )

    def test_refuses_a_cash_drop(self) -> None:
        self._refuses(
            _raw().replace("*** HOLE CARDS ***", "Cash Drop to Pot : total $5\n*** HOLE CARDS ***"),
            "ggpoker_cash_drop",
        )

    def test_refuses_variants_that_are_not_holdem(self) -> None:
        self._refuses(
            _raw().replace("Hold'em No Limit", "Omaha Pot Limit"),
            "ggpoker_unsupported_variant",
        )
        self._refuses(
            _raw().replace("Dealt to Hero [Ah Kd]", "Dealt to Hero [Ah Kd 7c 2s]"),
            "ggpoker_unsupported_variant",
        )

    def test_refuses_tournament_hands_until_a_real_export_exists(self) -> None:
        self._refuses(
            _raw().replace(
                "Poker Hand #RC837124540: Hold'em No Limit ($0.1/$0.25)",
                "Poker Hand #TM837124540: Tournament #12345, Hold'em No Limit - Level I (10/20)",
            ),
            "ggpoker_tournament_unsupported",
        )

    def test_refuses_an_action_line_it_does_not_understand(self) -> None:
        self._refuses(
            _raw().replace("Hero: bets $1", "Hero: straddles $1"),
            "ggpoker_unsupported_action",
        )

    def test_refuses_a_malformed_header_and_a_missing_table(self) -> None:
        self._refuses(
            _raw().replace("($0.1/$0.25)", "(broken)"),
            "ggpoker_malformed_header",
        )
        self._refuses(
            _raw().replace(
                "Table 'RushAndCash689800' 6-max Seat #1 is the button",
                "Table 'RushAndCash689800'",
            ),
            "ggpoker_missing_table",
        )

    def test_refuses_a_summary_line_it_cannot_read(self) -> None:
        self._refuses(
            _raw().replace(
                "Total pot $3.6 | Rake $0.15 | Jackpot $0.05 | Bingo $0",
                "Total pot $3.6 | Rake unknown",
            ),
            "ggpoker_unsupported_summary",
        )

    def test_an_unsupported_hand_is_isolated_rather_than_failing_the_import(self) -> None:
        """One refused hand must not take the rest of the file with it."""

        import tempfile

        from rivermind_core.importer import HandHistoryImporter, ImportItemStatus
        from rivermind_core.storage import SQLiteHandStore

        good = _raw()
        bad = good.replace("100000000001", "100000000001").replace(
            "Poker Hand #RC837124540", "Poker Hand #RC837124541"
        ).replace("*** FLOP *** [2c 7d Ts]", "*** FIRST FLOP *** [2c 7d Ts]")
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteHandStore(Path(temp) / "x.db") as store:
                report = HandHistoryImporter(default_registry(), store).import_text(
                    "gg.txt", good + "\n\n" + bad
                )
        statuses = [item.status for item in report.items]
        self.assertIn(ImportItemStatus.IMPORTED, statuses)
        self.assertIn(ImportItemStatus.UNSUPPORTED, statuses)
        self.assertEqual(report.imported, 1)


class SharedGrammarTest(unittest.TestCase):
    def test_both_sites_use_the_same_line_grammar(self) -> None:
        """One definition of the action grammar, so the two cannot drift."""

        from rivermind_core.parsers import _common, ggpoker, pokerstars

        for name in ("SEAT_RE", "DEALT_RE", "TABLE_RE", "SHOW_RE", "CARD_RE"):
            self.assertIs(getattr(ggpoker, name), getattr(_common, name))
            self.assertIs(getattr(pokerstars, name), getattr(_common, name))
        self.assertIs(ggpoker.parse_action, _common.parse_action)
        self.assertIs(pokerstars.parse_action, _common.parse_action)
        # The summary line is exactly what does differ, so it is not shared.
        self.assertIsNot(ggpoker.SUMMARY_RE, pokerstars.SUMMARY_RE)

    def test_the_pokerstars_showdown_marker_is_not_ggpokers(self) -> None:
        self.assertEqual(ggpoker_marker(), "*** SHOWDOWN ***")


def ggpoker_marker() -> str:
    from rivermind_core.parsers.ggpoker import _SHOWDOWN_MARKER

    return _SHOWDOWN_MARKER


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
