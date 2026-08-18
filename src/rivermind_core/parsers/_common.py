"""Line grammar shared by the site parsers.

GGPoker copied PokerStars' line format almost exactly, so the seat, blind,
action, return, collect and showdown grammars are literally the same text.  One
definition means the two sites cannot drift apart; what genuinely differs — the
header, the summary line, and each site's own unsupported variants — stays in
the site module.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from rivermind_core.models import Action, ActionType, BettingRound

from .base import HandHistoryParseError


NUMBER = r"[\d,]+(?:\.\d+)?"
TABLE_RE = re.compile(
    r"^Table '(?P<table_name>.+)' (?P<max_seats>\d+)-max "
    r"Seat #(?P<button_seat>\d+) is the button$"
)
SEAT_RE = re.compile(
    rf"^Seat (?P<seat>\d+): (?P<name>.+) "
    rf"\([$€£]?(?P<stack>{NUMBER}) in chips\)$"
)
DEALT_RE = re.compile(r"^Dealt to (?P<name>.+) \[(?P<cards>[^]]+)]$")
POST_RE = re.compile(
    rf"^(?P<name>.+): posts (?P<kind>small blind|big blind|the ante) "
    rf"[$€£]?(?P<amount>{NUMBER})(?P<all_in> and is all-in)?$"
)
SIMPLE_RE = re.compile(r"^(?P<name>.+): (?P<kind>folds|checks)$")
CHIP_ACTION_RE = re.compile(
    rf"^(?P<name>.+): (?P<kind>calls|bets) "
    rf"[$€£]?(?P<amount>{NUMBER})(?P<all_in> and is all-in)?$"
)
RAISE_RE = re.compile(
    rf"^(?P<name>.+): raises [$€£]?(?P<amount>{NUMBER}) to "
    rf"[$€£]?(?P<to_amount>{NUMBER})(?P<all_in> and is all-in)?$"
)
RETURN_RE = re.compile(
    rf"^Uncalled bet \([$€£]?(?P<amount>{NUMBER})\) returned to (?P<name>.+)$"
)
COLLECT_RE = re.compile(
    rf"^(?P<name>.+) collected [$€£]?(?P<amount>{NUMBER}) "
    rf"from (?:main |side )?pot(?:-\d+)?$"
)
SHOW_RE = re.compile(r"^(?P<name>.+): shows \[(?P<cards>[^]]+)](?: .*)?$")
MUCK_RE = re.compile(r"^(?P<name>.+): (?:mucks hand|doesn't show hand)$")
CARD_RE = re.compile(r"\b[2-9TJQKA][cdhs]\b")



def split_lines(raw_text: str) -> list[str]:
    lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
    if not lines:
        raise HandHistoryParseError("Hand history is empty", code="empty_hand")
    return lines


def parse_action(
    line: str, street: BettingRound, sequence: int
) -> Action | None:
    post = POST_RE.fullmatch(line)
    if post:
        post_types = {
            "small blind": ActionType.POST_SMALL_BLIND,
            "big blind": ActionType.POST_BIG_BLIND,
            "the ante": ActionType.POST_ANTE,
        }
        return Action(
            sequence=sequence,
            street=street,
            player=post.group("name"),
            action_type=post_types[post.group("kind")],
            amount=decimal_amount(post.group("amount")),
            is_all_in=post.group("all_in") is not None,
            raw_text=line,
        )

    simple = SIMPLE_RE.fullmatch(line)
    if simple:
        return Action(
            sequence=sequence,
            street=street,
            player=simple.group("name"),
            action_type=(
                ActionType.FOLD if simple.group("kind") == "folds" else ActionType.CHECK
            ),
            raw_text=line,
        )

    chip_action = CHIP_ACTION_RE.fullmatch(line)
    if chip_action:
        return Action(
            sequence=sequence,
            street=street,
            player=chip_action.group("name"),
            action_type=(
                ActionType.CALL
                if chip_action.group("kind") == "calls"
                else ActionType.BET
            ),
            amount=decimal_amount(chip_action.group("amount")),
            is_all_in=chip_action.group("all_in") is not None,
            raw_text=line,
        )

    raised = RAISE_RE.fullmatch(line)
    if raised:
        return Action(
            sequence=sequence,
            street=street,
            player=raised.group("name"),
            action_type=ActionType.RAISE,
            amount=decimal_amount(raised.group("amount")),
            to_amount=decimal_amount(raised.group("to_amount")),
            is_all_in=raised.group("all_in") is not None,
            raw_text=line,
        )

    returned = RETURN_RE.fullmatch(line)
    if returned:
        return Action(
            sequence=sequence,
            street=street,
            player=returned.group("name"),
            action_type=ActionType.RETURN,
            amount=decimal_amount(returned.group("amount")),
            raw_text=line,
        )

    collected = COLLECT_RE.fullmatch(line)
    if collected:
        return Action(
            sequence=sequence,
            street=street,
            player=collected.group("name"),
            action_type=ActionType.COLLECT,
            amount=decimal_amount(collected.group("amount")),
            raw_text=line,
        )

    shown = SHOW_RE.fullmatch(line)
    if shown:
        return Action(
            sequence=sequence,
            street=BettingRound.SHOWDOWN,
            player=shown.group("name"),
            action_type=ActionType.SHOW,
            raw_text=line,
        )

    mucked = MUCK_RE.fullmatch(line)
    if mucked:
        return Action(
            sequence=sequence,
            street=BettingRound.SHOWDOWN,
            player=mucked.group("name"),
            action_type=ActionType.MUCK,
            raw_text=line,
        )

    return None


def decimal_amount(value: str | None) -> Decimal:
    if value is None:
        raise HandHistoryParseError("Missing numeric value", code="missing_number")
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise HandHistoryParseError(f"Invalid numeric value: {value}") from exc
