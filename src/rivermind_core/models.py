from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


CARD_PATTERN = re.compile(r"^[2-9TJQKA][cdhs]$")


class HandValidationError(ValueError):
    """Raised when a normalized hand violates domain invariants."""


class GameType(StrEnum):
    CASH = "cash"
    TOURNAMENT = "tournament"


class BettingRound(StrEnum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"


class ActionType(StrEnum):
    POST_SMALL_BLIND = "post_small_blind"
    POST_BIG_BLIND = "post_big_blind"
    POST_ANTE = "post_ante"
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"
    RAISE = "raise"
    RETURN = "return"
    COLLECT = "collect"
    SHOW = "show"
    MUCK = "muck"


@dataclass(frozen=True, slots=True)
class Player:
    seat: int
    name: str
    starting_stack: Decimal
    is_hero: bool = False
    hole_cards: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.seat <= 0:
            raise HandValidationError("Player seat must be positive")
        if not self.name.strip():
            raise HandValidationError("Player name cannot be empty")
        if self.starting_stack < 0:
            raise HandValidationError("Starting stack cannot be negative")
        if len(self.hole_cards) not in (0, 2):
            raise HandValidationError("Hold'em players must have zero or two known cards")
        _validate_cards(self.hole_cards)


@dataclass(frozen=True, slots=True)
class Action:
    sequence: int
    street: BettingRound
    player: str
    action_type: ActionType
    amount: Decimal | None = None
    to_amount: Decimal | None = None
    is_all_in: bool = False
    raw_text: str = ""

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise HandValidationError("Action sequence cannot be negative")
        if not self.player.strip():
            raise HandValidationError("Action player cannot be empty")
        if self.amount is not None and self.amount < 0:
            raise HandValidationError("Action amount cannot be negative")
        if self.to_amount is not None and self.to_amount < 0:
            raise HandValidationError("Raise target cannot be negative")


@dataclass(frozen=True, slots=True)
class HandHistory:
    site: str
    hand_id: str
    game_type: GameType
    game_name: str
    currency: str | None
    table_name: str
    max_seats: int
    button_seat: int
    small_blind: Decimal
    big_blind: Decimal
    played_at_raw: str | None
    players: tuple[Player, ...]
    actions: tuple[Action, ...]
    board: tuple[str, ...] = ()
    total_pot: Decimal | None = None
    rake: Decimal | None = None
    raw_text: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.hand_id.strip():
            raise HandValidationError("Hand id cannot be empty")
        if not 2 <= self.max_seats <= 10:
            raise HandValidationError("Table size must be between 2 and 10")
        if self.small_blind < 0 or self.big_blind <= 0:
            raise HandValidationError("Blind values are invalid")
        if self.small_blind > self.big_blind:
            raise HandValidationError("Small blind cannot exceed big blind")

        seats = [player.seat for player in self.players]
        names = [player.name for player in self.players]
        if len(seats) != len(set(seats)):
            raise HandValidationError("Player seats must be unique")
        if len(names) != len(set(names)):
            raise HandValidationError("Player names must be unique")
        if self.button_seat not in seats:
            raise HandValidationError("Button seat must belong to a player")

        _validate_cards(self.board)
        if len(self.board) not in (0, 3, 4, 5):
            raise HandValidationError("Board must contain 0, 3, 4, or 5 cards")

        known_cards = list(self.board)
        for player in self.players:
            known_cards.extend(player.hole_cards)
        if len(known_cards) != len(set(known_cards)):
            raise HandValidationError("Known cards cannot be duplicated")

        expected_sequences = list(range(len(self.actions)))
        actual_sequences = [action.sequence for action in self.actions]
        if actual_sequences != expected_sequences:
            raise HandValidationError("Action sequence must be contiguous and ordered")

    @property
    def hero(self) -> Player | None:
        return next((player for player in self.players if player.is_hero), None)


def _validate_cards(cards: tuple[str, ...] | list[str]) -> None:
    invalid = [card for card in cards if not CARD_PATTERN.fullmatch(card)]
    if invalid:
        raise HandValidationError(f"Invalid card codes: {invalid}")
