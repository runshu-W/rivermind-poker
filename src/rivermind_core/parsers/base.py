from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from rivermind_core.models import HandHistory


class HandHistoryParseError(ValueError):
    """Raised when no parser can normalize a hand or parsing fails."""


class HandHistoryParser(ABC):
    name: str

    @abstractmethod
    def can_parse(self, raw_text: str) -> bool:
        """Return whether this parser recognizes the supplied text."""

    @abstractmethod
    def parse(self, raw_text: str) -> HandHistory:
        """Parse one hand into the canonical model."""


class ParserRegistry:
    def __init__(self, parsers: Iterable[HandHistoryParser] = ()) -> None:
        self._parsers = list(parsers)

    def register(self, parser: HandHistoryParser) -> None:
        self._parsers.append(parser)

    def parse(self, raw_text: str) -> HandHistory:
        normalized = raw_text.strip()
        if not normalized:
            raise HandHistoryParseError("Hand history is empty")

        for parser in self._parsers:
            if parser.can_parse(normalized):
                return parser.parse(normalized)

        raise HandHistoryParseError("No registered parser recognizes this hand history")

