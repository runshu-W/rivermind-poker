"""Tests for ``tools/check_repository.py``.

The guard had no tests, and it shipped a bug that fired on every developer
machine: it walked the filesystem while its own function was named
``_tracked_files``, so a gitignored ``data/private/*.db`` — a file git could
never commit — failed the check.  A guard that always fails is a guard people
learn to ignore, which makes it worse than no guard at all.

These tests build throwaway git repositories and run the real script against
them as a subprocess, because the thing under test is precisely "what does it
consider to be in the repository".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "tools"))

from check_repository import is_hash_addressed  # noqa: E402


GUARD = PROJECT_ROOT / "tools" / "check_repository.py"

MINIMAL_GITATTRIBUTES = (
    "solutions/**/*.json -text\n"
    "tests/fixtures/** -text\n"
)
EMPTY_CATALOG = '{\n  "schema_version": "solution-catalog/1.0.0",\n  "solutions": []\n}\n'


class HashAddressedRulesTest(unittest.TestCase):
    """``Path.match`` does not do recursive ``**`` before 3.13.

    Using it here would have silently stopped checking the two files that matter
    most — the catalog and the fixture manifest — while still reporting a pass.
    """

    def test_the_catalog_itself_is_covered(self) -> None:
        self.assertTrue(is_hash_addressed("solutions/catalog.json"))

    def test_the_fixture_manifest_is_covered(self) -> None:
        self.assertTrue(is_hash_addressed("tests/fixtures/manifest.json"))

    def test_nested_artifacts_and_fixtures_are_covered(self) -> None:
        self.assertTrue(is_hash_addressed("solutions/fixtures/a.test_only.json"))
        self.assertTrue(is_hash_addressed("tests/fixtures/nested/deep/hand.txt"))

    def test_unrelated_files_are_not_covered(self) -> None:
        self.assertFalse(is_hash_addressed("src/rivermind_core/models.py"))
        self.assertFalse(is_hash_addressed("solutions/README.md"))
        self.assertFalse(is_hash_addressed("docs/tests/fixtures/x.txt"))


class _Repo:
    """A throwaway git repository containing a copy of the guard."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "tools").mkdir(parents=True)
        (root / "solutions").mkdir()
        (root / "tests" / "fixtures").mkdir(parents=True)
        shutil.copy2(GUARD, root / "tools" / "check_repository.py")
        self.write(".gitattributes", MINIMAL_GITATTRIBUTES)
        self.write("solutions/catalog.json", EMPTY_CATALOG)
        self.git("init", "-q")
        self.git("config", "user.email", "guard@example.invalid")
        self.git("config", "user.name", "Guard Test")

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def write_bytes(self, relative: str, payload: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True
        )

    def run_guard(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, os.fspath(self.root / "tools" / "check_repository.py")],
            cwd=self.root,
            capture_output=True,
            text=True,
        )


class RepositoryGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.repo = _Repo(Path(self._temp.name) / "repo")

    def test_a_clean_repository_passes_and_says_git_answered(self) -> None:
        self.repo.git("add", "-A")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("file list from git", result.stdout)

    def test_a_gitignored_database_does_not_fail_the_check(self) -> None:
        """The regression. Every developer who runs the app has one of these."""

        self.repo.write(".gitignore", "data/private/\n*.db\n")
        self.repo.write_bytes("data/private/leak-qa/rivermind.db", b"SQLite format 3\x00")
        self.repo.git("add", "-A")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("rivermind.db", result.stderr)

    def test_a_database_that_is_not_ignored_still_fails(self) -> None:
        """Untracked but unignored: the next ``git add -A`` would commit it."""

        self.repo.write_bytes("data/rivermind.db", b"SQLite format 3\x00")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("data/rivermind.db", result.stderr)

    def test_a_force_added_database_still_fails(self) -> None:
        """``--cached`` lists it even though ``.gitignore`` names it."""

        self.repo.write(".gitignore", "*.db\n")
        self.repo.write_bytes("data/rivermind.db", b"SQLite format 3\x00")
        self.repo.git("add", "-f", "data/rivermind.db")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("data/rivermind.db", result.stderr)

    def test_crlf_in_the_catalog_is_caught(self) -> None:
        """The catalog is hash-addressed; the old glob never matched it."""

        self.repo.write_bytes(
            "solutions/catalog.json", EMPTY_CATALOG.encode().replace(b"\n", b"\r\n")
        )
        self.repo.git("add", "-A")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("solutions/catalog.json", result.stderr)
        self.assertIn("CRLF", result.stderr)

    def test_crlf_in_a_fixture_is_caught(self) -> None:
        self.repo.write_bytes("tests/fixtures/manifest.json", b'{\r\n  "a": 1\r\n}\r\n')
        self.repo.git("add", "-A")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("tests/fixtures/manifest.json", result.stderr)

    def test_a_gitignored_crlf_file_is_not_reported(self) -> None:
        self.repo.write(".gitignore", "tests/fixtures/scratch/\n")
        self.repo.write_bytes("tests/fixtures/scratch/x.txt", b"a\r\nb\r\n")
        self.repo.git("add", "-A")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_missing_gitattributes_rule_fails(self) -> None:
        self.repo.write(".gitattributes", "solutions/**/*.json -text\n")
        self.repo.git("add", "-A")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("tests/fixtures/** -text", result.stderr)

    def test_a_non_empty_default_catalog_fails(self) -> None:
        self.repo.write(
            "solutions/catalog.json",
            '{\n  "schema_version": "solution-catalog/1.0.0",\n'
            '  "solutions": [{"artifact_id": "x"}]\n}\n',
        )
        self.repo.git("add", "-A")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer empty", result.stderr)

    def test_a_private_hand_history_outside_fixtures_fails(self) -> None:
        self.repo.write("notes/session.txt", "PokerStars Hand #123: Hold'em No Limit\n")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("notes/session.txt", result.stderr)

    def test_a_gitignored_hand_history_does_not_fail(self) -> None:
        """Importers write here; the directory is ignored for exactly that reason."""

        self.repo.write(".gitignore", "data/imports/\n")
        self.repo.write(
            "data/imports/session.txt", "PokerStars Hand #123: Hold'em No Limit\n"
        )
        self.repo.git("add", "-A")
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_provider_key_fails(self) -> None:
        # Assembled at runtime so this file does not itself contain a literal
        # that trips the scanner. Adding this file to the guard's allowlist
        # would have been the wrong fix: a secret scanner with exceptions is a
        # secret scanner you cannot reason about.
        fake_key = "sk-" + ("a1b2c3d4e5" * 3)
        self.repo.write("src/config.py", f'KEY = "{fake_key}"\n')
        result = self.repo.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("provider API key", result.stderr)


class GuardFallbackTest(unittest.TestCase):
    """Without git the guard must still run, and must say what it did."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name) / "plain"
        (root / "tools").mkdir(parents=True)
        (root / "solutions").mkdir()
        (root / "tests" / "fixtures").mkdir(parents=True)
        shutil.copy2(GUARD, root / "tools" / "check_repository.py")
        (root / ".gitattributes").write_text(MINIMAL_GITATTRIBUTES, encoding="utf-8")
        (root / "solutions" / "catalog.json").write_text(EMPTY_CATALOG, encoding="utf-8")
        self.root = root

    def _run(self, *, path: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ, PATH=path)
        return subprocess.run(
            [sys.executable, os.fspath(self.root / "tools" / "check_repository.py")],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_outside_a_repository_it_falls_back_and_says_so(self) -> None:
        result = self._run(path=os.environ.get("PATH", ""))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("file list from filesystem", result.stdout)
        self.assertIn("git could not list this directory", result.stderr)

    def test_without_a_git_binary_it_falls_back_rather_than_crashing(self) -> None:
        empty = Path(self._temp.name) / "no-tools"
        empty.mkdir()
        result = self._run(path=os.fspath(empty))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("file list from filesystem", result.stdout)

    def test_the_fallback_is_stricter_not_looser(self) -> None:
        """It sees ignored files too, so it fails closed rather than open."""

        (self.root / "data").mkdir()
        (self.root / ".gitignore").write_text("data/\n", encoding="utf-8")
        (self.root / "data" / "x.db").write_bytes(b"SQLite format 3\x00")
        result = self._run(path=os.environ.get("PATH", ""))
        self.assertEqual(result.returncode, 1)
        self.assertIn("data/x.db", result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
