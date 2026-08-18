from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from rivermind_core.models import (
    Action,
    ActionType,
    BettingRound,
    GameType,
    HandHistory,
    Player,
    enrich_player_context,
)

from .base import (
    HandHistoryParseError,
    HandHistoryParser,
    UnsupportedHandHistoryError,
)


NUMBER = r"[\d,]+(?:\.\d+)?"
CASH_HEADER_RE = re.compile(
    rf"^PokerStars Hand #(?P<hand_id>\d+):\s+"
    rf"(?P<game_name>.*?)\s+\("
    rf"[$€£]?(?P<small_blind>{NUMBER})/"
    rf"[$€£]?(?P<big_blind>{NUMBER})\s*"
    rf"(?P<currency>[A-Z]{{3}})?\)\s+-\s+"
    rf"(?P<played_at>.+)$"
)
TOURNAMENT_HEADER_RE = re.compile(
    rf"^PokerStars Hand #(?P<hand_id>\d+):\s+"
    rf"Tournament #(?P<tournament_id>\d+),\s+"
    rf"(?:[$€£]?(?P<buy_in>{NUMBER})\+[$€£]?(?P<fee>{NUMBER})\s+"
    rf"(?P<currency>[A-Z]{{3}})|(?P<freeroll>Freeroll))\s+"
    rf"(?P<game_name>.*?)\s+-\s+Level (?P<level>.*?)\s+"
    rf"\((?P<small_blind>{NUMBER})/(?P<big_blind>{NUMBER})\)\s+-\s+"
    rf"(?P<played_at>.+)$"
)
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
SUMMARY_RE = re.compile(
    rf"^Total pot [$€£]?(?P<total_pot>{NUMBER})(?: .*)? \| "
    rf"Rake [$€£]?(?P<rake>{NUMBER})$"
)
SHOW_RE = re.compile(r"^(?P<name>.+): shows \[(?P<cards>[^]]+)](?: .*)?$")
MUCK_RE = re.compile(r"^(?P<name>.+): (?:mucks hand|doesn't show hand)$")
CARD_RE = re.compile(r"\b[2-9TJQKA][cdhs]\b")


@dataclass(frozen=True, slots=True)
class _Header:
    hand_id: str
    game_type: GameType
    game_name: str
    currency: str | None
    small_blind: Decimal
    big_blind: Decimal
    played_at: str
    tournament_id: str | None = None
    tournament_level: str | None = None
    buy_in: Decimal | None = None
    fee: Decimal | None = None


class PokerStarsCashParser(HandHistoryParser):
    """PokerStars English cash-game parser with strict action handling."""

    name = "pokerstars_cash_v4"

    def can_parse(self, raw_text: str) -> bool:
        first_line = raw_text.lstrip().splitlines()[0]
        return first_line.startswith("PokerStars Hand #") and "Tournament #" not in first_line

    def parse(self, raw_text: str) -> HandHistory:
        lines = _lines(raw_text)
        if "Tournament #" in lines[0]:
            raise UnsupportedHandHistoryError(
                "Use the PokerStars tournament parser for tournament hands",
                code="pokerstars_wrong_parser",
            )
        match = CASH_HEADER_RE.fullmatch(lines[0])
        if match is None:
            raise HandHistoryParseError(
                "Malformed or unsupported PokerStars cash-game header",
                code="pokerstars_malformed_header",
            )
        header = _Header(
            hand_id=match.group("hand_id"),
            game_type=GameType.CASH,
            game_name=match.group("game_name"),
            currency=match.group("currency"),
            small_blind=_decimal(match.group("small_blind")),
            big_blind=_decimal(match.group("big_blind")),
            played_at=match.group("played_at"),
        )
        return _parse_body(lines, raw_text, header)


class PokerStarsTournamentParser(HandHistoryParser):
    """PokerStars English paid-entry and freeroll tournament parser."""

    name = "pokerstars_tournament_v2"

    def can_parse(self, raw_text: str) -> bool:
        first_line = raw_text.lstrip().splitlines()[0]
        return first_line.startswith("PokerStars Hand #") and "Tournament #" in first_line

    def parse(self, raw_text: str) -> HandHistory:
        lines = _lines(raw_text)
        match = TOURNAMENT_HEADER_RE.fullmatch(lines[0])
        if match is None:
            raise HandHistoryParseError(
                "Malformed or unsupported PokerStars tournament header",
                code="pokerstars_malformed_tournament_header",
            )
        is_freeroll = match.group("freeroll") is not None
        header = _Header(
            hand_id=match.group("hand_id"),
            game_type=GameType.TOURNAMENT,
            game_name=match.group("game_name"),
            currency=None if is_freeroll else match.group("currency"),
            small_blind=_decimal(match.group("small_blind")),
            big_blind=_decimal(match.group("big_blind")),
            played_at=match.group("played_at"),
            tournament_id=match.group("tournament_id"),
            tournament_level=match.group("level"),
            buy_in=Decimal("0") if is_freeroll else _decimal(match.group("buy_in")),
            fee=Decimal("0") if is_freeroll else _decimal(match.group("fee")),
        )
        return _parse_body(lines, raw_text, header)


def _lines(raw_text: str) -> list[str]:
    lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
    if not lines:
        raise HandHistoryParseError("Hand history is empty", code="empty_hand")
    return lines


def _parse_body(lines: list[str], raw_text: str, header: _Header) -> HandHistory:
    table_line = next(
        (match for line in lines if (match := TABLE_RE.fullmatch(line)) is not None),
        None,
    )
    if table_line is None:
        raise HandHistoryParseError(
            "Missing or unsupported PokerStars table line",
            code="pokerstars_missing_table",
        )

    players = [
        Player(
            seat=int(match.group("seat")),
            name=match.group("name"),
            starting_stack=_decimal(match.group("stack")),
        )
        for line in lines
        if (match := SEAT_RE.fullmatch(line)) is not None
    ]
    if len(players) < 2:
        raise HandHistoryParseError(
            "A hand must contain at least two seats",
            code="pokerstars_missing_players",
        )

    dealt = next(
        (match for line in lines if (match := DEALT_RE.fullmatch(line)) is not None),
        None,
    )
    if dealt is not None:
        hero_name = dealt.group("name")
        hero_cards = tuple(dealt.group("cards").split())
        players = [
            replace(
                player,
                is_hero=player.name == hero_name,
                hole_cards=hero_cards if player.name == hero_name else (),
            )
            for player in players
        ]

    shown_cards = {
        shown.group("name"): tuple(shown.group("cards").split())
        for line in lines
        if (shown := SHOW_RE.fullmatch(line)) is not None
    }
    if shown_cards:
        players = [
            replace(player, hole_cards=shown_cards.get(player.name, player.hole_cards))
            for player in players
        ]

    street = BettingRound.PREFLOP
    board: tuple[str, ...] = ()
    actions: list[Action] = []
    total_pot: Decimal | None = None
    rake: Decimal | None = None
    in_summary = False

    for line in lines:
        if line.startswith("*** FLOP ***"):
            street = BettingRound.FLOP
            board = tuple(CARD_RE.findall(line))
            continue
        if line.startswith("*** TURN ***"):
            street = BettingRound.TURN
            board = tuple(CARD_RE.findall(line))
            continue
        if line.startswith("*** RIVER ***"):
            street = BettingRound.RIVER
            board = tuple(CARD_RE.findall(line))
            continue
        if line.startswith("*** SHOW DOWN ***"):
            street = BettingRound.SHOWDOWN
            continue
        if line.startswith("*** SUMMARY ***"):
            in_summary = True
            continue

        summary = SUMMARY_RE.fullmatch(line)
        if summary:
            total_pot = _decimal(summary.group("total_pot"))
            rake = _decimal(summary.group("rake"))
            continue
        if line.startswith("Total pot "):
            raise HandHistoryParseError(
                f"Unsupported PokerStars summary line: {line}",
                code="pokerstars_unsupported_summary",
            )
        if in_summary:
            continue

        parsed_action = _parse_action(line, street, len(actions))
        if parsed_action is not None:
            actions.append(parsed_action)
            continue
        if any(line.startswith(f"{player.name}: ") for player in players):
            if ": said, " in line:
                continue
            raise HandHistoryParseError(
                f"Unsupported PokerStars action line: {line}",
                code="pokerstars_unsupported_action",
            )

    players = list(
        enrich_player_context(
            players,
            button_seat=int(table_line.group("button_seat")),
            big_blind=header.big_blind,
        )
    )

    return HandHistory(
        site="pokerstars",
        hand_id=header.hand_id,
        game_type=header.game_type,
        game_name=header.game_name,
        currency=header.currency,
        table_name=table_line.group("table_name"),
        max_seats=int(table_line.group("max_seats")),
        button_seat=int(table_line.group("button_seat")),
        small_blind=header.small_blind,
        big_blind=header.big_blind,
        played_at_raw=header.played_at,
        players=tuple(players),
        actions=tuple(actions),
        board=board,
        total_pot=total_pot,
        rake=rake,
        tournament_id=header.tournament_id,
        tournament_level=header.tournament_level,
        buy_in=header.buy_in,
        fee=header.fee,
        raw_text=raw_text,
    )


def _parse_action(
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
            amount=_decimal(post.group("amount")),
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
            amount=_decimal(chip_action.group("amount")),
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
            amount=_decimal(raised.group("amount")),
            to_amount=_decimal(raised.group("to_amount")),
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
            amount=_decimal(returned.group("amount")),
            raw_text=line,
        )

    collected = COLLECT_RE.fullmatch(line)
    if collected:
        return Action(
            sequence=sequence,
            street=street,
            player=collected.group("name"),
            action_type=ActionType.COLLECT,
            amount=_decimal(collected.group("amount")),
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


def _decimal(value: str | None) -> Decimal:
    if value is None:
        raise HandHistoryParseError("Missing numeric value", code="missing_number")
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise HandHistoryParseError(f"Invalid numeric value: {value}") from exc
