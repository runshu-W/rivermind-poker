from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from rivermind_core.models import ActionType, BettingRound, HandHistory


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


@dataclass(slots=True)
class _MutableStat:
    occurrences: int = 0
    opportunities: int = 0

    def observe(self, occurred: bool) -> None:
        self.opportunities += 1
        self.occurrences += int(occurred)

    def freeze(self) -> StatValue:
        return StatValue(self.occurrences, self.opportunities)


@dataclass(slots=True)
class _Accumulator:
    hands: int = 0
    vpip: _MutableStat = field(default_factory=_MutableStat)
    pfr: _MutableStat = field(default_factory=_MutableStat)
    rfi: _MutableStat = field(default_factory=_MutableStat)
    three_bet: _MutableStat = field(default_factory=_MutableStat)


@dataclass(frozen=True, slots=True)
class _PreflopObservation:
    vpip_players: frozenset[str]
    pfr_players: frozenset[str]
    rfi_opportunities: frozenset[str]
    rfi_players: frozenset[str]
    three_bet_opportunities: frozenset[str]
    three_bet_players: frozenset[str]


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


def calculate_player_stats(
    hands: Iterable[HandHistory],
    *,
    player_name: str | None = None,
    heroes_only: bool = False,
) -> tuple[PlayerStats, ...]:
    """Calculate deterministic preflop stats from normalized actions.

    RFI and 3Bet use opportunity denominators. An RFI opportunity exists when
    a player's first preflop decision arrives before any voluntary entry. A
    3Bet opportunity exists when that first decision faces exactly one raise.
    """

    if player_name is not None and heroes_only:
        raise ValueError("player_name and heroes_only cannot be combined")

    accumulators: dict[str, _Accumulator] = {}
    for hand in hands:
        if player_name is not None:
            selected_names = {
                player.name for player in hand.players if player.name == player_name
            }
        elif heroes_only:
            selected_names = {
                player.name for player in hand.players if player.is_hero
            }
        else:
            selected_names = {player.name for player in hand.players}
        if not selected_names:
            continue

        observation = _observe_preflop(hand)
        for name in selected_names:
            accumulator = accumulators.setdefault(name, _Accumulator())
            accumulator.hands += 1
            accumulator.vpip.observe(name in observation.vpip_players)
            accumulator.pfr.observe(name in observation.pfr_players)
            if name in observation.rfi_opportunities:
                accumulator.rfi.observe(name in observation.rfi_players)
            if name in observation.three_bet_opportunities:
                accumulator.three_bet.observe(name in observation.three_bet_players)

    return tuple(
        PlayerStats(
            player_name=name,
            hands=accumulator.hands,
            vpip=accumulator.vpip.freeze(),
            pfr=accumulator.pfr.freeze(),
            rfi=accumulator.rfi.freeze(),
            three_bet=accumulator.three_bet.freeze(),
        )
        for name, accumulator in sorted(accumulators.items())
    )


def _observe_preflop(hand: HandHistory) -> _PreflopObservation:
    vpip_players: set[str] = set()
    pfr_players: set[str] = set()
    rfi_opportunities: set[str] = set()
    rfi_players: set[str] = set()
    three_bet_opportunities: set[str] = set()
    three_bet_players: set[str] = set()
    first_decision_seen: set[str] = set()
    entered_players: set[str] = set()
    raise_count = 0

    for action in hand.actions:
        if action.street != BettingRound.PREFLOP:
            continue
        if action.action_type not in DECISION_TYPES:
            continue

        is_first_decision = action.player not in first_decision_seen
        if is_first_decision:
            if not entered_players:
                rfi_opportunities.add(action.player)
                if action.action_type == ActionType.RAISE:
                    rfi_players.add(action.player)
            if raise_count == 1:
                three_bet_opportunities.add(action.player)
                if action.action_type == ActionType.RAISE:
                    three_bet_players.add(action.player)
            first_decision_seen.add(action.player)

        if action.action_type in VOLUNTARY_ENTRY_TYPES:
            vpip_players.add(action.player)
            entered_players.add(action.player)
        if action.action_type == ActionType.RAISE:
            pfr_players.add(action.player)
            raise_count += 1

    return _PreflopObservation(
        vpip_players=frozenset(vpip_players),
        pfr_players=frozenset(pfr_players),
        rfi_opportunities=frozenset(rfi_opportunities),
        rfi_players=frozenset(rfi_players),
        three_bet_opportunities=frozenset(three_bet_opportunities),
        three_bet_players=frozenset(three_bet_players),
    )
