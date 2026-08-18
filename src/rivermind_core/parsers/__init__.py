from .base import (
    HandHistoryParseError,
    ParserRegistry,
    UnsupportedHandHistoryError,
)
from .pokerstars import PokerStarsCashParser, PokerStarsTournamentParser


def default_registry() -> ParserRegistry:
    return ParserRegistry([PokerStarsTournamentParser(), PokerStarsCashParser()])


__all__ = [
    "HandHistoryParseError",
    "ParserRegistry",
    "PokerStarsCashParser",
    "PokerStarsTournamentParser",
    "UnsupportedHandHistoryError",
    "default_registry",
]
