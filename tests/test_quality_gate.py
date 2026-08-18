from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    SolutionObjective,
    SolutionQuality,
    SolutionSpec,
    load_solution_catalog,
)
from rivermind_core.importer import HandHistoryImporter  # noqa: E402
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.quality_gate import (  # noqa: E402
    QUALITY_ATTESTATION_SCHEMA_VERSION,
    QUALITY_GATE_POLICY_VERSION,
    AttestationValidationError,
    QualityAttestationVerification,
    verify_quality_attestation,
)
from rivermind_core.solve_quality import (  # noqa: E402
    SOLVE_QUALITY_REPORT_SCHEMA_VERSION,
    ReportValidationError,
)
from rivermind_core.strategy_artifacts import (  # noqa: E402
    STRATEGY_ARTIFACT_SCHEMA_VERSION,
    ArtifactValidationError,
    verify_catalog_artifact,
)
from rivermind_core.storage import SQLiteHandStore  # noqa: E402
from rivermind_core.strategy_query import (  # noqa: E402
    TEACHING_BLOCK_NO_ATTESTATION,
    TEACHING_BLOCK_QUALITY,
    StrategyQueryError,
    build_strategy_evidence,
)


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
SOLUTIONS = PROJECT_ROOT / "solutions"

SOLUTION_ID = "gate-test.hu-cash.btn-flop-cbet"
REPORT_ID = "gate-test.report.0001"
ATTESTATION_ID = "gate-test.grant.0001"
TREE = "gate-test.flop-cbet/0.0.1"
SOLVER = "gate-test.solver"
SOLVER_VERSION = "1.2.3"
SOLVER_CONFIG = "gate-test-config"
ARTIFACT_ID = "strategy/node.json"

SOLVE_START = "2026-08-01T00:00:00Z"
SOLVE_END = "2026-08-02T00:00:00Z"
SIGNED_OWNER = "2026-08-03T00:00:00Z"
SIGNED_INDEPENDENT = "2026-08-04T00:00:00Z"
GRANTED_AT = "2026-08-05T00:00:00Z"

_RAKE_LIMIT = "Rake was modelled as a fixed 5 percent with a 3 BB cap; other schedules are out of scope."


def _hu_cash_spec():
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


def _six_max_spec(objective: SolutionObjective = SolutionObjective.CHIP_EV):
    hand = default_registry().parse(
        (FIXTURES / "pokerstars_mtt.txt").read_text(encoding="utf-8")
    )
    return extract_decision_game_spec(
        hand,
        before_action=8,
        objective=objective,
        tournament_context_id=(
            None if objective is SolutionObjective.CHIP_EV else "gate-test.mtt.1"
        ),
    )


def _artifact_document(fingerprint: str) -> dict[str, Any]:
    return {
        "schema_version": STRATEGY_ARTIFACT_SCHEMA_VERSION,
        "solution_id": SOLUTION_ID,
        "game_spec_fingerprint": fingerprint,
        "action_tree_version": TREE,
        "node_id": "gate-test.node",
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
            }
        ],
        "provenance": {
            "solver_name": SOLVER,
            "solver_version": SOLVER_VERSION,
            "solver_config_id": SOLVER_CONFIG,
            "generated_at": SOLVE_END,
            "quality": "verified",
            "quality_report_id": REPORT_ID,
            "license": "Gate test licence.",
        },
    }


def _report_document(fingerprint: str) -> dict[str, Any]:
    return {
        "schema_version": SOLVE_QUALITY_REPORT_SCHEMA_VERSION,
        "report_id": REPORT_ID,
        "solution_id": SOLUTION_ID,
        "game_spec_fingerprint": fingerprint,
        "action_tree_version": TREE,
        "solver": {
            "name": SOLVER,
            "version": SOLVER_VERSION,
            "config_id": SOLVER_CONFIG,
            "config_sha256": "1" * 64,
        },
        "claim_class": "equilibrium_approximation",
        "source": {
            "origin": "in_house",
            "provider": "RiverMind gate test",
            "license_id": "internal-test-only",
            "obtained_at": "2026-07-01T00:00:00Z",
            "display_allowed": True,
            "redistribution_allowed": False,
        },
        "solve": {
            "started_at": SOLVE_START,
            "completed_at": SOLVE_END,
            "iterations": 500000,
            "convergence_metric": "exploitability",
            "convergence_value": "0.0031",
            "convergence_unit": "bb_per_100",
            "convergence_threshold": "0.005",
            "board_abstraction": "none",
            "bet_size_abstraction": "two sizes, no translation",
            "card_isomorphism_used": False,
            "rake_model_id": "pokerstars.cash.example",
        },
        "evaluation": {
            "scope": "single_node",
            "board_sample_size": 1,
            "independent_recheck": True,
            "recheck_tool_name": "gate-test.rechecker",
            "recheck_tool_version": "0.1",
        },
        "limits": [
            "Single flop node only; nothing is proven about other boards or streets.",
            _RAKE_LIMIT,
        ],
    }


def _attestation_document(artifact_sha: str, report_sha: str) -> dict[str, Any]:
    return {
        "schema_version": QUALITY_ATTESTATION_SCHEMA_VERSION,
        "attestation_id": ATTESTATION_ID,
        "solution_id": SOLUTION_ID,
        "artifact_sha256": artifact_sha,
        "report": {"path": "report.json", "id": REPORT_ID, "sha256": report_sha},
        "granted_quality": "verified",
        "policy_version": QUALITY_GATE_POLICY_VERSION,
        "granted_at": GRANTED_AT,
        "reviewers": [
            {
                "reviewer_id": "solver.owner",
                "role": "solver_owner",
                "signed_at": SIGNED_OWNER,
                "statement_sha256": "2" * 64,
            },
            {
                "reviewer_id": "independent.reviewer",
                "role": "independent_reviewer",
                "signed_at": SIGNED_INDEPENDENT,
                "statement_sha256": "3" * 64,
            },
        ],
    }


Mutator = Callable[[dict[str, Any]], None] | None


class _Bundle:
    """A complete, mutable verified grant laid out on disk."""

    def __init__(
        self,
        root: Path,
        *,
        game_spec=None,
        artifact: Mutator = None,
        report: Mutator = None,
        attestation: Mutator = None,
        catalog_quality: SolutionQuality = SolutionQuality.VERIFIED,
        artifact_id: str = ARTIFACT_ID,
        solver_name: str = SOLVER,
        solver_version: str = SOLVER_VERSION,
        action_tree_version: str = TREE,
    ) -> None:
        self.root = root
        self.game_spec = game_spec if game_spec is not None else _hu_cash_spec()
        fingerprint = self.game_spec.fingerprint

        artifact_doc = _artifact_document(fingerprint)
        if artifact is not None:
            artifact(artifact_doc)
        self.artifact_path = root.joinpath(*artifact_id.split("/"))
        artifact_sha = _write_json(self.artifact_path, artifact_doc)

        self.solution = SolutionSpec(
            solution_id=SOLUTION_ID,
            game_spec=self.game_spec,
            solver_name=solver_name,
            solver_version=solver_version,
            action_tree_version=action_tree_version,
            quality=catalog_quality,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha,
        )
        self.catalog_path = root / "catalog.json"
        _write_json(
            self.catalog_path,
            SolutionCatalog(
                catalog_id="gate-test",
                catalog_version="0.1.0",
                solutions=(self.solution,),
            ).to_dict(),
        )

        report_doc = _report_document(fingerprint)
        # The report must name this node's actual rake schedule, or none at all.
        rake = self.game_spec.rake
        report_doc["solve"]["rake_model_id"] = None if rake is None else rake.model_id
        if report is not None:
            report(report_doc)
        self.report_path = root / "grants" / "report.json"
        report_sha = _write_json(self.report_path, report_doc)

        attestation_doc = _attestation_document(artifact_sha, report_sha)
        if attestation is not None:
            attestation(attestation_doc)
        self.attestation_path = root / "grants" / "attestation.json"
        _write_json(self.attestation_path, attestation_doc)

    def verify(self):
        return verify_quality_attestation(
            self.attestation_path,
            catalog=load_solution_catalog(self.catalog_path),
            catalog_root=self.catalog_path.parent,
        )

    def artifact_verification(self):
        return verify_catalog_artifact(
            load_solution_catalog(self.catalog_path),
            SOLUTION_ID,
            root=self.catalog_path.parent,
        )


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class QualityGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def _bundle(self, **kwargs) -> _Bundle:
        return _Bundle(self.root, **kwargs)

    def _reject(self, pattern: str, **kwargs) -> None:
        bundle = self._bundle(**kwargs)
        with self.assertRaisesRegex(
            (AttestationValidationError, ReportValidationError, ArtifactValidationError),
            pattern,
        ):
            bundle.verify()

    # -- happy path --------------------------------------------------------

    def test_a_complete_grant_passes_the_gate(self) -> None:
        grant = self._bundle().verify()
        self.assertEqual(grant.solution_id, SOLUTION_ID)
        self.assertEqual(grant.policy_version, QUALITY_GATE_POLICY_VERSION)
        self.assertEqual(grant.report.report_id, REPORT_ID)
        payload = grant.to_dict()
        self.assertTrue(payload["grant_valid"])
        self.assertEqual(payload["granted_quality"], "verified")
        self.assertEqual(payload["report"]["claim_class"], "equilibrium_approximation")
        self.assertEqual(len(payload["reviewers"]), 2)
        self.assertEqual(len(payload["report"]["limits"]), 2)

    # -- byte pinning ------------------------------------------------------

    def test_rejects_an_attestation_pinned_to_other_artifact_bytes(self) -> None:
        self._reject(
            "artifact sha-256 does not match the attestation",
            attestation=lambda doc: doc.update(artifact_sha256="a" * 64),
        )

    def test_rejects_an_attestation_pinned_to_other_report_bytes(self) -> None:
        self._reject(
            "report sha-256 does not match the attestation",
            attestation=lambda doc: doc["report"].update(sha256="b" * 64),
        )

    def test_rejects_a_report_edited_after_signing(self) -> None:
        bundle = self._bundle()
        payload = json.loads(bundle.report_path.read_text(encoding="utf-8"))
        payload["solve"]["convergence_value"] = "0.000001"
        _write_json(bundle.report_path, payload)
        with self.assertRaisesRegex(
            AttestationValidationError, "report sha-256 does not match"
        ):
            bundle.verify()

    def test_rejects_an_artifact_edited_after_signing(self) -> None:
        bundle = self._bundle()
        payload = json.loads(bundle.artifact_path.read_text(encoding="utf-8"))
        payload["entries"][0]["policies"][0]["probability"] = "0.76"
        payload["entries"][0]["policies"][1]["probability"] = "0.24"
        _write_json(bundle.artifact_path, payload)
        with self.assertRaisesRegex(ArtifactValidationError, "sha-256 does not match"):
            bundle.verify()

    # -- identity ----------------------------------------------------------

    def test_rejects_report_identity_mismatches(self) -> None:
        self._reject(
            "report solution_id does not match",
            report=lambda doc: doc.update(solution_id="gate-test.other"),
        )
        self._reject(
            "report game_spec_fingerprint does not match",
            report=lambda doc: doc.update(game_spec_fingerprint="c" * 64),
        )
        self._reject(
            "report action_tree_version does not match",
            report=lambda doc: doc.update(action_tree_version="other/9.9.9"),
        )
        self._reject(
            "report solver name does not match",
            report=lambda doc: doc["solver"].update(name="other.solver"),
        )
        self._reject(
            "report solver config id does not match",
            report=lambda doc: doc["solver"].update(config_id="other-config"),
        )

    def test_rejects_an_artifact_pointing_at_another_report(self) -> None:
        self._reject(
            "points at a different quality report",
            artifact=lambda doc: doc["provenance"].update(
                quality_report_id="gate-test.report.9999"
            ),
        )

    def test_rejects_a_report_id_that_disagrees_with_the_attestation(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["report"]["id"] = "gate-test.report.9999"

        self._reject("report id does not match the attestation", attestation=mutate)

    # -- the label must already be verified on both sides ------------------

    def test_rejects_a_grant_for_a_solution_that_is_not_labelled_verified(self) -> None:
        def artifact(doc: dict[str, Any]) -> None:
            doc["provenance"]["quality"] = "experimental"

        self._reject(
            "both already carry the 'verified' label|quality label does not match",
            artifact=artifact,
            catalog_quality=SolutionQuality.EXPERIMENTAL,
        )

    def test_a_fixture_artifact_can_never_be_blessed(self) -> None:
        self._reject(
            "cannot claim 'verified'",
            artifact_id="fixtures/node.json",
        )

    # -- convergence, licence, recheck -------------------------------------

    def test_rejects_convergence_worse_than_its_own_threshold(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["solve"]["convergence_value"] = "0.01"
            doc["solve"]["convergence_threshold"] = "0.005"

        self._reject("did not reach the report's own threshold", report=mutate)

    def test_accepts_convergence_exactly_at_the_threshold(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["solve"]["convergence_value"] = "0.005"

        self.assertTrue(self._bundle(report=mutate).verify().to_dict()["grant_valid"])

    def test_rejects_a_licence_that_forbids_display(self) -> None:
        self._reject(
            "does not permit showing this strategy",
            report=lambda doc: doc["source"].update(display_allowed=False),
        )

    def test_rejects_a_solve_nobody_rechecked(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["evaluation"]["independent_recheck"] = False
            doc["evaluation"]["recheck_tool_name"] = None
            doc["evaluation"]["recheck_tool_version"] = None

        self._reject("requires an independent recheck", report=mutate)

    def test_rejects_a_recheck_claim_with_no_tool(self) -> None:
        self._reject(
            "requires both recheck_tool_name",
            report=lambda doc: doc["evaluation"].update(recheck_tool_name=None),
        )
        self._reject(
            "implies evaluation.independent_recheck",
            report=lambda doc: doc["evaluation"].update(independent_recheck=False),
        )

    def test_rejects_the_solver_rechecking_itself(self) -> None:
        self._reject(
            "cannot be performed by the solver that produced the solve",
            report=lambda doc: doc["evaluation"].update(recheck_tool_name=SOLVER),
        )

    # -- reviewers ---------------------------------------------------------

    def test_rejects_too_few_reviewers(self) -> None:
        self._reject(
            "requires between 2 and 16 reviewers",
            attestation=lambda doc: doc.update(reviewers=doc["reviewers"][:1]),
        )

    def test_rejects_a_solver_owner_blessing_their_own_solve(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["reviewers"][1]["role"] = "solver_owner"

        self._reject("at least 1 independent_reviewer", attestation=mutate)

    def test_rejects_the_same_reviewer_signing_twice(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["reviewers"][1]["reviewer_id"] = doc["reviewers"][0]["reviewer_id"]

        self._reject("may sign only once", attestation=mutate)

    def test_rejects_signatures_outside_the_solve_and_grant_window(self) -> None:
        self._reject(
            "signed after the grant timestamp",
            attestation=lambda doc: doc["reviewers"][0].update(
                signed_at="2026-09-01T00:00:00Z"
            ),
        )
        self._reject(
            "signed before the solve finished",
            attestation=lambda doc: doc["reviewers"][0].update(
                signed_at="2026-07-15T00:00:00Z"
            ),
        )

    def test_rejects_a_grant_that_predates_its_solve(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["granted_at"] = "2026-08-01T12:00:00Z"
            for reviewer in doc["reviewers"]:
                reviewer["signed_at"] = "2026-08-01T06:00:00Z"

        self._reject("predates the solve|signed before the solve", attestation=mutate)

    # -- schema and policy -------------------------------------------------

    def test_rejects_granting_anything_other_than_verified(self) -> None:
        self._reject(
            "only grants 'verified'",
            attestation=lambda doc: doc.update(granted_quality="fast_approx"),
        )

    def test_rejects_an_unknown_policy_version(self) -> None:
        self._reject(
            "unsupported quality gate policy",
            attestation=lambda doc: doc.update(policy_version="quality-gate-policy/9.9.9"),
        )

    def test_rejects_unknown_schema_versions(self) -> None:
        self._reject(
            "unsupported schema version",
            attestation=lambda doc: doc.update(schema_version="quality-attestation/2.0.0"),
        )
        self._reject(
            "unsupported schema version",
            report=lambda doc: doc.update(schema_version="solve-quality-report/2.0.0"),
        )

    def test_rejects_unknown_and_missing_keys(self) -> None:
        self._reject(
            "unknown=..extra",
            attestation=lambda doc: doc.update(extra=1),
        )
        self._reject(
            "missing=..limits",
            report=lambda doc: doc.pop("limits"),
        )

    def test_rejects_a_report_that_claims_no_limits(self) -> None:
        self._reject(
            "at least one explicit limit",
            report=lambda doc: doc.update(limits=[]),
        )
        self._reject(
            "limits must not repeat",
            report=lambda doc: doc.update(limits=[doc["limits"][0], doc["limits"][0]]),
        )

    def test_rejects_a_solve_that_finished_before_it_started(self) -> None:
        self._reject(
            "completed_at precedes",
            report=lambda doc: doc["solve"].update(completed_at="2026-07-01T00:00:00Z"),
        )

    def test_rejects_a_report_path_that_escapes_the_grant_directory(self) -> None:
        self._reject(
            "illegal path segment",
            attestation=lambda doc: doc["report"].update(path="a/../../report.json"),
        )

    def test_rejects_duplicate_json_keys_in_a_grant(self) -> None:
        bundle = self._bundle()
        raw = bundle.attestation_path.read_text(encoding="utf-8").replace(
            '"granted_quality": "verified"',
            '"granted_quality": "fast_approx",\n  "granted_quality": "verified"',
            1,
        )
        bundle.attestation_path.write_text(raw, encoding="utf-8", newline="")
        with self.assertRaisesRegex(AttestationValidationError, "repeats the key"):
            bundle.verify()

    # -- claim class -------------------------------------------------------

    def test_rejects_an_equilibrium_claim_at_a_six_handed_node(self) -> None:
        self._reject(
            "equilibrium_approximation requires a two-player node",
            game_spec=_six_max_spec(),
        )

    def test_rejects_an_equilibrium_claim_under_icm(self) -> None:
        self._reject(
            "requires a two-player node|requires the chip_ev objective",
            game_spec=_six_max_spec(SolutionObjective.ICM),
        )

    def test_accepts_an_empirical_claim_at_a_six_handed_node(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["claim_class"] = "empirical_quality"

        grant = self._bundle(game_spec=_six_max_spec(), report=mutate).verify()
        self.assertEqual(grant.report.claim_class.value, "empirical_quality")

    def test_rejects_a_claim_measured_by_a_training_diagnostic(self) -> None:
        """average_regret tracks training progress, not distance from equilibrium."""

        self._reject(
            "measures training progress",
            report=lambda doc: doc["solve"].update(convergence_metric="average_regret"),
        )

    # -- rake scope --------------------------------------------------------

    def test_rejects_a_solve_that_modelled_a_different_rake(self) -> None:
        self._reject(
            "modelled a different rake schedule",
            report=lambda doc: doc["solve"].update(rake_model_id="some.other.rake"),
        )

    def test_rejects_a_solve_that_is_silent_about_a_raked_node(self) -> None:
        self._reject(
            "must say which rake",
            report=lambda doc: doc["solve"].update(rake_model_id=None),
        )

    def test_rejects_a_rake_claim_on_an_unraked_node(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["solve"]["rake_model_id"] = "pokerstars.cash.example"

        self._reject(
            "but this node has no rake",
            game_spec=_six_max_spec(),
            report=mutate,
        )

    # -- clocks ------------------------------------------------------------

    def test_rejects_an_artifact_generated_outside_the_solve_window(self) -> None:
        self._reject(
            "falls outside the solve-to-grant window",
            artifact=lambda doc: doc["provenance"].update(
                generated_at="1971-01-01T00:00:00Z"
            ),
        )
        self._reject(
            "falls outside the solve-to-grant window",
            artifact=lambda doc: doc["provenance"].update(
                generated_at="2099-01-01T00:00:00Z"
            ),
        )

    def test_rejects_a_licence_obtained_after_the_solve(self) -> None:
        self._reject(
            "you cannot solve with data or a licence you did not have yet",
            report=lambda doc: doc["source"].update(
                obtained_at="2099-01-01T00:00:00Z"
            ),
        )

    def test_rejects_a_grant_dated_in_the_future(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["granted_at"] = "2999-01-01T00:00:00Z"

        self._reject("dated in the future", attestation=mutate)

    # -- policy ceilings ---------------------------------------------------

    def test_rejects_convergence_worse_than_the_policy_ceiling(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["solve"]["convergence_value"] = "5"
            doc["solve"]["convergence_threshold"] = "10"

        self._reject("worse than the policy ceiling", report=mutate)

    def test_rejects_a_self_declared_threshold_looser_than_policy(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["solve"]["convergence_threshold"] = "999999"

        self._reject("looser than the policy ceiling", report=mutate)

    def test_rejects_reviewer_ids_that_differ_only_in_case(self) -> None:
        def mutate(doc: dict[str, Any]) -> None:
            doc["reviewers"][1]["reviewer_id"] = doc["reviewers"][0][
                "reviewer_id"
            ].upper()

        self._reject("compared case-insensitively", attestation=mutate)

    def test_a_hand_built_verification_cannot_forge_a_grant(self) -> None:
        """The dataclass re-asserts the gate's invariants for library callers."""

        bundle = self._bundle()
        grant = bundle.verify()
        with self.assertRaisesRegex(
            AttestationValidationError, "does not pin the verified artifact bytes"
        ):
            QualityAttestationVerification(
                attestation=grant.attestation,
                report=grant.report,
                verification=_Bundle(
                    self.root / "other",
                    artifact=lambda doc: doc["entries"][0]["policies"][0].update(
                        probability="0.5", ev="2.4"
                    )
                    or doc["entries"][0]["policies"][1].update(probability="0.5"),
                ).artifact_verification(),
                policy_version=QUALITY_GATE_POLICY_VERSION,
            )


class TeachingUnlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.bundle = _Bundle(Path(self._temp.name))

    def test_verified_bytes_alone_are_not_teachable(self) -> None:
        evidence = build_strategy_evidence(
            self.bundle.artifact_verification(), combo="AhKh"
        )
        self.assertEqual(evidence.quality, SolutionQuality.VERIFIED)
        self.assertFalse(evidence.usable_for_teaching)
        self.assertEqual(evidence.teaching_block_reason, TEACHING_BLOCK_NO_ATTESTATION)
        self.assertIsNone(evidence.to_dict()["attestation_id"])

    def test_a_signed_grant_unlocks_teaching(self) -> None:
        evidence = build_strategy_evidence(
            self.bundle.artifact_verification(),
            combo="AhKh",
            attestation=self.bundle.verify(),
        )
        self.assertTrue(evidence.usable_for_teaching)
        self.assertIsNone(evidence.teaching_block_reason)
        self.assertEqual(evidence.to_dict()["attestation_id"], ATTESTATION_ID)

    def test_a_grant_for_other_bytes_is_refused(self) -> None:
        grant = self.bundle.verify()
        other = _Bundle(
            Path(self._temp.name) / "other",
            artifact=lambda doc: doc["entries"][0]["policies"][0].update(
                probability="0.5", ev="2.4"
            )
            or doc["entries"][0]["policies"][1].update(probability="0.5"),
        )
        with self.assertRaisesRegex(StrategyQueryError, "different artifact bytes"):
            build_strategy_evidence(
                other.artifact_verification(), combo="AhKh", attestation=grant
            )

    def test_the_committed_test_only_slice_is_blocked_on_quality(self) -> None:
        catalog_path = SOLUTIONS / "catalog.test_only.json"
        verification = verify_catalog_artifact(
            load_solution_catalog(catalog_path),
            "test-only.pokerstars-cash.btn-flop-cbet",
            root=catalog_path.parent,
        )
        evidence = build_strategy_evidence(verification, combo="AhKh")
        self.assertFalse(evidence.usable_for_teaching)
        self.assertEqual(evidence.teaching_block_reason, TEACHING_BLOCK_QUALITY)


class QualityGateCLITest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.bundle = _Bundle(self.root)

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, buffer.getvalue()

    def test_quality_verify_command_reports_the_grant(self) -> None:
        code, output = self._run(
            [
                "gto-quality-verify",
                os.fspath(self.bundle.attestation_path),
                "--catalog",
                os.fspath(self.bundle.catalog_path),
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        grant = json.loads(output)["grant"]
        self.assertTrue(grant["grant_valid"])
        self.assertEqual(grant["policy_version"], QUALITY_GATE_POLICY_VERSION)
        self.assertEqual(grant["report"]["source"]["license_id"], "internal-test-only")

    def test_quality_verify_command_fails_closed(self) -> None:
        payload = json.loads(self.bundle.attestation_path.read_text(encoding="utf-8"))
        payload["reviewers"] = payload["reviewers"][:1]
        _write_json(self.bundle.attestation_path, payload)
        code, output = self._run(
            [
                "gto-quality-verify",
                os.fspath(self.bundle.attestation_path),
                "--catalog",
                os.fspath(self.bundle.catalog_path),
                "--json",
            ]
        )
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output)["grant_valid"])

    def _package(self, draft: Path, *extra: str) -> tuple[int, dict]:
        code, output = self._run(
            [
                "gto-artifact-package",
                os.fspath(draft),
                "--catalog",
                os.fspath(self.bundle.catalog_path),
                "--json",
                *extra,
            ]
        )
        return code, json.loads(output)

    def test_packaging_is_lossless_for_formatting_alone(self) -> None:
        """A reformatted, differently-spelled draft must land on the same hash."""

        draft = self.root / "draft.json"
        document = json.loads(self.bundle.artifact_path.read_text(encoding="utf-8"))
        document["entries"][0]["policies"][0]["ev"] = "2.50"
        draft.write_text(json.dumps(document), encoding="utf-8")

        code, result = self._package(draft)
        self.assertEqual(code, 0)
        self.assertFalse(result["draft_was_canonical"])
        self.assertTrue(result["sha256_matches_catalog"])
        self.assertFalse(result["written"])
        self.assertEqual(result["quality"], "verified")

    def test_packaging_a_changed_strategy_requires_a_new_hash(self) -> None:
        draft = self.root / "draft.json"
        document = json.loads(self.bundle.artifact_path.read_text(encoding="utf-8"))
        document["entries"][0]["policies"][0]["probability"] = "0.6"
        document["entries"][0]["policies"][1]["probability"] = "0.4"
        draft.write_text(json.dumps(document, indent=2), encoding="utf-8")

        code, result = self._package(draft)
        self.assertEqual(code, 0)
        self.assertFalse(result["sha256_matches_catalog"])
        self.assertFalse(result["written"])

        code, result = self._package(draft, "--write", "--update-catalog")
        self.assertEqual(code, 0)
        self.assertTrue(result["written"])
        self.assertTrue(result["catalog_updated"])

        verification = verify_catalog_artifact(
            load_solution_catalog(self.bundle.catalog_path),
            SOLUTION_ID,
            root=self.bundle.catalog_path.parent,
        )
        self.assertEqual(verification.artifact_sha256, result["canonical_sha256"])
        self.assertEqual(
            verification.artifact.entries[0].policies[0].probability, Decimal("0.6")
        )

    def test_repackaging_invalidates_an_existing_grant(self) -> None:
        """Re-hashing the artifact must break the signature that pinned it."""

        self.assertTrue(self.bundle.verify().to_dict()["grant_valid"])
        draft = self.root / "draft.json"
        document = json.loads(self.bundle.artifact_path.read_text(encoding="utf-8"))
        document["entries"][0]["policies"][0]["probability"] = "0.6"
        document["entries"][0]["policies"][1]["probability"] = "0.4"
        draft.write_text(json.dumps(document), encoding="utf-8")
        self._package(draft, "--write", "--update-catalog")

        with self.assertRaisesRegex(
            AttestationValidationError, "artifact sha-256 does not match"
        ):
            self.bundle.verify()

    def test_package_rejects_a_draft_the_catalog_does_not_know(self) -> None:
        draft = self.root / "draft.json"
        document = json.loads(self.bundle.artifact_path.read_text(encoding="utf-8"))
        document["solution_id"] = "gate-test.unknown"
        draft.write_text(json.dumps(document), encoding="utf-8")
        code, _ = self._run(
            [
                "gto-artifact-package",
                os.fspath(draft),
                "--catalog",
                os.fspath(self.bundle.catalog_path),
                "--json",
            ]
        )
        self.assertEqual(code, 2)

    def test_package_refuses_to_record_a_hash_it_did_not_write(self) -> None:
        code, _ = self._run(
            [
                "gto-artifact-package",
                os.fspath(self.bundle.artifact_path),
                "--catalog",
                os.fspath(self.bundle.catalog_path),
                "--update-catalog",
                "--json",
            ]
        )
        self.assertEqual(code, 2)

    def test_package_refuses_to_clobber_the_catalog(self) -> None:
        draft = self.root / "clobber-draft.json"
        draft.write_bytes(self.bundle.artifact_path.read_bytes())
        catalog = json.loads(self.bundle.catalog_path.read_text(encoding="utf-8"))
        catalog["solutions"][0]["artifact"]["id"] = "catalog.json"
        _write_json(self.bundle.catalog_path, catalog)
        original = self.bundle.catalog_path.read_bytes()

        buffer = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(errors):
            code = main(
                [
                    "gto-artifact-package",
                    os.fspath(draft),
                    "--catalog",
                    os.fspath(self.bundle.catalog_path),
                    "--write",
                    "--update-catalog",
                    "--json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("points at the catalog file itself", errors.getvalue())
        self.assertEqual(self.bundle.catalog_path.read_bytes(), original)

    def test_package_refuses_to_clobber_a_foreign_json_file(self) -> None:
        bundle = _Bundle(self.root / "foreign", artifact_id="grants/report.json")
        draft = self.root / "foreign-draft.json"
        draft.write_bytes(bundle.artifact_path.read_bytes())
        report_before = bundle.report_path.read_bytes()
        code, _ = self._run(
            [
                "gto-artifact-package",
                os.fspath(draft),
                "--catalog",
                os.fspath(bundle.catalog_path),
                "--write",
                "--json",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(bundle.report_path.read_bytes(), report_before)

    def test_package_survives_malformed_drafts(self) -> None:
        cases = {
            "deep.json": b"[" * 3000 + b"]" * 3000,
            "list.json": b"[1, 2, 3]",
            "binary.json": b"\xff\xfe\x00",
        }
        for name, payload in cases.items():
            with self.subTest(draft=name):
                draft = self.root / name
                draft.write_bytes(payload)
                code, _ = self._run(
                    [
                        "gto-artifact-package",
                        os.fspath(draft),
                        "--catalog",
                        os.fspath(self.bundle.catalog_path),
                        "--json",
                    ]
                )
                self.assertEqual(code, 2)

    def test_quality_verify_survives_a_malformed_catalog(self) -> None:
        broken = self.root / "deep-catalog.json"
        broken.write_bytes(b"[" * 3000 + b"]" * 3000)
        code, _ = self._run(
            [
                "gto-quality-verify",
                os.fspath(self.bundle.attestation_path),
                "--catalog",
                os.fspath(broken),
                "--json",
            ]
        )
        self.assertEqual(code, 2)

    def test_package_grants_no_quality_label(self) -> None:
        code, output = self._run(
            [
                "gto-artifact-package",
                os.fspath(self.bundle.artifact_path),
                "--catalog",
                os.fspath(self.bundle.catalog_path),
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("grants no quality label", json.loads(output)["boundary"])


class QueryWithGrantCLITest(unittest.TestCase):
    """match -> artifact verify -> quality gate -> teachable strategy facts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._db_temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls._db_temp.name) / "query.db"
        with SQLiteHandStore(cls.database) as store:
            HandHistoryImporter(default_registry(), store).import_text(
                "cash.txt",
                (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8"),
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._db_temp.cleanup()

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.bundle = _Bundle(Path(self._temp.name))

    def _query(self, *extra: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "gto-query",
                    "pokerstars",
                    "100000000001",
                    "--before-action",
                    "5",
                    "--database",
                    os.fspath(self.database),
                    "--catalog",
                    os.fspath(self.bundle.catalog_path),
                    "--rake-model",
                    "pokerstars.cash.example",
                    "--rake-percent",
                    "5",
                    "--rake-cap-bb",
                    "3",
                    "--combo",
                    "AhKh",
                    "--json",
                    *extra,
                ]
            )
        return code, json.loads(buffer.getvalue())

    def test_without_a_grant_the_facts_are_returned_but_not_teachable(self) -> None:
        code, payload = self._query()
        self.assertEqual(code, 0)
        self.assertEqual(payload["match"]["status"], "exact")
        self.assertTrue(payload["strategy_available"])
        self.assertIsNone(payload["quality_grant"])
        evidence = payload["strategy_evidence"]
        self.assertEqual(evidence["quality"], "verified")
        self.assertFalse(evidence["usable_for_teaching"])
        self.assertEqual(evidence["teaching_block_reason"], "no_quality_attestation")

    def test_a_valid_grant_makes_the_same_facts_teachable(self) -> None:
        code, payload = self._query(
            "--attestation", os.fspath(self.bundle.attestation_path)
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["strategy_available"])
        self.assertTrue(payload["quality_grant"]["grant_valid"])
        evidence = payload["strategy_evidence"]
        self.assertTrue(evidence["usable_for_teaching"])
        self.assertIsNone(evidence["teaching_block_reason"])
        self.assertEqual(evidence["attestation_id"], ATTESTATION_ID)

    def test_a_grant_for_another_node_is_named_correctly(self) -> None:
        """The refusal reason must say 'attestation', not 'combo'."""

        other = _Bundle(Path(self._temp.name) / "other", game_spec=_six_max_spec())
        merged = json.loads(self.bundle.catalog_path.read_text(encoding="utf-8"))
        foreign = json.loads(other.catalog_path.read_text(encoding="utf-8"))
        foreign["solutions"][0]["solution_id"] = "gate-test.other-node"
        foreign["solutions"][0]["artifact"]["id"] = "other/node.json"
        merged["solutions"].extend(foreign["solutions"])
        _write_json(self.bundle.catalog_path, merged)
        target = self.bundle.catalog_path.parent / "other" / "node.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(other.artifact_path.read_bytes())

        code, result = self._query(
            "--attestation", os.fspath(other.attestation_path)
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["reason"], "attestation_verification_failed")
        self.assertIsNone(result["strategy_evidence"])

    def test_a_broken_grant_blocks_the_query_loudly(self) -> None:
        payload = json.loads(self.bundle.attestation_path.read_text(encoding="utf-8"))
        payload["artifact_sha256"] = "f" * 64
        _write_json(self.bundle.attestation_path, payload)
        code, result = self._query(
            "--attestation", os.fspath(self.bundle.attestation_path)
        )
        self.assertEqual(code, 2)
        self.assertFalse(result["strategy_available"])
        self.assertEqual(result["reason"], "attestation_verification_failed")
        self.assertIsNone(result["strategy_evidence"])


class RepositoryQualityGuardTest(unittest.TestCase):
    def test_no_committed_grant_or_report_anywhere(self) -> None:
        """The gate exists; the repository has never passed through it."""

        offenders = []
        for path in PROJECT_ROOT.rglob("*.json"):
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):  # pragma: no cover
                continue
            if QUALITY_ATTESTATION_SCHEMA_VERSION in text:
                offenders.append((str(path), "attestation"))
            if SOLVE_QUALITY_REPORT_SCHEMA_VERSION in text:
                offenders.append((str(path), "solve quality report"))
            if '"quality": "verified"' in text:
                offenders.append((str(path), "verified label"))
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
