"""Read-only strategy queries over verified artifacts.

Nothing in this module reads a file, trusts a catalog entry or recomputes a
hash.  It only accepts an :class:`~rivermind_core.strategy_artifacts.ArtifactVerification`
that has already passed the strict loader, and turns it into a
:class:`StrategyEvidence` record: the frozen, quality-labelled shape that Study,
Practice and the AI Coach are allowed to read.

The coach may quote these facts.  It may never produce them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Iterable

from rivermind_core.board_isomorphism import (
    BOARD_ISOMORPHISM_VERSION,
    SuitPermutation,
)
from rivermind_core.gto_specs import SolutionQuality, SpecValidationError
from rivermind_core.quality_gate import QualityAttestationVerification
from rivermind_core.strategy_artifacts import (
    ArtifactProvenance,
    ArtifactVerification,
    EVSemantics,
    EVUnit,
    StrategyActionKind,
    canonical_combo,
)


STRATEGY_EVIDENCE_SCHEMA_VERSION = "strategy-evidence/1.0.0"

#: Aggregation across the combos an artifact contains is a *derived* view, so it
#: carries its own version.  v1.0.0 is a weight-weighted mean of the artifact's
#: own entries and nothing else.
AGGREGATION_POLICY_VERSION = "strategy-aggregation/1.0.0"
AGGREGATION_DECIMAL_PLACES = 6

#: Aggregation runs in an explicit high-precision context.  The default 28-digit
#: context would silently round intermediate products, so the "no invented
#: numbers" rule has to be enforced by arithmetic, not by hope.
AGGREGATION_PRECISION = 96

_QUANTUM = Decimal(1).scaleb(-AGGREGATION_DECIMAL_PLACES)

_TEACHABLE_QUALITIES = frozenset({SolutionQuality.VERIFIED})

#: Why a set of otherwise valid facts may still not be shown to a learner.
TEACHING_BLOCK_QUALITY = "quality_not_verified"
TEACHING_BLOCK_NO_ATTESTATION = "no_quality_attestation"


class StrategyQueryError(SpecValidationError):
    """Raised when a query cannot be answered from verified content alone."""


class ComboNotCoveredError(StrategyQueryError):
    """Raised when a legal combination is simply not present in the artifact."""


class EvidenceScope(str):
    """Marker strings for what a :class:`StrategyEvidence` record describes."""

    COMBO = "combo"
    ARTIFACT_ENTRIES = "artifact_entries"


@dataclass(frozen=True, slots=True)
class ActionFact:
    """One action of the node, with the probability (and EV) that was verified."""

    action_id: str
    kind: StrategyActionKind
    size_bb: Decimal | None
    probability: Decimal
    ev: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "size_bb": None if self.size_bb is None else _text(self.size_bb),
            "probability": _text(self.probability),
            "ev": None if self.ev is None else _text(self.ev),
        }


@dataclass(frozen=True, slots=True)
class StrategyEvidence:
    """Immutable, quality-labelled strategy facts drawn from a verified artifact."""

    solution_id: str
    node_id: str
    game_spec_fingerprint: str
    action_tree_version: str
    quality: SolutionQuality
    ev_unit: EVUnit
    ev_semantics: EVSemantics
    scope: str
    combo: str | None
    combo_weight: Decimal | None
    combo_count: int
    covered_weight: Decimal
    actions: tuple[ActionFact, ...]
    provenance: ArtifactProvenance
    aggregation_policy_version: str | None
    attestation_id: str | None
    #: Set when the solution was solved on a relabelled board. Combination
    #: labels below are in the *observed* frame; this is how they were carried
    #: across.
    suit_permutation: SuitPermutation | None = None
    solution_frame_combo: str | None = None

    @property
    def usable_for_teaching(self) -> bool:
        """Two independent conditions, both required.

        The label must say ``verified`` *and* a signed grant must have passed the
        quality gate for these exact artifact bytes.  A hand-edited catalog and
        artifact can agree on ``verified`` between themselves; without an
        attestation that agreement teaches nobody.
        """

        return self.quality in _TEACHABLE_QUALITIES and self.attestation_id is not None

    @property
    def teaching_block_reason(self) -> str | None:
        """Why these facts may not be shown, or ``None`` when they may."""

        if self.quality not in _TEACHABLE_QUALITIES:
            return TEACHING_BLOCK_QUALITY
        if self.attestation_id is None:
            return TEACHING_BLOCK_NO_ATTESTATION
        return None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": STRATEGY_EVIDENCE_SCHEMA_VERSION,
            "solution_id": self.solution_id,
            "node_id": self.node_id,
            "game_spec_fingerprint": self.game_spec_fingerprint,
            "action_tree_version": self.action_tree_version,
            "quality": self.quality.value,
            "usable_for_teaching": self.usable_for_teaching,
            "teaching_block_reason": self.teaching_block_reason,
            "attestation_id": self.attestation_id,
            "ev_unit": self.ev_unit.value,
            "ev_semantics": self.ev_semantics.value,
            "scope": self.scope,
            "combo": self.combo,
            "combo_weight": (
                None if self.combo_weight is None else _text(self.combo_weight)
            ),
            "combo_count": self.combo_count,
            "covered_weight": _text(self.covered_weight),
            "coverage": (
                "This artifact is a single-node vertical slice. Aggregates cover only "
                "the combinations the artifact contains, not the node's full range."
            ),
            "aggregation_policy_version": self.aggregation_policy_version,
            "actions": [item.to_dict() for item in self.actions],
            "provenance": self.provenance.to_dict(),
            "boundary": (
                "Every number here was read from a hash-verified artifact. No value "
                "was generated, interpolated or completed by a language model."
            ),
        }
        if self.suit_permutation is not None:
            payload["board_isomorphism"] = {
                "isomorphism_version": BOARD_ISOMORPHISM_VERSION,
                "suit_permutation": self.suit_permutation.to_dict(),
                "solution_frame_combo": self.solution_frame_combo,
                "assumption": (
                    "Relabelling suits is only sound if the solve's input ranges "
                    "were suit symmetric. That is a property of the solve, not of "
                    "the board; the solve quality report should state it."
                ),
            }
        return payload


def build_strategy_evidence(
    verification: ArtifactVerification,
    *,
    combo: str | None = None,
    attestation: QualityAttestationVerification | None = None,
    suit_permutation: SuitPermutation | None = None,
) -> StrategyEvidence:
    """Return strategy evidence for one combination, or for the artifact's slice.

    Pass ``attestation`` only when the quality gate has already accepted a grant
    for these exact bytes; it is what unlocks ``usable_for_teaching``.

    Pass ``suit_permutation`` when the match was isomorphic: ``combo`` is then
    read in the *observed* hand's frame and carried into the solution's frame,
    and the reported combination stays in the observed frame so the caller never
    has to think in the solver's relabelling.
    """

    artifact = verification.artifact
    attestation_id = _checked_attestation_id(verification, attestation)
    covered_weight = _sum(item.weight for item in artifact.entries)
    if suit_permutation is not None and suit_permutation.is_identity:
        suit_permutation = None
    if combo is None:
        actions = _aggregate_actions(verification)
        return StrategyEvidence(
            solution_id=artifact.solution_id,
            node_id=artifact.node_id,
            game_spec_fingerprint=artifact.game_spec_fingerprint,
            action_tree_version=artifact.action_tree_version,
            quality=artifact.provenance.quality,
            ev_unit=artifact.ev_unit,
            ev_semantics=artifact.ev_semantics,
            scope=EvidenceScope.ARTIFACT_ENTRIES,
            combo=None,
            combo_weight=None,
            combo_count=len(artifact.entries),
            covered_weight=covered_weight,
            actions=actions,
            provenance=artifact.provenance,
            aggregation_policy_version=AGGREGATION_POLICY_VERSION,
            attestation_id=attestation_id,
            suit_permutation=suit_permutation,
        )

    observed_combo = canonical_combo(combo)
    normalized = (
        observed_combo
        if suit_permutation is None
        else suit_permutation.apply_combo(observed_combo)
    )
    entry = artifact.entry_for(normalized)
    if entry is None:
        raise ComboNotCoveredError(
            f"combo {observed_combo} is not present in artifact "
            f"{artifact.solution_id!r}"
            + ("" if suit_permutation is None else f" (as {normalized})")
        )
    actions = tuple(
        ActionFact(
            action_id=action.action_id,
            kind=action.kind,
            size_bb=action.size_bb,
            probability=entry.policy_for(action.action_id).probability,
            ev=entry.policy_for(action.action_id).ev,
        )
        for action in artifact.actions
    )
    return StrategyEvidence(
        solution_id=artifact.solution_id,
        node_id=artifact.node_id,
        game_spec_fingerprint=artifact.game_spec_fingerprint,
        action_tree_version=artifact.action_tree_version,
        quality=artifact.provenance.quality,
        ev_unit=artifact.ev_unit,
        ev_semantics=artifact.ev_semantics,
        scope=EvidenceScope.COMBO,
        combo=observed_combo,
        combo_weight=entry.weight,
        combo_count=len(artifact.entries),
        covered_weight=covered_weight,
        actions=actions,
        provenance=artifact.provenance,
        aggregation_policy_version=None,
        attestation_id=attestation_id,
        suit_permutation=suit_permutation,
        solution_frame_combo=None if suit_permutation is None else normalized,
    )


def _checked_attestation_id(
    verification: ArtifactVerification,
    attestation: QualityAttestationVerification | None,
) -> str | None:
    """Refuse an attestation that was issued for different bytes."""

    if attestation is None:
        return None
    if attestation.solution_id != verification.solution_id:
        raise StrategyQueryError(
            "the attestation was issued for a different solution: "
            f"{attestation.solution_id!r} != {verification.solution_id!r}"
        )
    if attestation.artifact_sha256 != verification.artifact_sha256:
        raise StrategyQueryError(
            "the attestation was issued for different artifact bytes"
        )
    return attestation.attestation.attestation_id


def _aggregate_actions(verification: ArtifactVerification) -> tuple[ActionFact, ...]:
    """Weight-weighted mean over the artifact's own entries (``strategy-aggregation/1.0.0``).

    ``probability(a) = sum(w_e * p_ea) / sum(w_e)``

    ``ev(a) = sum(w_e * p_ea * ev_ea) / sum(w_e * p_ea)``; ``null`` when no
    weight reaches the action, because an EV nobody realises is not a fact.
    """

    artifact = verification.artifact
    with localcontext() as context:
        context.prec = AGGREGATION_PRECISION
        total_weight = _sum(item.weight for item in artifact.entries)
        if total_weight <= 0:  # pragma: no cover - the loader rejects zero weights
            raise StrategyQueryError("artifact entries carry no weight")

        # One pass over the entries, indexed by action id: the naive nested scan
        # is quadratic in the action count.
        weighted_probability = {item.action_id: Decimal("0") for item in artifact.actions}
        weighted_ev = dict(weighted_probability)
        for entry in artifact.entries:
            for policy in entry.policies:
                share = entry.weight * policy.probability
                weighted_probability[policy.action_id] += share
                if policy.ev is not None:
                    weighted_ev[policy.action_id] += share * policy.ev

        try:
            facts = tuple(
                ActionFact(
                    action_id=action.action_id,
                    kind=action.kind,
                    size_bb=action.size_bb,
                    probability=_round(
                        weighted_probability[action.action_id] / total_weight
                    ),
                    ev=(
                        None
                        if not artifact.ev_present
                        or weighted_probability[action.action_id] == 0
                        else _round(
                            weighted_ev[action.action_id]
                            / weighted_probability[action.action_id]
                        )
                    ),
                )
                for action in artifact.actions
            )
        except (InvalidOperation, ArithmeticError) as exc:
            raise StrategyQueryError(
                f"aggregation exceeded the versioned numeric range: {exc}"
            ) from exc
    return facts


def _sum(values: Iterable[Decimal]) -> Decimal:
    return sum(values, start=Decimal("0"))


def _round(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _text(value: Decimal) -> str:
    """Format without ``normalize()``, which would round against the decimal context."""

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-", "-0"}:
        return "0"
    return text
