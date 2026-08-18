"""Verify that every relative link in the documentation resolves.

Broken links in a handoff document are worse than missing ones: they tell the
next reader that a contract is written down somewhere when it is not.

Run it locally exactly as CI does::

    python tools/check_docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRECTORIES = {".git", "__pycache__", ".venv", "venv", "node_modules"}

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_EXTERNAL = ("http://", "https://", "mailto:", "#")

#: Every document that the handoff and README promise a reader.
REQUIRED_DOCS = (
    "README.md",
    "PROJECT_PLAN.md",
    "docs/ARCHITECTURE.md",
    "docs/CLAUDE_HANDOFF.md",
    "docs/GTO_MATCHER.md",
    "docs/STRATEGY_ARTIFACTS.md",
    "docs/SOLVE_QUALITY_GATE.md",
    "docs/SOLVER_INGEST_TEXASSOLVER.md",
    "docs/BOARD_ISOMORPHISM.md",
)


def _markdown_files() -> list[Path]:
    return sorted(
        path
        for path in PROJECT_ROOT.rglob("*.md")
        if not SKIP_DIRECTORIES & set(path.relative_to(PROJECT_ROOT).parts)
    )


def main() -> int:
    problems: list[str] = []

    for required in REQUIRED_DOCS:
        if not (PROJECT_ROOT / required).is_file():
            problems.append(f"{required}: promised document is missing")

    checked = 0
    for document in _markdown_files():
        relative = document.relative_to(PROJECT_ROOT).as_posix()
        for link in _LINK.findall(document.read_text(encoding="utf-8")):
            if link.startswith(_EXTERNAL):
                continue
            checked += 1
            target = (document.parent / link.split("#", 1)[0]).resolve()
            if not target.exists():
                problems.append(f"{relative}: link '{link}' does not resolve")

    if problems:
        print("Documentation checks failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Documentation checks passed: {checked} relative links across "
          f"{len(_markdown_files())} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
