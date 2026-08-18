from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal

from rivermind_core.models import (
    Action,
    BettingRound,
    GameType,
    HandHistory,
    Player,
    enrich_player_context,
)

from ._common import (
    CARD_RE,
    NUMBER,
    SEAT_RE,
    SHOW_RE,
    TABLE_RE,
    DEALT_RE,
    decimal_amount,
    parse_action,
    split_lines,
)
from .base import (
    HandHistoryParseError,
    HandHistoryParser,
    UnsupportedHandHistoryError,
)


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
SUMMARY_RE = re.compile(
    rf"^Total pot [$€£]?(?P<total_pot>{NUMBER})(?: .*)? \| "
    rf"Rake [$€£]?(?P<rake>{NUMBER})$"
)


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
    header_prefix = "PokerStars Hand #"

    def can_parse(self, raw_text: str) -> bool:
        first_line = raw_text.lstrip().splitlines()[0]
        return first_line.startswith("PokerStars Hand #") and "Tournament #" not in first_line

    def parse(self, raw_text: str) -> HandHistory:
        lines = split_lines(raw_text)
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
            small_blind=decimal_amount(match.group("small_blind")),
            big_blind=decimal_amount(match.group("big_blind")),
            played_at=match.group("played_at"),
        )
        return _parse_body(lines, raw_text, header)


class PokerStarsTournamentParser(HandHistoryParser):
    """PokerStars English paid-entry and freeroll tournament parser."""

    name = "pokerstars_tournament_v2"
    header_prefix = "PokerStars Hand #"

    def can_parse(self, raw_text: str) -> bool:
        first_line = raw_text.lstrip().splitlines()[0]
        return first_line.startswith("PokerStars Hand #") and "Tournament #" in first_line

    def parse(self, raw_text: str) -> HandHistory:
        lines = split_lines(raw_text)
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
            small_blind=decimal_amount(match.group("small_blind")),
            big_blind=decimal_amount(match.group("big_blind")),
            played_at=match.group("played_at"),
            tournament_id=match.group("tournament_id"),
            tournament_level=match.group("level"),
            buy_in=Decimal("0") if is_freeroll else decimal_amount(match.group("buy_in")),
            fee=Decimal("0") if is_freeroll else decimal_amount(match.group("fee")),
        )
        return _parse_body(lines, raw_text, header)


def split_lines(raw_text: str) -> list[str]:
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
            starting_stack=decimal_amount(match.group("stack")),
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
            total_pot = decimal_amount(summary.group("total_pot"))
            rake = decimal_amount(summary.group("rake"))
            continue
        if line.startswith("Total pot "):
            raise HandHistoryParseError(
                f"Unsupported PokerStars summary line: {line}",
                code="pokerstars_unsupported_summary",
            )
        if in_summary:
            continue

        parsed_action = parse_action(line, street, len(actions))
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
