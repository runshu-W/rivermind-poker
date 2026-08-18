from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from rivermind_core.models import HandHistory


class HandHistoryParseError(ValueError):
    """Raised when no parser can normalize a hand or parsing fails."""

    def __init__(self, message: str, *, code: str = "parse_error") -> None:
        super().__init__(message)
        self.code = code


class UnsupportedHandHistoryError(HandHistoryParseError):
    """Raised when a hand is recognized but its variant is not supported."""

    def __init__(self, message: str, *, code: str = "unsupported_format") -> None:
        super().__init__(message, code=code)


class HandHistoryParser(ABC):
    name: str

    #: The literal text every hand from this site starts with.  The importer
    #: uses it to find hand boundaries, so a new parser makes multi-hand files
    #: from its site splittable without touching the importer.
    header_prefix: str = ""

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

    def header_prefixes(self) -> tuple[str, ...]:
        """Every registered site's hand-boundary marker, longest first.

        Longest first matters: ``PokerStars Hand #`` must be tried before a
        shorter prefix that happens to be a substring of it.
        """

        return tuple(
            sorted(
                {parser.header_prefix for parser in self._parsers if parser.header_prefix},
                key=len,
                reverse=True,
            )
        )

    def parse(self, raw_text: str) -> HandHistory:
        _, hand = self.parse_with_parser(raw_text)
        return hand

    def parse_with_parser(
        self, raw_text: str
    ) -> tuple[HandHistoryParser, HandHistory]:
        normalized = raw_text.strip()
        if not normalized:
            raise HandHistoryParseError("Hand history is empty", code="empty_hand")

        for parser in self._parsers:
            if parser.can_parse(normalized):
                return parser, parser.parse(normalized)

        raise UnsupportedHandHistoryError(
            "No registered parser recognizes this hand history",
            code="unknown_format",
        )
