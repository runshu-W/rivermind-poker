"""Shared strict-reading primitives for RiverMind's versioned protocols.

Every protocol that carries strategy content — the strategy artifact, the solve
quality report, the quality attestation — has to reject the same family of
inputs: unknown keys, duplicate JSON keys, lone surrogates, ``NaN``, floats,
over-precise or over-large decimals, malformed timestamps.

Keeping one implementation of those rules is the point of this module.  Three
copies of "reject NaN" drift apart; one copy does not.  Each protocol supplies
its own exception type so error messages stay attributable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from rivermind_core.gto_specs import SpecValidationError


#: Total digit characters allowed in any decimal string.  Without a magnitude
#: bound a 40-digit value would round against the default 28-digit decimal
#: context and the protocol would report a number that is not in the file.
MAX_DECIMAL_DIGITS = 18

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_PATTERN = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")

# Control characters, C1 controls and surrogate code points are excluded.  A
# lone surrogate survives JSON decoding but cannot be re-encoded to UTF-8, which
# would crash the caller after validation had already "passed".
TEXT_PATTERN = re.compile(r"^[^\x00-\x1f\x7f-\x9f\ud800-\udfff]{1,128}$")
LONG_TEXT_PATTERN = re.compile(r"^[^\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]{1,1024}$")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class StrictReader:
    """Reads a decoded JSON document, refusing anything ambiguous.

    Parameters
    ----------
    error:
        The exception class to raise.  Must derive from
        :class:`~rivermind_core.gto_specs.SpecValidationError` so callers can
        keep catching one base type.
    """

    __slots__ = ("_error",)

    def __init__(self, error: type[SpecValidationError]) -> None:
        self._error = error

    # -- document level ----------------------------------------------------

    def load_json(self, raw: bytes, field: str) -> Any:
        """Decode UTF-8 JSON, rejecting duplicate keys and runaway nesting."""

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise self._error(f"{field} must be UTF-8: {exc}") from exc
        try:
            return json.loads(text, object_pairs_hook=self._reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise self._error(f"{field} is not valid JSON: {exc}") from exc
        except RecursionError as exc:
            raise self._error(f"{field} JSON is nested too deeply to validate") from exc

    def _reject_duplicate_keys(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise self._error(f"JSON repeats the key {key!r}")
            seen[key] = value
        return seen

    def schema_version(self, payload: Mapping[str, Any], expected: str) -> None:
        actual = payload.get("schema_version")
        if actual != expected:
            raise self._error(f"unsupported schema version: {actual!r}")

    # -- structure ---------------------------------------------------------

    def mapping(self, value: Any, field: str) -> Mapping[str, Any]:
        if not isinstance(value, dict):
            raise self._error(f"{field} must be an object")
        return value

    def sequence(self, value: Any, field: str) -> list[Any]:
        if not isinstance(value, list):
            raise self._error(f"{field} must be an array")
        return value

    def exact_keys(
        self,
        payload: Mapping[str, Any],
        expected: Iterable[str],
        field: str,
    ) -> None:
        actual = set(payload)
        wanted = set(expected)
        if actual != wanted:
            missing = sorted(wanted - actual)
            unknown = sorted(actual - wanted)
            raise self._error(
                f"{field} keys do not match contract; missing={missing}, unknown={unknown}"
            )

    # -- scalars -----------------------------------------------------------

    def text(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise self._error(f"{field} must be a string")
        if not TEXT_PATTERN.fullmatch(value):
            raise self._error(f"{field} must be 1 to 128 printable characters")
        return value

    def long_text(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise self._error(f"{field} must be a string")
        if not LONG_TEXT_PATTERN.fullmatch(value):
            raise self._error(f"{field} must be 1 to 1024 printable characters")
        return value

    def optional_text(self, value: Any, field: str) -> str | None:
        return None if value is None else self.text(value, field)

    def flag(self, value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise self._error(f"{field} must be true or false")
        return value

    def integer(self, value: Any, field: str, *, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._error(f"{field} must be an integer")
        if not minimum <= value <= maximum:
            raise self._error(f"{field} must be between {minimum} and {maximum}")
        return value

    def sha256(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise self._error(f"{field} must be 64 lowercase hex characters")
        return value

    def timestamp(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not TIMESTAMP_PATTERN.fullmatch(value):
            raise self._error(
                f"{field} must be an RFC3339 UTC timestamp such as 2026-08-18T00:00:00Z"
            )
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise self._error(f"{field} is not a real timestamp: {exc}") from exc
        return value

    def decimal(
        self,
        value: Any,
        field: str,
        *,
        places: int,
        allow_negative: bool = True,
    ) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, str):
            raise self._error(
                f"{field} must be a decimal string so NaN, Infinity and float drift "
                "are impossible"
            )
        if not DECIMAL_PATTERN.fullmatch(value):
            raise self._error(f"{field} is not a plain decimal string: {value!r}")
        if not allow_negative and value.startswith("-"):
            raise self._error(f"{field} must not be negative")
        _, _, fraction = value.partition(".")
        if len(fraction) > places:
            raise self._error(
                f"{field} exceeds the versioned precision of {places} decimal places"
            )
        digits = len(value) - value.count(".") - value.count("-")
        if digits > MAX_DECIMAL_DIGITS:
            raise self._error(
                f"{field} exceeds the versioned magnitude of {MAX_DECIMAL_DIGITS} digits"
            )
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - pattern already guards
            raise self._error(f"{field} is not a decimal: {exc}") from exc
        if not parsed.is_finite():  # pragma: no cover - pattern already guards
            raise self._error(f"{field} must be finite")
        return parsed


def parse_timestamp(value: str) -> datetime:
    """Parse an already-validated RFC3339 UTC timestamp for ordering checks."""

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def decimal_text(value: Decimal) -> str:
    """Format without ``normalize()``, which would round against the decimal context."""

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-", "-0"}:
        return "0"
    return text


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """The one serialization RiverMind hashes: UTF-8, 2-space indent, LF, trailing newline."""

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return text.encode("utf-8")


_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def resolve_sandboxed_path(
    root: Path,
    relative: str,
    *,
    error: type[SpecValidationError],
    label: str,
    must_exist: bool = True,
) -> Path:
    """Resolve ``relative`` to an existing ``.json`` file strictly inside ``root``.

    Rejects absolute paths, drive letters, ``..``, backslashes, odd characters and
    any path that passes through a symbolic link.  Hardlinks are *not* detected;
    the defence against those is not treating an untrusted directory as a root.
    """

    if not relative or relative.startswith("/"):
        raise error(f"{label} id must be a relative path below its own directory")
    if "\\" in relative:
        raise error(f"{label} id must use POSIX '/' separators")
    if not relative.endswith(".json"):
        raise error(f"{label} id must reference a .json file")
    segments = relative.split("/")
    for segment in segments:
        if segment in {"", ".", ".."}:
            raise error(f"{label} id contains an illegal path segment {segment!r}")
        if not _PATH_SEGMENT_PATTERN.fullmatch(segment):
            raise error(f"{label} id path segment {segment!r} is not allowed")
    try:
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise error(f"{label} root directory is unavailable: {exc}") from exc
    if not root_resolved.is_dir():
        raise error(f"{label} root must be a directory")

    candidate = root_resolved.joinpath(*segments)
    node = candidate
    while node != root_resolved:
        if node.is_symlink():
            raise error(
                f"{label} path traverses a symbolic link; refusing to leave the sandbox"
            )
        parent = node.parent
        if parent == node:  # pragma: no cover - candidate is built from root
            raise error(f"{label} path escapes the sandbox")
        node = parent
    if not must_exist:
        return candidate
    if not candidate.is_file():
        raise error(f"{label} file not found: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise error(f"{label} file is unreadable: {exc}") from exc
    if not resolved.is_relative_to(root_resolved):
        raise error(f"{label} path escapes the sandbox")
    return candidate
