"""The independent gate that grants ``verified``.

The strategy artifact loader deliberately cannot promote a quality label.  It
checks identity, integrity and internal consistency, and it insists that the
catalog and the artifact agree — but "these bytes are the bytes we expected" is
not the same claim as "this strategy is good enough to teach".

That second claim is made here, by a separate document that a human signs:

```text
QualityAttestation
  → 定位并加载 SolveQualityReport（沙箱内、哈希钉死）
  → 完整验证 StrategyArtifact（哈希、身份、内容）
  → 报告与制品、目录条目身份一致
  → 收敛达到自己声明的门槛
  → 许可允许展示
  → claim_class 与牌局结构相符
  → 至少两名签署人，其中至少一名独立复核
  → 时间顺序自洽
  → 才允许 usable_for_teaching = true
```

Two properties matter more than the field list.  First, the gate reads the
artifact through the ordinary loader, so a fixture artifact can never be blessed
— the loader rejects ``verified`` under a ``fixtures/`` directory before the gate
is even consulted.  Second, an attestation pins the *bytes* of both the artifact
and the report, so re-signing is required after any edit to either.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from rivermind_core._contracts import (
    StrictReader,
    canonical_json_bytes,
    parse_timestamp,
    resolve_sandboxed_path,
)
from rivermind_core.gto_specs import (
    SolutionCatalog,
    SolutionQuality,
    SpecValidationError,
)
from rivermind_core.solve_quality import (
    ConvergenceUnit,
    SolveQualityReport,
    check_report_against_node,
    load_solve_quality_report,
)
from rivermind_core.strategy_artifacts import (
    ArtifactVerification,
    verify_catalog_artifact,
)


QUALITY_ATTESTATION_SCHEMA_VERSION = "quality-attestation/1.0.0"

#: The rules enforced below.  Changing any of them requires a new policy version
#: and, in practice, re-signing every existing attestation.
QUALITY_GATE_POLICY_VERSION = "quality-gate-policy/1.0.0"

MIN_REVIEWERS = 2
MIN_INDEPENDENT_REVIEWERS = 1
MAX_REVIEWERS = 16
MAX_ATTESTATION_BYTES = 256 * 1024

#: Clocks drift and timezones get mangled; a grant may be this far ahead of now.
MAX_GRANT_CLOCK_SKEW = timedelta(days=1)

#: Absolute ceilings, per unit, on the convergence a grant may accept.
#:
#: Without these the numeric half of the gate is entirely self-certified: a
#: report could declare a threshold of 999999 and then "meet" it.  These are
#: deliberately conservative v1.0.0 starting values, not a house standard —
#: raising or lowering them requires a new ``quality-gate-policy`` version.
MAX_CONVERGENCE_BY_UNIT: dict[str, Decimal] = {
    ConvergenceUnit.BB_PER_100.value: Decimal("1"),
    ConvergenceUnit.BB.value: Decimal("0.01"),
    ConvergenceUnit.PERCENT_OF_POT.value: Decimal("1"),
}


class AttestationValidationError(SpecValidationError):
    """Raised when an attestation cannot justify granting ``verified``."""


_READ = StrictReader(AttestationValidationError)


class ReviewerRole(StrEnum):
    SOLVER_OWNER = "solver_owner"
    INDEPENDENT_REVIEWER = "independent_reviewer"


@dataclass(frozen=True, slots=True)
class ReviewerSignature:
    """One named human accepting responsibility for the grant.

    ``statement_sha256`` pins the reviewer's written statement, which lives
    outside the repository.  The gate cannot read it; it exists so an audit can
    prove which text was signed.
    """

    reviewer_id: str
    role: ReviewerRole
    signed_at: str
    statement_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reviewer_id": self.reviewer_id,
            "role": self.role.value,
            "signed_at": self.signed_at,
            "statement_sha256": self.statement_sha256,
        }


@dataclass(frozen=True, slots=True)
class QualityAttestation:
    """A signed grant of ``verified`` for one exact artifact."""

    attestation_id: str
    solution_id: str
    artifact_sha256: str
    report_path: str
    report_id: str
    report_sha256: str
    granted_quality: SolutionQuality
    policy_version: str
    granted_at: str
    reviewers: tuple[ReviewerSignature, ...]

    def __post_init__(self) -> None:
        if self.granted_quality is not SolutionQuality.VERIFIED:
            raise AttestationValidationError(
                "the quality gate only grants 'verified'; lesser labels need no gate"
            )
        if self.policy_version != QUALITY_GATE_POLICY_VERSION:
            raise AttestationValidationError(
                f"unsupported quality gate policy: {self.policy_version!r}"
            )
        if not MIN_REVIEWERS <= len(self.reviewers) <= MAX_REVIEWERS:
            raise AttestationValidationError(
                f"a grant requires between {MIN_REVIEWERS} and {MAX_REVIEWERS} reviewers"
            )
        identifiers = [item.reviewer_id.casefold() for item in self.reviewers]
        if len(set(identifiers)) != len(identifiers):
            raise AttestationValidationError(
                "a reviewer may sign only once; ids are compared case-insensitively"
            )
        independent = sum(
            1
            for item in self.reviewers
            if item.role is ReviewerRole.INDEPENDENT_REVIEWER
        )
        if independent < MIN_INDEPENDENT_REVIEWERS:
            raise AttestationValidationError(
                f"a grant requires at least {MIN_INDEPENDENT_REVIEWERS} "
                "independent_reviewer; the solver owner cannot bless their own solve"
            )
        granted = parse_timestamp(self.granted_at)
        for item in self.reviewers:
            if parse_timestamp(item.signed_at) > granted:
                raise AttestationValidationError(
                    f"reviewer {item.reviewer_id!r} signed after the grant timestamp"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": QUALITY_ATTESTATION_SCHEMA_VERSION,
            "attestation_id": self.attestation_id,
            "solution_id": self.solution_id,
            "artifact_sha256": self.artifact_sha256,
            "report": {
                "path": self.report_path,
                "id": self.report_id,
                "sha256": self.report_sha256,
            },
            "granted_quality": self.granted_quality.value,
            "policy_version": self.policy_version,
            "granted_at": self.granted_at,
            "reviewers": [item.to_dict() for item in self.reviewers],
        }


@dataclass(frozen=True, slots=True)
class QualityAttestationVerification:
    """Proof that a grant of ``verified`` satisfies the whole gate policy."""

    attestation: QualityAttestation
    report: SolveQualityReport
    verification: ArtifactVerification
    policy_version: str

    def __post_init__(self) -> None:
        # A hand-constructed verification is the cheapest way to fake a grant,
        # so the invariants are re-asserted here rather than only in the gate.
        if self.policy_version != QUALITY_GATE_POLICY_VERSION:
            raise AttestationValidationError(
                f"unsupported quality gate policy: {self.policy_version!r}"
            )
        if self.attestation.granted_quality is not SolutionQuality.VERIFIED:
            raise AttestationValidationError("a grant must grant 'verified'")
        if self.verification.quality is not SolutionQuality.VERIFIED:
            raise AttestationValidationError(
                "the verified artifact does not carry the 'verified' label"
            )
        if self.attestation.artifact_sha256 != self.verification.artifact_sha256:
            raise AttestationValidationError(
                "the grant does not pin the verified artifact bytes"
            )
        if self.attestation.report_id != self.report.report_id:
            raise AttestationValidationError("the grant does not pin this report")

    @property
    def solution_id(self) -> str:
        return self.attestation.solution_id

    @property
    def artifact_sha256(self) -> str:
        return self.attestation.artifact_sha256

    def to_dict(self) -> dict[str, object]:
        report = self.report
        return {
            "schema_version": QUALITY_ATTESTATION_SCHEMA_VERSION,
            "policy_version": self.policy_version,
            "attestation_id": self.attestation.attestation_id,
            "solution_id": self.solution_id,
            "artifact_sha256": self.artifact_sha256,
            "granted_quality": self.attestation.granted_quality.value,
            "granted_at": self.attestation.granted_at,
            "reviewers": [item.to_dict() for item in self.attestation.reviewers],
            "report": {
                "id": report.report_id,
                "sha256": self.attestation.report_sha256,
                "claim_class": report.claim_class.value,
                "source": report.source.to_dict(),
                "solve": report.solve.to_dict(),
                "evaluation": report.evaluation.to_dict(),
                "limits": list(report.limits),
            },
            "node_id": self.verification.node_id,
            "game_spec_fingerprint": self.verification.game_spec_fingerprint,
            "grant_valid": True,
            "boundary": (
                "This grant is scoped to these exact artifact and report bytes. "
                "Editing either invalidates it and requires re-signing."
            ),
        }


def serialize_quality_attestation(attestation: QualityAttestation) -> bytes:
    """Return the one byte sequence that represents ``attestation``."""

    return canonical_json_bytes(attestation.to_dict())


def write_quality_attestation(path: Path, attestation: QualityAttestation) -> str:
    """Write ``attestation`` in canonical form and return its SHA-256."""

    payload = serialize_quality_attestation(attestation)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def load_quality_attestation(path: Path) -> QualityAttestation:
    """Load and structurally validate an attestation document."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AttestationValidationError(f"cannot read attestation: {exc}") from exc
    if size > MAX_ATTESTATION_BYTES:
        raise AttestationValidationError(
            f"attestation exceeds the {MAX_ATTESTATION_BYTES} byte limit"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AttestationValidationError(f"cannot read attestation: {exc}") from exc
    return quality_attestation_from_dict(_READ.load_json(raw, "attestation"))


def quality_attestation_from_dict(payload: Any) -> QualityAttestation:
    document = _READ.mapping(payload, "attestation")
    _READ.exact_keys(
        document,
        {
            "schema_version",
            "attestation_id",
            "solution_id",
            "artifact_sha256",
            "report",
            "granted_quality",
            "policy_version",
            "granted_at",
            "reviewers",
        },
        "attestation",
    )
    _READ.schema_version(document, QUALITY_ATTESTATION_SCHEMA_VERSION)
    report = _READ.mapping(document["report"], "report")
    _READ.exact_keys(report, {"path", "id", "sha256"}, "report")
    reviewers = _READ.sequence(document["reviewers"], "reviewers")
    try:
        granted_quality = SolutionQuality(
            _READ.text(document["granted_quality"], "granted_quality")
        )
    except ValueError as exc:
        raise AttestationValidationError(f"unknown granted_quality: {exc}") from exc
    return QualityAttestation(
        attestation_id=_READ.text(document["attestation_id"], "attestation_id"),
        solution_id=_READ.text(document["solution_id"], "solution_id"),
        artifact_sha256=_READ.sha256(document["artifact_sha256"], "artifact_sha256"),
        report_path=_READ.text(report["path"], "report.path"),
        report_id=_READ.text(report["id"], "report.id"),
        report_sha256=_READ.sha256(report["sha256"], "report.sha256"),
        granted_quality=granted_quality,
        policy_version=_READ.text(document["policy_version"], "policy_version"),
        granted_at=_READ.timestamp(document["granted_at"], "granted_at"),
        reviewers=tuple(
            _reviewer_from_dict(item, index) for index, item in enumerate(reviewers)
        ),
    )


def verify_quality_attestation(
    attestation_path: Path,
    *,
    catalog: SolutionCatalog,
    catalog_root: Path,
    now: datetime | None = None,
) -> QualityAttestationVerification:
    """Run the whole gate.  Any failure means ``verified`` is not granted.

    ``now`` is injectable so tests are deterministic; production passes ``None``
    and the real clock is used to reject grants dated in the future.
    """

    attestation = load_quality_attestation(attestation_path)
    report, report_digest = _load_report(attestation, attestation_path)

    if report_digest != attestation.report_sha256:
        raise AttestationValidationError(
            "solve quality report sha-256 does not match the attestation: "
            f"expected {attestation.report_sha256}, computed {report_digest}"
        )
    if report.report_id != attestation.report_id:
        raise AttestationValidationError(
            "solve quality report id does not match the attestation"
        )

    verification = verify_catalog_artifact(
        catalog, attestation.solution_id, root=catalog_root
    )
    if verification.quality is not SolutionQuality.VERIFIED:
        raise AttestationValidationError(
            "the catalog and artifact must both already carry the 'verified' label "
            f"before a grant is meaningful; found {verification.quality.value}. "
            "Update the catalog and artifact together, then re-sign."
        )
    if verification.artifact_sha256 != attestation.artifact_sha256:
        raise AttestationValidationError(
            "artifact sha-256 does not match the attestation: "
            f"expected {attestation.artifact_sha256}, computed "
            f"{verification.artifact_sha256}"
        )

    _require_report_identity(report, verification)
    _require_policy(report, verification, attestation, now=now)
    return QualityAttestationVerification(
        attestation=attestation,
        report=report,
        verification=verification,
        policy_version=QUALITY_GATE_POLICY_VERSION,
    )


def _load_report(
    attestation: QualityAttestation,
    attestation_path: Path,
) -> tuple[SolveQualityReport, str]:
    path = resolve_sandboxed_path(
        attestation_path.parent,
        attestation.report_path,
        error=AttestationValidationError,
        label="solve quality report",
    )
    return load_solve_quality_report(path)


def _require_report_identity(
    report: SolveQualityReport,
    verification: ArtifactVerification,
) -> None:
    artifact = verification.artifact
    if report.solution_id != artifact.solution_id:
        raise AttestationValidationError(
            "report solution_id does not match the artifact"
        )
    if report.game_spec_fingerprint != artifact.game_spec_fingerprint:
        raise AttestationValidationError(
            "report game_spec_fingerprint does not match the artifact"
        )
    if report.action_tree_version != artifact.action_tree_version:
        raise AttestationValidationError(
            "report action_tree_version does not match the artifact"
        )
    provenance = artifact.provenance
    if report.solver_name != provenance.solver_name:
        raise AttestationValidationError("report solver name does not match the artifact")
    if report.solver_version != provenance.solver_version:
        raise AttestationValidationError(
            "report solver version does not match the artifact"
        )
    if report.solver_config_id != provenance.solver_config_id:
        raise AttestationValidationError(
            "report solver config id does not match the artifact"
        )
    if provenance.quality_report_id != report.report_id:
        raise AttestationValidationError(
            "the artifact points at a different quality report: "
            f"{provenance.quality_report_id!r} != {report.report_id!r}"
        )


def _require_policy(
    report: SolveQualityReport,
    verification: ArtifactVerification,
    attestation: QualityAttestation,
    *,
    now: datetime | None,
) -> None:
    solve = report.solve
    if not solve.meets_threshold:
        raise AttestationValidationError(
            "convergence did not reach the report's own threshold: "
            f"{solve.convergence_value} > {solve.convergence_threshold} "
            f"{solve.convergence_unit.value}"
        )
    ceiling = MAX_CONVERGENCE_BY_UNIT[solve.convergence_unit.value]
    if solve.convergence_value > ceiling:
        raise AttestationValidationError(
            "convergence is worse than the policy ceiling: "
            f"{solve.convergence_value} > {ceiling} {solve.convergence_unit.value}"
        )
    if solve.convergence_threshold > ceiling:
        raise AttestationValidationError(
            "the report's own threshold is looser than the policy ceiling: "
            f"{solve.convergence_threshold} > {ceiling} {solve.convergence_unit.value}"
        )
    if not report.source.display_allowed:
        raise AttestationValidationError(
            "the licence does not permit showing this strategy to a user"
        )
    if not report.evaluation.independent_recheck:
        raise AttestationValidationError(
            "a grant requires an independent recheck of the solve"
        )

    # Rake scope and claim class are properties of the report *against this node*.
    check_report_against_node(report, verification.game_spec)

    started = parse_timestamp(solve.started_at)
    completed = parse_timestamp(solve.completed_at)
    granted = parse_timestamp(attestation.granted_at)
    if granted < completed:
        raise AttestationValidationError("the grant predates the solve it blesses")
    generated = parse_timestamp(verification.artifact.provenance.generated_at)
    if not started <= generated <= granted:
        raise AttestationValidationError(
            "the artifact's generated_at falls outside the solve-to-grant window: "
            f"{verification.artifact.provenance.generated_at} not within "
            f"[{solve.started_at}, {attestation.granted_at}]"
        )
    for reviewer in attestation.reviewers:
        if parse_timestamp(reviewer.signed_at) < completed:
            raise AttestationValidationError(
                f"reviewer {reviewer.reviewer_id!r} signed before the solve finished"
            )
    horizon = (now or datetime.now(timezone.utc)) + MAX_GRANT_CLOCK_SKEW
    if granted > horizon:
        raise AttestationValidationError(
            f"the grant is dated in the future: {attestation.granted_at}"
        )


def _reviewer_from_dict(value: Any, index: int) -> ReviewerSignature:
    payload = _READ.mapping(value, f"reviewers[{index}]")
    _READ.exact_keys(
        payload,
        {"reviewer_id", "role", "signed_at", "statement_sha256"},
        f"reviewers[{index}]",
    )
    try:
        role = ReviewerRole(_READ.text(payload["role"], f"reviewers[{index}].role"))
    except ValueError as exc:
        raise AttestationValidationError(f"unknown reviewer role: {exc}") from exc
    return ReviewerSignature(
        reviewer_id=_READ.text(
            payload["reviewer_id"], f"reviewers[{index}].reviewer_id"
        ),
        role=role,
        signed_at=_READ.timestamp(
            payload["signed_at"], f"reviewers[{index}].signed_at"
        ),
        statement_sha256=_READ.sha256(
            payload["statement_sha256"], f"reviewers[{index}].statement_sha256"
        ),
    )

