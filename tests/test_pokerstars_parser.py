from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core import ActionType, BettingRound  # noqa: E402
from rivermind_core.parsers import (  # noqa: E402
    HandHistoryParseError,
    PokerStarsCashParser,
    default_registry,
)


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "pokerstars_cash.txt"


class PokerStarsCashParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw_hand = FIXTURE.read_text(encoding="utf-8")

    def test_parses_canonical_hand(self) -> None:
        hand = PokerStarsCashParser().parse(self.raw_hand)

        self.assertEqual(hand.site, "pokerstars")
        self.assertEqual(hand.hand_id, "100000000001")
        self.assertEqual(hand.table_name, "RiverMind Alpha")
        self.assertEqual(hand.max_seats, 6)
        self.assertEqual(hand.button_seat, 3)
        self.assertEqual(hand.small_blind, Decimal("0.05"))
        self.assertEqual(hand.big_blind, Decimal("0.10"))
        self.assertEqual(hand.currency, "USD")
        self.assertEqual(hand.board, ("2c", "7d", "Ts", "As"))
        self.assertEqual(hand.total_pot, Decimal("1.32"))
        self.assertEqual(hand.rake, Decimal("0.03"))

    def test_marks_hero_and_hole_cards(self) -> None:
        hand = default_registry().parse(self.raw_hand)

        self.assertIsNotNone(hand.hero)
        assert hand.hero is not None
        self.assertEqual(hand.hero.name, "Hero")
        self.assertEqual(hand.hero.hole_cards, ("Ah", "Kd"))

    def test_normalizes_action_sequence_and_streets(self) -> None:
        hand = default_registry().parse(self.raw_hand)

        self.assertEqual(
            [action.sequence for action in hand.actions],
            list(range(len(hand.actions))),
        )
        self.assertEqual(hand.actions[0].action_type, ActionType.POST_SMALL_BLIND)
        self.assertEqual(hand.actions[2].action_type, ActionType.RAISE)
        self.assertEqual(hand.actions[2].to_amount, Decimal("0.30"))
        turn_bet = next(
            action
            for action in hand.actions
            if action.street == BettingRound.TURN
            and action.action_type == ActionType.BET
        )
        self.assertEqual(turn_bet.amount, Decimal("0.75"))
        self.assertEqual(hand.actions[-1].action_type, ActionType.COLLECT)

    def test_registry_rejects_unknown_format(self) -> None:
        with self.assertRaises(HandHistoryParseError):
            default_registry().parse("Unknown room hand #1")

    def test_parser_fails_closed_for_unsupported_header(self) -> None:
        raw = self.raw_hand.replace(
            "Hold'em No Limit ($0.05/$0.10 USD)",
            "Tournament #1, Hold'em No Limit - Level I (10/20)",
            1,
        )
        with self.assertRaises(HandHistoryParseError):
            PokerStarsCashParser().parse(raw)

    def test_parser_does_not_silently_drop_unknown_player_action(self) -> None:
        raw = self.raw_hand.replace(
            "Villain: posts big blind $0.10",
            "Villain: posts big blind $0.10\nHero: straddles $0.20",
            1,
        )

        with self.assertRaisesRegex(
            HandHistoryParseError, "Unsupported PokerStars action line"
        ):
            PokerStarsCashParser().parse(raw)


if __name__ == "__main__":
    unittest.main()
