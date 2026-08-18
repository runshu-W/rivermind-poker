from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from rivermind_core.models import (
    Action,
    ActionType,
    BettingRound,
    GameType,
    HandHistory,
    Player,
)

from .base import (
    HandHistoryParseError,
    HandHistoryParser,
    UnsupportedHandHistoryError,
)


MONEY = r"[$€£]?([\d,]+(?:\.\d+)?)"
HEADER_RE = re.compile(
    rf"^PokerStars Hand #(?P<hand_id>\d+):\s+"
    rf"(?P<game_name>.*?)\s+\({MONEY}/{MONEY}\s*(?P<currency>[A-Z]{{3}})?\)\s+-\s+"
    rf"(?P<played_at>.+)$"
)
TABLE_RE = re.compile(
    r"^Table '(?P<table_name>.+)' (?P<max_seats>\d+)-max "
    r"Seat #(?P<button_seat>\d+) is the button$"
)
SEAT_RE = re.compile(
    rf"^Seat (?P<seat>\d+): (?P<name>.+) \({MONEY} in chips\)$"
)
DEALT_RE = re.compile(r"^Dealt to (?P<name>.+) \[(?P<cards>[^]]+)]$")
POST_RE = re.compile(
    rf"^(?P<name>.+): posts (?P<kind>small blind|big blind|the ante) {MONEY}"
    r"(?P<all_in> and is all-in)?$"
)
SIMPLE_RE = re.compile(r"^(?P<name>.+): (?P<kind>folds|checks)$")
CHIP_ACTION_RE = re.compile(
    rf"^(?P<name>.+): (?P<kind>calls|bets) {MONEY}"
    r"(?P<all_in> and is all-in)?$"
)
RAISE_RE = re.compile(
    rf"^(?P<name>.+): raises {MONEY} to {MONEY}"
    r"(?P<all_in> and is all-in)?$"
)
RETURN_RE = re.compile(rf"^Uncalled bet \({MONEY}\) returned to (?P<name>.+)$")
COLLECT_RE = re.compile(
    rf"^(?P<name>.+) collected {MONEY} from (?:main |side )?pot(?:-\d+)?$"
)
SUMMARY_RE = re.compile(rf"^Total pot {MONEY}(?: .*)? \| Rake {MONEY}$")
SHOW_RE = re.compile(r"^(?P<name>.+): shows \[(?P<cards>[^]]+)](?: .*)?$")
MUCK_RE = re.compile(r"^(?P<name>.+): (?:mucks hand|doesn't show hand)$")
CARD_RE = re.compile(r"\b[2-9TJQKA][cdhs]\b")


class PokerStarsCashParser(HandHistoryParser):
    """Initial PokerStars English cash-game parser.

    The parser intentionally fails on unsupported headers instead of producing
    a guessed normalization. Tournament and localized formats will be separate
    adapters sharing the same canonical model.
    """

    name = "pokerstars_cash_v1"

    def can_parse(self, raw_text: str) -> bool:
        first_line = raw_text.lstrip().splitlines()[0]
        return first_line.startswith("PokerStars Hand #")

    def parse(self, raw_text: str) -> HandHistory:
        lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
        if not lines:
            raise HandHistoryParseError("Hand history is empty", code="empty_hand")

        if "Tournament #" in lines[0]:
            raise UnsupportedHandHistoryError(
                "PokerStars tournament hands are not supported yet",
                code="pokerstars_tournament_not_supported",
            )

        header = HEADER_RE.fullmatch(lines[0])
        if header is None:
            raise HandHistoryParseError(
                "Malformed or unsupported PokerStars cash-game header",
                code="pokerstars_malformed_header",
            )

        table_line = next((TABLE_RE.fullmatch(line) for line in lines if TABLE_RE.fullmatch(line)), None)
        if table_line is None:
            raise HandHistoryParseError(
                "Missing or unsupported PokerStars table line",
                code="pokerstars_missing_table",
            )

        players: list[Player] = []
        for line in lines:
            match = SEAT_RE.fullmatch(line)
            if match is None:
                continue
            players.append(
                Player(
                    seat=int(match.group("seat")),
                    name=match.group("name"),
                    starting_stack=_decimal(match.group(3)),
                )
            )

        if len(players) < 2:
            raise HandHistoryParseError(
                "A hand must contain at least two seats",
                code="pokerstars_missing_players",
            )

        hero_name: str | None = None
        hero_cards: tuple[str, ...] = ()
        for line in lines:
            dealt = DEALT_RE.fullmatch(line)
            if dealt:
                hero_name = dealt.group("name")
                hero_cards = tuple(dealt.group("cards").split())
                break

        if hero_name is not None:
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
                replace(
                    player,
                    hole_cards=shown_cards.get(player.name, player.hole_cards),
                )
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
                total_pot = _decimal(summary.group(1))
                rake = _decimal(summary.group(2))
                continue
            if line.startswith("Total pot "):
                raise HandHistoryParseError(
                    f"Unsupported PokerStars summary line: {line}",
                    code="pokerstars_unsupported_summary",
                )
            if in_summary:
                continue

            parsed_action = self._parse_action(line, street, len(actions))
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

        return HandHistory(
            site="pokerstars",
            hand_id=header.group("hand_id"),
            game_type=GameType.CASH,
            game_name=header.group("game_name"),
            currency=header.group("currency"),
            table_name=table_line.group("table_name"),
            max_seats=int(table_line.group("max_seats")),
            button_seat=int(table_line.group("button_seat")),
            small_blind=_decimal(header.group(3)),
            big_blind=_decimal(header.group(4)),
            played_at_raw=header.group("played_at"),
            players=tuple(players),
            actions=tuple(actions),
            board=board,
            total_pot=total_pot,
            rake=rake,
            raw_text=raw_text,
        )

    @staticmethod
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
                amount=_decimal(post.group(3)),
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
                amount=_decimal(chip_action.group(3)),
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
                amount=_decimal(raised.group(2)),
                to_amount=_decimal(raised.group(3)),
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
                amount=_decimal(returned.group(1)),
                raw_text=line,
            )

        collected = COLLECT_RE.fullmatch(line)
        if collected:
            return Action(
                sequence=sequence,
                street=street,
                player=collected.group("name"),
                action_type=ActionType.COLLECT,
                amount=_decimal(collected.group(2)),
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


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise HandHistoryParseError(f"Invalid numeric value: {value}") from exc
