from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rivermind_core.models import Action, ActionType, HandHistory


ZERO = Decimal("0")


class HandAccountingError(ValueError):
    """Raised when normalized actions cannot produce a valid chip ledger."""


@dataclass(frozen=True, slots=True)
class PlayerHandResult:
    player_name: str
    invested: Decimal
    returned: Decimal
    collected: Decimal
    net_result: Decimal
    net_result_bb: Decimal


@dataclass(frozen=True, slots=True)
class ActionLedgerEntry:
    action: Action
    invested: Decimal
    returned: Decimal
    collected: Decimal
    pot_after: Decimal


@dataclass(frozen=True, slots=True)
class HandLedger:
    results: tuple[PlayerHandResult, ...]
    accounted_total_pot: Decimal
    declared_total_pot: Decimal | None
    rake: Decimal
    pot_delta: Decimal | None
    balance_delta: Decimal

    @property
    def is_balanced(self) -> bool:
        return self.balance_delta == ZERO and self.pot_delta in (None, ZERO)

    def result_for(self, player_name: str) -> PlayerHandResult | None:
        return next(
            (result for result in self.results if result.player_name == player_name),
            None,
        )


@dataclass(slots=True)
class _MutableResult:
    invested: Decimal = ZERO
    returned: Decimal = ZERO
    collected: Decimal = ZERO


INVEST_AMOUNT_TYPES = {
    ActionType.POST_SMALL_BLIND,
    ActionType.POST_BIG_BLIND,
    ActionType.POST_ANTE,
    ActionType.CALL,
    ActionType.BET,
}
def calculate_hand_ledger(hand: HandHistory) -> HandLedger:
    mutable = {player.name: _MutableResult() for player in hand.players}
    for entry in calculate_action_ledger(hand):
        result = mutable[entry.action.player]
        result.invested += entry.invested
        result.returned += entry.returned
        result.collected += entry.collected

    results = tuple(
        PlayerHandResult(
            player_name=player.name,
            invested=mutable[player.name].invested,
            returned=mutable[player.name].returned,
            collected=mutable[player.name].collected,
            net_result=(
                mutable[player.name].returned
                + mutable[player.name].collected
                - mutable[player.name].invested
            ),
            net_result_bb=(
                mutable[player.name].returned
                + mutable[player.name].collected
                - mutable[player.name].invested
            )
            / hand.big_blind,
        )
        for player in hand.players
    )
    total_invested = sum((result.invested for result in results), ZERO)
    total_returned = sum((result.returned for result in results), ZERO)
    accounted_total_pot = total_invested - total_returned
    rake = hand.rake or ZERO
    balance_delta = sum((result.net_result for result in results), ZERO) + rake
    pot_delta = (
        None
        if hand.total_pot is None
        else accounted_total_pot - hand.total_pot
    )
    return HandLedger(
        results=results,
        accounted_total_pot=accounted_total_pot,
        declared_total_pot=hand.total_pot,
        rake=rake,
        pot_delta=pot_delta,
        balance_delta=balance_delta,
    )


def calculate_action_ledger(hand: HandHistory) -> tuple[ActionLedgerEntry, ...]:
    live_contributions: dict[tuple[str, str], Decimal] = {}
    pot = ZERO
    entries: list[ActionLedgerEntry] = []

    for action in hand.actions:
        street_key = (action.street.value, action.player)
        current_live = live_contributions.get(street_key, ZERO)
        invested = ZERO
        returned = ZERO
        collected = ZERO

        if action.action_type in INVEST_AMOUNT_TYPES:
            invested = _required_amount(action)
            if action.action_type != ActionType.POST_ANTE:
                live_contributions[street_key] = current_live + invested
        elif action.action_type == ActionType.RAISE:
            if action.to_amount is not None:
                invested = action.to_amount - current_live
            else:
                invested = _required_amount(action)
            if invested < ZERO:
                raise HandAccountingError(
                    f"Raise target is below committed amount: {action.raw_text}"
                )
            live_contributions[street_key] = current_live + invested
        elif action.action_type == ActionType.RETURN:
            returned = _required_amount(action)
            live_contributions[street_key] = max(ZERO, current_live - returned)
        elif action.action_type == ActionType.COLLECT:
            collected = _required_amount(action)

        pot += invested - returned - collected
        entries.append(
            ActionLedgerEntry(
                action=action,
                invested=invested,
                returned=returned,
                collected=collected,
                pot_after=pot,
            )
        )
    return tuple(entries)


def _required_amount(action: Action) -> Decimal:
    if action.amount is None:
        raise HandAccountingError(f"Action requires an amount: {action.raw_text}")
    return action.amount
