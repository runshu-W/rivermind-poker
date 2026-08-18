from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from rivermind_core.models import (
    ActionType,
    Action,
    BettingRound,
    GameType,
    HandHistory,
    PlayerPosition,
    enrich_player_context,
)


METRIC_NAMES = (
    "vpip",
    "pfr",
    "rfi",
    "three_bet",
    "call_open",
    "cold_call",
    "fold_to_three_bet",
    "flop_cbet",
    "fold_to_flop_cbet",
)


@dataclass(frozen=True, slots=True)
class StatValue:
    occurrences: int
    opportunities: int

    def __post_init__(self) -> None:
        if self.occurrences < 0 or self.opportunities < 0:
            raise ValueError("Stat counts cannot be negative")
        if self.occurrences > self.opportunities:
            raise ValueError("Stat occurrences cannot exceed opportunities")

    @property
    def percentage(self) -> float | None:
        if self.opportunities == 0:
            return None
        return self.occurrences * 100 / self.opportunities


@dataclass(frozen=True, slots=True)
class PlayerStats:
    player_name: str
    hands: int
    vpip: StatValue
    pfr: StatValue
    rfi: StatValue
    three_bet: StatValue
    call_open: StatValue
    cold_call: StatValue
    fold_to_three_bet: StatValue
    flop_cbet: StatValue
    fold_to_flop_cbet: StatValue


@dataclass(frozen=True, slots=True)
class PlayerHandStatRow:
    site: str
    hand_id: str
    game_type: GameType
    tournament_id: str | None
    player_name: str
    is_hero: bool
    position: PlayerPosition
    starting_stack_bb: Decimal
    effective_stack_bb: Decimal
    vpip: bool
    pfr: bool
    rfi_opportunity: bool
    rfi: bool
    three_bet_opportunity: bool
    three_bet: bool
    call_open_opportunity: bool
    call_open: bool
    cold_call_opportunity: bool
    cold_call: bool
    fold_to_three_bet_opportunity: bool
    fold_to_three_bet: bool
    flop_cbet_opportunity: bool
    flop_cbet: bool
    fold_to_flop_cbet_opportunity: bool
    fold_to_flop_cbet: bool


@dataclass(frozen=True, slots=True)
class StatsFilter:
    game_types: frozenset[GameType] = frozenset()
    positions: frozenset[PlayerPosition] = frozenset()
    min_effective_stack_bb: Decimal | None = None
    max_effective_stack_bb: Decimal | None = None

    def __post_init__(self) -> None:
        if self.min_effective_stack_bb is not None and self.min_effective_stack_bb < 0:
            raise ValueError("Minimum effective stack cannot be negative")
        if self.max_effective_stack_bb is not None and self.max_effective_stack_bb < 0:
            raise ValueError("Maximum effective stack cannot be negative")
        if (
            self.min_effective_stack_bb is not None
            and self.max_effective_stack_bb is not None
            and self.min_effective_stack_bb > self.max_effective_stack_bb
        ):
            raise ValueError("Minimum effective stack cannot exceed maximum")

    def matches(self, row: PlayerHandStatRow) -> bool:
        if self.game_types and row.game_type not in self.game_types:
            return False
        if self.positions and row.position not in self.positions:
            return False
        if (
            self.min_effective_stack_bb is not None
            and row.effective_stack_bb < self.min_effective_stack_bb
        ):
            return False
        if (
            self.max_effective_stack_bb is not None
            and row.effective_stack_bb > self.max_effective_stack_bb
        ):
            return False
        return True


@dataclass(slots=True)
class _MutableStat:
    occurrences: int = 0
    opportunities: int = 0

    def observe(self, occurred: bool, opportunity: bool = True) -> None:
        if not opportunity:
            return
        self.opportunities += 1
        self.occurrences += int(occurred)

    def freeze(self) -> StatValue:
        return StatValue(self.occurrences, self.opportunities)


@dataclass(slots=True)
class _Accumulator:
    hands: int = 0
    metrics: dict[str, _MutableStat] = field(
        default_factory=lambda: {name: _MutableStat() for name in METRIC_NAMES}
    )


@dataclass(frozen=True, slots=True)
class _HandObservation:
    vpip_players: frozenset[str]
    pfr_players: frozenset[str]
    rfi_opportunities: frozenset[str]
    rfi_players: frozenset[str]
    three_bet_opportunities: frozenset[str]
    three_bet_players: frozenset[str]
    call_open_opportunities: frozenset[str]
    call_open_players: frozenset[str]
    cold_call_opportunities: frozenset[str]
    cold_call_players: frozenset[str]
    fold_to_three_bet_opportunities: frozenset[str]
    fold_to_three_bet_players: frozenset[str]
    flop_cbet_opportunities: frozenset[str]
    flop_cbet_players: frozenset[str]
    fold_to_flop_cbet_opportunities: frozenset[str]
    fold_to_flop_cbet_players: frozenset[str]


DECISION_TYPES = {
    ActionType.FOLD,
    ActionType.CHECK,
    ActionType.CALL,
    ActionType.BET,
    ActionType.RAISE,
}
VOLUNTARY_ENTRY_TYPES = {
    ActionType.CALL,
    ActionType.BET,
    ActionType.RAISE,
}
FACING_BET_DECISION_TYPES = {
    ActionType.FOLD,
    ActionType.CALL,
    ActionType.RAISE,
}
BLIND_POSITIONS = {PlayerPosition.SMALL_BLIND, PlayerPosition.BIG_BLIND}


def calculate_player_stats(
    hands: Iterable[HandHistory],
    *,
    player_name: str | None = None,
    heroes_only: bool = False,
    stat_filter: StatsFilter | None = None,
) -> tuple[PlayerStats, ...]:
    rows = (
        row
        for hand in hands
        for row in build_player_hand_stat_rows(hand)
    )
    return aggregate_player_stat_rows(
        rows,
        player_name=player_name,
        heroes_only=heroes_only,
        stat_filter=stat_filter,
    )


def aggregate_player_stat_rows(
    rows: Iterable[PlayerHandStatRow],
    *,
    player_name: str | None = None,
    heroes_only: bool = False,
    stat_filter: StatsFilter | None = None,
) -> tuple[PlayerStats, ...]:
    if player_name is not None and heroes_only:
        raise ValueError("player_name and heroes_only cannot be combined")
    active_filter = stat_filter or StatsFilter()
    accumulators: dict[str, _Accumulator] = {}

    for row in rows:
        if player_name is not None and row.player_name != player_name:
            continue
        if heroes_only and not row.is_hero:
            continue
        if not active_filter.matches(row):
            continue
        accumulator = accumulators.setdefault(row.player_name, _Accumulator())
        accumulator.hands += 1
        for metric_name in METRIC_NAMES:
            opportunity_name = f"{metric_name}_opportunity"
            opportunity = getattr(row, opportunity_name, True)
            accumulator.metrics[metric_name].observe(
                bool(getattr(row, metric_name)), bool(opportunity)
            )

    return tuple(
        PlayerStats(
            player_name=name,
            hands=accumulator.hands,
            **{
                metric_name: accumulator.metrics[metric_name].freeze()
                for metric_name in METRIC_NAMES
            },
        )
        for name, accumulator in sorted(accumulators.items())
    )


def build_player_hand_stat_rows(hand: HandHistory) -> tuple[PlayerHandStatRow, ...]:
    players = hand.players
    if any(
        player.position is None
        or player.starting_stack_bb is None
        or player.effective_stack_bb is None
        for player in players
    ):
        players = enrich_player_context(
            players, button_seat=hand.button_seat, big_blind=hand.big_blind
        )
    position_by_name = {
        player.name: player.position for player in players
    }
    observation = _observe_hand(hand, position_by_name)

    rows: list[PlayerHandStatRow] = []
    for player in players:
        assert player.position is not None
        assert player.starting_stack_bb is not None
        assert player.effective_stack_bb is not None
        rows.append(
            PlayerHandStatRow(
                site=hand.site,
                hand_id=hand.hand_id,
                game_type=hand.game_type,
                tournament_id=hand.tournament_id,
                player_name=player.name,
                is_hero=player.is_hero,
                position=player.position,
                starting_stack_bb=player.starting_stack_bb,
                effective_stack_bb=player.effective_stack_bb,
                vpip=player.name in observation.vpip_players,
                pfr=player.name in observation.pfr_players,
                rfi_opportunity=player.name in observation.rfi_opportunities,
                rfi=player.name in observation.rfi_players,
                three_bet_opportunity=(
                    player.name in observation.three_bet_opportunities
                ),
                three_bet=player.name in observation.three_bet_players,
                call_open_opportunity=(
                    player.name in observation.call_open_opportunities
                ),
                call_open=player.name in observation.call_open_players,
                cold_call_opportunity=(
                    player.name in observation.cold_call_opportunities
                ),
                cold_call=player.name in observation.cold_call_players,
                fold_to_three_bet_opportunity=(
                    player.name in observation.fold_to_three_bet_opportunities
                ),
                fold_to_three_bet=(
                    player.name in observation.fold_to_three_bet_players
                ),
                flop_cbet_opportunity=(
                    player.name in observation.flop_cbet_opportunities
                ),
                flop_cbet=player.name in observation.flop_cbet_players,
                fold_to_flop_cbet_opportunity=(
                    player.name in observation.fold_to_flop_cbet_opportunities
                ),
                fold_to_flop_cbet=(
                    player.name in observation.fold_to_flop_cbet_players
                ),
            )
        )
    return tuple(rows)


def _observe_hand(
    hand: HandHistory,
    position_by_name: dict[str, PlayerPosition | None],
) -> _HandObservation:
    preflop = [
        action for action in hand.actions if action.street == BettingRound.PREFLOP
    ]
    vpip_players: set[str] = set()
    pfr_players: set[str] = set()
    rfi_opportunities: set[str] = set()
    rfi_players: set[str] = set()
    three_bet_opportunities: set[str] = set()
    three_bet_players: set[str] = set()
    call_open_opportunities: set[str] = set()
    call_open_players: set[str] = set()
    cold_call_opportunities: set[str] = set()
    cold_call_players: set[str] = set()
    fold_to_three_bet_opportunities: set[str] = set()
    fold_to_three_bet_players: set[str] = set()
    first_decision_seen: set[str] = set()
    entered_players: set[str] = set()
    raise_count = 0
    first_raiser: str | None = None
    three_bet_seen = False
    fold_to_three_bet_resolved = False
    last_preflop_aggressor: str | None = None

    for action in preflop:
        if action.action_type not in DECISION_TYPES:
            continue

        if (
            three_bet_seen
            and not fold_to_three_bet_resolved
            and action.player == first_raiser
            and action.action_type in FACING_BET_DECISION_TYPES
        ):
            fold_to_three_bet_opportunities.add(action.player)
            fold_to_three_bet_resolved = True
            if action.action_type == ActionType.FOLD:
                fold_to_three_bet_players.add(action.player)

        if action.player not in first_decision_seen:
            if not entered_players:
                rfi_opportunities.add(action.player)
                if action.action_type == ActionType.RAISE:
                    rfi_players.add(action.player)
            if raise_count == 1:
                three_bet_opportunities.add(action.player)
                call_open_opportunities.add(action.player)
                if action.action_type == ActionType.RAISE:
                    three_bet_players.add(action.player)
                if action.action_type == ActionType.CALL:
                    call_open_players.add(action.player)
                if position_by_name[action.player] not in BLIND_POSITIONS:
                    cold_call_opportunities.add(action.player)
                    if action.action_type == ActionType.CALL:
                        cold_call_players.add(action.player)
            first_decision_seen.add(action.player)

        if action.action_type in VOLUNTARY_ENTRY_TYPES:
            vpip_players.add(action.player)
            entered_players.add(action.player)
        if action.action_type == ActionType.RAISE:
            pfr_players.add(action.player)
            last_preflop_aggressor = action.player
            if raise_count == 0:
                first_raiser = action.player
            elif (
                raise_count == 1
                and first_raiser is not None
                and first_raiser != action.player
            ):
                three_bet_seen = True
            raise_count += 1

    (
        flop_cbet_opportunities,
        flop_cbet_players,
        fold_to_flop_cbet_opportunities,
        fold_to_flop_cbet_players,
    ) = _observe_flop_cbet(hand, preflop, last_preflop_aggressor)

    return _HandObservation(
        vpip_players=frozenset(vpip_players),
        pfr_players=frozenset(pfr_players),
        rfi_opportunities=frozenset(rfi_opportunities),
        rfi_players=frozenset(rfi_players),
        three_bet_opportunities=frozenset(three_bet_opportunities),
        three_bet_players=frozenset(three_bet_players),
        call_open_opportunities=frozenset(call_open_opportunities),
        call_open_players=frozenset(call_open_players),
        cold_call_opportunities=frozenset(cold_call_opportunities),
        cold_call_players=frozenset(cold_call_players),
        fold_to_three_bet_opportunities=frozenset(
            fold_to_three_bet_opportunities
        ),
        fold_to_three_bet_players=frozenset(fold_to_three_bet_players),
        flop_cbet_opportunities=flop_cbet_opportunities,
        flop_cbet_players=flop_cbet_players,
        fold_to_flop_cbet_opportunities=fold_to_flop_cbet_opportunities,
        fold_to_flop_cbet_players=fold_to_flop_cbet_players,
    )


def _observe_flop_cbet(
    hand: HandHistory,
    preflop_actions: list[Action],
    aggressor: str | None,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    if aggressor is None or len(hand.board) < 3:
        return frozenset(), frozenset(), frozenset(), frozenset()

    folded_preflop = {
        action.player
        for action in preflop_actions
        if action.action_type == ActionType.FOLD
    }
    active_defenders = {
        player.name
        for player in hand.players
        if player.name != aggressor and player.name not in folded_preflop
    }
    flop_actions = [
        action for action in hand.actions if action.street == BettingRound.FLOP
    ]
    prior_bet = False
    cbet_index: int | None = None

    for index, action in enumerate(flop_actions):
        if action.player == aggressor and action.action_type in DECISION_TYPES:
            if prior_bet:
                return frozenset(), frozenset(), frozenset(), frozenset()
            if action.action_type != ActionType.BET:
                return frozenset({aggressor}), frozenset(), frozenset(), frozenset()
            cbet_index = index
            break
        if action.action_type in {ActionType.BET, ActionType.RAISE}:
            prior_bet = True

    if cbet_index is None:
        return frozenset(), frozenset(), frozenset(), frozenset()

    fold_opportunities: set[str] = set()
    folded_players: set[str] = set()
    resolved: set[str] = set()
    for action in flop_actions[cbet_index + 1 :]:
        if action.player not in active_defenders or action.player in resolved:
            continue
        if action.action_type not in FACING_BET_DECISION_TYPES:
            continue
        fold_opportunities.add(action.player)
        resolved.add(action.player)
        if action.action_type == ActionType.FOLD:
            folded_players.add(action.player)

    return (
        frozenset({aggressor}),
        frozenset({aggressor}),
        frozenset(fold_opportunities),
        frozenset(folded_players),
    )
