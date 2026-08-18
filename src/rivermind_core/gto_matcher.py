from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from rivermind_core.accounting import calculate_action_ledger
from rivermind_core.board_isomorphism import (
    BOARD_ISOMORPHISM_VERSION,
    SuitPermutation,
    canonical_board_fingerprint,
    permutation_between,
)
from rivermind_core.gto_specs import (
    GameSpec,
    PositionAnte,
    PositionStack,
    RakeSpec,
    SolutionCatalog,
    SolutionObjective,
    SolutionSpec,
    SpecValidationError,
)
from rivermind_core.models import Action, ActionType, BettingRound, HandHistory, PlayerPosition


MATCH_POLICY_VERSION = "gto-match-policy/1.0.0"


class MatchStatus(StrEnum):
    EXACT = "exact"
    #: Identical to a catalog node up to board equivalence. Only reachable when
    #: the caller opts in, so consumers checking for ``EXACT`` keep failing
    #: closed on relabelled boards until they are updated deliberately.
    ISOMORPHIC = "isomorphic"
    APPROXIMATE = "approximate"
    UNSUPPORTED = "unsupported"


class MatchReason(StrEnum):
    EXACT_NODE = "exact_node"
    ISOMORPHIC_NODE = "isomorphic_node"
    WITHIN_EXPLICIT_THRESHOLDS = "within_explicit_thresholds"
    CATALOG_EMPTY = "catalog_empty"
    MISSING_RAKE_METADATA = "missing_rake_metadata"
    NO_HARD_COMPATIBLE_SOLUTION = "no_hard_compatible_solution"
    THRESHOLDS_EXCEEDED = "thresholds_exceeded"
    AMBIGUOUS_BEST_MATCH = "ambiguous_best_match"


@dataclass(frozen=True, slots=True)
class MappingThresholds:
    stack_bb: Decimal = Decimal("5")
    pot_bb: Decimal = Decimal("1")
    small_blind_bb: Decimal = Decimal("0.1")
    big_blind_bb: Decimal = Decimal("0")
    ante_bb: Decimal = Decimal("0.1")
    rake_percent: Decimal = Decimal("0.5")
    rake_cap_bb: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        for field, value in (
            ("stack_bb", self.stack_bb),
            ("pot_bb", self.pot_bb),
            ("small_blind_bb", self.small_blind_bb),
            ("big_blind_bb", self.big_blind_bb),
            ("ante_bb", self.ante_bb),
            ("rake_percent", self.rake_percent),
            ("rake_cap_bb", self.rake_cap_bb),
        ):
            if not value.is_finite() or value < 0:
                raise SpecValidationError(f"mapping threshold {field} must be finite and nonnegative")


@dataclass(frozen=True, slots=True)
class SpecDifference:
    field: str
    observed: str
    solution: str
    absolute_delta: str | None
    threshold: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "field": self.field,
            "observed": self.observed,
            "solution": self.solution,
            "absolute_delta": self.absolute_delta,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class GTOMatchResult:
    status: MatchStatus
    reason: MatchReason
    observed_fingerprint: str
    policy_version: str
    solution: SolutionSpec | None = None
    normalized_distance: Decimal | None = None
    differences: tuple[SpecDifference, ...] = ()
    candidate_solution_ids: tuple[str, ...] = ()
    #: Present only for an isomorphic match: the relabelling that carries the
    #: observed board's suits onto the solution's.
    suit_permutation: "SuitPermutation | None" = None

    @property
    def solution_reference_available(self) -> bool:
        return self.solution is not None

    def to_dict(self) -> dict[str, object]:
        solution = None
        if self.solution is not None:
            solution = {
                "solution_id": self.solution.solution_id,
                "quality": self.solution.quality.value,
                "solver": {
                    "name": self.solution.solver_name,
                    "version": self.solution.solver_version,
                },
                "action_tree_version": self.solution.action_tree_version,
                "artifact": {
                    "id": self.solution.artifact_id,
                    "sha256": self.solution.artifact_sha256,
                },
                "game_spec_fingerprint": self.solution.game_spec.fingerprint,
            }
        payload: dict[str, object] = {
            "status": self.status.value,
            "reason": self.reason.value,
            "policy_version": self.policy_version,
            "observed_fingerprint": self.observed_fingerprint,
            "solution_reference_available": self.solution_reference_available,
            "solution": solution,
            "normalized_distance": (
                None
                if self.normalized_distance is None
                else _decimal_text(self.normalized_distance)
            ),
            "differences": [item.to_dict() for item in self.differences],
            "candidate_solution_ids": list(self.candidate_solution_ids),
            "boundary": (
                "This result maps metadata only. It does not contain or invent "
                "strategy frequencies, actions, or EV."
            ),
        }
        if self.suit_permutation is not None:
            # Only present when board equivalence was actually used, so a
            # default match still serializes exactly as gto-match-policy/1.0.0
            # always did.
            payload["board_isomorphism"] = {
                "isomorphism_version": BOARD_ISOMORPHISM_VERSION,
                "suit_permutation": self.suit_permutation.to_dict(),
                "note": (
                    "The solution was solved on a relabelled board. Combinations "
                    "must be mapped through this permutation before they mean "
                    "anything in the observed hand."
                ),
            }
        return payload


def extract_decision_game_spec(
    hand: HandHistory,
    *,
    before_action: int,
    objective: SolutionObjective = SolutionObjective.CHIP_EV,
    rake: RakeSpec | None = None,
    tournament_context_id: str | None = None,
) -> GameSpec:
    if not 0 <= before_action < len(hand.actions):
        raise SpecValidationError("before_action must identify an existing action sequence")
    target = hand.actions[before_action]
    if target.sequence != before_action:
        raise SpecValidationError("hand action sequences are not canonical")
    if target.action_type not in {
        ActionType.FOLD,
        ActionType.CHECK,
        ActionType.CALL,
        ActionType.BET,
        ActionType.RAISE,
    }:
        raise SpecValidationError("before_action must point to a voluntary decision action")
    if objective in {SolutionObjective.ICM, SolutionObjective.PKO} and tournament_context_id is None:
        raise SpecValidationError("ICM and PKO extraction requires tournament_context_id")
    if hand.game_type.value == "cash" and rake is None:
        # The query remains valid, but matching will fail closed until rake is supplied.
        pass

    position_by_name: dict[str, PlayerPosition] = {}
    for player in hand.players:
        if player.position is None:
            raise SpecValidationError("all players require canonical positions")
        position_by_name[player.name] = player.position

    entries = calculate_action_ledger(hand)
    invested = {player.name: Decimal("0") for player in hand.players}
    returned = {player.name: Decimal("0") for player in hand.players}
    active = {player.name for player in hand.players}
    ante_by_position: dict[PlayerPosition, Decimal] = {}
    action_history: list[str] = []
    pot = Decimal("0")
    for entry in entries[:before_action]:
        action = entry.action
        invested[action.player] += entry.invested
        returned[action.player] += entry.returned
        pot = entry.pot_after
        position = position_by_name[action.player]
        if action.action_type == ActionType.POST_ANTE:
            ante_by_position[position] = (
                ante_by_position.get(position, Decimal("0")) + entry.invested / hand.big_blind
            )
        action_history.append(_action_token(action, position, hand.big_blind))
        if action.action_type == ActionType.FOLD:
            active.discard(action.player)

    if target.player not in active:
        raise SpecValidationError("target player is not active before the selected action")
    board = _visible_board(hand.board, target.street)
    stacks = tuple(
        sorted(
            (
                PositionStack(
                    position=position_by_name[player.name],
                    remaining_bb=(
                        player.starting_stack
                        - invested[player.name]
                        + returned[player.name]
                    )
                    / hand.big_blind,
                )
                for player in hand.players
            ),
            key=lambda item: item.position.value,
        )
    )
    antes = tuple(
        PositionAnte(position=position, ante_bb=amount)
        for position, amount in sorted(
            ante_by_position.items(), key=lambda item: item[0].value
        )
    )
    return GameSpec(
        game_type=hand.game_type,
        players_dealt=len(hand.players),
        small_blind_bb=hand.small_blind / hand.big_blind,
        big_blind_bb=Decimal("1"),
        antes=antes,
        objective=objective,
        tournament_context_id=tournament_context_id,
        rake=rake,
        street=target.street,
        board=board,
        player_to_act=position_by_name[target.player],
        active_positions=tuple(
            sorted(
                (position_by_name[name] for name in active),
                key=lambda item: item.value,
            )
        ),
        stacks=stacks,
        action_history=tuple(action_history),
        pot_bb=pot / hand.big_blind,
    )


def match_game_spec(
    observed: GameSpec,
    catalog: SolutionCatalog,
    *,
    thresholds: MappingThresholds = MappingThresholds(),
    index: "SolutionCatalogIndex | None" = None,
    board_isomorphism: bool = False,
) -> GTOMatchResult:
    """Map one observed node onto the catalog.

    ``index`` is a pure optimization: pass a
    :class:`~rivermind_core.gto_index.SolutionCatalogIndex` built from the same
    catalog to skip rebuilding it on every call.  Results are identical either
    way; a differential test enforces that.

    ``board_isomorphism`` opts into ``board-isomorphism/1.0.0``: a node whose
    board is a relabelling or reordering of a catalog node's board becomes an
    :attr:`MatchStatus.ISOMORPHIC` hit carrying the suit permutation.  It is off
    by default because a caller checking for ``EXACT`` should keep failing closed
    on relabelled boards until it has been updated to apply that permutation.
    With it off, every byte of this function's output is what
    ``gto-match-policy/1.0.0`` always produced.
    """

    common = {
        "observed_fingerprint": observed.fingerprint,
        "policy_version": MATCH_POLICY_VERSION,
    }
    if not catalog.solutions:
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.CATALOG_EMPTY,
            **common,
        )
    if observed.game_type.value == "cash" and observed.rake is None:
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.MISSING_RAKE_METADATA,
            **common,
        )

    index = _resolved_index(catalog, index)
    exact = index.exact_matches(common["observed_fingerprint"])
    if len(exact) == 1:
        return GTOMatchResult(
            status=MatchStatus.EXACT,
            reason=MatchReason.EXACT_NODE,
            solution=exact[0],
            normalized_distance=Decimal("0"),
            **common,
        )
    if len(exact) > 1:
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.AMBIGUOUS_BEST_MATCH,
            candidate_solution_ids=tuple(sorted(item.solution_id for item in exact)),
            **common,
        )

    if board_isomorphism:
        equivalent = index.board_equivalent(canonical_board_fingerprint(observed))
        if len(equivalent) == 1:
            return GTOMatchResult(
                status=MatchStatus.ISOMORPHIC,
                reason=MatchReason.ISOMORPHIC_NODE,
                solution=equivalent[0],
                normalized_distance=Decimal("0"),
                suit_permutation=permutation_between(
                    observed, equivalent[0].game_spec
                ),
                **common,
            )
        if len(equivalent) > 1:
            return GTOMatchResult(
                status=MatchStatus.UNSUPPORTED,
                reason=MatchReason.AMBIGUOUS_BEST_MATCH,
                candidate_solution_ids=tuple(
                    sorted(item.solution_id for item in equivalent)
                ),
                **common,
            )

    compatible = index.hard_compatible(observed)
    if not compatible:
        nearest = index.nearest_hard(observed)
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.NO_HARD_COMPATIBLE_SOLUTION,
            differences=_hard_differences(observed, nearest[0].game_spec),
            candidate_solution_ids=tuple(
                sorted(solution.solution_id for solution in nearest)
            ),
            **common,
        )

    # One numeric pass over the compatible bucket; the original recomputed it.
    scored = [
        (solution, *_numeric_differences(observed, solution.game_spec, thresholds))
        for solution in compatible
    ]
    within = [
        (distance, solution, differences)
        for solution, differences, exceeded, distance in scored
        if not exceeded
    ]
    if not within:
        solution, differences, _, distance = min(scored, key=lambda item: item[3])
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.THRESHOLDS_EXCEEDED,
            normalized_distance=distance,
            differences=differences,
            candidate_solution_ids=(solution.solution_id,),
            **common,
        )

    within.sort(key=lambda item: (item[0], item[1].solution_id))
    best_distance = within[0][0]
    tied = [item for item in within if item[0] == best_distance]
    if len(tied) > 1:
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.AMBIGUOUS_BEST_MATCH,
            normalized_distance=best_distance,
            candidate_solution_ids=tuple(item[1].solution_id for item in tied),
            **common,
        )
    distance, solution, differences = within[0]
    return GTOMatchResult(
        status=MatchStatus.APPROXIMATE,
        reason=MatchReason.WITHIN_EXPLICIT_THRESHOLDS,
        solution=solution,
        normalized_distance=distance,
        differences=differences,
        **common,
    )


def _resolved_index(
    catalog: SolutionCatalog,
    index: "SolutionCatalogIndex | None",
) -> "SolutionCatalogIndex":
    from rivermind_core.gto_index import SolutionCatalogIndex

    if index is None:
        return SolutionCatalogIndex(catalog)
    if index.catalog is not catalog:
        raise SpecValidationError(
            "the supplied index was built for a different catalog object; "
            "rebuild it rather than matching against stale keys"
        )
    return index


#: The dimensions that must match exactly.  Order matters: it is the order the
#: differences are reported in, and the index keys off the same tuple, so there
#: is exactly one definition of "hard dimension" in the codebase.
HARD_FIELD_NAMES = (
    "game_type",
    "players_dealt",
    "objective",
    "tournament_context_id",
    "street",
    "board",
    "player_to_act",
    "active_positions",
    "stack_positions",
    "ante_positions",
    "action_history",
    "rake_model",
)


def hard_key(spec: GameSpec) -> tuple[object, ...]:
    """The hashable tuple of everything that must match exactly."""

    return (
        spec.game_type,
        spec.players_dealt,
        spec.objective,
        spec.tournament_context_id,
        spec.street,
        spec.board,
        spec.player_to_act,
        spec.active_positions,
        tuple(item.position for item in spec.stacks),
        tuple(item.position for item in spec.antes),
        spec.action_history,
        None if spec.rake is None else spec.rake.model_id,
    )


def hard_differences_from_keys(
    observed: tuple[object, ...],
    solution: tuple[object, ...],
) -> tuple[SpecDifference, ...]:
    """Report every hard dimension on which two keys disagree."""

    return tuple(
        SpecDifference(
            field=name,
            observed=_value_text(left),
            solution=_value_text(right),
            absolute_delta=None,
            threshold=None,
        )
        for name, left, right in zip(HARD_FIELD_NAMES, observed, solution, strict=True)
        if left != right
    )


def _hard_differences(
    observed: GameSpec,
    solution: GameSpec,
) -> tuple[SpecDifference, ...]:
    return hard_differences_from_keys(hard_key(observed), hard_key(solution))


def _numeric_differences(
    observed: GameSpec,
    solution: GameSpec,
    thresholds: MappingThresholds,
) -> tuple[tuple[SpecDifference, ...], bool, Decimal]:
    pairs: list[tuple[str, Decimal, Decimal, Decimal]] = [
        (
            "small_blind_bb",
            observed.small_blind_bb,
            solution.small_blind_bb,
            thresholds.small_blind_bb,
        ),
        (
            "big_blind_bb",
            observed.big_blind_bb,
            solution.big_blind_bb,
            thresholds.big_blind_bb,
        ),
        ("pot_bb", observed.pot_bb, solution.pot_bb, thresholds.pot_bb),
    ]
    pairs.extend(
        (
            f"stacks.{left.position.value}.remaining_bb",
            left.remaining_bb,
            right.remaining_bb,
            thresholds.stack_bb,
        )
        for left, right in zip(observed.stacks, solution.stacks, strict=True)
    )
    pairs.extend(
        (
            f"antes.{left.position.value}.ante_bb",
            left.ante_bb,
            right.ante_bb,
            thresholds.ante_bb,
        )
        for left, right in zip(observed.antes, solution.antes, strict=True)
    )
    if observed.rake is not None and solution.rake is not None:
        pairs.extend(
            (
                ("rake.percent", observed.rake.percent, solution.rake.percent, thresholds.rake_percent),
                ("rake.cap_bb", observed.rake.cap_bb, solution.rake.cap_bb, thresholds.rake_cap_bb),
            )
        )

    differences: list[SpecDifference] = []
    exceeded = False
    distance = Decimal("0")
    for field, actual, expected, threshold in pairs:
        delta = abs(actual - expected)
        if delta == 0:
            continue
        if delta > threshold:
            exceeded = True
        component = Decimal("0") if threshold == 0 and delta == 0 else (
            Decimal("Infinity") if threshold == 0 else delta / threshold
        )
        distance += component
        differences.append(
            SpecDifference(
                field=field,
                observed=_decimal_text(actual),
                solution=_decimal_text(expected),
                absolute_delta=_decimal_text(delta),
                threshold=_decimal_text(threshold),
            )
        )
    return tuple(differences), exceeded, distance


def _action_token(action: Action, position: PlayerPosition, big_blind: Decimal) -> str:
    components = [action.street.value, position.value, action.action_type.value]
    if action.amount is not None:
        components.append(f"amount_bb={_decimal_text(action.amount / big_blind)}")
    if action.to_amount is not None:
        components.append(f"to_bb={_decimal_text(action.to_amount / big_blind)}")
    if action.is_all_in:
        components.append("all_in=1")
    return "|".join(components)


def _visible_board(board: tuple[str, ...], street: BettingRound) -> tuple[str, ...]:
    visible = {
        BettingRound.PREFLOP: 0,
        BettingRound.FLOP: 3,
        BettingRound.TURN: 4,
        BettingRound.RIVER: 5,
        BettingRound.SHOWDOWN: 5,
    }[street]
    return board[:visible]


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _value_text(value: object) -> str:
    if isinstance(value, tuple):
        return "[" + ",".join(_value_text(item) for item in value) + "]"
    if isinstance(value, StrEnum):
        return value.value
    if value is None:
        return "null"
    return str(value)
