from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol, Sequence
from uuid import uuid4

from rivermind_core.models import HandHistory, HandValidationError
from rivermind_core.parsers import (
    HandHistoryParseError,
    ParserRegistry,
    UnsupportedHandHistoryError,
)


#: Fallback boundaries for callers that use ``split_hand_histories`` on its own.
#: ``HandHistoryImporter`` asks its registry instead, so registering a parser is
#: all it takes to make that site's multi-hand files splittable.
DEFAULT_HAND_START_PREFIXES = ("PokerStars Hand #", "Poker Hand #")


class ImportItemStatus(StrEnum):
    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class HandSegment:
    index: int
    start_line: int
    end_line: int
    raw_text: str


@dataclass(frozen=True, slots=True)
class ImportItemResult:
    index: int
    start_line: int
    end_line: int
    status: ImportItemStatus
    source_site: str | None = None
    parser_name: str | None = None
    hand_id: str | None = None
    fingerprint: str | None = None
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ImportBatchReport:
    batch_id: str
    source_name: str
    started_at: datetime
    completed_at: datetime
    items: tuple[ImportItemResult, ...]

    @property
    def imported(self) -> int:
        return self._count(ImportItemStatus.IMPORTED)

    @property
    def duplicates(self) -> int:
        return self._count(ImportItemStatus.DUPLICATE)

    @property
    def failed(self) -> int:
        return self._count(ImportItemStatus.FAILED)

    @property
    def unsupported(self) -> int:
        return self._count(ImportItemStatus.UNSUPPORTED)

    @property
    def detected(self) -> int:
        return len(self.items)

    @property
    def elapsed_ms(self) -> int:
        return max(
            0,
            round((self.completed_at - self.started_at).total_seconds() * 1000),
        )

    def _count(self, status: ImportItemStatus) -> int:
        return sum(item.status == status for item in self.items)


class ImportStore(Protocol):
    def begin_batch(
        self, batch_id: str, source_name: str, started_at: datetime
    ) -> None: ...

    def store_hand(
        self,
        hand: HandHistory,
        *,
        fingerprint: str,
        parser_name: str,
        source_name: str,
        imported_at: datetime,
    ) -> bool: ...

    def record_item(
        self,
        batch_id: str,
        item: ImportItemResult,
        raw_text: str,
    ) -> None: ...

    def complete_batch(self, report: ImportBatchReport) -> None: ...

    def abort_batch(self, batch_id: str) -> None: ...


def split_hand_histories(
    raw_text: str,
    *,
    prefixes: Sequence[str] = DEFAULT_HAND_START_PREFIXES,
) -> tuple[HandSegment, ...]:
    """Split a file into hands while preserving source line ranges.

    A site's hand header is an unambiguous boundary. If no known boundary
    exists, the entire non-empty input becomes one unsupported candidate so the
    caller receives a visible error instead of silently dropping the file.
    """

    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized.removeprefix("\ufeff")
    lines = normalized.splitlines()
    if not any(line.strip() for line in lines):
        return ()

    start_indexes = [
        index
        for index, line in enumerate(lines)
        if any(line.lstrip("\ufeff").startswith(prefix) for prefix in prefixes)
    ]
    if not start_indexes:
        text = "\n".join(lines).strip()
        return (HandSegment(0, 1, len(lines), text),)

    segments: list[HandSegment] = []
    for item_index, line_index in enumerate(start_indexes):
        next_index = (
            start_indexes[item_index + 1]
            if item_index + 1 < len(start_indexes)
            else len(lines)
        )
        segment_lines = lines[line_index:next_index]
        while segment_lines and not segment_lines[-1].strip():
            segment_lines.pop()
        segments.append(
            HandSegment(
                index=item_index,
                start_line=line_index + 1,
                end_line=line_index + len(segment_lines),
                raw_text="\n".join(segment_lines),
            )
        )
    return tuple(segments)


def hand_fingerprint(hand: HandHistory) -> str:
    """Return a stable, privacy-safe identity fingerprint for a parsed hand."""

    identity = f"{hand.site.lower()}\0{hand.hand_id.strip()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def detect_source_site(raw_text: str) -> str | None:
    first_line = raw_text.lstrip("\ufeff \t\r\n").splitlines()[0]
    if first_line.startswith("PokerStars Hand #"):
        return "pokerstars"
    return None


class HandHistoryImporter:
    def __init__(self, registry: ParserRegistry, store: ImportStore) -> None:
        self._registry = registry
        self._store = store

    def import_text(self, source_name: str, raw_text: str) -> ImportBatchReport:
        batch_id = str(uuid4())
        started_at = datetime.now(timezone.utc)
        self._store.begin_batch(batch_id, source_name, started_at)
        results: list[ImportItemResult] = []
        try:
            for segment in split_hand_histories(
                raw_text, prefixes=self._registry.header_prefixes()
            ):
                result = self._import_segment(source_name, segment)
                self._store.record_item(batch_id, result, segment.raw_text)
                results.append(result)

            report = ImportBatchReport(
                batch_id=batch_id,
                source_name=source_name,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                items=tuple(results),
            )
            self._store.complete_batch(report)
            return report
        except Exception:
            self._store.abort_batch(batch_id)
            raise

    def _import_segment(
        self, source_name: str, segment: HandSegment
    ) -> ImportItemResult:
        source_site = detect_source_site(segment.raw_text)
        try:
            parser, hand = self._registry.parse_with_parser(segment.raw_text)
        except UnsupportedHandHistoryError as exc:
            return ImportItemResult(
                index=segment.index,
                start_line=segment.start_line,
                end_line=segment.end_line,
                status=ImportItemStatus.UNSUPPORTED,
                source_site=source_site,
                error_code=exc.code,
                message=str(exc),
            )
        except (HandHistoryParseError, HandValidationError) as exc:
            return ImportItemResult(
                index=segment.index,
                start_line=segment.start_line,
                end_line=segment.end_line,
                status=ImportItemStatus.FAILED,
                source_site=source_site,
                error_code=getattr(exc, "code", "validation_error"),
                message=str(exc),
            )

        fingerprint = hand_fingerprint(hand)
        imported = self._store.store_hand(
            hand,
            fingerprint=fingerprint,
            parser_name=parser.name,
            source_name=source_name,
            imported_at=datetime.now(timezone.utc),
        )
        return ImportItemResult(
            index=segment.index,
            start_line=segment.start_line,
            end_line=segment.end_line,
            status=(
                ImportItemStatus.IMPORTED
                if imported
                else ImportItemStatus.DUPLICATE
            ),
            source_site=hand.site,
            parser_name=parser.name,
            hand_id=hand.hand_id,
            fingerprint=fingerprint,
        )
