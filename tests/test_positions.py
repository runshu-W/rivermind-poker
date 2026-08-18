from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.models import (  # noqa: E402
    Player,
    PlayerPosition,
    enrich_player_context,
)


class PositionAssignmentTest(unittest.TestCase):
    def test_assigns_standard_nine_max_positions_clockwise_from_button(self) -> None:
        players = tuple(
            Player(seat=seat, name=f"P{seat}", starting_stack=Decimal("100"))
            for seat in range(1, 10)
        )

        enriched = enrich_player_context(
            players, button_seat=9, big_blind=Decimal("2")
        )

        self.assertEqual(
            [player.position for player in enriched],
            [
                PlayerPosition.SMALL_BLIND,
                PlayerPosition.BIG_BLIND,
                PlayerPosition.UNDER_THE_GUN,
                PlayerPosition.UNDER_THE_GUN_1,
                PlayerPosition.UNDER_THE_GUN_2,
                PlayerPosition.LOJACK,
                PlayerPosition.HIJACK,
                PlayerPosition.CUTOFF,
                PlayerPosition.BUTTON,
            ],
        )
        self.assertTrue(
            all(player.starting_stack_bb == Decimal("50") for player in enriched)
        )
        self.assertTrue(
            all(player.effective_stack_bb == Decimal("50") for player in enriched)
        )


if __name__ == "__main__":
    unittest.main()
