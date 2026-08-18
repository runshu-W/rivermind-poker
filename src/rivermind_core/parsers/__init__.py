from .base import (
    HandHistoryParseError,
    ParserRegistry,
    UnsupportedHandHistoryError,
)
from .pokerstars import PokerStarsCashParser


def default_registry() -> ParserRegistry:
    return ParserRegistry([PokerStarsCashParser()])


__all__ = [
    "HandHistoryParseError",
    "ParserRegistry",
    "PokerStarsCashParser",
    "UnsupportedHandHistoryError",
    "default_registry",
]
