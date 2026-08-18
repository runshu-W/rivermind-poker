from ._common import parse_action, split_lines
from .base import (
    HandHistoryParseError,
    HandHistoryParser,
    ParserRegistry,
    UnsupportedHandHistoryError,
)
from .ggpoker import GGPokerCashParser
from .pokerstars import PokerStarsCashParser, PokerStarsTournamentParser


def default_registry() -> ParserRegistry:
    """Order matters only for speed: every parser recognizes its own header."""

    return ParserRegistry(
        [
            PokerStarsTournamentParser(),
            PokerStarsCashParser(),
            GGPokerCashParser(),
        ]
    )


__all__ = [
    "GGPokerCashParser",
    "HandHistoryParseError",
    "HandHistoryParser",
    "ParserRegistry",
    "PokerStarsCashParser",
    "PokerStarsTournamentParser",
    "UnsupportedHandHistoryError",
    "default_registry",
    "parse_action",
    "split_lines",
]
