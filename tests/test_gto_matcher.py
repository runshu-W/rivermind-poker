from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.cli import main  # noqa: E402
from rivermind_core.gto_matcher import (  # noqa: E402
    MatchReason,
    MatchStatus,
    extract_decision_game_spec,
    match_game_spec,
)
from rivermind_core.gto_specs import (  # noqa: E402
    GAME_SPEC_SCHEMA_VERSION,
    PositionStack,
    RakeSpec,
    SolutionCatalog,
    SolutionObjective,
    SolutionQuality,
    SolutionSpec,
    SpecValidationError,
    solution_catalog_from_dict,
)
from rivermind_core.importer import HandHistoryImporter  # noqa: E402
from rivermind_core.models import GameType  # noqa: E402
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.storage import SQLiteHandStore  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
ARTIFACT_HASH = "a" * 64


class GTOSpecAndExtractionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = default_registry()
        cls.cash = registry.parse(
            (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
        )
        cls.tournament = registry.parse(
            (FIXTURES / "pokerstars_mtt.txt").read_text(encoding="utf-8")
        )

    def test_extracts_a_deidentified_real_decision_node(self) -> None:
        spec = _cash_spec(self.cash)
        payload = spec.to_dict()
        serialized = json.dumps(payload)

        self.assertEqual(payload["schema_version"], GAME_SPEC_SCHEMA_VERSION)
        self.assertEqual(payload["node"]["street"], "flop")
        self.assertEqual(payload["node"]["board"], ["2c", "7d", "Ts"])
        self.assertEqual(payload["node"]["player_to_act"], "BTN")
        self.assertEqual(payload["node"]["pot_bb"], "6")
        self.assertNotIn("Hero", serialized)
        self.assertNotIn(self.cash.hand_id, serialized)

    def test_fingerprint_normalizes_equivalent_decimals(self) -> None:
        spec = _cash_spec(self.cash)
        equivalent = replace(
            spec,
            pot_bb=Decimal("6.000"),
            stacks=tuple(
                PositionStack(item.position, item.remaining_bb.quantize(Decimal("0.00")))
                for item in spec.stacks
            ),
        )
        self.assertEqual(spec.fingerprint, equivalent.fingerprint)

    def test_rejects_nondecision_and_missing_icm_context(self) -> None:
        with self.assertRaisesRegex(SpecValidationError, "voluntary decision"):
            extract_decision_game_spec(self.cash, before_action=0)
        with self.assertRaisesRegex(SpecValidationError, "tournament_context_id"):
            extract_decision_game_spec(
                self.tournament,
                before_action=8,
                objective=SolutionObjective.ICM,
            )

    def test_tournament_chip_ev_extracts_antes_without_cash_rake(self) -> None:
        spec = extract_decision_game_spec(self.tournament, before_action=8)
        self.assertEqual(spec.game_type, GameType.TOURNAMENT)
        self.assertEqual(len(spec.antes), 6)
        self.assertTrue(all(item.ante_bb == Decimal("0.1") for item in spec.antes))
        self.assertIsNone(spec.rake)


class GTOMatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cash = default_registry().parse(
            (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
        )
        cls.observed = _cash_spec(cls.cash)

    def test_exact_match_returns_artifact_metadata_but_no_strategy_claims(self) -> None:
        solution = _solution("exact", self.observed)
        result = match_game_spec(self.observed, _catalog(solution))
        payload = result.to_dict()

        self.assertEqual(result.status, MatchStatus.EXACT)
        self.assertEqual(result.reason, MatchReason.EXACT_NODE)
        self.assertTrue(payload["solution_reference_available"])
        self.assertEqual(payload["solution"]["artifact"]["id"], "artifact/exact")
        self.assertNotIn("frequencies", payload)
        self.assertNotIn("ev", payload)

    def test_approximate_match_explains_every_numeric_difference(self) -> None:
        changed_stacks = tuple(
            PositionStack(item.position, item.remaining_bb + Decimal("1"))
            for item in self.observed.stacks
        )
        approximate = replace(
            self.observed,
            stacks=changed_stacks,
            rake=replace(self.observed.rake, cap_bb=Decimal("3.2")),
        )
        result = match_game_spec(self.observed, _catalog(_solution("near", approximate)))

        self.assertEqual(result.status, MatchStatus.APPROXIMATE)
        self.assertEqual(result.reason, MatchReason.WITHIN_EXPLICIT_THRESHOLDS)
        self.assertEqual(
            {item.field for item in result.differences},
            {"stacks.BB.remaining_bb", "stacks.BTN.remaining_bb", "rake.cap_bb"},
        )

    def test_exceeding_threshold_fails_closed(self) -> None:
        changed = replace(
            self.observed,
            stacks=tuple(
                PositionStack(item.position, item.remaining_bb + Decimal("6"))
                for item in self.observed.stacks
            ),
        )
        result = match_game_spec(self.observed, _catalog(_solution("far", changed)))
        self.assertEqual(result.status, MatchStatus.UNSUPPORTED)
        self.assertEqual(result.reason, MatchReason.THRESHOLDS_EXCEEDED)
        self.assertFalse(result.solution_reference_available)

    def test_board_or_action_line_mismatch_is_not_approximated(self) -> None:
        changed = replace(self.observed, board=("2c", "7d", "9s"))
        result = match_game_spec(self.observed, _catalog(_solution("wrong-board", changed)))
        self.assertEqual(result.status, MatchStatus.UNSUPPORTED)
        self.assertEqual(result.reason, MatchReason.NO_HARD_COMPATIBLE_SOLUTION)
        self.assertEqual(result.differences[0].field, "board")

    def test_missing_cash_rake_and_empty_catalog_have_explicit_reasons(self) -> None:
        no_rake = replace(self.observed, rake=None)
        solution = _solution("known-rake", self.observed)
        missing = match_game_spec(no_rake, _catalog(solution))
        empty = match_game_spec(no_rake, _catalog())
        self.assertEqual(missing.reason, MatchReason.MISSING_RAKE_METADATA)
        self.assertEqual(empty.reason, MatchReason.CATALOG_EMPTY)

    def test_equal_best_candidates_are_rejected_as_ambiguous(self) -> None:
        changed = replace(self.observed, pot_bb=self.observed.pot_bb + Decimal("0.2"))
        result = match_game_spec(
            self.observed,
            _catalog(_solution("candidate-a", changed), _solution("candidate-b", changed)),
        )
        self.assertEqual(result.status, MatchStatus.UNSUPPORTED)
        self.assertEqual(result.reason, MatchReason.AMBIGUOUS_BEST_MATCH)
        self.assertEqual(result.candidate_solution_ids, ("candidate-a", "candidate-b"))


class GTOCatalogAndCLITest(unittest.TestCase):
    def test_catalog_round_trip_is_strict_and_versioned(self) -> None:
        hand = default_registry().parse(
            (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
        )
        catalog = _catalog(_solution("roundtrip", _cash_spec(hand)))
        restored = solution_catalog_from_dict(catalog.to_dict())
        self.assertEqual(restored, catalog)

        invalid = catalog.to_dict()
        invalid["unknown"] = True
        with self.assertRaisesRegex(SpecValidationError, "unknown"):
            solution_catalog_from_dict(invalid)

    def test_cli_matches_a_stored_real_hand(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "rivermind.db"
            catalog_path = root / "catalog.json"
            with SQLiteHandStore(database) as store:
                HandHistoryImporter(default_registry(), store).import_text(
                    "cash.txt",
                    (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8"),
                )
                hand = store.load_hand("pokerstars", "100000000001")
            assert hand is not None
            catalog_path.write_text(
                json.dumps(_catalog(_solution("cli-exact", _cash_spec(hand))).to_dict()),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "gto-match",
                        "pokerstars",
                        "100000000001",
                        "--before-action",
                        "5",
                        "--database",
                        os.fspath(database),
                        "--catalog",
                        os.fspath(catalog_path),
                        "--rake-model",
                        "pokerstars.cash.fixture",
                        "--rake-percent",
                        "5",
                        "--rake-cap-bb",
                        "3",
                        "--json",
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["match"]["status"], "exact")
            self.assertEqual(payload["match"]["solution"]["solution_id"], "cli-exact")


def _cash_spec(hand):
    return extract_decision_game_spec(
        hand,
        before_action=5,
        rake=RakeSpec(
            model_id="pokerstars.cash.fixture",
            percent=Decimal("5"),
            cap_bb=Decimal("3"),
        ),
    )


def _solution(solution_id, game_spec):
    return SolutionSpec(
        solution_id=solution_id,
        game_spec=game_spec,
        solver_name="fixture-solver",
        solver_version="1.0",
        action_tree_version="fixture-tree/1.0",
        quality=SolutionQuality.TEST_ONLY,
        artifact_id=f"artifact/{solution_id}",
        artifact_sha256=ARTIFACT_HASH,
    )


def _catalog(*solutions):
    return SolutionCatalog(
        catalog_id="test-catalog",
        catalog_version="1.0",
        solutions=tuple(solutions),
    )


if __name__ == "__main__":
    unittest.main()
