"""A reusable index over a solution catalog.

``match_game_spec`` originally scanned the whole catalog on every call, and the
dominant cost was not the scan — it was ``GameSpec.fingerprint``, a property that
recomputes a SHA-256 over canonical JSON on every access.  A thousand-node
catalog therefore paid a thousand hashes per match.

This index computes each solution's fingerprint and hard key exactly once, so a
match becomes two dictionary lookups.  The catalog is immutable and the
fingerprint is a pure function of the node — deliberately independent of the
ambient ``decimal`` context — so a cached index cannot go stale.

The index changes *cost*, never *outcome*.  Every ordering and tie-breaking rule
of the linear matcher is preserved, including which solution's differences get
reported when nothing matches, and there is a differential test that compares the
two implementations byte for byte on randomized catalogs.
"""

from __future__ import annotations

from dataclasses import dataclass

from rivermind_core.board_isomorphism import canonical_board_fingerprint
from rivermind_core.gto_matcher import hard_key
from rivermind_core.gto_specs import GameSpec, SolutionCatalog, SolutionSpec


GTO_CATALOG_INDEX_VERSION = "gto-catalog-index/1.0.0"


@dataclass(frozen=True, slots=True)
class IndexedSolution:
    """One catalog entry with its derived keys precomputed."""

    position: int
    solution: SolutionSpec
    fingerprint: str
    hard_key: tuple[object, ...]


class SolutionCatalogIndex:
    """Fingerprint and hard-dimension lookups over one immutable catalog."""

    __slots__ = (
        "_catalog",
        "_entries",
        "_by_fingerprint",
        "_by_hard_key",
        "_by_canonical_board",
    )

    def __init__(self, catalog: SolutionCatalog) -> None:
        self._catalog = catalog
        entries: list[IndexedSolution] = []
        by_fingerprint: dict[str, list[IndexedSolution]] = {}
        by_hard_key: dict[tuple[object, ...], list[IndexedSolution]] = {}
        for position, solution in enumerate(catalog.solutions):
            entry = IndexedSolution(
                position=position,
                solution=solution,
                fingerprint=solution.game_spec.fingerprint,
                hard_key=hard_key(solution.game_spec),
            )
            entries.append(entry)
            by_fingerprint.setdefault(entry.fingerprint, []).append(entry)
            by_hard_key.setdefault(entry.hard_key, []).append(entry)
        self._entries = tuple(entries)
        self._by_fingerprint = {key: tuple(value) for key, value in by_fingerprint.items()}
        self._by_hard_key = {key: tuple(value) for key, value in by_hard_key.items()}
        # Board-equivalence lookups cost a second hash per solution, so they are
        # only built if somebody actually asks for isomorphic matching.
        self._by_canonical_board: dict[str, tuple[SolutionSpec, ...]] | None = None

    @property
    def catalog(self) -> SolutionCatalog:
        return self._catalog

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def distinct_hard_keys(self) -> int:
        """How many hard-dimension buckets the catalog collapses into.

        A catalog of the same node at many stack depths has one bucket, because
        stack *sizes* are numeric dimensions while stack *positions* are hard.
        """

        return len(self._by_hard_key)

    def exact_matches(self, fingerprint: str) -> tuple[SolutionSpec, ...]:
        """Solutions whose normalized fingerprint is identical, in catalog order."""

        return tuple(item.solution for item in self._by_fingerprint.get(fingerprint, ()))

    def hard_compatible(self, observed: GameSpec) -> tuple[SolutionSpec, ...]:
        """Solutions agreeing on every hard dimension, in catalog order."""

        return tuple(
            item.solution for item in self._by_hard_key.get(hard_key(observed), ())
        )

    def nearest_hard(self, observed: GameSpec) -> tuple[SolutionSpec, ...]:
        """The solutions that disagree on the fewest hard dimensions.

        Returned in catalog order, so the caller can report the first one's
        differences exactly as the linear scan did.  Buckets, not solutions, are
        compared: every solution sharing a hard key has the same difference
        count, so a catalog covering one node at twenty stack depths costs one
        comparison rather than twenty.
        """

        if not self._entries:
            return ()
        observed_key = hard_key(observed)
        best_count: int | None = None
        best_buckets: list[tuple[IndexedSolution, ...]] = []
        for key, bucket in self._by_hard_key.items():
            count = sum(
                1
                for left, right in zip(observed_key, key, strict=True)
                if left != right
            )
            if best_count is None or count < best_count:
                best_count = count
                best_buckets = [bucket]
            elif count == best_count:
                best_buckets.append(bucket)
        found = [entry for bucket in best_buckets for entry in bucket]
        found.sort(key=lambda item: item.position)
        return tuple(item.solution for item in found)

    def board_equivalent(self, fingerprint: str) -> tuple[SolutionSpec, ...]:
        """Solutions whose node is identical up to board equivalence.

        The canonical map is built on first use: it costs one extra SHA-256 per
        solution, which callers that never ask for isomorphic matching should
        not pay.
        """

        if self._by_canonical_board is None:
            grouped: dict[str, list[SolutionSpec]] = {}
            for entry in self._entries:
                key = canonical_board_fingerprint(entry.solution.game_spec)
                grouped.setdefault(key, []).append(entry.solution)
            self._by_canonical_board = {
                key: tuple(value) for key, value in grouped.items()
            }
        return self._by_canonical_board.get(fingerprint, ())

    def to_dict(self) -> dict[str, object]:
        return {
            "index_version": GTO_CATALOG_INDEX_VERSION,
            "catalog_id": self._catalog.catalog_id,
            "catalog_version": self._catalog.catalog_version,
            "solutions": len(self._entries),
            "distinct_fingerprints": len(self._by_fingerprint),
            "distinct_hard_keys": len(self._by_hard_key),
            "boundary": (
                "An index changes lookup cost only. It never changes which "
                "solution matches, and it grants no quality label."
            ),
        }
