"""Solve quality reports.

A strategy artifact says *what* the strategy is.  This module says *how it was
produced and how good it is* — solver configuration, licence, convergence
evidence, evaluation scope, and an explicit list of what the report does not
prove.

The report is a separate, separately-hashed document on purpose.  Strategy
content and quality evidence should be reviewable independently: an artifact can
be byte-perfect and still be worthless, and a report is not allowed to travel
inside the thing it is judging.

Nothing here grants ``verified``.  Granting is the quality gate's job
(:mod:`rivermind_core.quality_gate`), and it needs a report plus signatures.
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
    StrictReader,
    canonical_json_bytes,
    decimal_text,
    parse_timestamp,
)
from rivermind_core.gto_specs import GameSpec, SolutionObjective, SpecValidationError


SOLVE_QUALITY_REPORT_SCHEMA_VERSION = "solve-quality-report/1.0.0"

#: Reports are small documents; anything larger is a mistake, not a report.
MAX_REPORT_BYTES = 1 * 1024 * 1024

CONVERGENCE_DECIMAL_PLACES = 9

_REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class ReportValidationError(SpecValidationError):
    """Raised when a solve quality report cannot be used as quality evidence."""


_READ = StrictReader(ReportValidationError)


class SourceOrigin(StrEnum):
    IN_HOUSE = "in_house"
    LICENSED = "licensed"
    PUBLIC_DATASET = "public_dataset"


class ClaimClass(StrEnum):
    """What the report is entitled to claim.

    ``equilibrium_approximation`` is the strong claim — "this approximates the
    unique two-player zero-sum equilibrium to within the stated distance".  It is
    only meaningful for heads-up chip-EV play.

    ``empirical_quality`` is the honest claim everywhere else: multiway pots and
    ICM/PKO objectives have no equivalent uniqueness result, so the report may
    only assert measured performance under a stated method.
    """

    EQUILIBRIUM_APPROXIMATION = "equilibrium_approximation"
    EMPIRICAL_QUALITY = "empirical_quality"


class ConvergenceMetric(StrEnum):
    EXPLOITABILITY = "exploitability"
    NASH_DISTANCE = "nash_distance"
    AVERAGE_REGRET = "average_regret"
    BEST_RESPONSE_GAP = "best_response_gap"


class ConvergenceUnit(StrEnum):
    BB_PER_100 = "bb_per_100"
    BB = "bb"
    PERCENT_OF_POT = "percent_of_pot"


#: Metrics that only carry their usual meaning in a two-player zero-sum game.
_ZERO_SUM_METRICS = frozenset(
    {
        ConvergenceMetric.EXPLOITABILITY,
        ConvergenceMetric.NASH_DISTANCE,
        ConvergenceMetric.BEST_RESPONSE_GAP,
    }
)


@dataclass(frozen=True, slots=True)
class SolveSource:
    """Where the strategy came from and what may legally be done with it."""

    origin: SourceOrigin
    provider: str
    license_id: str
    obtained_at: str
    display_allowed: bool
    redistribution_allowed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin.value,
            "provider": self.provider,
            "license_id": self.license_id,
            "obtained_at": self.obtained_at,
            "display_allowed": self.display_allowed,
            "redistribution_allowed": self.redistribution_allowed,
        }


@dataclass(frozen=True, slots=True)
class SolveRun:
    """The solve itself: configuration, effort spent, and convergence evidence."""

    started_at: str
    completed_at: str
    iterations: int
    convergence_metric: ConvergenceMetric
    convergence_value: Decimal
    convergence_unit: ConvergenceUnit
    convergence_threshold: Decimal
    board_abstraction: str
    bet_size_abstraction: str
    card_isomorphism_used: bool
    #: The rake schedule the solve actually modelled, or ``None`` for an unraked
    #: node.  A free-text "we handled rake somehow" note is not checkable; this
    #: is, against the node's own ``RakeSpec.model_id``.
    rake_model_id: str | None

    def __post_init__(self) -> None:
        if parse_timestamp(self.completed_at) < parse_timestamp(self.started_at):
            raise ReportValidationError("solve.completed_at precedes solve.started_at")
        if self.convergence_value < 0 or self.convergence_threshold < 0:
            raise ReportValidationError("convergence values must not be negative")

    @property
    def meets_threshold(self) -> bool:
        return self.convergence_value <= self.convergence_threshold

    def to_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "iterations": self.iterations,
            "convergence_metric": self.convergence_metric.value,
            "convergence_value": decimal_text(self.convergence_value),
            "convergence_unit": self.convergence_unit.value,
            "convergence_threshold": decimal_text(self.convergence_threshold),
            "board_abstraction": self.board_abstraction,
            "bet_size_abstraction": self.bet_size_abstraction,
            "card_isomorphism_used": self.card_isomorphism_used,
            "rake_model_id": self.rake_model_id,
        }


@dataclass(frozen=True, slots=True)
class SolveEvaluation:
    """How thoroughly the solve was checked, and by whom."""

    scope: str
    board_sample_size: int
    independent_recheck: bool
    recheck_tool_name: str | None
    recheck_tool_version: str | None

    def __post_init__(self) -> None:
        declared = (self.recheck_tool_name, self.recheck_tool_version)
        if self.independent_recheck and None in declared:
            raise ReportValidationError(
                "evaluation.independent_recheck requires both recheck_tool_name "
                "and recheck_tool_version"
            )
        if not self.independent_recheck and declared != (None, None):
            raise ReportValidationError(
                "a recheck tool implies evaluation.independent_recheck"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "board_sample_size": self.board_sample_size,
            "independent_recheck": self.independent_recheck,
            "recheck_tool_name": self.recheck_tool_name,
            "recheck_tool_version": self.recheck_tool_version,
        }


@dataclass(frozen=True, slots=True)
class SolveQualityReport:
    """Quality evidence for exactly one solution."""

    report_id: str
    solution_id: str
    game_spec_fingerprint: str
    action_tree_version: str
    solver_name: str
    solver_version: str
    solver_config_id: str
    solver_config_sha256: str
    claim_class: ClaimClass
    source: SolveSource
    solve: SolveRun
    evaluation: SolveEvaluation
    limits: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _REPORT_ID_PATTERN.fullmatch(self.report_id):
            raise ReportValidationError("report_id has an invalid identifier")
        if parse_timestamp(self.source.obtained_at) > parse_timestamp(
            self.solve.completed_at
        ):
            raise ReportValidationError(
                "source.obtained_at is after the solve finished; you cannot solve "
                "with data or a licence you did not have yet"
            )
        if self.evaluation.independent_recheck and (
            self.evaluation.recheck_tool_name == self.solver_name
        ):
            raise ReportValidationError(
                "an independent recheck cannot be performed by the solver that "
                f"produced the solve ({self.solver_name!r})"
            )
        if (
            self.claim_class is ClaimClass.EQUILIBRIUM_APPROXIMATION
            and self.solve.convergence_metric not in _ZERO_SUM_METRICS
        ):
            raise ReportValidationError(
                f"{self.solve.convergence_metric.value} measures training progress, "
                "not distance from equilibrium; equilibrium_approximation requires "
                f"one of {sorted(item.value for item in _ZERO_SUM_METRICS)}"
            )
        if not self.limits:
            raise ReportValidationError(
                "a report must state at least one explicit limit; "
                "a report that claims no limits is not evidence"
            )
        if len(set(self.limits)) != len(self.limits):
            raise ReportValidationError("limits must not repeat")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SOLVE_QUALITY_REPORT_SCHEMA_VERSION,
            "report_id": self.report_id,
            "solution_id": self.solution_id,
            "game_spec_fingerprint": self.game_spec_fingerprint,
            "action_tree_version": self.action_tree_version,
            "solver": {
                "name": self.solver_name,
                "version": self.solver_version,
                "config_id": self.solver_config_id,
                "config_sha256": self.solver_config_sha256,
            },
            "claim_class": self.claim_class.value,
            "source": self.source.to_dict(),
            "solve": self.solve.to_dict(),
            "evaluation": self.evaluation.to_dict(),
            "limits": list(self.limits),
        }


def serialize_solve_quality_report(report: SolveQualityReport) -> bytes:
    """Return the one byte sequence that represents ``report``."""

    return canonical_json_bytes(report.to_dict())


def write_solve_quality_report(path: Path, report: SolveQualityReport) -> str:
    """Write ``report`` in canonical form and return its SHA-256."""

    payload = serialize_solve_quality_report(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def load_solve_quality_report(path: Path) -> tuple[SolveQualityReport, str]:
    """Load a report from disk and return it with the SHA-256 of its bytes."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReportValidationError(f"cannot read solve quality report: {exc}") from exc
    if size > MAX_REPORT_BYTES:
        raise ReportValidationError(
            f"solve quality report exceeds the {MAX_REPORT_BYTES} byte limit"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportValidationError(f"cannot read solve quality report: {exc}") from exc
    payload = _READ.load_json(raw, "solve quality report")
    return solve_quality_report_from_dict(payload), hashlib.sha256(raw).hexdigest()


def solve_quality_report_from_dict(payload: Any) -> SolveQualityReport:
    """Parse a report under the frozen v1.0.0 contract."""

    document = _READ.mapping(payload, "report")
    _READ.exact_keys(
        document,
        {
            "schema_version",
            "report_id",
            "solution_id",
            "game_spec_fingerprint",
            "action_tree_version",
            "solver",
            "claim_class",
            "source",
            "solve",
            "evaluation",
            "limits",
        },
        "report",
    )
    _READ.schema_version(document, SOLVE_QUALITY_REPORT_SCHEMA_VERSION)

    solver = _READ.mapping(document["solver"], "solver")
    _READ.exact_keys(solver, {"name", "version", "config_id", "config_sha256"}, "solver")
    limits = _READ.sequence(document["limits"], "limits")
    if len(limits) > 32:
        raise ReportValidationError("limits may list at most 32 entries")

    return SolveQualityReport(
        report_id=_READ.text(document["report_id"], "report_id"),
        solution_id=_READ.text(document["solution_id"], "solution_id"),
        game_spec_fingerprint=_READ.sha256(
            document["game_spec_fingerprint"], "game_spec_fingerprint"
        ),
        action_tree_version=_READ.text(
            document["action_tree_version"], "action_tree_version"
        ),
        solver_name=_READ.text(solver["name"], "solver.name"),
        solver_version=_READ.text(solver["version"], "solver.version"),
        solver_config_id=_READ.text(solver["config_id"], "solver.config_id"),
        solver_config_sha256=_READ.sha256(
            solver["config_sha256"], "solver.config_sha256"
        ),
        claim_class=_enum(ClaimClass, document["claim_class"], "claim_class"),
        source=_source_from_dict(document["source"]),
        solve=_solve_from_dict(document["solve"]),
        evaluation=_evaluation_from_dict(document["evaluation"]),
        limits=tuple(
            _READ.long_text(item, f"limits[{index}]")
            for index, item in enumerate(limits)
        ),
    )


def check_report_against_node(report: SolveQualityReport, game_spec: GameSpec) -> None:
    """Cross-check a report against the node it claims to describe."""

    _check_rake_scope(report, game_spec)
    _check_claim_class(report, game_spec)


def _check_rake_scope(report: SolveQualityReport, game_spec: GameSpec) -> None:
    """The solve must have modelled this node's actual rake, or none at all.

    The previous shape of this rule searched the free-text ``limits`` for the
    word "rake", which a report saying "rake was ignored completely" satisfied.
    A structured id compared against the node's own ``RakeSpec`` cannot be
    talked around.
    """

    declared = report.solve.rake_model_id
    if game_spec.rake is None:
        if declared is not None:
            raise ReportValidationError(
                f"solve.rake_model_id is {declared!r} but this node has no rake"
            )
        return
    if declared is None:
        raise ReportValidationError(
            "this node is raked under model "
            f"{game_spec.rake.model_id!r}; solve.rake_model_id must say which rake "
            "the solve modelled"
        )
    if declared != game_spec.rake.model_id:
        raise ReportValidationError(
            "the solve modelled a different rake schedule: "
            f"{declared!r} != {game_spec.rake.model_id!r}"
        )


def _check_claim_class(report: SolveQualityReport, game_spec: GameSpec) -> None:
    """Refuse a strong equilibrium claim where no equilibrium result applies.

    Heads-up chip-EV play is two-player zero-sum, so exploitability means what
    the marketing says it means.  Three-handed pots and ICM/PKO objectives are
    neither two-player nor zero-sum; a solver may still be measurably good there,
    but calling it an equilibrium approximation is a claim nobody can support.
    """

    if report.claim_class is not ClaimClass.EQUILIBRIUM_APPROXIMATION:
        return
    if game_spec.players_dealt != 2:
        raise ReportValidationError(
            "equilibrium_approximation requires a two-player node; "
            f"this node deals {game_spec.players_dealt} players. Use empirical_quality."
        )
    if game_spec.objective is not SolutionObjective.CHIP_EV:
        raise ReportValidationError(
            "equilibrium_approximation requires the chip_ev objective; "
            f"{game_spec.objective.value} is not zero-sum in chips. Use empirical_quality."
        )


def _source_from_dict(value: Any) -> SolveSource:
    payload = _READ.mapping(value, "source")
    _READ.exact_keys(
        payload,
        {
            "origin",
            "provider",
            "license_id",
            "obtained_at",
            "display_allowed",
            "redistribution_allowed",
        },
        "source",
    )
    return SolveSource(
        origin=_enum(SourceOrigin, payload["origin"], "source.origin"),
        provider=_READ.text(payload["provider"], "source.provider"),
        license_id=_READ.text(payload["license_id"], "source.license_id"),
        obtained_at=_READ.timestamp(payload["obtained_at"], "source.obtained_at"),
        display_allowed=_READ.flag(payload["display_allowed"], "source.display_allowed"),
        redistribution_allowed=_READ.flag(
            payload["redistribution_allowed"], "source.redistribution_allowed"
        ),
    )


def _solve_from_dict(value: Any) -> SolveRun:
    payload = _READ.mapping(value, "solve")
    _READ.exact_keys(
        payload,
        {
            "started_at",
            "completed_at",
            "iterations",
            "convergence_metric",
            "convergence_value",
            "convergence_unit",
            "convergence_threshold",
            "board_abstraction",
            "bet_size_abstraction",
            "card_isomorphism_used",
            "rake_model_id",
        },
        "solve",
    )
    return SolveRun(
        started_at=_READ.timestamp(payload["started_at"], "solve.started_at"),
        completed_at=_READ.timestamp(payload["completed_at"], "solve.completed_at"),
        iterations=_READ.integer(
            payload["iterations"], "solve.iterations", minimum=1, maximum=10**12
        ),
        convergence_metric=_enum(
            ConvergenceMetric, payload["convergence_metric"], "solve.convergence_metric"
        ),
        convergence_value=_READ.decimal(
            payload["convergence_value"],
            "solve.convergence_value",
            places=CONVERGENCE_DECIMAL_PLACES,
            allow_negative=False,
        ),
        convergence_unit=_enum(
            ConvergenceUnit, payload["convergence_unit"], "solve.convergence_unit"
        ),
        convergence_threshold=_READ.decimal(
            payload["convergence_threshold"],
            "solve.convergence_threshold",
            places=CONVERGENCE_DECIMAL_PLACES,
            allow_negative=False,
        ),
        board_abstraction=_READ.text(
            payload["board_abstraction"], "solve.board_abstraction"
        ),
        bet_size_abstraction=_READ.text(
            payload["bet_size_abstraction"], "solve.bet_size_abstraction"
        ),
        card_isomorphism_used=_READ.flag(
            payload["card_isomorphism_used"], "solve.card_isomorphism_used"
        ),
        rake_model_id=_READ.optional_text(
            payload["rake_model_id"], "solve.rake_model_id"
        ),
    )


def _evaluation_from_dict(value: Any) -> SolveEvaluation:
    payload = _READ.mapping(value, "evaluation")
    _READ.exact_keys(
        payload,
        {
            "scope",
            "board_sample_size",
            "independent_recheck",
            "recheck_tool_name",
            "recheck_tool_version",
        },
        "evaluation",
    )
    return SolveEvaluation(
        scope=_READ.text(payload["scope"], "evaluation.scope"),
        board_sample_size=_READ.integer(
            payload["board_sample_size"],
            "evaluation.board_sample_size",
            minimum=1,
            maximum=10**9,
        ),
        independent_recheck=_READ.flag(
            payload["independent_recheck"], "evaluation.independent_recheck"
        ),
        recheck_tool_name=_READ.optional_text(
            payload["recheck_tool_name"], "evaluation.recheck_tool_name"
        ),
        recheck_tool_version=_READ.optional_text(
            payload["recheck_tool_version"], "evaluation.recheck_tool_version"
        ),
    )


def _enum(enum_class, value: Any, field: str):
    text = _READ.text(value, field)
    try:
        return enum_class(text)
    except ValueError as exc:
        raise ReportValidationError(f"unsupported {field}: {exc}") from exc
