"""GGPoker cash-game parser.

GGPoker's export copies PokerStars' line format closely enough that the seat,
blind, action, return and showdown grammars are shared verbatim (see
``_common``).  What differs is where the risk lives:

======================  ==========================================
PokerStars              GGPoker
======================  ==========================================
``PokerStars Hand #12`` ``Poker Hand #RC837124540``
``($0.05/$0.10 USD)``   ``($0.1/$0.25)`` — no currency code
``*** SHOW DOWN ***``   ``*** SHOWDOWN ***`` — no space
``| Rake $0.75``        ``| Rake $0.75 | Jackpot $0.37 | Bingo $0``
real screen names       per-table anonymous ids; hero is ``Hero``
======================  ==========================================

GGPoker also ships features that have no equivalent in the canonical model.
Each of them is **detected and refused**, never approximated:

* **Run it two or three times.**  ``*** FIRST FLOP ***`` / ``*** SECOND RIVER ***``
  and ``Hand was run two times``.  The model carries one board and one
  settlement; silently keeping the first board would report a result the player
  never got.
* **EV Cashout.**  ``Hero: Chooses to EV Cashout`` moves money outside the pot,
  so the chip ledger would not balance.
* **Cash Drop.**  ``Cash Drop to Pot : total $5`` adds money nobody posted.
* **Non-Hold'em.**  Omaha exports use the same shell with four hole cards.

.. warning::

   This parser was written against GGPoker's published line format and the hand
   histories quoted in public tracker-support threads, not against a verified
   export from a live account.  The committed fixtures are reconstructions.
   Before trusting it on real data, run a genuine PokerCraft export through
   ``rivermind import`` and check that nothing lands in the ``failed`` or
   ``unsupported`` buckets for the wrong reason.
"""

from __future__ import annotations

import re
from dataclasses import replace
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
    DEALT_RE,
    NUMBER,
    SEAT_RE,
    SHOW_RE,
    TABLE_RE,
    decimal_amount,
    parse_action,
    split_lines,
)
from .base import (
    HandHistoryParseError,
    HandHistoryParser,
    UnsupportedHandHistoryError,
)


HEADER_PREFIX = "Poker Hand #"

#: ``RC`` is Rush & Cash, ``HD`` a regular hold'em table, ``OM`` Omaha, ``TM`` a
#: tournament.  The prefix is part of the id, so it is kept verbatim.
CASH_HEADER_RE = re.compile(
    rf"^Poker Hand #(?P<hand_id>[A-Z]{{0,4}}\d+):\s+"
    rf"(?P<game_name>.*?)\s+\("
    rf"[$€£¥]?(?P<small_blind>{NUMBER})/"
    rf"[$€£¥]?(?P<big_blind>{NUMBER})\s*"
    rf"(?P<currency>[A-Z]{{3}})?\)\s+-\s+"
    rf"(?P<played_at>.+)$"
)

#: GGPoker appends its own columns after the rake, and which ones appear depends
#: on the game.  Anything after ``Rake`` is accepted and ignored.
SUMMARY_RE = re.compile(
    rf"^Total pot [$€£¥]?(?P<total_pot>{NUMBER})(?: .*?)? \| "
    rf"Rake [$€£¥]?(?P<rake>{NUMBER})(?: \| .*)?$"
)

#: Streets when the hand was run once.  Run-it-twice uses FIRST/SECOND/THIRD.
_STREETS = {
    "*** FLOP ***": BettingRound.FLOP,
    "*** TURN ***": BettingRound.TURN,
    "*** RIVER ***": BettingRound.RIVER,
}
_SHOWDOWN_MARKER = "*** SHOWDOWN ***"

_MULTI_RUN_MARKER = re.compile(
    r"^\*\*\* (FIRST|SECOND|THIRD) (FLOP|TURN|RIVER) \*\*\*"
)
_MULTI_RUN_SUMMARY = re.compile(r"^Hand was run (two|three) times$")

#: Lines that describe money moving outside the normal betting ledger.
_UNSUPPORTED_LINES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        _MULTI_RUN_MARKER,
        "ggpoker_run_it_multiple_times",
        "the hand was run more than once, so it has more than one board",
    ),
    (
        _MULTI_RUN_SUMMARY,
        "ggpoker_run_it_multiple_times",
        "the hand was run more than once, so it has more than one board",
    ),
    (
        re.compile(r"^.+: (Chooses to EV Cashout|Pays Cashout Risk|Receives Cashout)"),
        "ggpoker_ev_cashout",
        "EV Cashout settles money outside the pot",
    ),
    (
        re.compile(r"^Cash Drop to Pot\s*:"),
        "ggpoker_cash_drop",
        "a Cash Drop adds money that nobody posted",
    ),
    (
        re.compile(r"^Bounty prize|^.+ wins the bounty"),
        "ggpoker_bounty",
        "bounty prizes are settled outside the pot",
    ),
)

#: Hold'em is the only variant the canonical model covers today.
_SUPPORTED_GAME_NAMES = ("Hold'em No Limit", "Hold'em Pot Limit", "Hold'em Limit")


class GGPokerCashParser(HandHistoryParser):
    """GGPoker English cash-game parser.

    Tournament hands are recognized and refused rather than guessed at: the
    tournament header carries buy-in, level and bounty fields that have not been
    verified against a real export.
    """

    name = "ggpoker_cash_v1"
    header_prefix = HEADER_PREFIX

    def can_parse(self, raw_text: str) -> bool:
        first_line = raw_text.lstrip().splitlines()[0]
        return first_line.startswith(HEADER_PREFIX)

    def parse(self, raw_text: str) -> HandHistory:
        lines = split_lines(raw_text)
        header_line = lines[0]
        if "Tournament #" in header_line:
            raise UnsupportedHandHistoryError(
                "GGPoker tournament hands are not supported yet; the tournament "
                "header has not been verified against a real export",
                code="ggpoker_tournament_unsupported",
            )
        match = CASH_HEADER_RE.fullmatch(header_line)
        if match is None:
            raise HandHistoryParseError(
                "Malformed or unsupported GGPoker cash-game header",
                code="ggpoker_malformed_header",
            )
        game_name = match.group("game_name")
        if not game_name.startswith("Hold'em"):
            raise UnsupportedHandHistoryError(
                f"GGPoker variant is not supported: {game_name}",
                code="ggpoker_unsupported_variant",
            )
        if game_name not in _SUPPORTED_GAME_NAMES:
            raise UnsupportedHandHistoryError(
                f"GGPoker Hold'em variant is not supported: {game_name}",
                code="ggpoker_unsupported_variant",
            )

        _reject_unsupported_features(lines)
        return _build_hand(lines, raw_text, match)


def _reject_unsupported_features(lines: list[str]) -> None:
    """Refuse hands whose money does not fit the canonical ledger."""

    for line in lines:
        for pattern, code, reason in _UNSUPPORTED_LINES:
            if pattern.match(line):
                raise UnsupportedHandHistoryError(
                    f"GGPoker hand is not supported: {reason} ({line})",
                    code=code,
                )


def _build_hand(lines: list[str], raw_text: str, header) -> HandHistory:
    table = next(
        (match for line in lines if (match := TABLE_RE.fullmatch(line)) is not None),
        None,
    )
    if table is None:
        raise HandHistoryParseError(
            "Missing or unsupported GGPoker table line",
            code="ggpoker_missing_table",
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
            code="ggpoker_missing_players",
        )

    dealt = next(
        (match for line in lines if (match := DEALT_RE.fullmatch(line)) is not None),
        None,
    )
    if dealt is not None:
        hero_name = dealt.group("name")
        hero_cards = tuple(dealt.group("cards").split())
        if len(hero_cards) != 2:
            raise UnsupportedHandHistoryError(
                f"GGPoker hero was dealt {len(hero_cards)} cards; only Hold'em is "
                "supported",
                code="ggpoker_unsupported_variant",
            )
        players = [
            replace(
                player,
                is_hero=player.name == hero_name,
                hole_cards=hero_cards if player.name == hero_name else (),
            )
            for player in players
        ]

    shown = {
        match.group("name"): tuple(match.group("cards").split())
        for line in lines
        if (match := SHOW_RE.fullmatch(line)) is not None
    }
    if shown:
        players = [
            replace(player, hole_cards=shown.get(player.name, player.hole_cards))
            for player in players
        ]

    street = BettingRound.PREFLOP
    board: tuple[str, ...] = ()
    actions: list[Action] = []
    total_pot: Decimal | None = None
    rake: Decimal | None = None
    in_summary = False

    for line in lines:
        marker = next(
            (key for key in _STREETS if line.startswith(key)),
            None,
        )
        if marker is not None:
            street = _STREETS[marker]
            board = tuple(CARD_RE.findall(line))
            continue
        if line.startswith(_SHOWDOWN_MARKER):
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
                f"Unsupported GGPoker summary line: {line}",
                code="ggpoker_unsupported_summary",
            )
        if in_summary:
            continue

        parsed = parse_action(line, street, len(actions))
        if parsed is not None:
            actions.append(parsed)
            continue
        if any(line.startswith(f"{player.name}: ") for player in players):
            raise HandHistoryParseError(
                f"Unsupported GGPoker action line: {line}",
                code="ggpoker_unsupported_action",
            )

    players = list(
        enrich_player_context(
            players,
            button_seat=int(table.group("button_seat")),
            big_blind=decimal_amount(header.group("big_blind")),
        )
    )

    return HandHistory(
        site="ggpoker",
        hand_id=header.group("hand_id"),
        game_type=GameType.CASH,
        game_name=header.group("game_name"),
        currency=header.group("currency"),
        table_name=table.group("table_name"),
        max_seats=int(table.group("max_seats")),
        button_seat=int(table.group("button_seat")),
        small_blind=decimal_amount(header.group("small_blind")),
        big_blind=decimal_amount(header.group("big_blind")),
        played_at_raw=header.group("played_at"),
        players=tuple(players),
        actions=tuple(actions),
        board=board,
        total_pot=total_pot,
        rake=rake,
        raw_text=raw_text,
    )
