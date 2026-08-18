from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum


CARD_PATTERN = re.compile(r"^[2-9TJQKA][cdhs]$")


class HandValidationError(ValueError):
    """Raised when a normalized hand violates domain invariants."""


class GameType(StrEnum):
    CASH = "cash"
    TOURNAMENT = "tournament"


class PlayerPosition(StrEnum):
    SMALL_BLIND = "SB"
    BIG_BLIND = "BB"
    UNDER_THE_GUN = "UTG"
    UNDER_THE_GUN_1 = "UTG+1"
    UNDER_THE_GUN_2 = "UTG+2"
    UNDER_THE_GUN_3 = "UTG+3"
    LOJACK = "LJ"
    HIJACK = "HJ"
    CUTOFF = "CO"
    BUTTON = "BTN"


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
    position: PlayerPosition | None = None
    starting_stack_bb: Decimal | None = None
    effective_stack_bb: Decimal | None = None

    def __post_init__(self) -> None:
        if self.seat <= 0:
            raise HandValidationError("Player seat must be positive")
        if not self.name.strip():
            raise HandValidationError("Player name cannot be empty")
        if self.starting_stack < 0:
            raise HandValidationError("Starting stack cannot be negative")
        if len(self.hole_cards) not in (0, 2):
            raise HandValidationError("Hold'em players must have zero or two known cards")
        if self.starting_stack_bb is not None and self.starting_stack_bb < 0:
            raise HandValidationError("Starting stack in BB cannot be negative")
        if self.effective_stack_bb is not None and self.effective_stack_bb < 0:
            raise HandValidationError("Effective stack in BB cannot be negative")
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
    tournament_id: str | None = None
    tournament_level: str | None = None
    buy_in: Decimal | None = None
    fee: Decimal | None = None
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
        if self.game_type == GameType.TOURNAMENT and not self.tournament_id:
            raise HandValidationError("Tournament hands require a tournament id")
        if self.game_type == GameType.CASH and self.tournament_id is not None:
            raise HandValidationError("Cash hands cannot have a tournament id")
        if self.buy_in is not None and self.buy_in < 0:
            raise HandValidationError("Tournament buy-in cannot be negative")
        if self.fee is not None and self.fee < 0:
            raise HandValidationError("Tournament fee cannot be negative")

        seats = [player.seat for player in self.players]
        names = [player.name for player in self.players]
        if len(seats) != len(set(seats)):
            raise HandValidationError("Player seats must be unique")
        if len(names) != len(set(names)):
            raise HandValidationError("Player names must be unique")
        if self.button_seat not in seats:
            raise HandValidationError("Button seat must belong to a player")
        unknown_action_players = {
            action.player for action in self.actions if action.player not in names
        }
        if unknown_action_players:
            raise HandValidationError(
                f"Actions reference unknown players: {sorted(unknown_action_players)}"
            )

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


def enrich_player_context(
    players: tuple[Player, ...] | list[Player],
    *,
    button_seat: int,
    big_blind: Decimal,
) -> tuple[Player, ...]:
    """Assign canonical positions and preflop table-effective stacks."""

    if big_blind <= 0:
        raise HandValidationError("Big blind must be positive")
    ordered = sorted(players, key=lambda player: player.seat)
    button_index = next(
        (index for index, player in enumerate(ordered) if player.seat == button_seat),
        None,
    )
    if button_index is None:
        raise HandValidationError("Button seat must belong to a player")

    clockwise = ordered[button_index + 1 :] + ordered[: button_index + 1]
    positions = _positions_for_player_count(len(clockwise))
    position_by_name = {
        player.name: position for player, position in zip(clockwise, positions, strict=True)
    }

    enriched: list[Player] = []
    for player in players:
        largest_opponent_stack = max(
            opponent.starting_stack
            for opponent in players
            if opponent.name != player.name
        )
        enriched.append(
            replace(
                player,
                position=position_by_name[player.name],
                starting_stack_bb=player.starting_stack / big_blind,
                effective_stack_bb=(
                    min(player.starting_stack, largest_opponent_stack) / big_blind
                ),
            )
        )
    return tuple(enriched)


def _positions_for_player_count(count: int) -> tuple[PlayerPosition, ...]:
    if not 2 <= count <= 10:
        raise HandValidationError("Position assignment supports 2 to 10 players")
    if count == 2:
        return (PlayerPosition.BIG_BLIND, PlayerPosition.BUTTON)

    middle_positions = {
        0: (),
        1: (PlayerPosition.CUTOFF,),
        2: (PlayerPosition.UNDER_THE_GUN, PlayerPosition.CUTOFF),
        3: (
            PlayerPosition.UNDER_THE_GUN,
            PlayerPosition.HIJACK,
            PlayerPosition.CUTOFF,
        ),
        4: (
            PlayerPosition.UNDER_THE_GUN,
            PlayerPosition.LOJACK,
            PlayerPosition.HIJACK,
            PlayerPosition.CUTOFF,
        ),
        5: (
            PlayerPosition.UNDER_THE_GUN,
            PlayerPosition.UNDER_THE_GUN_1,
            PlayerPosition.LOJACK,
            PlayerPosition.HIJACK,
            PlayerPosition.CUTOFF,
        ),
        6: (
            PlayerPosition.UNDER_THE_GUN,
            PlayerPosition.UNDER_THE_GUN_1,
            PlayerPosition.UNDER_THE_GUN_2,
            PlayerPosition.LOJACK,
            PlayerPosition.HIJACK,
            PlayerPosition.CUTOFF,
        ),
        7: (
            PlayerPosition.UNDER_THE_GUN,
            PlayerPosition.UNDER_THE_GUN_1,
            PlayerPosition.UNDER_THE_GUN_2,
            PlayerPosition.UNDER_THE_GUN_3,
            PlayerPosition.LOJACK,
            PlayerPosition.HIJACK,
            PlayerPosition.CUTOFF,
        ),
    }[count - 3]
    return (
        PlayerPosition.SMALL_BLIND,
        PlayerPosition.BIG_BLIND,
        *middle_positions,
        PlayerPosition.BUTTON,
    )
