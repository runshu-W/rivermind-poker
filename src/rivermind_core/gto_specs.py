from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from rivermind_core.models import BettingRound, CARD_PATTERN, GameType, PlayerPosition


GAME_SPEC_SCHEMA_VERSION = "game-spec/1.0.0"
SOLUTION_SPEC_SCHEMA_VERSION = "solution-spec/1.0.0"
SOLUTION_CATALOG_SCHEMA_VERSION = "solution-catalog/1.0.0"

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SpecValidationError(ValueError):
    """Raised when GTO metadata cannot be used without inventing information."""


class SolutionObjective(StrEnum):
    CHIP_EV = "chip_ev"
    ICM = "icm"
    PKO = "pko"


class SolutionQuality(StrEnum):
    VERIFIED = "verified"
    FAST_APPROX = "fast_approx"
    EXPERIMENTAL = "experimental"
    TEST_ONLY = "test_only"


@dataclass(frozen=True, slots=True)
class RakeSpec:
    model_id: str
    percent: Decimal
    cap_bb: Decimal

    def __post_init__(self) -> None:
        _validate_id(self.model_id, "rake model id")
        _validate_decimal(self.percent, "rake percent", minimum=Decimal("0"))
        _validate_decimal(self.cap_bb, "rake cap", minimum=Decimal("0"))
        if self.percent > Decimal("100"):
            raise SpecValidationError("rake percent cannot exceed 100")

    def to_dict(self) -> dict[str, str]:
        return {
            "model_id": self.model_id,
            "percent": _decimal_text(self.percent),
            "cap_bb": _decimal_text(self.cap_bb),
        }


@dataclass(frozen=True, slots=True)
class PositionStack:
    position: PlayerPosition
    remaining_bb: Decimal

    def __post_init__(self) -> None:
        _validate_decimal(
            self.remaining_bb,
            f"remaining stack for {self.position.value}",
            minimum=Decimal("0"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "position": self.position.value,
            "remaining_bb": _decimal_text(self.remaining_bb),
        }


@dataclass(frozen=True, slots=True)
class PositionAnte:
    position: PlayerPosition
    ante_bb: Decimal

    def __post_init__(self) -> None:
        _validate_decimal(
            self.ante_bb,
            f"ante for {self.position.value}",
            minimum=Decimal("0"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "position": self.position.value,
            "ante_bb": _decimal_text(self.ante_bb),
        }


@dataclass(frozen=True, slots=True)
class GameSpec:
    game_type: GameType
    players_dealt: int
    small_blind_bb: Decimal
    big_blind_bb: Decimal
    antes: tuple[PositionAnte, ...]
    objective: SolutionObjective
    tournament_context_id: str | None
    rake: RakeSpec | None
    street: BettingRound
    board: tuple[str, ...]
    player_to_act: PlayerPosition
    active_positions: tuple[PlayerPosition, ...]
    stacks: tuple[PositionStack, ...]
    action_history: tuple[str, ...]
    pot_bb: Decimal

    def __post_init__(self) -> None:
        if not 2 <= self.players_dealt <= 9:
            raise SpecValidationError("players_dealt must be between 2 and 9")
        _validate_decimal(
            self.small_blind_bb,
            "small blind",
            minimum=Decimal("0"),
        )
        _validate_decimal(
            self.big_blind_bb,
            "big blind",
            minimum=Decimal("0"),
            positive=True,
        )
        if self.small_blind_bb > self.big_blind_bb:
            raise SpecValidationError("small blind cannot exceed big blind")
        _validate_decimal(self.pot_bb, "pot", minimum=Decimal("0"))
        if self.street == BettingRound.SHOWDOWN:
            raise SpecValidationError("showdown is not a decision street")
        expected_board = {
            BettingRound.PREFLOP: 0,
            BettingRound.FLOP: 3,
            BettingRound.TURN: 4,
            BettingRound.RIVER: 5,
        }[self.street]
        if len(self.board) != expected_board:
            raise SpecValidationError(
                f"{self.street.value} nodes require {expected_board} visible board cards"
            )
        if any(not CARD_PATTERN.fullmatch(card) for card in self.board):
            raise SpecValidationError("board contains an invalid card code")
        if len(set(self.board)) != len(self.board):
            raise SpecValidationError("board cards must be unique")

        stack_positions = tuple(item.position for item in self.stacks)
        if len(self.stacks) != self.players_dealt:
            raise SpecValidationError("stacks must contain every dealt position")
        if len(set(stack_positions)) != len(stack_positions):
            raise SpecValidationError("stack positions must be unique")
        if tuple(sorted(stack_positions, key=lambda item: item.value)) != stack_positions:
            raise SpecValidationError("stacks must be sorted by canonical position value")
        if not self.active_positions:
            raise SpecValidationError("at least one active position is required")
        if len(set(self.active_positions)) != len(self.active_positions):
            raise SpecValidationError("active positions must be unique")
        if tuple(sorted(self.active_positions, key=lambda item: item.value)) != self.active_positions:
            raise SpecValidationError(
                "active positions must be sorted by canonical position value"
            )
        if not set(self.active_positions).issubset(stack_positions):
            raise SpecValidationError("active positions must be dealt positions")
        if self.player_to_act not in self.active_positions:
            raise SpecValidationError("player_to_act must still be active")

        ante_positions = tuple(item.position for item in self.antes)
        if len(set(ante_positions)) != len(ante_positions):
            raise SpecValidationError("ante positions must be unique")
        if tuple(sorted(ante_positions, key=lambda item: item.value)) != ante_positions:
            raise SpecValidationError("antes must be sorted by canonical position value")
        if not set(ante_positions).issubset(stack_positions):
            raise SpecValidationError("ante positions must be dealt positions")

        if any(not item or len(item) > 256 for item in self.action_history):
            raise SpecValidationError("action history tokens must be 1 to 256 characters")
        if self.game_type == GameType.CASH:
            if self.objective != SolutionObjective.CHIP_EV:
                raise SpecValidationError("cash games only support chip_ev")
            if self.tournament_context_id is not None:
                raise SpecValidationError("cash games cannot have tournament context")
        else:
            if self.rake is not None:
                raise SpecValidationError("tournament nodes cannot have cash rake metadata")
            if self.objective in {SolutionObjective.ICM, SolutionObjective.PKO}:
                if self.tournament_context_id is None:
                    raise SpecValidationError(
                        "ICM and PKO nodes require tournament_context_id"
                    )
        if self.tournament_context_id is not None:
            _validate_id(self.tournament_context_id, "tournament context id")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": GAME_SPEC_SCHEMA_VERSION,
            "game_type": self.game_type.value,
            "players_dealt": self.players_dealt,
            "blinds_bb": {
                "small": _decimal_text(self.small_blind_bb),
                "big": _decimal_text(self.big_blind_bb),
            },
            "antes": [item.to_dict() for item in self.antes],
            "objective": self.objective.value,
            "tournament_context_id": self.tournament_context_id,
            "rake": None if self.rake is None else self.rake.to_dict(),
            "node": {
                "street": self.street.value,
                "board": list(self.board),
                "player_to_act": self.player_to_act.value,
                "active_positions": [item.value for item in self.active_positions],
                "stacks": [item.to_dict() for item in self.stacks],
                "action_history": list(self.action_history),
                "pot_bb": _decimal_text(self.pot_bb),
            },
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SolutionSpec:
    solution_id: str
    game_spec: GameSpec
    solver_name: str
    solver_version: str
    action_tree_version: str
    quality: SolutionQuality
    artifact_id: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.solution_id, "solution id"),
            (self.solver_name, "solver name"),
            (self.solver_version, "solver version"),
            (self.action_tree_version, "action tree version"),
            (self.artifact_id, "artifact id"),
        ):
            _validate_id(value, field)
        if not _SHA256_PATTERN.fullmatch(self.artifact_sha256):
            raise SpecValidationError("artifact_sha256 must be 64 lowercase hex characters")
        if self.game_spec.game_type == GameType.CASH and self.game_spec.rake is None:
            raise SpecValidationError("cash solutions require explicit rake metadata")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SOLUTION_SPEC_SCHEMA_VERSION,
            "solution_id": self.solution_id,
            "game_spec": self.game_spec.to_dict(),
            "solver": {
                "name": self.solver_name,
                "version": self.solver_version,
            },
            "action_tree_version": self.action_tree_version,
            "quality": self.quality.value,
            "artifact": {
                "id": self.artifact_id,
                "sha256": self.artifact_sha256,
            },
        }


@dataclass(frozen=True, slots=True)
class SolutionCatalog:
    catalog_id: str
    catalog_version: str
    solutions: tuple[SolutionSpec, ...]

    def __post_init__(self) -> None:
        _validate_id(self.catalog_id, "catalog id")
        _validate_id(self.catalog_version, "catalog version")
        identifiers = [item.solution_id for item in self.solutions]
        if len(identifiers) != len(set(identifiers)):
            raise SpecValidationError("solution ids must be unique within a catalog")
        artifacts = [item.artifact_id for item in self.solutions]
        if len(artifacts) != len(set(artifacts)):
            raise SpecValidationError(
                "artifact ids must be unique within a catalog; two solutions sharing "
                "one file cannot both stay valid when it is rewritten"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SOLUTION_CATALOG_SCHEMA_VERSION,
            "catalog_id": self.catalog_id,
            "catalog_version": self.catalog_version,
            "solutions": [item.to_dict() for item in self.solutions],
        }


#: Catalogs are metadata indexes, not data files.
MAX_CATALOG_BYTES = 64 * 1024 * 1024


def load_solution_catalog(path: Path) -> SolutionCatalog:
    try:
        if path.stat().st_size > MAX_CATALOG_BYTES:
            raise SpecValidationError(
                f"solution catalog exceeds the {MAX_CATALOG_BYTES} byte limit"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecValidationError(f"cannot load solution catalog: {exc}") from exc
    except RecursionError as exc:
        raise SpecValidationError(
            "solution catalog JSON is nested too deeply to validate"
        ) from exc
    if not isinstance(payload, dict):
        raise SpecValidationError("solution catalog root must be an object")
    return solution_catalog_from_dict(payload)


def solution_catalog_from_dict(payload: Mapping[str, Any]) -> SolutionCatalog:
    _require_keys(
        payload,
        {"schema_version", "catalog_id", "catalog_version", "solutions"},
        "solution catalog",
    )
    _require_schema(payload["schema_version"], SOLUTION_CATALOG_SCHEMA_VERSION)
    solutions = payload["solutions"]
    if not isinstance(solutions, list):
        raise SpecValidationError("solutions must be an array")
    try:
        return SolutionCatalog(
            catalog_id=_string(payload["catalog_id"], "catalog_id"),
            catalog_version=_string(payload["catalog_version"], "catalog_version"),
            solutions=tuple(_solution_from_dict(item) for item in solutions),
        )
    except SpecValidationError:
        raise
    except ValueError as exc:
        raise SpecValidationError(f"catalog contains an invalid enum value: {exc}") from exc


def _solution_from_dict(value: Any) -> SolutionSpec:
    payload = _mapping(value, "solution")
    _require_keys(
        payload,
        {
            "schema_version",
            "solution_id",
            "game_spec",
            "solver",
            "action_tree_version",
            "quality",
            "artifact",
        },
        "solution",
    )
    _require_schema(payload["schema_version"], SOLUTION_SPEC_SCHEMA_VERSION)
    solver = _mapping(payload["solver"], "solver")
    _require_keys(solver, {"name", "version"}, "solver")
    artifact = _mapping(payload["artifact"], "artifact")
    _require_keys(artifact, {"id", "sha256"}, "artifact")
    return SolutionSpec(
        solution_id=_string(payload["solution_id"], "solution_id"),
        game_spec=_game_from_dict(payload["game_spec"]),
        solver_name=_string(solver["name"], "solver.name"),
        solver_version=_string(solver["version"], "solver.version"),
        action_tree_version=_string(
            payload["action_tree_version"], "action_tree_version"
        ),
        quality=SolutionQuality(_string(payload["quality"], "quality")),
        artifact_id=_string(artifact["id"], "artifact.id"),
        artifact_sha256=_string(artifact["sha256"], "artifact.sha256"),
    )


def _game_from_dict(value: Any) -> GameSpec:
    payload = _mapping(value, "game_spec")
    _require_keys(
        payload,
        {
            "schema_version",
            "game_type",
            "players_dealt",
            "blinds_bb",
            "antes",
            "objective",
            "tournament_context_id",
            "rake",
            "node",
        },
        "game_spec",
    )
    _require_schema(payload["schema_version"], GAME_SPEC_SCHEMA_VERSION)
    blinds = _mapping(payload["blinds_bb"], "blinds_bb")
    _require_keys(blinds, {"small", "big"}, "blinds_bb")
    node = _mapping(payload["node"], "node")
    _require_keys(
        node,
        {
            "street",
            "board",
            "player_to_act",
            "active_positions",
            "stacks",
            "action_history",
            "pot_bb",
        },
        "node",
    )
    ante_values = _list(payload["antes"], "antes")
    stack_values = _list(node["stacks"], "node.stacks")
    rake_value = payload["rake"]
    rake = None
    if rake_value is not None:
        rake_payload = _mapping(rake_value, "rake")
        _require_keys(rake_payload, {"model_id", "percent", "cap_bb"}, "rake")
        rake = RakeSpec(
            model_id=_string(rake_payload["model_id"], "rake.model_id"),
            percent=_decimal(rake_payload["percent"], "rake.percent"),
            cap_bb=_decimal(rake_payload["cap_bb"], "rake.cap_bb"),
        )
    return GameSpec(
        game_type=GameType(_string(payload["game_type"], "game_type")),
        players_dealt=_integer(payload["players_dealt"], "players_dealt"),
        small_blind_bb=_decimal(blinds["small"], "blinds_bb.small"),
        big_blind_bb=_decimal(blinds["big"], "blinds_bb.big"),
        antes=tuple(_ante_from_dict(item) for item in ante_values),
        objective=SolutionObjective(_string(payload["objective"], "objective")),
        tournament_context_id=_optional_string(
            payload["tournament_context_id"], "tournament_context_id"
        ),
        rake=rake,
        street=BettingRound(_string(node["street"], "node.street")),
        board=tuple(_string(item, "node.board item") for item in _list(node["board"], "node.board")),
        player_to_act=PlayerPosition(
            _string(node["player_to_act"], "node.player_to_act")
        ),
        active_positions=tuple(
            PlayerPosition(_string(item, "node.active_positions item"))
            for item in _list(node["active_positions"], "node.active_positions")
        ),
        stacks=tuple(_stack_from_dict(item) for item in stack_values),
        action_history=tuple(
            _string(item, "node.action_history item")
            for item in _list(node["action_history"], "node.action_history")
        ),
        pot_bb=_decimal(node["pot_bb"], "node.pot_bb"),
    )


def _ante_from_dict(value: Any) -> PositionAnte:
    payload = _mapping(value, "ante")
    _require_keys(payload, {"position", "ante_bb"}, "ante")
    return PositionAnte(
        PlayerPosition(_string(payload["position"], "ante.position")),
        _decimal(payload["ante_bb"], "ante.ante_bb"),
    )


def _stack_from_dict(value: Any) -> PositionStack:
    payload = _mapping(value, "stack")
    _require_keys(payload, {"position", "remaining_bb"}, "stack")
    return PositionStack(
        PlayerPosition(_string(payload["position"], "stack.position")),
        _decimal(payload["remaining_bb"], "stack.remaining_bb"),
    )


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal_text(value: Decimal) -> str:
    """Format without ``normalize()``.

    ``normalize()`` rounds against the ambient decimal context, which would make
    a published node fingerprint depend on global interpreter state rather than
    on the node.  Stripping trailing zeros by hand keeps the hash a property of
    the data alone.
    """

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-", "-0"}:
        return "0"
    return text


def _validate_decimal(
    value: Decimal,
    field: str,
    *,
    minimum: Decimal,
    positive: bool = False,
) -> None:
    if not value.is_finite():
        raise SpecValidationError(f"{field} must be finite")
    if value < minimum or (positive and value == minimum):
        comparator = "positive" if positive else f"at least {minimum}"
        raise SpecValidationError(f"{field} must be {comparator}")


def _validate_id(value: str, field: str) -> None:
    if not _ID_PATTERN.fullmatch(value):
        raise SpecValidationError(f"{field} has an invalid identifier")


def _require_schema(actual: Any, expected: str) -> None:
    if actual != expected:
        raise SpecValidationError(f"unsupported schema version: {actual!r}")


def _require_keys(payload: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise SpecValidationError(
            f"{field} keys do not match contract; missing={missing}, unknown={unknown}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SpecValidationError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise SpecValidationError(f"{field} must be an array")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SpecValidationError(f"{field} must be a string")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SpecValidationError(f"{field} must be an integer")
    return value


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise SpecValidationError(f"{field} must be a decimal string")
    try:
        return Decimal(value)
    except (ArithmeticError, ValueError) as exc:
        raise SpecValidationError(f"{field} must be a valid decimal string") from exc
