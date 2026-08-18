from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core import ActionType, GameType, PlayerPosition  # noqa: E402
from rivermind_core.parsers import (  # noqa: E402
    PokerStarsTournamentParser,
    default_registry,
)


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "pokerstars_mtt.txt"


class PokerStarsTournamentParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw_hand = FIXTURE.read_text(encoding="utf-8")

    def test_registry_routes_and_normalizes_paid_tournament(self) -> None:
        hand = default_registry().parse(self.raw_hand)

        self.assertEqual(hand.game_type, GameType.TOURNAMENT)
        self.assertEqual(hand.hand_id, "200000000001")
        self.assertEqual(hand.tournament_id, "900000001")
        self.assertEqual(hand.tournament_level, "III")
        self.assertEqual(hand.buy_in, Decimal("10.00"))
        self.assertEqual(hand.fee, Decimal("1.00"))
        self.assertEqual(hand.currency, "USD")
        self.assertEqual(hand.small_blind, Decimal("25"))
        self.assertEqual(hand.big_blind, Decimal("50"))
        self.assertEqual(hand.max_seats, 6)
        positions = {player.name: player.position for player in hand.players}
        self.assertEqual(
            positions,
            {
                "SmallBlind": PlayerPosition.SMALL_BLIND,
                "BigBlind": PlayerPosition.BIG_BLIND,
                "Hero": PlayerPosition.UNDER_THE_GUN,
                "Villain": PlayerPosition.HIJACK,
                "Cutoff": PlayerPosition.CUTOFF,
                "Button": PlayerPosition.BUTTON,
            },
        )
        assert hand.hero is not None
        self.assertEqual(hand.hero.starting_stack_bb, Decimal("64"))
        self.assertEqual(hand.hero.effective_stack_bb, Decimal("64"))

    def test_parses_antes_three_bet_board_and_showdown_cards(self) -> None:
        hand = PokerStarsTournamentParser().parse(self.raw_hand)

        antes = [
            action
            for action in hand.actions
            if action.action_type == ActionType.POST_ANTE
        ]
        raises = [
            action
            for action in hand.actions
            if action.action_type == ActionType.RAISE
        ]
        villain = next(player for player in hand.players if player.name == "Villain")

        self.assertEqual(len(antes), 6)
        self.assertEqual([action.to_amount for action in raises], [Decimal("100"), Decimal("300")])
        self.assertEqual(hand.board, ("2c", "7d", "Ts", "As", "4h"))
        self.assertEqual(villain.hole_cards, ("Qs", "Qh"))
        self.assertEqual(hand.total_pot, Decimal("1080"))
        self.assertEqual(hand.rake, Decimal("0"))

    def test_supports_freeroll_header_without_claiming_currency(self) -> None:
        freeroll = self.raw_hand.replace(
            "$10.00+$1.00 USD", "Freeroll", 1
        ).replace("#200000000001", "#200000000002", 1)

        hand = PokerStarsTournamentParser().parse(freeroll)

        self.assertIsNone(hand.currency)
        self.assertEqual(hand.buy_in, Decimal("0"))
        self.assertEqual(hand.fee, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
