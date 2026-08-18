from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.cli import main  # noqa: E402
from rivermind_core.gto_matcher import extract_decision_game_spec  # noqa: E402
from rivermind_core.gto_specs import (  # noqa: E402
    RakeSpec,
    SolutionCatalog,
    SolutionQuality,
    SolutionSpec,
    load_solution_catalog,
)
from rivermind_core.importer import HandHistoryImporter  # noqa: E402
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.storage import SQLiteHandStore  # noqa: E402
from rivermind_core import strategy_artifacts  # noqa: E402
from rivermind_core.strategy_artifacts import (  # noqa: E402
    STRATEGY_ARTIFACT_SCHEMA_VERSION,
    ArtifactValidationError,
    canonical_combo,
    resolve_artifact_path,
    serialize_strategy_artifact,
    strategy_artifact_from_dict,
    verify_catalog_artifact,
    verify_solution_artifact,
)
from rivermind_core.strategy_query import (  # noqa: E402
    AGGREGATION_POLICY_VERSION,
    STRATEGY_EVIDENCE_SCHEMA_VERSION,
    ComboNotCoveredError,
    build_strategy_evidence,
)


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
SOLUTIONS = PROJECT_ROOT / "solutions"
COMMITTED_CATALOG = SOLUTIONS / "catalog.test_only.json"
COMMITTED_SOLUTION_ID = "test-only.pokerstars-cash.btn-flop-cbet"

#: The committed demo node.  Pinned so a silent change to GameSpec normalization
#: cannot quietly invalidate every published fingerprint.
DEMO_FINGERPRINT = "85b7db3215c80f307bf4c745ab4a001b68f14ab3f0c938577b99e1c6bb469f55"

SOLVER_NAME = "rivermind.handwritten"
SOLVER_VERSION = "0.0.0"
ACTION_TREE_VERSION = "rivermind.test-only.flop-cbet/0.0.1"
SOLUTION_ID = "test-only.node"
ARTIFACT_ID = "fixtures/node.test_only.json"


def _demo_game_spec():
    hand = default_registry().parse(
        (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
    )
    return extract_decision_game_spec(
        hand,
        before_action=5,
        rake=RakeSpec(
            model_id="pokerstars.cash.example",
            percent=Decimal("5"),
            cap_bb=Decimal("3"),
        ),
    )


def _base_artifact(fingerprint: str) -> dict[str, Any]:
    return {
        "schema_version": STRATEGY_ARTIFACT_SCHEMA_VERSION,
        "solution_id": SOLUTION_ID,
        "game_spec_fingerprint": fingerprint,
        "action_tree_version": ACTION_TREE_VERSION,
        "node_id": "cash.hu.100bb.flop.btn-cbet-vs-bb-check",
        "ev_unit": "bb",
        "ev_semantics": "action_ev_from_node",
        "actions": [
            {"action_id": "bet_2bb", "kind": "bet", "size_bb": "2"},
            {"action_id": "check", "kind": "check", "size_bb": None},
        ],
        "entries": [
            {
                "combo": "AhKh",
                "weight": "1",
                "policies": [
                    {"action_id": "bet_2bb", "probability": "0.75", "ev": "2.5"},
                    {"action_id": "check", "probability": "0.25", "ev": "2.2"},
                ],
            },
            {
                "combo": "QcJc",
                "weight": "0.5",
                "policies": [
                    {"action_id": "bet_2bb", "probability": "0.4", "ev": "1.9"},
                    {"action_id": "check", "probability": "0.6", "ev": "1.8"},
                ],
            },
        ],
        "provenance": {
            "solver_name": SOLVER_NAME,
            "solver_version": SOLVER_VERSION,
            "solver_config_id": "unit-test",
            "generated_at": "2026-08-18T00:00:00Z",
            "quality": "test_only",
            "quality_report_id": None,
            "license": "RiverMind unit-test fixture.",
        },
    }


class _Case:
    """A temporary catalog plus artifact that individual tests can corrupt."""

    def __init__(
        self,
        root: Path,
        *,
        game_spec,
        artifact: dict[str, Any],
        quality: SolutionQuality = SolutionQuality.TEST_ONLY,
        artifact_id: str = ARTIFACT_ID,
        artifact_sha256: str | None = None,
        solver_name: str = SOLVER_NAME,
        solver_version: str = SOLVER_VERSION,
        action_tree_version: str = ACTION_TREE_VERSION,
        solution_id: str = SOLUTION_ID,
        write_artifact: bool = True,
    ) -> None:
        self.root = root
        self.artifact_path = root.joinpath(*artifact_id.split("/"))
        if write_artifact:
            self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
            self.artifact_path.write_text(payload, encoding="utf-8", newline="\n")
            digest = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
        else:
            digest = "0" * 64
        self.solution = SolutionSpec(
            solution_id=solution_id,
            game_spec=game_spec,
            solver_name=solver_name,
            solver_version=solver_version,
            action_tree_version=action_tree_version,
            quality=quality,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256 or digest,
        )
        self.catalog_path = root / "catalog.json"
        catalog = SolutionCatalog(
            catalog_id="unit-test",
            catalog_version="0.1.0",
            solutions=(self.solution,),
        )
        self.catalog_path.write_text(
            json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def verify(self):
        return verify_solution_artifact(self.solution, root=self.root)


def _rehash(case: "_Case") -> SolutionSpec:
    """Re-point a case's catalog entry at the current bytes on disk."""

    return SolutionSpec(
        solution_id=case.solution.solution_id,
        game_spec=case.solution.game_spec,
        solver_name=case.solution.solver_name,
        solver_version=case.solution.solver_version,
        action_tree_version=case.solution.action_tree_version,
        quality=case.solution.quality,
        artifact_id=case.solution.artifact_id,
        artifact_sha256=hashlib.sha256(case.artifact_path.read_bytes()).hexdigest(),
    )


class StrategyArtifactContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.game_spec = _demo_game_spec()
        cls.fingerprint = cls.game_spec.fingerprint

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def _case(self, mutate: Callable[[dict[str, Any]], None] | None = None, **kwargs):
        artifact = _base_artifact(self.fingerprint)
        if mutate is not None:
            mutate(artifact)
        return _Case(self.root, game_spec=self.game_spec, artifact=artifact, **kwargs)

    def _reject(self, pattern: str, mutate=None, **kwargs) -> None:
        case = self._case(mutate, **kwargs)
        with self.assertRaisesRegex(ArtifactValidationError, pattern):
            case.verify()

    # -- happy path --------------------------------------------------------

    def test_verifies_a_well_formed_artifact(self) -> None:
        verification = self._case().verify()
        self.assertEqual(verification.solution_id, SOLUTION_ID)
        self.assertEqual(verification.game_spec_fingerprint, self.fingerprint)
        self.assertEqual(verification.action_tree_version, ACTION_TREE_VERSION)
        self.assertEqual(verification.quality, SolutionQuality.TEST_ONLY)
        self.assertTrue(verification.artifact.ev_present)
        self.assertEqual(verification.artifact.action_ids, ("bet_2bb", "check"))
        payload = verification.to_dict()
        self.assertTrue(payload["strategy_content_verified"])
        self.assertEqual(payload["combo_count"], 2)
        self.assertEqual(payload["ev_unit"], "bb")
        self.assertEqual(payload["ev_semantics"], "action_ev_from_node")

    def test_demo_node_fingerprint_is_pinned(self) -> None:
        self.assertEqual(self.fingerprint, DEMO_FINGERPRINT)

    def test_ev_may_be_absent_when_absent_everywhere(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            for entry in artifact["entries"]:
                for policy in entry["policies"]:
                    policy["ev"] = None

        verification = self._case(mutate).verify()
        self.assertFalse(verification.artifact.ev_present)
        evidence = build_strategy_evidence(verification, combo="AhKh")
        self.assertTrue(all(item.ev is None for item in evidence.actions))

    # -- integrity and identity -------------------------------------------

    def test_rejects_hash_mismatch(self) -> None:
        self._reject("sha-256 does not match", artifact_sha256="c" * 64)

    def test_rejects_edited_artifact_after_hashing(self) -> None:
        case = self._case()
        payload = json.loads(case.artifact_path.read_text(encoding="utf-8"))
        payload["entries"][0]["policies"][0]["probability"] = "0.76"
        payload["entries"][0]["policies"][1]["probability"] = "0.24"
        case.artifact_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(ArtifactValidationError, "sha-256 does not match"):
            case.verify()

    def test_rejects_solution_id_mismatch(self) -> None:
        self._reject(
            "solution_id does not match",
            lambda artifact: artifact.update(solution_id="test-only.other"),
        )

    def test_rejects_game_spec_fingerprint_mismatch(self) -> None:
        self._reject(
            "game_spec_fingerprint does not match",
            lambda artifact: artifact.update(game_spec_fingerprint="d" * 64),
        )

    def test_rejects_action_tree_version_mismatch(self) -> None:
        self._reject(
            "action_tree_version does not match",
            lambda artifact: artifact.update(action_tree_version="other/9.9.9"),
        )

    def test_rejects_solver_identity_mismatch(self) -> None:
        self._reject(
            "solver name does not match",
            lambda artifact: artifact["provenance"].update(solver_name="other.solver"),
        )
        self._reject(
            "solver version does not match",
            lambda artifact: artifact["provenance"].update(solver_version="9.9.9"),
        )

    def test_rejects_quality_label_upgrade(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            artifact["provenance"]["quality"] = "verified"
            artifact["provenance"]["quality_report_id"] = "qa-1"

        self._reject("quality label does not match", mutate)

    def test_rejects_verified_artifact_inside_a_fixture_directory(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            artifact["provenance"]["quality"] = "verified"
            artifact["provenance"]["quality_report_id"] = "qa-1"

        self._reject(
            "cannot claim 'verified'",
            mutate,
            quality=SolutionQuality.VERIFIED,
        )

    def test_rejects_verified_without_a_quality_report(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            artifact["provenance"]["quality"] = "verified"

        self._reject(
            "require provenance.quality_report_id",
            mutate,
            quality=SolutionQuality.VERIFIED,
            artifact_id="strategy/node.json",
        )

    # -- schema ------------------------------------------------------------

    def test_rejects_unknown_schema_version(self) -> None:
        self._reject(
            "unsupported artifact schema version",
            lambda artifact: artifact.update(schema_version="strategy-artifact/2.0.0"),
        )

    def test_rejects_unknown_and_missing_top_level_keys(self) -> None:
        self._reject(
            "unknown=..extra",
            lambda artifact: artifact.update(extra=1),
        )
        self._reject(
            "missing=..node_id",
            lambda artifact: artifact.pop("node_id"),
        )

    def test_rejects_unknown_ev_unit_and_semantics(self) -> None:
        self._reject(
            "unsupported ev_unit",
            lambda artifact: artifact.update(ev_unit="chips"),
        )
        self._reject(
            "unsupported ev_semantics",
            lambda artifact: artifact.update(ev_semantics="whole_hand_net"),
        )

    def test_rejects_missing_provenance_fields(self) -> None:
        self._reject(
            "provenance keys do not match",
            lambda artifact: artifact["provenance"].pop("license"),
        )
        self._reject(
            "generated_at must be an RFC3339 UTC timestamp",
            lambda artifact: artifact["provenance"].update(generated_at="2026-08-18"),
        )

    # -- actions -----------------------------------------------------------

    def test_rejects_a_single_action_node(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            artifact["actions"] = artifact["actions"][:1]
            for entry in artifact["entries"]:
                entry["policies"] = entry["policies"][:1]
                entry["policies"][0]["probability"] = "1"

        self._reject("at least two actions", mutate)

    def test_rejects_duplicate_and_unsorted_actions(self) -> None:
        def duplicate(artifact: dict[str, Any]) -> None:
            artifact["actions"][1]["action_id"] = "bet_2bb"

        self._reject("action ids must be unique", duplicate)

        def unsorted_actions(artifact: dict[str, Any]) -> None:
            artifact["actions"].reverse()

        self._reject("must be sorted by action_id", unsorted_actions)

    def test_rejects_missing_and_illegal_action_sizes(self) -> None:
        self._reject(
            "requires size_bb",
            lambda artifact: artifact["actions"][0].update(size_bb=None),
        )
        self._reject(
            "cannot declare size_bb",
            lambda artifact: artifact["actions"][1].update(size_bb="1"),
        )
        self._reject(
            "positive finite size_bb",
            lambda artifact: artifact["actions"][0].update(size_bb="0"),
        )

    def test_rejects_unknown_action_kind(self) -> None:
        self._reject(
            "unknown action kind",
            lambda artifact: artifact["actions"][1].update(kind="shove"),
        )

    def test_rejects_duplicate_sized_actions(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            artifact["actions"][1] = {
                "action_id": "bet_2bb_again",
                "kind": "bet",
                "size_bb": "2",
            }
            for entry in artifact["entries"]:
                entry["policies"][1]["action_id"] = "bet_2bb_again"

        self._reject("distinct sizes per kind", mutate)

    # -- combos ------------------------------------------------------------

    def test_rejects_illegal_duplicate_and_noncanonical_combos(self) -> None:
        self._reject(
            "contains an invalid card",
            lambda artifact: artifact["entries"][0].update(combo="AxKh"),
        )
        self._reject(
            "repeats the same card",
            lambda artifact: artifact["entries"][0].update(combo="AhAh"),
        )
        self._reject(
            "is not canonical",
            lambda artifact: artifact["entries"][0].update(combo="KhAh"),
        )
        self._reject(
            "must be exactly two cards",
            lambda artifact: artifact["entries"][0].update(combo="AhKhQh"),
        )

    def test_rejects_duplicate_and_unsorted_combo_entries(self) -> None:
        def duplicate(artifact: dict[str, Any]) -> None:
            artifact["entries"][1] = deepcopy(artifact["entries"][0])

        self._reject("combo entries must be unique", duplicate)

        def unsorted_entries(artifact: dict[str, Any]) -> None:
            artifact["entries"].reverse()

        self._reject("must be sorted by combo", unsorted_entries)

    def test_rejects_a_combo_that_uses_a_board_card(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["combo"] = "AhTs"
            artifact["entries"].sort(key=lambda item: item["combo"])

        self._reject("conflicts with the board card", mutate)

    def test_rejects_illegal_weights(self) -> None:
        self._reject(
            "greater than 0 and at most 1",
            lambda artifact: artifact["entries"][0].update(weight="0"),
        )
        self._reject(
            "greater than 0 and at most 1",
            lambda artifact: artifact["entries"][0].update(weight="1.5"),
        )

    def test_rejects_empty_entry_list(self) -> None:
        self._reject(
            "at least one combo entry",
            lambda artifact: artifact.update(entries=[]),
        )

    # -- policies ----------------------------------------------------------

    def test_rejects_missing_duplicate_and_undeclared_actions_per_combo(self) -> None:
        self._reject(
            "missing declared action",
            lambda artifact: artifact["entries"][0]["policies"].pop(),
        )

        def duplicate(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["policies"][1]["action_id"] = "bet_2bb"

        self._reject("repeats an action", duplicate)

        def undeclared(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["policies"][1]["action_id"] = "raise_9bb"

        self._reject("undeclared action", undeclared)

        def unsorted_policies(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["policies"].reverse()

        self._reject("policies must be sorted by action_id", unsorted_policies)

    def test_rejects_probabilities_outside_the_unit_interval(self) -> None:
        def negative(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["policies"][0]["probability"] = "-0.1"
            artifact["entries"][0]["policies"][1]["probability"] = "1.1"

        self._reject("must not be negative", negative)

        def negative_zero(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["policies"][0]["probability"] = "-0"
            artifact["entries"][0]["policies"][1]["probability"] = "1"

        self._reject("must not be negative", negative_zero)

        def above_one(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["policies"][0]["probability"] = "1.5"
            artifact["entries"][0]["policies"][1]["probability"] = "0"

        self._reject("probability must be within", above_one)

    def test_rejects_magnitudes_that_would_round_against_the_decimal_context(self) -> None:
        """A 40-digit EV must not come back as a different 40-digit EV."""

        def mutate(artifact: dict[str, Any]) -> None:
            for entry in artifact["entries"]:
                for policy in entry["policies"]:
                    policy["ev"] = "1234567890123456789012345678901234567890"

        self._reject("exceeds the versioned magnitude", mutate)
        self._reject(
            "exceeds the versioned magnitude",
            lambda artifact: artifact["actions"][0].update(size_bb="9" * 40),
        )

    def test_round_trips_the_largest_allowed_magnitude_exactly(self) -> None:
        largest = "123456789012.345678"

        def mutate(artifact: dict[str, Any]) -> None:
            for entry in artifact["entries"]:
                for policy in entry["policies"]:
                    policy["ev"] = largest

        verification = self._case(mutate).verify()
        evidence = build_strategy_evidence(verification, combo="AhKh")
        self.assertEqual([item.to_dict()["ev"] for item in evidence.actions], [largest, largest])
        aggregate = build_strategy_evidence(verification)
        self.assertEqual([item.to_dict()["ev"] for item in aggregate.actions], [largest, largest])

    def test_rejects_structurally_absurd_artifacts(self) -> None:
        def too_many_actions(artifact: dict[str, Any]) -> None:
            artifact["actions"] = [
                {"action_id": f"bet_{index:04d}", "kind": "bet", "size_bb": str(index + 1)}
                for index in range(strategy_artifacts.MAX_ACTIONS + 1)
            ]

        self._reject("at most 64 actions", too_many_actions)

    def test_rejects_duplicate_json_keys(self) -> None:
        case = self._case()
        raw = case.artifact_path.read_text(encoding="utf-8")
        raw = raw.replace(
            '"node_id":', '"node_id": "cash.hu.100bb.flop.btn-cbet-vs-bb-check",\n  "node_id":', 1
        )
        case.artifact_path.write_text(raw, encoding="utf-8", newline="")
        case.solution = _rehash(case)
        with self.assertRaisesRegex(ArtifactValidationError, "repeats the key"):
            case.verify()

    def test_rejects_a_lone_surrogate_that_cannot_be_re_encoded(self) -> None:
        """A \\ud800 escape is legal JSON inside a valid UTF-8 file, but cannot be echoed."""

        case = self._case()
        raw = case.artifact_path.read_text(encoding="utf-8").replace(
            '"cash.hu.100bb.flop.btn-cbet-vs-bb-check"',
            '"cash.hu.\\ud800.pwn"',
            1,
        )
        case.artifact_path.write_text(raw, encoding="utf-8", newline="")
        case.solution = _rehash(case)
        with self.assertRaisesRegex(ArtifactValidationError, "printable characters"):
            case.verify()

    def test_rejects_json_nested_too_deeply(self) -> None:
        """CPython 3.12 raised json's nesting limit, so pick a depth past all of them.

        The point of the test is that runaway nesting fails closed rather than
        escaping as an unhandled RecursionError; the exact depth at which the
        parser gives up is an interpreter detail.
        """

        case = self._case()
        case.artifact_path.write_bytes(b"[" * 50_000 + b"]" * 50_000)
        case.solution = _rehash(case)
        with self.assertRaisesRegex(ArtifactValidationError, "nested too deeply"):
            case.verify()

    def test_rejects_shallower_nesting_that_the_parser_accepts(self) -> None:
        """Below the recursion limit the document parses — and is still refused."""

        case = self._case()
        case.artifact_path.write_bytes(b"[" * 200 + b"]" * 200)
        case.solution = _rehash(case)
        with self.assertRaisesRegex(ArtifactValidationError, "must be an object"):
            case.verify()

    def test_rejects_probabilities_that_do_not_sum_to_one(self) -> None:
        self._reject(
            "probabilities sum to",
            lambda artifact: artifact["entries"][0]["policies"][0].update(
                probability="0.5"
            ),
        )

    def test_accepts_rounding_inside_the_versioned_tolerance(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["policies"][0]["probability"] = "0.750001"
            artifact["entries"][0]["policies"][1]["probability"] = "0.25"

        verification = self._case(mutate).verify()
        self.assertEqual(verification.artifact.entries[0].policies[0].probability, Decimal("0.750001"))

    def test_rejects_nonfinite_and_nonstring_numbers(self) -> None:
        self._reject(
            "must be a decimal string",
            lambda artifact: artifact["entries"][0]["policies"][0].update(
                probability=0.75
            ),
        )
        self._reject(
            "not a plain decimal string",
            lambda artifact: artifact["entries"][0]["policies"][0].update(
                probability="NaN"
            ),
        )
        self._reject(
            "not a plain decimal string",
            lambda artifact: artifact["entries"][0]["policies"][0].update(ev="Infinity"),
        )
        self._reject(
            "not a plain decimal string",
            lambda artifact: artifact["entries"][0]["policies"][0].update(ev="1e3"),
        )

    def test_rejects_a_json_nan_literal(self) -> None:
        case = self._case()
        raw = case.artifact_path.read_text(encoding="utf-8").replace('"0.75"', "NaN", 1)
        case.artifact_path.write_bytes(raw.encode("utf-8"))
        case.solution = _rehash(case)
        with self.assertRaisesRegex(ArtifactValidationError, "must be a decimal string"):
            case.verify()

    def test_rejects_precision_beyond_the_versioned_contract(self) -> None:
        def mutate(artifact: dict[str, Any]) -> None:
            artifact["entries"][0]["policies"][0]["probability"] = "0.7500001"
            artifact["entries"][0]["policies"][1]["probability"] = "0.2499999"

        self._reject("exceeds the versioned precision", mutate)

    def test_rejects_partially_declared_ev(self) -> None:
        self._reject(
            "must declare EV for every action or for none",
            lambda artifact: artifact["entries"][0]["policies"][0].update(ev=None),
        )

        def mixed_entries(artifact: dict[str, Any]) -> None:
            for policy in artifact["entries"][0]["policies"]:
                policy["ev"] = None

        self._reject("for every action of every combo, or for none", mixed_entries)

    # -- sandbox -----------------------------------------------------------

    def test_rejects_path_traversal_and_foreign_locations(self) -> None:
        for artifact_id, pattern in (
            ("a/../../escape.json", "illegal path segment"),
            ("/etc/passwd.json", "relative path below its own directory"),
            ("fixtures\\node.json", "POSIX"),
            ("fixtures/node.txt", "must reference a .json file"),
            ("file:/fixtures/node.json", "not allowed"),
            ("", "relative path below its own directory"),
        ):
            with self.subTest(artifact_id=artifact_id):
                with self.assertRaisesRegex(ArtifactValidationError, pattern):
                    resolve_artifact_path(self.root, artifact_id)

    def test_rejects_a_symlinked_artifact(self) -> None:
        outside = self.root.parent / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        self.addCleanup(outside.unlink, True)
        link = self.root / "linked.json"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            self.skipTest("symlinks are unavailable on this platform")
        with self.assertRaisesRegex(ArtifactValidationError, "symbolic link"):
            resolve_artifact_path(self.root, "linked.json")

    def test_rejects_a_missing_artifact(self) -> None:
        case = self._case(write_artifact=False)
        with self.assertRaisesRegex(ArtifactValidationError, "artifact file not found"):
            case.verify()

    def test_rejects_an_oversized_artifact(self) -> None:
        case = self._case()
        original = strategy_artifacts.MAX_ARTIFACT_BYTES
        strategy_artifacts.MAX_ARTIFACT_BYTES = 16
        self.addCleanup(setattr, strategy_artifacts, "MAX_ARTIFACT_BYTES", original)
        with self.assertRaisesRegex(ArtifactValidationError, "byte limit"):
            case.verify()

    def test_rejects_invalid_json_and_unknown_solution_ids(self) -> None:
        case = self._case()
        case.artifact_path.write_bytes(b"{ not json")
        case.solution = _rehash(case)
        with self.assertRaisesRegex(ArtifactValidationError, "not valid JSON"):
            case.verify()

        catalog = load_solution_catalog(self._case().catalog_path)
        with self.assertRaisesRegex(ArtifactValidationError, "does not contain solution"):
            verify_catalog_artifact(catalog, "missing", root=self.root)


class StrategyQueryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_solution_catalog(COMMITTED_CATALOG)
        cls.verification = verify_catalog_artifact(
            cls.catalog, COMMITTED_SOLUTION_ID, root=COMMITTED_CATALOG.parent
        )

    def test_committed_catalog_matches_the_committed_artifact_bytes(self) -> None:
        solution = self.catalog.solutions[0]
        path = SOLUTIONS.joinpath(*solution.artifact_id.split("/"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(digest, solution.artifact_sha256)

    def test_committed_artifact_is_in_canonical_form(self) -> None:
        """serialize(load(bytes)) == bytes, so the hash is reproducible anywhere."""

        solution = self.catalog.solutions[0]
        path = SOLUTIONS.joinpath(*solution.artifact_id.split("/"))
        raw = path.read_bytes()
        artifact = strategy_artifact_from_dict(
            json.loads(raw.decode("utf-8")), game_spec=solution.game_spec
        )
        self.assertEqual(serialize_strategy_artifact(artifact), raw)

    def test_default_catalog_stays_empty(self) -> None:
        default = load_solution_catalog(SOLUTIONS / "catalog.json")
        self.assertEqual(default.solutions, ())

    def test_repository_ships_no_verified_strategy(self) -> None:
        for path in SOLUTIONS.rglob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(
                '"verified"',
                json.dumps(payload, ensure_ascii=False),
                msg=f"{path} claims verified strategy",
            )

    def test_combo_evidence_quotes_the_artifact_exactly(self) -> None:
        evidence = build_strategy_evidence(self.verification, combo="QcJc")
        payload = evidence.to_dict()
        self.assertEqual(payload["schema_version"], STRATEGY_EVIDENCE_SCHEMA_VERSION)
        self.assertEqual(payload["scope"], "combo")
        self.assertEqual(payload["combo"], "QcJc")
        self.assertEqual(payload["combo_weight"], "0.5")
        self.assertIsNone(payload["aggregation_policy_version"])
        self.assertFalse(payload["usable_for_teaching"])
        self.assertEqual(
            [(item["action_id"], item["probability"], item["ev"]) for item in payload["actions"]],
            [("bet_2bb", "0.4", "1.9"), ("bet_4_5bb", "0.35", "2"), ("check", "0.25", "1.8")],
        )

    def test_aggregate_evidence_is_a_versioned_weighted_mean(self) -> None:
        evidence = build_strategy_evidence(self.verification)
        payload = evidence.to_dict()
        self.assertEqual(payload["scope"], "artifact_entries")
        self.assertEqual(
            payload["aggregation_policy_version"], AGGREGATION_POLICY_VERSION
        )
        self.assertEqual(payload["covered_weight"], "3.5")
        probabilities = {
            item["action_id"]: Decimal(item["probability"])
            for item in payload["actions"]
        }
        # (1*0.6 + 1*0.5 + 1*0.5 + 0.5*0.4) / 3.5
        self.assertEqual(probabilities["bet_2bb"], Decimal("0.514286"))
        self.assertLess(
            abs(sum(probabilities.values()) - Decimal("1")), Decimal("0.00001")
        )

    def test_refuses_uncovered_and_malformed_combinations(self) -> None:
        with self.assertRaises(ComboNotCoveredError):
            build_strategy_evidence(self.verification, combo="9d9h")
        with self.assertRaisesRegex(ArtifactValidationError, "not canonical"):
            build_strategy_evidence(self.verification, combo="KhAh")
        with self.assertRaisesRegex(ArtifactValidationError, "invalid card"):
            build_strategy_evidence(self.verification, combo="AxKh")

    def test_canonical_combo_orders_rank_then_suit(self) -> None:
        self.assertEqual(canonical_combo("AhKh"), "AhKh")
        self.assertEqual(canonical_combo("8c8d"), "8c8d")
        with self.assertRaises(ArtifactValidationError):
            canonical_combo("8d8c")


class StrategyPipelineCLITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls._temp.name) / "cli.db"
        with SQLiteHandStore(cls.database) as store:
            HandHistoryImporter(default_registry(), store).import_text(
                "cash.txt",
                (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8"),
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, buffer.getvalue()

    def _query(self, *extra: str, catalog: Path = COMMITTED_CATALOG) -> tuple[int, dict]:
        code, output = self._run(
            [
                "gto-query",
                "pokerstars",
                "100000000001",
                "--before-action",
                "5",
                "--database",
                os.fspath(self.database),
                "--catalog",
                os.fspath(catalog),
                "--rake-model",
                "pokerstars.cash.example",
                "--rake-percent",
                "5",
                "--rake-cap-bb",
                "3",
                "--json",
                *extra,
            ]
        )
        return code, json.loads(output)

    def test_artifact_verify_command_reports_the_full_chain(self) -> None:
        code, output = self._run(
            [
                "gto-artifact-verify",
                os.fspath(COMMITTED_CATALOG),
                COMMITTED_SOLUTION_ID,
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)["verification"]
        self.assertTrue(payload["strategy_content_verified"])
        self.assertEqual(payload["quality"], "test_only")
        self.assertEqual(payload["game_spec_fingerprint"], DEMO_FINGERPRINT)

    def test_artifact_verify_command_fails_closed(self) -> None:
        code, output = self._run(
            [
                "gto-artifact-verify",
                os.fspath(COMMITTED_CATALOG),
                "test-only.missing",
                "--json",
            ]
        )
        self.assertEqual(code, 2)
        payload = json.loads(output)
        self.assertFalse(payload["strategy_content_verified"])

    def test_end_to_end_match_verify_query(self) -> None:
        code, payload = self._query("--combo", "AhKh")
        self.assertEqual(code, 0)
        self.assertEqual(payload["match"]["status"], "exact")
        self.assertTrue(payload["strategy_available"])
        self.assertEqual(payload["reason"], "verified_artifact")
        self.assertEqual(
            payload["artifact_verification"]["game_spec_fingerprint"],
            DEMO_FINGERPRINT,
        )
        evidence = payload["strategy_evidence"]
        self.assertEqual(evidence["combo"], "AhKh")
        self.assertFalse(evidence["usable_for_teaching"])
        self.assertEqual(evidence["actions"][0]["probability"], "0.5")

    def test_unsupported_match_never_returns_strategy_or_ev(self) -> None:
        code, payload = self._query(catalog=SOLUTIONS / "catalog.json")
        self.assertEqual(code, 0)
        self.assertEqual(payload["match"]["status"], "unsupported")
        self.assertFalse(payload["strategy_available"])
        self.assertIsNone(payload["strategy_evidence"])
        self.assertIsNone(payload["artifact_verification"])
        self.assertEqual(payload["reason"], "match_catalog_empty")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("probability", serialized)
        self.assertNotIn("action_ev_from_node", serialized)

    def test_missing_rake_structure_still_fails_closed(self) -> None:
        code, output = self._run(
            [
                "gto-query",
                "pokerstars",
                "100000000001",
                "--before-action",
                "5",
                "--database",
                os.fspath(self.database),
                "--catalog",
                os.fspath(COMMITTED_CATALOG),
                "--json",
            ]
        )
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertFalse(payload["strategy_available"])
        self.assertEqual(payload["reason"], "match_missing_rake_metadata")

    def test_uncovered_combo_returns_no_strategy(self) -> None:
        code, payload = self._query("--combo", "9d9h")
        self.assertEqual(code, 0)
        self.assertFalse(payload["strategy_available"])
        self.assertEqual(payload["reason"], "combo_not_covered")
        self.assertIsNone(payload["strategy_evidence"])

    def test_malformed_combo_is_a_user_error(self) -> None:
        code, payload = self._query("--combo", "KhAh")
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason"], "invalid_combo")

    def test_tampered_artifact_makes_the_query_fail_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog_payload = json.loads(COMMITTED_CATALOG.read_text(encoding="utf-8"))
            catalog_payload["solutions"][0]["artifact"]["sha256"] = "e" * 64
            catalog_path = root / "catalog.json"
            catalog_path.write_text(
                json.dumps(catalog_payload, ensure_ascii=False), encoding="utf-8"
            )
            source = SOLUTIONS / "fixtures" / "btn_flop_cbet.test_only.json"
            target = root / "fixtures" / "btn_flop_cbet.test_only.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

            code, payload = self._query(catalog=catalog_path)
        self.assertEqual(code, 2)
        self.assertFalse(payload["strategy_available"])
        self.assertEqual(payload["reason"], "artifact_verification_failed")
        self.assertIn("sha-256 does not match", payload["error"])
        self.assertIsNone(payload["strategy_evidence"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
