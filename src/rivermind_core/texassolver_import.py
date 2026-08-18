"""Convert a TexasSolver strategy dump into a strategy artifact draft.

TexasSolver (https://github.com/bupticybee/TexasSolver, AGPL v3) writes its
solved tree as one JSON document::

    {"node_type": "action_node",
     "player": 1,
     "actions": ["CHECK", "BET 4.0", "BET 98.0"],
     "strategy": {"actions": [...], "strategy": {"AsAh": [0.99, 0.01, 3.4e-12]}},
     "childrens": {"CHECK": {...}, "BET 4.0": {...}}}

Three things have to be reconciled with ``strategy-artifact/1.0.0``:

* **Combination spelling.**  TexasSolver writes ``AsAh``; the canonical form is
  rank-descending then ``c < d < h < s``, so ``AhAs``.
* **Numbers.**  The dump holds float32 values in scientific notation
  (``6.5e-11``).  The artifact protocol takes plain decimal strings with at most
  six places, so probabilities are rounded and then renormalized to sum to
  exactly 1 — the residual goes to the largest probability, deterministically.
* **Weights.**  The dump does not carry the input range weights.  This module
  therefore refuses to guess: either supply the range string it was solved with,
  or state explicitly that uniform weights are an accepted approximation.

The converter never writes ``verified``.  Promoting an artifact to that label is
a deliberate edit followed by the quality gate, never a single command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Iterable, Mapping

from rivermind_core._contracts import StrictReader, decimal_text
from rivermind_core.gto_specs import GameSpec, SolutionQuality, SpecValidationError
from rivermind_core.models import CARD_PATTERN
from rivermind_core.strategy_artifacts import (
    PROBABILITY_DECIMAL_PLACES,
    STRATEGY_ARTIFACT_SCHEMA_VERSION,
    canonical_combo,
)


TEXASSOLVER_IMPORT_VERSION = "texassolver-import/1.0.0"

#: TexasSolver's own range parser drops anything at or below this weight.
MIN_RANGE_WEIGHT = Decimal("0.005")

_RANKS = "23456789TJQKA"
_SUITS = "cdhs"
_QUANTUM = Decimal(1).scaleb(-PROBABILITY_DECIMAL_PLACES)

_SIZED_LABEL = re.compile(r"^(BET|RAISE)\s+([0-9]+(?:\.[0-9]+)?)$")
_PLAIN_LABELS = {"CHECK": "check", "FOLD": "fold", "CALL": "call"}


class SolverImportError(SpecValidationError):
    """Raised when a solver dump cannot be converted without inventing data."""


_READ = StrictReader(SolverImportError)


@dataclass(frozen=True, slots=True)
class ConvertedNode:
    """A draft artifact plus what the human needs in order to check it."""

    document: dict[str, Any]
    solver_player: int
    action_labels: tuple[str, ...]
    combo_count: int
    max_renormalization: Decimal
    weights_source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "importer_version": TEXASSOLVER_IMPORT_VERSION,
            "solver_player": self.solver_player,
            "solver_player_seat": "ip" if self.solver_player == 0 else "oop",
            "action_labels": list(self.action_labels),
            "combo_count": self.combo_count,
            "max_renormalization": decimal_text(self.max_renormalization),
            "weights_source": self.weights_source,
            "quality": self.document["provenance"]["quality"],
            "boundary": (
                "This is a draft. It has not been packaged, hashed, registered or "
                "granted any quality label beyond the one stated above."
            ),
        }


def parse_texassolver_range(
    range_str: str,
    board: Iterable[str] = (),
) -> dict[str, Decimal]:
    """Expand a TexasSolver range string into canonical combos and weights.

    Implements the notation TexasSolver itself accepts: ``XY`` (all combos),
    ``XYs`` (suited), ``XYo`` (offsuit), each optionally followed by
    ``:weight``.  ``+`` notation is *not* supported, because TexasSolver does not
    support it either — enumerate instead.
    """

    blocked = set(board)
    for card in blocked:
        if not CARD_PATTERN.fullmatch(card):
            raise SolverImportError(f"board contains an invalid card: {card!r}")

    weights: dict[str, Decimal] = {}
    for raw in range_str.split(","):
        token = raw.strip()
        if not token:
            continue
        head, _, weight_text = token.partition(":")
        head = head.strip()
        weight = Decimal("1")
        if weight_text:
            try:
                weight = Decimal(weight_text.strip())
            except ArithmeticError as exc:
                raise SolverImportError(
                    f"range entry {token!r} has an unreadable weight"
                ) from exc
        if not weight.is_finite() or weight <= MIN_RANGE_WEIGHT:
            # TexasSolver drops these before solving, so the dump will not
            # contain them either.
            continue
        if weight > 1:
            raise SolverImportError(f"range entry {token!r} has a weight above 1")

        for combo in _expand(head, token):
            if set(_cards(combo)) & blocked:
                continue
            if combo in weights and weights[combo] != weight:
                raise SolverImportError(
                    f"combo {combo} is listed twice with different weights"
                )
            weights[combo] = weight
    if not weights:
        raise SolverImportError("range string expands to no combinations")
    return weights


def _expand(head: str, token: str) -> list[str]:
    if len(head) not in {2, 3}:
        raise SolverImportError(f"range entry {token!r} is not valid notation")
    first, second = head[0], head[1]
    for rank in (first, second):
        if rank not in _RANKS:
            raise SolverImportError(f"range entry {token!r} has an unknown rank")
    suffix = head[2] if len(head) == 3 else ""
    if suffix not in {"", "s", "o"}:
        raise SolverImportError(
            f"range entry {token!r} must end in 's', 'o', or nothing"
        )
    if suffix == "s" and first == second:
        raise SolverImportError(f"range entry {token!r}: a pair cannot be suited")

    combos: list[str] = []
    for i, suit_a in enumerate(_SUITS):
        for j, suit_b in enumerate(_SUITS):
            if first == second and j <= i:
                continue
            if first != second and suffix == "s" and suit_a != suit_b:
                continue
            if suffix == "o" and suit_a == suit_b:
                continue
            if first == second and suit_a == suit_b:
                continue
            card_a, card_b = first + suit_a, second + suit_b
            if card_a == card_b:
                continue
            combos.append(canonical_combo("".join(sorted((card_a, card_b), key=_key))))
    return sorted(set(combos))


def _key(card: str) -> tuple[int, int]:
    return (-_RANKS.index(card[0]), _SUITS.index(card[1]))


def _cards(combo: str) -> tuple[str, str]:
    return (combo[:2], combo[2:])


def convert_texassolver_dump(
    dump: Any,
    *,
    game_spec: GameSpec,
    node_path: tuple[str, ...],
    solution_id: str,
    action_tree_version: str,
    node_id: str,
    solver_version: str,
    solver_config_id: str,
    generated_at: str,
    license_text: str,
    quality: SolutionQuality = SolutionQuality.EXPERIMENTAL,
    quality_report_id: str | None = None,
    range_weights: Mapping[str, Decimal] | None = None,
    assume_uniform_weights: bool = False,
    solver_name: str = "TexasSolver",
) -> ConvertedNode:
    """Convert one node of a TexasSolver dump into a draft artifact document."""

    if quality is SolutionQuality.VERIFIED:
        raise SolverImportError(
            "the importer never writes 'verified'; set the label deliberately and "
            "then run the quality gate"
        )
    if range_weights is None and not assume_uniform_weights:
        raise SolverImportError(
            "the dump does not carry range weights: pass the range string it was "
            "solved with, or state explicitly that uniform weights are acceptable"
        )
    if range_weights is not None and assume_uniform_weights:
        raise SolverImportError(
            "supply a range or assume uniform weights, not both"
        )

    node = _descend(dump, node_path)
    strategy = _READ.mapping(node.get("strategy"), "node.strategy")
    _READ.exact_keys(strategy, {"actions", "strategy"}, "node.strategy")
    labels = tuple(
        _READ.text(item, "node.strategy.actions item")
        for item in _READ.sequence(strategy["actions"], "node.strategy.actions")
    )
    if len(labels) < 2:
        raise SolverImportError("the selected node is not a decision with two actions")

    player = node.get("player")
    if not isinstance(player, int) or player not in {0, 1}:
        raise SolverImportError("the selected node has no usable player index")

    actions = _actions_from_labels(labels, game_spec)
    order = sorted(range(len(labels)), key=lambda index: actions[index]["action_id"])
    sorted_actions = [actions[index] for index in order]

    table = _READ.mapping(strategy["strategy"], "node.strategy.strategy")
    board = set(game_spec.board)
    entries: list[dict[str, Any]] = []
    worst_shift = Decimal("0")
    for raw_combo, raw_probabilities in table.items():
        combo = _canonical(raw_combo)
        if set(_cards(combo)) & board:
            raise SolverImportError(
                f"the dump contains {combo}, which uses a board card; the solve and "
                "the GameSpec disagree about the board"
            )
        values = _READ.sequence(raw_probabilities, f"strategy for {combo}")
        if len(values) != len(labels):
            raise SolverImportError(
                f"combo {combo} has {len(values)} probabilities for {len(labels)} actions"
            )
        probabilities, shift = _normalize([values[index] for index in order], combo)
        worst_shift = max(worst_shift, shift)
        weight = _weight_for(combo, range_weights)
        entries.append(
            {
                "combo": combo,
                "weight": decimal_text(weight),
                "policies": [
                    {
                        "action_id": sorted_actions[position]["action_id"],
                        "probability": decimal_text(probabilities[position]),
                        "ev": None,
                    }
                    for position in range(len(sorted_actions))
                ],
            }
        )
    if not entries:
        raise SolverImportError("the selected node has no combinations")
    entries.sort(key=lambda item: item["combo"])

    document = {
        "schema_version": STRATEGY_ARTIFACT_SCHEMA_VERSION,
        "solution_id": solution_id,
        "game_spec_fingerprint": game_spec.fingerprint,
        "action_tree_version": action_tree_version,
        "node_id": node_id,
        # TexasSolver's console dump carries strategy only; EV is reachable
        # through its GUI/API but not through dump_result, so every EV is null.
        "ev_unit": "bb",
        "ev_semantics": "action_ev_from_node",
        "actions": sorted_actions,
        "entries": entries,
        "provenance": {
            "solver_name": solver_name,
            "solver_version": solver_version,
            "solver_config_id": solver_config_id,
            "generated_at": generated_at,
            "quality": quality.value,
            "quality_report_id": quality_report_id,
            "license": license_text,
        },
    }
    return ConvertedNode(
        document=document,
        solver_player=player,
        action_labels=labels,
        combo_count=len(entries),
        max_renormalization=worst_shift,
        weights_source=(
            "uniform (declared as an approximation)"
            if range_weights is None
            else "parsed from the supplied range string"
        ),
    )


def _descend(dump: Any, node_path: tuple[str, ...]) -> Mapping[str, Any]:
    node = _READ.mapping(dump, "dump root")
    walked: list[str] = []
    for label in node_path:
        children = node.get("childrens")
        if not isinstance(children, dict) or label not in children:
            available = sorted(children) if isinstance(children, dict) else []
            raise SolverImportError(
                f"no child {label!r} at {'/'.join(walked) or 'root'}; "
                f"available: {available}"
            )
        node = _READ.mapping(children[label], f"node {label}")
        walked.append(label)
    if node.get("node_type") != "action_node":
        raise SolverImportError(
            f"node at {'/'.join(walked) or 'root'} is {node.get('node_type')!r}, "
            "not an action node"
        )
    return node


def _actions_from_labels(
    labels: tuple[str, ...],
    game_spec: GameSpec,
) -> list[dict[str, Any]]:
    remaining = {item.position: item.remaining_bb for item in game_spec.stacks}
    stack = remaining.get(game_spec.player_to_act)
    actions: list[dict[str, Any]] = []
    for label in labels:
        plain = _PLAIN_LABELS.get(label.strip().upper())
        if plain is not None:
            actions.append({"action_id": plain, "kind": plain, "size_bb": None})
            continue
        match = _SIZED_LABEL.match(label.strip().upper())
        if match is None:
            raise SolverImportError(f"unrecognized solver action label: {label!r}")
        size = Decimal(match.group(2))
        if size <= 0:
            raise SolverImportError(f"action {label!r} has a non-positive size")
        # TexasSolver spells an all-in as a bet or raise of the whole stack.
        kind = "all_in" if stack is not None and size == stack else match.group(1).lower()
        prefix = "allin" if kind == "all_in" else kind
        actions.append(
            {
                "action_id": f"{prefix}_{decimal_text(size).replace('.', '_')}",
                "kind": kind,
                "size_bb": decimal_text(size),
            }
        )
    identifiers = [item["action_id"] for item in actions]
    if len(set(identifiers)) != len(identifiers):
        raise SolverImportError(f"solver actions collide after naming: {identifiers}")
    return actions


def _canonical(raw: Any) -> str:
    text = _READ.text(raw, "combo key")
    if len(text) != 4:
        raise SolverImportError(f"combo key {text!r} is not two cards")
    cards = (text[:2], text[2:])
    for card in cards:
        if not CARD_PATTERN.fullmatch(card):
            raise SolverImportError(f"combo key {text!r} contains an invalid card")
    return canonical_combo("".join(sorted(cards, key=_key)))


def _normalize(values: list[Any], combo: str) -> tuple[list[Decimal], Decimal]:
    """Round to the protocol's precision, then make the row sum to exactly 1."""

    numbers: list[Decimal] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SolverImportError(f"combo {combo} has a non-numeric probability")
        number = Decimal(repr(float(value)))
        if not number.is_finite():
            raise SolverImportError(f"combo {combo} has a non-finite probability")
        if number < Decimal("-0.000001") or number > Decimal("1.000001"):
            raise SolverImportError(
                f"combo {combo} has a probability outside [0, 1]: {value}"
            )
        numbers.append(min(max(number, Decimal("0")), Decimal("1")))

    rounded = [item.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN) for item in numbers]
    residual = Decimal("1") - sum(rounded, start=Decimal("0"))
    if abs(residual) > Decimal("0.0001"):
        raise SolverImportError(
            f"combo {combo} probabilities sum to {decimal_text(sum(rounded, Decimal('0')))}; "
            "the dump is not a probability distribution"
        )
    if residual != 0:
        # Deterministic target: the largest probability, ties broken by position.
        target = max(range(len(rounded)), key=lambda i: (rounded[i], -i))
        rounded[target] += residual
        if not Decimal("0") <= rounded[target] <= Decimal("1"):
            raise SolverImportError(
                f"combo {combo} cannot be renormalized inside [0, 1]"
            )
    return rounded, abs(residual)


def _weight_for(combo: str, range_weights: Mapping[str, Decimal] | None) -> Decimal:
    if range_weights is None:
        return Decimal("1")
    weight = range_weights.get(combo)
    if weight is None:
        raise SolverImportError(
            f"the dump contains {combo} but the supplied range does not; "
            "the range string does not match the solve"
        )
    return weight
