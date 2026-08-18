from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.parsers import default_registry  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


class GoldenCorpusTest(unittest.TestCase):
    def test_every_manifest_hand_parses_to_declared_identity(self) -> None:
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))

        for item in manifest["hands"]:
            with self.subTest(file=item["file"]):
                raw_text = (FIXTURES / item["file"]).read_text(encoding="utf-8")
                hand = default_registry().parse(raw_text)
                self.assertEqual(hand.site, item["site"])
                self.assertEqual(hand.game_type.value, item["game_type"])
                self.assertEqual(hand.hand_id, item["hand_id"])


if __name__ == "__main__":
    unittest.main()
