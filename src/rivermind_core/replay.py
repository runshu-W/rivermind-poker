from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rivermind_core.accounting import PlayerHandResult, calculate_action_ledger, calculate_hand_ledger
from rivermind_core.models import ActionType, BettingRound, HandHistory, PlayerPosition


@dataclass(frozen=True, slots=True)
class ReplayPlayer:
    seat: int
    name: str
    position: PlayerPosition | None
    starting_stack: Decimal
    starting_stack_bb: Decimal | None
    is_hero: bool
    hole_cards: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    sequence: int
    street: BettingRound
    visible_board: tuple[str, ...]
    player_name: str
    action_type: ActionType
    amount: Decimal | None
    to_amount: Decimal | None
    is_all_in: bool
    invested: Decimal
    returned: Decimal
    collected: Decimal
    pot_after: Decimal


@dataclass(frozen=True, slots=True)
class HandReplay:
    site: str
    hand_id: str
    table_name: str
    played_at_raw: str | None
    button_seat: int
    small_blind: Decimal
    big_blind: Decimal
    board: tuple[str, ...]
    players: tuple[ReplayPlayer, ...]
    frames: tuple[ReplayFrame, ...]
    results: tuple[PlayerHandResult, ...]
    accounting_balanced: bool


def build_hand_replay(hand: HandHistory) -> HandReplay:
    ledger = calculate_hand_ledger(hand)
    frames = tuple(
        ReplayFrame(
            sequence=entry.action.sequence,
            street=entry.action.street,
            visible_board=_visible_board(hand.board, entry.action.street),
            player_name=entry.action.player,
            action_type=entry.action.action_type,
            amount=entry.action.amount,
            to_amount=entry.action.to_amount,
            is_all_in=entry.action.is_all_in,
            invested=entry.invested,
            returned=entry.returned,
            collected=entry.collected,
            pot_after=entry.pot_after,
        )
        for entry in calculate_action_ledger(hand)
    )
    return HandReplay(
        site=hand.site,
        hand_id=hand.hand_id,
        table_name=hand.table_name,
        played_at_raw=hand.played_at_raw,
        button_seat=hand.button_seat,
        small_blind=hand.small_blind,
        big_blind=hand.big_blind,
        board=hand.board,
        players=tuple(
            ReplayPlayer(
                seat=player.seat,
                name=player.name,
                position=player.position,
                starting_stack=player.starting_stack,
                starting_stack_bb=player.starting_stack_bb,
                is_hero=player.is_hero,
                hole_cards=player.hole_cards,
            )
            for player in hand.players
        ),
        frames=frames,
        results=ledger.results,
        accounting_balanced=ledger.is_balanced,
    )


def _visible_board(
    board: tuple[str, ...], street: BettingRound
) -> tuple[str, ...]:
    visible_count = {
        BettingRound.PREFLOP: 0,
        BettingRound.FLOP: 3,
        BettingRound.TURN: 4,
        BettingRound.RIVER: 5,
        BettingRound.SHOWDOWN: 5,
    }[street]
    return board[:visible_count]
