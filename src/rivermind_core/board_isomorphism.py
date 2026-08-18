"""Board equivalence: suit relabelling and flop order.

Two boards can be written differently and still pose exactly the same strategic
problem.  ``board-isomorphism/1.0.0`` freezes two independent reasons for that,
and only these two:

1. **Suit relabelling.** Poker does not care which suit is which, only which
   cards share a suit.  ``2c 7d Ts`` and ``2h 7s Td`` are the same board under
   ``c→h, d→s, s→d``.
2. **Flop order.** The three flop cards are revealed simultaneously, so no
   action can distinguish them.  ``2c 7d Ts`` and ``Ts 2c 7d`` are the same
   board.  The turn and the river are *not* reorderable: each arrives on its own
   street with betting in between.

Together they collapse the flop space by more than an order of magnitude:

    all three-card flops              22,100
    flops up to board equivalence      1,755
    redundancy                          12.6x

Without this, a catalog has to store every spelling separately.  It is deliberately
*not* wired into the matcher's default path: a silent "these boards look similar"
heuristic is exactly the kind of thing this project refuses to do.  A caller has
to ask for isomorphic matching explicitly, and every result carries the
permutation that was applied, so the mapping is auditable rather than implied.

## What the equivalence assumes

Relabelling suits is only sound when everything *else* about the node is suit
symmetric.  In practice that means the ranges the node was solved with: a solve
run with a range that treats hearts differently from spades is not transferable.
Standard preflop ranges are suit symmetric, but this is an assumption about the
solve, not a theorem about the board, and a solve quality report should say so.

Preflop nodes have no board and are therefore left alone: with no community
cards there is nothing to relabel against, and collapsing suits preflop would
change what a combination *means* (``AhKh`` and ``AhKs`` are not the same hand).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from typing import Iterable, Mapping

from rivermind_core.gto_specs import GameSpec, SpecValidationError
from rivermind_core.models import CARD_PATTERN


BOARD_ISOMORPHISM_VERSION = "board-isomorphism/1.0.0"

SUITS = "cdhs"
_RANKS = "23456789TJQKA"

#: Every relabelling of the four suits, in a fixed order so the canonical form
#: is deterministic across interpreters.
_PERMUTATIONS: tuple[tuple[str, ...], ...] = tuple(permutations(SUITS))


class IsomorphismError(SpecValidationError):
    """Raised when a board or permutation cannot be handled without guessing."""


@dataclass(frozen=True, slots=True)
class SuitPermutation:
    """A relabelling of suits, and its inverse.

    ``mapping`` sends a suit in the *observed* frame to a suit in the *canonical*
    frame.  ``inverse`` sends it back, which is what turns a solution's
    combinations into the observed board's combinations.
    """

    mapping: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        sources = tuple(item[0] for item in self.mapping)
        targets = tuple(item[1] for item in self.mapping)
        if sorted(sources) != list(SUITS) or sorted(targets) != list(SUITS):
            raise IsomorphismError("a suit permutation must be a bijection over cdhs")
        if sources != tuple(SUITS):
            raise IsomorphismError("suit permutation sources must be in 'cdhs' order")

    @property
    def is_identity(self) -> bool:
        return all(source == target for source, target in self.mapping)

    @property
    def forward(self) -> Mapping[str, str]:
        return dict(self.mapping)

    @property
    def backward(self) -> Mapping[str, str]:
        return {target: source for source, target in self.mapping}

    def apply_card(self, card: str) -> str:
        """Relabel one card's suit, observed frame → canonical frame."""

        _require_card(card)
        return card[0] + self.forward[card[1]]

    def unapply_card(self, card: str) -> str:
        """Relabel one card's suit, canonical frame → observed frame."""

        _require_card(card)
        return card[0] + self.backward[card[1]]

    def relabel_board(self, board: Iterable[str]) -> tuple[str, ...]:
        """Relabel suits only.  This does **not** reorder the flop.

        Use :func:`canonicalize_board` when you want the canonical spelling;
        this method exists for callers that need the relabelling on its own.
        """

        return tuple(self.apply_card(card) for card in board)

    def apply_combo(self, combo: str) -> str:
        """Relabel a two-card combination and re-canonicalize its spelling."""

        return _canonical_pair(self.apply_card(combo[:2]), self.apply_card(combo[2:]))

    def unapply_combo(self, combo: str) -> str:
        return _canonical_pair(
            self.unapply_card(combo[:2]), self.unapply_card(combo[2:])
        )

    def to_dict(self) -> dict[str, str]:
        return {source: target for source, target in self.mapping}

    def inverse(self) -> "SuitPermutation":
        """The relabelling that undoes this one."""

        back = self.backward
        return SuitPermutation.from_targets(tuple(back[suit] for suit in SUITS))

    def then(self, other: "SuitPermutation") -> "SuitPermutation":
        """Apply this relabelling, then ``other``."""

        forward, onward = self.forward, other.forward
        return SuitPermutation.from_targets(
            tuple(onward[forward[suit]] for suit in SUITS)
        )

    @classmethod
    def identity(cls) -> "SuitPermutation":
        return cls(tuple((suit, suit) for suit in SUITS))

    @classmethod
    def from_targets(cls, targets: Iterable[str]) -> "SuitPermutation":
        return cls(tuple(zip(SUITS, targets, strict=True)))


@dataclass(frozen=True, slots=True)
class CanonicalBoard:
    """A board's canonical relabelling, and how to get back to the original."""

    board: tuple[str, ...]
    canonical: tuple[str, ...]
    permutation: SuitPermutation

    @property
    def is_canonical(self) -> bool:
        return self.board == self.canonical

    @property
    def reorders_the_flop(self) -> bool:
        """Whether the canonical form also had to sort the flop three."""

        return self.permutation.relabel_board(self.board) != self.canonical

    def to_dict(self) -> dict[str, object]:
        return {
            "isomorphism_version": BOARD_ISOMORPHISM_VERSION,
            "board": list(self.board),
            "canonical_board": list(self.canonical),
            "suit_permutation": self.permutation.to_dict(),
            "is_canonical": self.is_canonical,
        }


def canonicalize_board(board: Iterable[str]) -> CanonicalBoard:
    """Return the canonical spelling of ``board`` and the permutation used.

    The canonical form is the lexicographic minimum, over all 24 suit
    relabellings, of the board with its **flop three sorted** and its turn and
    river left in place.  Any total order would do; this one is stable, cheap,
    and independent of interpreter details.

    An empty board (a preflop node) is its own canonical form under the identity
    permutation — see the module docstring for why preflop is left alone.
    """

    cards = tuple(board)
    for card in cards:
        _require_card(card)
    if len(set(cards)) != len(cards):
        raise IsomorphismError("a board cannot repeat a card")
    if not cards:
        return CanonicalBoard((), (), SuitPermutation.identity())
    if len(cards) not in {3, 4, 5}:
        raise IsomorphismError(
            f"a board has 0, 3, 4 or 5 cards; got {len(cards)}"
        )

    best: tuple[str, ...] | None = None
    best_targets: tuple[str, ...] | None = None
    for targets in _PERMUTATIONS:
        relabel = dict(zip(SUITS, targets, strict=True))
        relabelled = [card[0] + relabel[card[1]] for card in cards]
        # Sorting has to happen after relabelling: a permutation can change
        # which of the three flop cards sorts first.
        candidate = tuple(sorted(relabelled[:3])) + tuple(relabelled[3:])
        if best is None or candidate < best:
            best = candidate
            best_targets = targets
    assert best is not None and best_targets is not None  # 24 permutations always run
    return CanonicalBoard(
        board=cards,
        canonical=best,
        permutation=SuitPermutation.from_targets(best_targets),
    )


def canonicalize_game_spec(spec: GameSpec) -> tuple[GameSpec, SuitPermutation]:
    """Rewrite a node onto its canonical board, returning the permutation used.

    Only the board carries suit information in a ``GameSpec``; positions, stacks
    and the action line are suit-free, so the rest of the node is untouched and
    the resulting fingerprint differs only by the relabelling.
    """

    from dataclasses import replace

    canonical = canonicalize_board(spec.board)
    if canonical.is_canonical:
        return spec, canonical.permutation
    return replace(spec, board=canonical.canonical), canonical.permutation


def canonical_board_fingerprint(spec: GameSpec) -> str:
    """The fingerprint this node would have on its canonical board."""

    canonical_spec, _ = canonicalize_game_spec(spec)
    return canonical_spec.fingerprint


def _canonical_pair(first: str, second: str) -> str:
    ordered = sorted((first, second), key=_card_sort_key)
    if ordered[0] == ordered[1]:
        raise IsomorphismError(f"combination repeats the card {ordered[0]}")
    return "".join(ordered)


def _card_sort_key(card: str) -> tuple[int, int]:
    return (-_RANKS.index(card[0]), SUITS.index(card[1]))


def _require_card(card: str) -> None:
    if not isinstance(card, str) or not CARD_PATTERN.fullmatch(card):
        raise IsomorphismError(f"invalid card code: {card!r}")


def permutation_between(observed: GameSpec, solution: GameSpec) -> SuitPermutation:
    """The relabelling that carries ``observed``'s suits onto ``solution``'s.

    Both nodes are canonicalized, then the observed-to-canonical map is composed
    with the inverse of the solution-to-canonical map.  The result is what a
    caller needs in order to read a solution's combinations in the frame of the
    hand they actually played.

    Raises when the two nodes are not board-equivalent, so a caller cannot get a
    permutation for boards that do not correspond.
    """

    left = canonicalize_board(observed.board)
    right = canonicalize_board(solution.board)
    if left.canonical != right.canonical:
        raise IsomorphismError(
            "these boards are not equivalent: "
            f"{list(observed.board)} canonicalizes to {list(left.canonical)}, "
            f"{list(solution.board)} to {list(right.canonical)}"
        )
    return left.permutation.then(right.permutation.inverse())
