from __future__ import annotations

import json
import os
import random
import sys
import unittest
from collections import Counter
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.gto_index import (  # noqa: E402
    GTO_CATALOG_INDEX_VERSION,
    SolutionCatalogIndex,
)
from rivermind_core.gto_matcher import (  # noqa: E402
    GTOMatchResult,
    MappingThresholds,
    MatchReason,
    MatchStatus,
    _hard_differences,
    _numeric_differences,
    extract_decision_game_spec,
    hard_key,
    match_game_spec,
)
from rivermind_core.gto_specs import (  # noqa: E402
    PositionStack,
    RakeSpec,
    SolutionCatalog,
    SolutionObjective,
    SolutionQuality,
    SolutionSpec,
    SpecValidationError,
)
from rivermind_core.models import PlayerPosition  # noqa: E402
from rivermind_core.parsers import default_registry  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def match_game_spec_linear(
    observed,
    catalog: SolutionCatalog,
    *,
    thresholds: MappingThresholds = MappingThresholds(),
) -> GTOMatchResult:
    """The pre-index implementation, kept as the executable specification.

    The indexed matcher must agree with this on every input.  If a future change
    makes them disagree, that is a behaviour change and needs a new
    ``gto-match-policy`` version, not a quiet edit.
    """

    common = {
        "observed_fingerprint": observed.fingerprint,
        "policy_version": "gto-match-policy/1.0.0",
    }
    if not catalog.solutions:
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED, reason=MatchReason.CATALOG_EMPTY, **common
        )
    if observed.game_type.value == "cash" and observed.rake is None:
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.MISSING_RAKE_METADATA,
            **common,
        )

    exact = [
        solution
        for solution in catalog.solutions
        if solution.game_spec.fingerprint == observed.fingerprint
    ]
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

    hard_scores = [
        (solution, _hard_differences(observed, solution.game_spec))
        for solution in catalog.solutions
    ]
    compatible = [solution for solution, differences in hard_scores if not differences]
    if not compatible:
        fewest = min(len(differences) for _, differences in hard_scores)
        nearest = [
            (solution, differences)
            for solution, differences in hard_scores
            if len(differences) == fewest
        ]
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.NO_HARD_COMPATIBLE_SOLUTION,
            differences=nearest[0][1],
            candidate_solution_ids=tuple(
                sorted(solution.solution_id for solution, _ in nearest)
            ),
            **common,
        )

    within = []
    for solution in compatible:
        differences, exceeded, distance = _numeric_differences(
            observed, solution.game_spec, thresholds
        )
        if not exceeded:
            within.append((distance, solution, differences))
    if not within:
        nearest_solution = min(
            compatible,
            key=lambda item: _numeric_differences(
                observed, item.game_spec, thresholds
            )[2],
        )
        differences, _, distance = _numeric_differences(
            observed, nearest_solution.game_spec, thresholds
        )
        return GTOMatchResult(
            status=MatchStatus.UNSUPPORTED,
            reason=MatchReason.THRESHOLDS_EXCEEDED,
            normalized_distance=distance,
            differences=differences,
            candidate_solution_ids=(nearest_solution.solution_id,),
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


def _cash_node():
    hand = default_registry().parse(
        (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
    )
    return extract_decision_game_spec(
        hand,
        before_action=5,
        rake=RakeSpec("pokerstars.cash.example", Decimal("5"), Decimal("3")),
    )


def _mtt_node():
    hand = default_registry().parse(
        (FIXTURES / "pokerstars_mtt.txt").read_text(encoding="utf-8")
    )
    return extract_decision_game_spec(hand, before_action=8)


_BOARDS = (
    ("2c", "7d", "Ts"),
    ("Ah", "Kd", "2s"),
    ("9c", "9d", "4h"),
    ("Jh", "8h", "3c"),
)


def _mutate(base, rng: random.Random):
    """Perturb a node inside the invariants GameSpec enforces."""

    spec = base
    if len(spec.board) == 3 and rng.random() < 0.45:  # hard: board (flop nodes only)
        spec = replace(spec, board=rng.choice(_BOARDS))
    if rng.random() < 0.25:  # hard: who is to act
        candidates = [p for p in spec.active_positions]
        spec = replace(spec, player_to_act=rng.choice(candidates))
    if rng.random() < 0.25:  # hard: action line
        spec = replace(
            spec,
            action_history=spec.action_history + (f"flop|BB|check|v{rng.randint(0, 3)}",),
        )
    if rng.random() < 0.2 and spec.rake is not None:  # hard: rake model id
        spec = replace(
            spec,
            rake=RakeSpec(
                f"rake.{rng.randint(0, 2)}", spec.rake.percent, spec.rake.cap_bb
            ),
        )
    if rng.random() < 0.6:  # numeric: pot
        spec = replace(spec, pot_bb=spec.pot_bb + Decimal(rng.randint(0, 40)) / 10)
    if rng.random() < 0.6:  # numeric: stacks
        spec = replace(
            spec,
            stacks=tuple(
                PositionStack(
                    item.position,
                    item.remaining_bb + Decimal(rng.randint(0, 120)) / 10,
                )
                for item in spec.stacks
            ),
        )
    if rng.random() < 0.3 and spec.rake is not None:  # numeric: rake amounts
        spec = replace(
            spec,
            rake=RakeSpec(
                spec.rake.model_id,
                spec.rake.percent + Decimal(rng.randint(0, 3)) / 10,
                spec.rake.cap_bb,
            ),
        )
    return spec


def _catalog(specs, name: str) -> SolutionCatalog:
    return SolutionCatalog(
        catalog_id=name,
        catalog_version="1",
        solutions=tuple(
            SolutionSpec(
                solution_id=f"{name}-{index:04d}",
                game_spec=spec,
                solver_name="fuzz",
                solver_version="1",
                action_tree_version="fuzz/1",
                quality=SolutionQuality.TEST_ONLY,
                artifact_id=f"fuzz/{name}-{index:04d}.json",
                artifact_sha256="c" * 64,
            )
            for index, spec in enumerate(specs)
        ),
    )


class CatalogIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cash = _cash_node()
        cls.mtt = _mtt_node()

    def test_index_reports_its_own_shape(self) -> None:
        rng = random.Random(1)
        specs = [_mutate(self.cash, rng) for _ in range(60)]
        catalog = _catalog(specs, "shape")
        index = SolutionCatalogIndex(catalog)
        payload = index.to_dict()
        self.assertEqual(payload["index_version"], GTO_CATALOG_INDEX_VERSION)
        self.assertEqual(payload["solutions"], 60)
        self.assertEqual(len(index), 60)
        self.assertLessEqual(index.distinct_hard_keys, 60)
        self.assertEqual(payload["distinct_hard_keys"], index.distinct_hard_keys)

    def test_stack_depths_collapse_into_one_hard_bucket(self) -> None:
        """The realistic catalog shape: one node, many stack depths."""

        specs = [
            replace(
                self.cash,
                stacks=tuple(
                    PositionStack(item.position, Decimal(depth))
                    for item in self.cash.stacks
                ),
            )
            for depth in range(20, 220, 10)
        ]
        index = SolutionCatalogIndex(_catalog(specs, "depths"))
        self.assertEqual(len(index), 20)
        self.assertEqual(index.distinct_hard_keys, 1)
        self.assertEqual(len(index.hard_compatible(self.cash)), 20)

    def test_exact_lookup_finds_every_duplicate_in_catalog_order(self) -> None:
        specs = [self.cash, _mutate(self.cash, random.Random(3)), self.cash]
        index = SolutionCatalogIndex(_catalog(specs, "dup"))
        found = index.exact_matches(self.cash.fingerprint)
        self.assertEqual(
            [item.solution_id for item in found], ["dup-0000", "dup-0002"]
        )

    def test_nearest_hard_is_returned_in_catalog_order(self) -> None:
        far = replace(self.cash, board=("Ah", "Kd", "2s"), action_history=("x",))
        near = replace(self.cash, board=("Ah", "Kd", "2s"))
        index = SolutionCatalogIndex(_catalog([far, near, near], "near"))
        found = index.nearest_hard(self.cash)
        self.assertEqual(
            [item.solution_id for item in found], ["near-0001", "near-0002"]
        )

    def test_refuses_an_index_built_for_another_catalog(self) -> None:
        catalog = _catalog([self.cash], "a")
        other = _catalog([self.cash], "b")
        with self.assertRaisesRegex(SpecValidationError, "different catalog object"):
            match_game_spec(self.cash, catalog, index=SolutionCatalogIndex(other))

    def test_an_empty_catalog_indexes_without_complaint(self) -> None:
        index = SolutionCatalogIndex(SolutionCatalog("empty", "1", ()))
        self.assertEqual(len(index), 0)
        self.assertEqual(index.nearest_hard(self.cash), ())
        self.assertEqual(index.exact_matches(self.cash.fingerprint), ())


class MatcherEquivalenceTest(unittest.TestCase):
    """The indexed matcher must be byte-identical to the linear specification."""

    def _compare(self, base, seed: int, catalog_size: int, probes: int) -> Counter:
        rng = random.Random(seed)
        specs = [_mutate(base, rng) for _ in range(catalog_size)]
        if rng.random() < 0.5:
            specs.append(base)  # guarantee some exact hits
        catalog = _catalog(specs, f"fuzz{seed}")
        index = SolutionCatalogIndex(catalog)
        reasons: Counter = Counter()
        for _ in range(probes):
            observed = base if rng.random() < 0.25 else _mutate(base, rng)
            expected = match_game_spec_linear(observed, catalog)
            actual = match_game_spec(observed, catalog, index=index)
            self.assertEqual(
                json.dumps(expected.to_dict(), sort_keys=True),
                json.dumps(actual.to_dict(), sort_keys=True),
                msg=f"seed={seed} fingerprint={observed.fingerprint[:12]}",
            )
            # Building the index inline must agree too.
            inline = match_game_spec(observed, catalog)
            self.assertEqual(
                json.dumps(expected.to_dict(), sort_keys=True),
                json.dumps(inline.to_dict(), sort_keys=True),
            )
            reasons[actual.reason.value] += 1
        return reasons

    def test_agrees_on_randomized_cash_catalogs(self) -> None:
        reasons: Counter = Counter()
        for seed in range(12):
            reasons += self._compare(_cash_node(), seed, catalog_size=25, probes=25)
        # The fuzz must actually reach every branch, or it proves nothing.
        for expected in (
            "exact_node",
            "within_explicit_thresholds",
            "no_hard_compatible_solution",
            "thresholds_exceeded",
        ):
            self.assertGreater(reasons[expected], 0, f"never reached {expected}: {reasons}")

    def test_agrees_on_randomized_tournament_catalogs(self) -> None:
        reasons: Counter = Counter()
        for seed in range(100, 106):
            reasons += self._compare(_mtt_node(), seed, catalog_size=20, probes=20)
        self.assertGreater(sum(reasons.values()), 0)

    def test_agrees_when_duplicate_nodes_make_the_answer_ambiguous(self) -> None:
        base = _cash_node()
        near = replace(base, pot_bb=base.pot_bb + Decimal("0.5"))
        twin = replace(base, pot_bb=base.pot_bb - Decimal("0.5"))
        for specs in ([base, base], [near, twin], [base, base, near]):
            catalog = _catalog(specs, "amb")
            expected = match_game_spec_linear(base, catalog)
            actual = match_game_spec(base, catalog, index=SolutionCatalogIndex(catalog))
            self.assertEqual(expected.to_dict(), actual.to_dict())
            self.assertEqual(actual.reason, MatchReason.AMBIGUOUS_BEST_MATCH)

    def test_agrees_on_the_documented_boundaries(self) -> None:
        base = _cash_node()
        empty = SolutionCatalog("e", "1", ())
        self.assertEqual(
            match_game_spec(base, empty).to_dict(),
            match_game_spec_linear(base, empty).to_dict(),
        )
        unraked = replace(base, rake=None)
        catalog = _catalog([base], "raked")
        self.assertEqual(
            match_game_spec(unraked, catalog).to_dict(),
            match_game_spec_linear(unraked, catalog).to_dict(),
        )

    def test_the_fingerprint_does_not_depend_on_the_decimal_context(self) -> None:
        """A cached fingerprint must stay valid, so the hash cannot read global state.

        ``Decimal.normalize()`` rounds against the ambient context. Using it in
        ``GameSpec.to_dict`` made a published node fingerprint depend on whatever
        precision happened to be set, which an index — caching fingerprints at
        build time — would then disagree with.
        """

        spec = replace(
            _cash_node(), pot_bb=Decimal("123456789012345678901234567890.5")
        )
        expected = spec.fingerprint
        with localcontext() as context:
            context.prec = 5
            self.assertEqual(spec.fingerprint, expected)

        catalog = _catalog([spec], "ctx")
        index = SolutionCatalogIndex(catalog)
        with localcontext() as context:
            context.prec = 5
            self.assertEqual(
                match_game_spec(spec, catalog, index=index).to_dict(),
                match_game_spec_linear(spec, catalog).to_dict(),
            )

    def test_hard_key_covers_exactly_the_hard_dimensions(self) -> None:
        base = _cash_node()
        self.assertEqual(hard_key(base), hard_key(replace(base, pot_bb=Decimal("99"))))
        self.assertNotEqual(
            hard_key(base), hard_key(replace(base, board=("Ah", "Kd", "2s")))
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
