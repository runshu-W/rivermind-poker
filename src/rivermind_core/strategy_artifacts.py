"""Strict, versioned strategy artifacts.

A :class:`~rivermind_core.gto_specs.SolutionSpec` only registers *metadata*: a
solver, an action-tree version, a quality label, an artifact id and a content
hash.  This module turns that reference into a strategy artifact that has been
located inside an explicit sandbox, hashed, identity-checked against the
solution, and validated field by field.

Everything here fails closed.  A malformed, ambiguous, unhashed, mislabeled or
out-of-sandbox artifact raises instead of returning partial strategy content.
No value in this module is ever inferred, defaulted or repaired.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from rivermind_core._contracts import (
    MAX_DECIMAL_DIGITS,
    SHA256_PATTERN,
    StrictReader,
    canonical_json_bytes,
    decimal_text,
    resolve_sandboxed_path,
)
from rivermind_core.gto_specs import (
    GameSpec,
    SolutionCatalog,
    SolutionQuality,
    SolutionSpec,
    SpecValidationError,
)
from rivermind_core.models import CARD_PATTERN


STRATEGY_ARTIFACT_SCHEMA_VERSION = "strategy-artifact/1.0.0"

#: Maximum size of a single artifact file.  v0.1 artifacts are single-node
#: vertical slices; anything larger is rejected rather than streamed.
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

#: Versioned numeric contract.  Changing any of these values requires a new
#: ``strategy-artifact`` schema version.
PROBABILITY_DECIMAL_PLACES = 6
EV_DECIMAL_PLACES = 6
WEIGHT_DECIMAL_PLACES = 6
SIZE_DECIMAL_PLACES = 6
PROBABILITY_SUM_TOLERANCE = Decimal("0.00001")

#: Structural bounds.  A hold'em node cannot have more than 1,326 distinct
#: combinations, and a real action tree does not branch 64 ways.
MAX_ACTIONS = 64
MAX_ENTRIES = 1326

_ACTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_RANK_ORDER = "23456789TJQKA"
_SUIT_ORDER = "cdhs"

#: Directory names that may never contain an artifact claiming ``verified``.
_TEST_ONLY_DIRECTORIES = frozenset({"fixtures"})


class ArtifactValidationError(SpecValidationError):
    """Raised when a strategy artifact cannot be trusted as strategy content."""


class StrategyActionKind(StrEnum):
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"
    RAISE = "raise"
    ALL_IN = "all_in"


class EVUnit(StrEnum):
    """v1.0.0 freezes a single EV unit.  Chips and prize value need a new version."""

    BIG_BLINDS = "bb"


class EVSemantics(StrEnum):
    """v1.0.0 freezes a single EV meaning: EV of the action from this node onward."""

    ACTION_EV_FROM_NODE = "action_ev_from_node"


_SIZED_KINDS = frozenset(
    {StrategyActionKind.BET, StrategyActionKind.RAISE, StrategyActionKind.ALL_IN}
)
_SINGLETON_KINDS = frozenset(
    {
        StrategyActionKind.FOLD,
        StrategyActionKind.CHECK,
        StrategyActionKind.CALL,
        StrategyActionKind.ALL_IN,
    }
)


@dataclass(frozen=True, slots=True)
class StrategyAction:
    """One declared, uniquely identified action of the node's action tree."""

    action_id: str
    kind: StrategyActionKind
    size_bb: Decimal | None

    def __post_init__(self) -> None:
        if not _ACTION_ID_PATTERN.fullmatch(self.action_id):
            raise ArtifactValidationError(
                f"action id {self.action_id!r} is not a lowercase snake-case identifier"
            )
        if self.kind in _SIZED_KINDS:
            if self.size_bb is None:
                raise ArtifactValidationError(
                    f"action {self.action_id!r} of kind {self.kind.value} requires size_bb"
                )
            if not self.size_bb.is_finite() or self.size_bb <= 0:
                raise ArtifactValidationError(
                    f"action {self.action_id!r} requires a positive finite size_bb"
                )
        elif self.size_bb is not None:
            raise ArtifactValidationError(
                f"action {self.action_id!r} of kind {self.kind.value} cannot declare size_bb"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "size_bb": None if self.size_bb is None else _decimal_text(self.size_bb),
        }


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """Probability, and optionally EV, of one declared action for one combo."""

    action_id: str
    probability: Decimal
    ev: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "probability": _decimal_text(self.probability),
            "ev": None if self.ev is None else _decimal_text(self.ev),
        }


@dataclass(frozen=True, slots=True)
class ComboStrategy:
    """The full action distribution for one concrete two-card combination."""

    combo: str
    weight: Decimal
    policies: tuple[ActionPolicy, ...]

    @property
    def cards(self) -> tuple[str, str]:
        return (self.combo[:2], self.combo[2:])

    def policy_for(self, action_id: str) -> ActionPolicy:
        for policy in self.policies:
            if policy.action_id == action_id:
                return policy
        raise ArtifactValidationError(
            f"combo {self.combo} does not declare action {action_id!r}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "combo": self.combo,
            "weight": _decimal_text(self.weight),
            "policies": [item.to_dict() for item in self.policies],
        }


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    """Where the strategy came from and who is allowed to trust it."""

    solver_name: str
    solver_version: str
    solver_config_id: str
    generated_at: str
    quality: SolutionQuality
    quality_report_id: str | None
    license: str

    def __post_init__(self) -> None:
        _READ.text(self.solver_name, "provenance.solver_name")
        _READ.text(self.solver_version, "provenance.solver_version")
        _READ.text(self.solver_config_id, "provenance.solver_config_id")
        _READ.text(self.license, "provenance.license")
        _READ.timestamp(self.generated_at, "provenance.generated_at")
        if self.quality_report_id is not None:
            _READ.text(self.quality_report_id, "provenance.quality_report_id")
        if self.quality == SolutionQuality.VERIFIED and self.quality_report_id is None:
            raise ArtifactValidationError(
                "verified artifacts require provenance.quality_report_id"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "solver_name": self.solver_name,
            "solver_version": self.solver_version,
            "solver_config_id": self.solver_config_id,
            "generated_at": self.generated_at,
            "quality": self.quality.value,
            "quality_report_id": self.quality_report_id,
            "license": self.license,
        }


@dataclass(frozen=True, slots=True)
class StrategyArtifact:
    """A validated single-node strategy slice.

    The artifact never contains its own final hash; integrity is owned by the
    :class:`SolutionSpec` that points at it.
    """

    solution_id: str
    game_spec_fingerprint: str
    action_tree_version: str
    node_id: str
    ev_unit: EVUnit
    ev_semantics: EVSemantics
    actions: tuple[StrategyAction, ...]
    entries: tuple[ComboStrategy, ...]
    provenance: ArtifactProvenance

    @property
    def ev_present(self) -> bool:
        return bool(self.entries) and self.entries[0].policies[0].ev is not None

    @property
    def action_ids(self) -> tuple[str, ...]:
        return tuple(item.action_id for item in self.actions)

    def action_for(self, action_id: str) -> StrategyAction:
        for action in self.actions:
            if action.action_id == action_id:
                return action
        raise ArtifactValidationError(f"undeclared action {action_id!r}")

    def entry_for(self, combo: str) -> ComboStrategy | None:
        for entry in self.entries:
            if entry.combo == combo:
                return entry
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": STRATEGY_ARTIFACT_SCHEMA_VERSION,
            "solution_id": self.solution_id,
            "game_spec_fingerprint": self.game_spec_fingerprint,
            "action_tree_version": self.action_tree_version,
            "node_id": self.node_id,
            "ev_unit": self.ev_unit.value,
            "ev_semantics": self.ev_semantics.value,
            "actions": [item.to_dict() for item in self.actions],
            "entries": [item.to_dict() for item in self.entries],
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    """Proof that an artifact's identity, integrity and content were checked."""

    solution_id: str
    artifact_id: str
    artifact_relative_path: str
    artifact_sha256: str
    artifact_bytes: int
    game_spec_fingerprint: str
    action_tree_version: str
    node_id: str
    quality: SolutionQuality
    artifact: StrategyArtifact
    #: The node this artifact belongs to.  Carried so downstream policy (the
    #: quality gate) can reason about table size and objective without
    #: re-reading the catalog.  Not serialized; the fingerprint stands in for it.
    game_spec: GameSpec

    @property
    def strategy_content_verified(self) -> bool:
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": STRATEGY_ARTIFACT_SCHEMA_VERSION,
            "solution_id": self.solution_id,
            "artifact": {
                "id": self.artifact_id,
                "relative_path": self.artifact_relative_path,
                "sha256": self.artifact_sha256,
                "bytes": self.artifact_bytes,
            },
            "game_spec_fingerprint": self.game_spec_fingerprint,
            "action_tree_version": self.action_tree_version,
            "node_id": self.node_id,
            "quality": self.quality.value,
            "ev_unit": self.artifact.ev_unit.value,
            "ev_semantics": self.artifact.ev_semantics.value,
            "ev_present": self.artifact.ev_present,
            "action_ids": list(self.artifact.action_ids),
            "combo_count": len(self.artifact.entries),
            "provenance": self.artifact.provenance.to_dict(),
            "strategy_content_verified": self.strategy_content_verified,
            "boundary": (
                "Verification proves identity, integrity and internal consistency. "
                "It does not upgrade the artifact's quality label and does not make "
                "test_only or experimental strategy safe to teach."
            ),
        }


def load_artifact_document(path: Path) -> Any:
    """Read and decode an artifact file under the same limits as verification.

    Used by packaging, which validates content but not the catalog hash.
    """

    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactValidationError(
                f"artifact exceeds the {MAX_ARTIFACT_BYTES} byte limit"
            )
        raw = path.read_bytes()
    except OSError as exc:
        raise ArtifactValidationError(f"cannot read artifact: {exc}") from exc
    return _READ.load_json(raw, "artifact")


def serialize_strategy_artifact(artifact: StrategyArtifact) -> bytes:
    """Return the one byte sequence that represents ``artifact``.

    Packaging normalizes decimals (``"2.0"`` becomes ``"2"``), so a draft and its
    packaged form can differ byte-wise.  The packaged form is idempotent:
    ``serialize(load(serialize(x))) == serialize(x)``, which is what makes the
    SHA-256 in the catalog reproducible on any machine.
    """

    return canonical_json_bytes(artifact.to_dict())


def write_strategy_artifact(path: Path, artifact: StrategyArtifact) -> str:
    """Write ``artifact`` in canonical form and return its SHA-256."""

    payload = serialize_strategy_artifact(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def resolve_artifact_path(
    root: Path,
    artifact_id: str,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve ``artifact_id`` to a file strictly inside ``root``.

    ``must_exist=False`` is for packaging a not-yet-written artifact; the path
    rules are identical, only the existence check is skipped.
    """

    return resolve_sandboxed_path(
        root,
        artifact_id,
        error=ArtifactValidationError,
        label="artifact",
        must_exist=must_exist,
    )


def verify_solution_artifact(
    solution: SolutionSpec,
    *,
    root: Path,
) -> ArtifactVerification:
    """Locate, hash, parse and validate the artifact referenced by ``solution``."""

    path = resolve_artifact_path(root, solution.artifact_id)
    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ArtifactValidationError(
            f"artifact exceeds the {MAX_ARTIFACT_BYTES} byte limit for v0.1 slices"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArtifactValidationError(f"cannot read artifact: {exc}") from exc

    digest = hashlib.sha256(raw).hexdigest()
    if not SHA256_PATTERN.fullmatch(solution.artifact_sha256):
        raise ArtifactValidationError("solution artifact_sha256 is not a sha-256 digest")
    if digest != solution.artifact_sha256:
        raise ArtifactValidationError(
            "artifact sha-256 does not match the solution reference: "
            f"expected {solution.artifact_sha256}, computed {digest}"
        )

    payload = _READ.load_json(raw, "artifact")
    artifact = strategy_artifact_from_dict(payload, game_spec=solution.game_spec)
    _require_identity(artifact, solution)
    _require_quality_placement(artifact, solution)

    return ArtifactVerification(
        solution_id=solution.solution_id,
        artifact_id=solution.artifact_id,
        artifact_relative_path=solution.artifact_id,
        artifact_sha256=digest,
        artifact_bytes=size,
        game_spec_fingerprint=artifact.game_spec_fingerprint,
        action_tree_version=artifact.action_tree_version,
        node_id=artifact.node_id,
        quality=artifact.provenance.quality,
        artifact=artifact,
        game_spec=solution.game_spec,
    )


def verify_catalog_artifact(
    catalog: SolutionCatalog,
    solution_id: str,
    *,
    root: Path,
) -> ArtifactVerification:
    """Verify one solution of ``catalog`` by id."""

    for solution in catalog.solutions:
        if solution.solution_id == solution_id:
            return verify_solution_artifact(solution, root=root)
    raise ArtifactValidationError(f"catalog does not contain solution {solution_id!r}")


def strategy_artifact_from_dict(
    payload: Any,
    *,
    game_spec: GameSpec,
) -> StrategyArtifact:
    """Parse a strategy artifact document under the frozen v1.0.0 contract."""

    document = _mapping(payload, "artifact")
    _exact_keys(
        document,
        {
            "schema_version",
            "solution_id",
            "game_spec_fingerprint",
            "action_tree_version",
            "node_id",
            "ev_unit",
            "ev_semantics",
            "actions",
            "entries",
            "provenance",
        },
        "artifact",
    )
    if document["schema_version"] != STRATEGY_ARTIFACT_SCHEMA_VERSION:
        raise ArtifactValidationError(
            f"unsupported artifact schema version: {document['schema_version']!r}"
        )
    try:
        ev_unit = EVUnit(_text(document["ev_unit"], "ev_unit"))
    except ValueError as exc:
        raise ArtifactValidationError(f"unsupported ev_unit: {exc}") from exc
    try:
        ev_semantics = EVSemantics(_text(document["ev_semantics"], "ev_semantics"))
    except ValueError as exc:
        raise ArtifactValidationError(f"unsupported ev_semantics: {exc}") from exc

    actions = _actions_from_list(document["actions"])
    action_ids = tuple(item.action_id for item in actions)
    entries = _entries_from_list(document["entries"], action_ids, game_spec)
    provenance = _provenance_from_dict(document["provenance"])

    fingerprint = _READ.sha256(
        document["game_spec_fingerprint"], "game_spec_fingerprint"
    )
    return StrategyArtifact(
        solution_id=_identifier(document["solution_id"], "solution_id"),
        game_spec_fingerprint=fingerprint,
        action_tree_version=_identifier(
            document["action_tree_version"], "action_tree_version"
        ),
        node_id=_identifier(document["node_id"], "node_id"),
        ev_unit=ev_unit,
        ev_semantics=ev_semantics,
        actions=actions,
        entries=entries,
        provenance=provenance,
    )


def _actions_from_list(value: Any) -> tuple[StrategyAction, ...]:
    items = _list(value, "actions")
    if len(items) < 2:
        raise ArtifactValidationError("a decision node requires at least two actions")
    if len(items) > MAX_ACTIONS:
        raise ArtifactValidationError(
            f"a node may declare at most {MAX_ACTIONS} actions"
        )
    actions: list[StrategyAction] = []
    for item in items:
        entry = _mapping(item, "action")
        _exact_keys(entry, {"action_id", "kind", "size_bb"}, "action")
        try:
            kind = StrategyActionKind(_text(entry["kind"], "action.kind"))
        except ValueError as exc:
            raise ArtifactValidationError(f"unknown action kind: {exc}") from exc
        size = entry["size_bb"]
        actions.append(
            StrategyAction(
                action_id=_text(entry["action_id"], "action.action_id"),
                kind=kind,
                size_bb=(
                    None
                    if size is None
                    else _decimal(
                        size,
                        "action.size_bb",
                        places=SIZE_DECIMAL_PLACES,
                        allow_negative=False,
                    )
                ),
            )
        )
    identifiers = [item.action_id for item in actions]
    if len(set(identifiers)) != len(identifiers):
        raise ArtifactValidationError("action ids must be unique within a node")
    if identifiers != sorted(identifiers):
        raise ArtifactValidationError("actions must be sorted by action_id")
    for kind in _SINGLETON_KINDS:
        if sum(1 for item in actions if item.kind == kind) > 1:
            raise ArtifactValidationError(
                f"action kind {kind.value} may only be declared once per node"
            )
    sizes = [
        (item.kind, item.size_bb) for item in actions if item.kind in _SIZED_KINDS
    ]
    if len(set(sizes)) != len(sizes):
        raise ArtifactValidationError("sized actions must have distinct sizes per kind")
    return tuple(actions)


def _entries_from_list(
    value: Any,
    action_ids: tuple[str, ...],
    game_spec: GameSpec,
) -> tuple[ComboStrategy, ...]:
    items = _list(value, "entries")
    if not items:
        raise ArtifactValidationError("an artifact requires at least one combo entry")
    if len(items) > MAX_ENTRIES:
        raise ArtifactValidationError(
            f"a node has at most {MAX_ENTRIES} distinct combinations"
        )
    board = set(game_spec.board)
    entries: list[ComboStrategy] = []
    ev_flags: set[bool] = set()
    for item in items:
        entry = _mapping(item, "entry")
        _exact_keys(entry, {"combo", "weight", "policies"}, "entry")
        combo = _combo(entry["combo"])
        cards = {combo[:2], combo[2:]}
        conflict = sorted(cards & board)
        if conflict:
            raise ArtifactValidationError(
                f"combo {combo} conflicts with the board card(s) {conflict}"
            )
        weight = _decimal(
            entry["weight"],
            "entry.weight",
            places=WEIGHT_DECIMAL_PLACES,
            allow_negative=False,
        )
        if weight <= 0 or weight > 1:
            raise ArtifactValidationError(
                f"combo {combo} weight must be greater than 0 and at most 1"
            )
        policies = _policies_from_list(entry["policies"], action_ids, combo)
        ev_flags.add(policies[0].ev is not None)
        entries.append(ComboStrategy(combo=combo, weight=weight, policies=policies))

    combos = [item.combo for item in entries]
    if len(set(combos)) != len(combos):
        raise ArtifactValidationError("combo entries must be unique")
    if combos != sorted(combos):
        raise ArtifactValidationError("combo entries must be sorted by combo")
    if len(ev_flags) != 1:
        raise ArtifactValidationError(
            "EV must be declared for every action of every combo, or for none"
        )
    return tuple(entries)


def _policies_from_list(
    value: Any,
    action_ids: tuple[str, ...],
    combo: str,
) -> tuple[ActionPolicy, ...]:
    items = _list(value, "entry.policies")
    policies: list[ActionPolicy] = []
    ev_flags: set[bool] = set()
    for item in items:
        entry = _mapping(item, "policy")
        _exact_keys(entry, {"action_id", "probability", "ev"}, "policy")
        action_id = _text(entry["action_id"], "policy.action_id")
        probability = _decimal(
            entry["probability"],
            f"combo {combo} probability",
            places=PROBABILITY_DECIMAL_PLACES,
            allow_negative=False,
        )
        if probability < 0 or probability > 1:
            raise ArtifactValidationError(
                f"combo {combo} action {action_id!r} probability must be within [0, 1]"
            )
        ev_value = entry["ev"]
        ev = (
            None
            if ev_value is None
            else _decimal(ev_value, f"combo {combo} ev", places=EV_DECIMAL_PLACES)
        )
        ev_flags.add(ev is not None)
        policies.append(
            ActionPolicy(action_id=action_id, probability=probability, ev=ev)
        )

    observed = [item.action_id for item in policies]
    if len(set(observed)) != len(observed):
        raise ArtifactValidationError(f"combo {combo} repeats an action")
    unknown = sorted(set(observed) - set(action_ids))
    if unknown:
        raise ArtifactValidationError(
            f"combo {combo} references undeclared action(s) {unknown}"
        )
    missing = sorted(set(action_ids) - set(observed))
    if missing:
        raise ArtifactValidationError(
            f"combo {combo} is missing declared action(s) {missing}"
        )
    if tuple(observed) != tuple(sorted(action_ids)):
        raise ArtifactValidationError(
            f"combo {combo} policies must be sorted by action_id"
        )
    if len(ev_flags) != 1:
        raise ArtifactValidationError(
            f"combo {combo} must declare EV for every action or for none"
        )
    total = sum((item.probability for item in policies), start=Decimal("0"))
    if abs(total - Decimal("1")) > PROBABILITY_SUM_TOLERANCE:
        raise ArtifactValidationError(
            f"combo {combo} probabilities sum to {_decimal_text(total)}; "
            f"tolerance is {_decimal_text(PROBABILITY_SUM_TOLERANCE)}"
        )
    return tuple(policies)


def _provenance_from_dict(value: Any) -> ArtifactProvenance:
    payload = _mapping(value, "provenance")
    _exact_keys(
        payload,
        {
            "solver_name",
            "solver_version",
            "solver_config_id",
            "generated_at",
            "quality",
            "quality_report_id",
            "license",
        },
        "provenance",
    )
    try:
        quality = SolutionQuality(_text(payload["quality"], "provenance.quality"))
    except ValueError as exc:
        raise ArtifactValidationError(f"unknown provenance quality: {exc}") from exc
    report_id = payload["quality_report_id"]
    return ArtifactProvenance(
        solver_name=_text(payload["solver_name"], "provenance.solver_name"),
        solver_version=_text(payload["solver_version"], "provenance.solver_version"),
        solver_config_id=_text(
            payload["solver_config_id"], "provenance.solver_config_id"
        ),
        generated_at=_text(payload["generated_at"], "provenance.generated_at"),
        quality=quality,
        quality_report_id=(
            None if report_id is None else _text(report_id, "provenance.quality_report_id")
        ),
        license=_text(payload["license"], "provenance.license"),
    )


def _require_identity(artifact: StrategyArtifact, solution: SolutionSpec) -> None:
    if artifact.solution_id != solution.solution_id:
        raise ArtifactValidationError(
            "artifact solution_id does not match the catalog entry: "
            f"{artifact.solution_id!r} != {solution.solution_id!r}"
        )
    expected_fingerprint = solution.game_spec.fingerprint
    if artifact.game_spec_fingerprint != expected_fingerprint:
        raise ArtifactValidationError(
            "artifact game_spec_fingerprint does not match the solution GameSpec: "
            f"{artifact.game_spec_fingerprint} != {expected_fingerprint}"
        )
    if artifact.action_tree_version != solution.action_tree_version:
        raise ArtifactValidationError(
            "artifact action_tree_version does not match the solution: "
            f"{artifact.action_tree_version!r} != {solution.action_tree_version!r}"
        )
    if artifact.provenance.quality != solution.quality:
        raise ArtifactValidationError(
            "artifact quality label does not match the catalog entry: "
            f"{artifact.provenance.quality.value} != {solution.quality.value}. "
            "Verification never upgrades a quality label."
        )
    if artifact.provenance.solver_name != solution.solver_name:
        raise ArtifactValidationError(
            "artifact solver name does not match the solution reference"
        )
    if artifact.provenance.solver_version != solution.solver_version:
        raise ArtifactValidationError(
            "artifact solver version does not match the solution reference"
        )


def _require_quality_placement(
    artifact: StrategyArtifact,
    solution: SolutionSpec,
) -> None:
    """Refuse artifacts that sit in a test directory while claiming ``verified``."""

    if artifact.provenance.quality != SolutionQuality.VERIFIED:
        return
    parts = {part.lower() for part in Path(solution.artifact_id).parts}
    if parts & _TEST_ONLY_DIRECTORIES:
        raise ArtifactValidationError(
            "artifacts stored under a test fixture directory cannot claim 'verified'"
        )


def canonical_combo(combo: str) -> str:
    """Validate a two-card combination and return its single canonical spelling.

    Cards are ordered by descending rank, then by the ``c < d < h < s`` suit
    order, so exactly one spelling of every combination is legal.
    """

    if not isinstance(combo, str) or len(combo) != 4:
        raise ArtifactValidationError(f"combo {combo!r} must be exactly two cards")
    cards = (combo[:2], combo[2:])
    for card in cards:
        if not CARD_PATTERN.fullmatch(card):
            raise ArtifactValidationError(f"combo {combo!r} contains an invalid card")
    if cards[0] == cards[1]:
        raise ArtifactValidationError(f"combo {combo!r} repeats the same card")
    canonical = "".join(sorted(cards, key=_card_sort_key))
    if canonical != combo:
        raise ArtifactValidationError(
            f"combo {combo!r} is not canonical; expected {canonical!r}"
        )
    return combo


def _combo(value: Any) -> str:
    return canonical_combo(_text(value, "entry.combo"))


def _card_sort_key(card: str) -> tuple[int, int]:
    return (-_RANK_ORDER.index(card[0]), _SUIT_ORDER.index(card[1]))


_READ = StrictReader(ArtifactValidationError)

_exact_keys = _READ.exact_keys
_mapping = _READ.mapping
_list = _READ.sequence
_text = _READ.text
_identifier = _READ.text
_decimal = _READ.decimal
_decimal_text = decimal_text
