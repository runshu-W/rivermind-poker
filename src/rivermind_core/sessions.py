from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from rivermind_core.accounting import calculate_hand_ledger
from rivermind_core.models import GameType, HandHistory


TIMESTAMP_RE = re.compile(r"(?P<date>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})")
DEFAULT_CASH_GAP = timedelta(minutes=30)
ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class PlayerHandOutcome:
    site: str
    hand_id: str
    player_name: str
    game_type: GameType
    currency: str | None
    tournament_id: str | None
    table_name: str
    played_at: datetime | None
    net_result: Decimal
    net_result_bb: Decimal
    accounting_balanced: bool


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    player_name: str
    game_type: GameType
    currency: str | None
    tournament_id: str | None
    started_at: datetime | None
    ended_at: datetime | None
    hands: int
    net_result: Decimal
    net_result_bb: Decimal
    accounting_balanced: bool
    hand_keys: tuple[tuple[str, str], ...]

    @property
    def result_unit(self) -> str:
        if self.game_type == GameType.TOURNAMENT:
            return "chips"
        return self.currency or "chips"


def parse_played_at(raw_value: str | None) -> datetime | None:
    if raw_value is None:
        return None
    match = TIMESTAMP_RE.search(raw_value)
    if match is None:
        return None
    return datetime.strptime(match.group("date"), "%Y/%m/%d %H:%M:%S")


def build_player_hand_outcomes(
    hands: Iterable[HandHistory],
    *,
    player_name: str | None = None,
    heroes_only: bool = False,
) -> tuple[PlayerHandOutcome, ...]:
    if player_name is not None and heroes_only:
        raise ValueError("player_name and heroes_only cannot be combined")

    outcomes: list[PlayerHandOutcome] = []
    for hand in hands:
        ledger = calculate_hand_ledger(hand)
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
        for result in ledger.results:
            if result.player_name not in selected_names:
                continue
            outcomes.append(
                PlayerHandOutcome(
                    site=hand.site,
                    hand_id=hand.hand_id,
                    player_name=result.player_name,
                    game_type=hand.game_type,
                    currency=hand.currency,
                    tournament_id=hand.tournament_id,
                    table_name=hand.table_name,
                    played_at=parse_played_at(hand.played_at_raw),
                    net_result=result.net_result,
                    net_result_bb=result.net_result_bb,
                    accounting_balanced=ledger.is_balanced,
                )
            )
    return tuple(outcomes)


def summarize_sessions(
    outcomes: Iterable[PlayerHandOutcome],
    *,
    cash_gap: timedelta = DEFAULT_CASH_GAP,
) -> tuple[SessionSummary, ...]:
    if cash_gap < timedelta(0):
        raise ValueError("Cash session gap cannot be negative")

    grouped: dict[tuple[str, str, str], list[PlayerHandOutcome]] = {}
    cash_by_player: dict[str, list[PlayerHandOutcome]] = {}
    for outcome in outcomes:
        if outcome.game_type == GameType.TOURNAMENT:
            tournament_key = outcome.tournament_id or outcome.hand_id
            grouped.setdefault(
                (
                    outcome.player_name,
                    GameType.TOURNAMENT.value,
                    f"{outcome.site}:{tournament_key}",
                ),
                [],
            ).append(outcome)
        else:
            cash_by_player.setdefault(outcome.player_name, []).append(outcome)

    for player_name, cash_outcomes in cash_by_player.items():
        ordered = sorted(cash_outcomes, key=_outcome_sort_key)
        current: list[PlayerHandOutcome] = []
        segment = 0
        for outcome in ordered:
            if current and _starts_new_cash_session(current[-1], outcome, cash_gap):
                grouped[(player_name, GameType.CASH.value, str(segment))] = current
                current = []
                segment += 1
            current.append(outcome)
        if current:
            grouped[(player_name, GameType.CASH.value, str(segment))] = current

    sessions = [_summarize_group(items) for items in grouped.values()]
    return tuple(sorted(sessions, key=_session_sort_key, reverse=True))


def _starts_new_cash_session(
    previous: PlayerHandOutcome,
    current: PlayerHandOutcome,
    cash_gap: timedelta,
) -> bool:
    if previous.site != current.site or previous.currency != current.currency:
        return True
    if previous.played_at is None or current.played_at is None:
        return True
    return current.played_at - previous.played_at > cash_gap


def _summarize_group(items: list[PlayerHandOutcome]) -> SessionSummary:
    ordered = sorted(items, key=_outcome_sort_key)
    timestamps = [item.played_at for item in ordered if item.played_at is not None]
    first = ordered[0]
    identity = (
        first.tournament_id
        if first.game_type == GameType.TOURNAMENT and first.tournament_id
        else first.hand_id
    )
    session_key = f"{first.player_name}\0{first.game_type.value}\0{first.site}\0{identity}"
    return SessionSummary(
        session_id=hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:16],
        player_name=first.player_name,
        game_type=first.game_type,
        currency=first.currency,
        tournament_id=first.tournament_id,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        hands=len(ordered),
        net_result=sum((item.net_result for item in ordered), ZERO),
        net_result_bb=sum((item.net_result_bb for item in ordered), ZERO),
        accounting_balanced=all(item.accounting_balanced for item in ordered),
        hand_keys=tuple((item.site, item.hand_id) for item in ordered),
    )


def _outcome_sort_key(outcome: PlayerHandOutcome) -> tuple[datetime, str]:
    return outcome.played_at or datetime.min, outcome.hand_id


def _session_sort_key(session: SessionSummary) -> tuple[datetime, str]:
    return session.started_at or datetime.min, session.session_id
