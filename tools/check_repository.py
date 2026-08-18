"""Repository hygiene checks that do not belong in the unit test suite.

These are about what is *in the working tree*, not about what the code does:
committed databases, private hand histories, secrets, and — most importantly —
the byte integrity of the files that are addressed by SHA-256.

Run it locally exactly as CI does::

    python tools/check_repository.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRECTORIES = {".git", "__pycache__", ".venv", "venv", "node_modules"}

#: Files whose exact bytes are hashed. A line-ending rewrite breaks every
#: recorded SHA-256, so ``.gitattributes`` marks them ``-text`` and this check
#: makes sure both the rule and the bytes survive.
HASH_ADDRESSED_GLOBS = ("solutions/**/*.json", "tests/fixtures/**/*")

REQUIRED_GITATTRIBUTES = (
    "solutions/**/*.json -text",
    "tests/fixtures/** -text",
)

FORBIDDEN_SUFFIXES = {".db", ".duckdb", ".sqlite", ".sqlite3", ".pyc"}
FORBIDDEN_NAMES = {".env", ".DS_Store", "Thumbs.db"}

#: Real hand histories are user data. The two committed fixtures are synthetic
#: and declared as such in ``tests/fixtures/manifest.json``.
HAND_HISTORY_MARKERS = ("PokerStars Hand #", "PokerStars Game #", "PokerStars Zoom Hand #")
HAND_HISTORY_ALLOWED = "tests/fixtures/"

#: A real provider key. This one is unambiguous, so it is checked everywhere.
_PROVIDER_KEY = re.compile(r"sk-[A-Za-z0-9]{20,}")

#: A best-effort tripwire for anything else that looks assigned rather than
#: read from the environment. This is a heuristic, not a proof: the real
#: defences are ``.gitignore`` and the rule that API keys are only ever read
#: from environment variables (enforced in ``openai_provider`` and its tests).
#: Obvious placeholders are skipped, because a check that always fires is a
#: check people learn to ignore.
_ASSIGNED_CREDENTIAL = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]([^'\"]{12,})['\"]"
)
_PLACEHOLDER_WORDS = (
    "test", "fake", "dummy", "example", "placeholder", "sample",
    "your-", "changeme", "redacted", "xxx", "none", "unset",
)

TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".txt", ".toml", ".cfg", ".ini"}


def _tracked_files() -> list[Path]:
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if SKIP_DIRECTORIES & set(path.relative_to(PROJECT_ROOT).parts):
            continue
        files.append(path)
    return sorted(files)


def _relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def check_no_forbidden_files(files: list[Path], problems: list[str]) -> None:
    for path in files:
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name in FORBIDDEN_NAMES:
            problems.append(f"{_relative(path)}: this file type must never be committed")


def check_no_private_hand_histories(files: list[Path], problems: list[str]) -> None:
    for path in files:
        if path.suffix.lower() != ".txt":
            continue
        relative = _relative(path)
        if relative.startswith(HAND_HISTORY_ALLOWED):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable files are not our problem
            continue
        if any(marker in text for marker in HAND_HISTORY_MARKERS):
            problems.append(
                f"{relative}: looks like a hand history outside tests/fixtures; "
                "player data must not be committed"
            )


def _looks_like_a_placeholder(value: str) -> bool:
    lowered = value.lower()
    if any(word in lowered for word in _PLACEHOLDER_WORDS):
        return True
    # Real keys are high entropy; placeholders are usually words with no digits.
    return not any(character.isdigit() for character in value)


def check_no_secrets(files: list[Path], problems: list[str]) -> None:
    allowed = {"tools/check_repository.py"}
    for path in files:
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        relative = _relative(path)
        if relative in allowed:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover
            continue
        if _PROVIDER_KEY.search(text):
            problems.append(f"{relative}: contains what looks like a provider API key")
        for _, value in _ASSIGNED_CREDENTIAL.findall(text):
            if not _looks_like_a_placeholder(value):
                problems.append(
                    f"{relative}: assigns a credential-looking literal; keys must be "
                    "read from the environment"
                )
                break


def check_hash_addressed_bytes(problems: list[str]) -> None:
    attributes = PROJECT_ROOT / ".gitattributes"
    if not attributes.is_file():
        problems.append(".gitattributes is missing; hashed files are unprotected")
    else:
        declared = attributes.read_text(encoding="utf-8")
        for rule in REQUIRED_GITATTRIBUTES:
            if rule not in declared:
                problems.append(
                    f".gitattributes no longer declares '{rule}'; a Windows checkout "
                    "would rewrite line endings and break every recorded SHA-256"
                )

    for glob in HASH_ADDRESSED_GLOBS:
        for path in PROJECT_ROOT.glob(glob):
            if not path.is_file():
                continue
            if b"\r\n" in path.read_bytes():
                problems.append(
                    f"{_relative(path)}: contains CRLF, so its SHA-256 no longer "
                    "matches what the catalog recorded"
                )


def check_default_catalog_is_empty(problems: list[str]) -> None:
    catalog = PROJECT_ROOT / "solutions" / "catalog.json"
    if not catalog.is_file():
        problems.append("solutions/catalog.json is missing")
        return
    text = catalog.read_text(encoding="utf-8")
    if '"solutions": []' not in text.replace("\n", " ").replace("  ", " "):
        problems.append(
            "solutions/catalog.json is no longer empty; the default catalog must "
            "not ship strategy references"
        )


def main() -> int:
    files = _tracked_files()
    problems: list[str] = []
    check_no_forbidden_files(files, problems)
    check_no_private_hand_histories(files, problems)
    check_no_secrets(files, problems)
    check_hash_addressed_bytes(problems)
    check_default_catalog_is_empty(problems)

    if problems:
        print("Repository checks failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Repository checks passed over {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
