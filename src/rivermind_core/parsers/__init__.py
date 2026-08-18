from .base import HandHistoryParseError, ParserRegistry
from .pokerstars import PokerStarsCashParser


def default_registry() -> ParserRegistry:
    return ParserRegistry([PokerStarsCashParser()])


__all__ = [
    "HandHistoryParseError",
    "ParserRegistry",
    "PokerStarsCashParser",
    "default_registry",
]

