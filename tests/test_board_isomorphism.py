from __future__ import annotations

import io
import json
import os
import random
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from itertools import combinations
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.board_isomorphism import (  # noqa: E402
    BOARD_ISOMORPHISM_VERSION,
    SUITS,
    IsomorphismError,
    SuitPermutation,
    canonical_board_fingerprint,
    canonicalize_board,
    canonicalize_game_spec,
    permutation_between,
)
from rivermind_core.gto_index import SolutionCatalogIndex  # noqa: E402
from rivermind_core.gto_matcher import (  # noqa: E402
    MatchReason,
    MatchStatus,
    extract_decision_game_spec,
    match_game_spec,
)
from rivermind_core.gto_specs import (  # noqa: E402
    RakeSpec,
    SolutionCatalog,
    SolutionQuality,
    SolutionSpec,
)
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.strategy_artifacts import (  # noqa: E402
    STRATEGY_ARTIFACT_SCHEMA_VERSION,
    verify_solution_artifact,
)
from rivermind_core.strategy_query import (  # noqa: E402
    ComboNotCoveredError,
    build_strategy_evidence,
)


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
RANKS = "23456789TJQKA"
DECK = [rank + suit for rank in RANKS for suit in SUITS]

#: The whole point of the protocol, as one number.
DISTINCT_FLOP_CLASSES = 1755


def _node():
    hand = default_registry().parse(
        (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
    )
    return extract_decision_game_spec(
        hand,
        before_action=5,
        rake=RakeSpec("pokerstars.cash.example", Decimal("5"), Decimal("3")),
    )


def _catalog(specs) -> SolutionCatalog:
    return SolutionCatalog(
        catalog_id="iso",
        catalog_version="1",
        solutions=tuple(
            SolutionSpec(
                solution_id=f"iso-{index}",
                game_spec=spec,
                solver_name="x",
                solver_version="1",
                action_tree_version="t/1",
                quality=SolutionQuality.TEST_ONLY,
                artifact_id=f"iso/{index}.json",
                artifact_sha256="b" * 64,
            )
            for index, spec in enumerate(specs)
        ),
    )


class CanonicalBoardTest(unittest.TestCase):
    def test_every_flop_collapses_to_the_expected_number_of_classes(self) -> None:
        """22,100 flops, 1,755 strategically distinct ones. This is the claim."""

        flops = list(combinations(DECK, 3))
        self.assertEqual(len(flops), 22100)
        classes = {canonicalize_board(flop).canonical for flop in flops}
        self.assertEqual(len(classes), DISTINCT_FLOP_CLASSES)

    def test_canonicalization_is_idempotent_on_every_flop(self) -> None:
        for flop in combinations(DECK, 3):
            canonical = canonicalize_board(flop).canonical
            self.assertEqual(canonicalize_board(canonical).canonical, canonical)

    def test_relabelled_boards_land_in_the_same_class(self) -> None:
        rng = random.Random(11)
        flops = list(combinations(DECK, 3))
        for _ in range(4000):
            flop = rng.choice(flops)
            relabel = dict(zip(SUITS, rng.sample(SUITS, 4)))
            twin = tuple(card[0] + relabel[card[1]] for card in flop)
            self.assertEqual(
                canonicalize_board(flop).canonical,
                canonicalize_board(twin).canonical,
            )

    def test_a_reordered_flop_is_the_same_board(self) -> None:
        self.assertEqual(
            canonicalize_board(("2c", "7d", "Ts")).canonical,
            canonicalize_board(("Ts", "2c", "7d")).canonical,
        )

    def test_a_reordered_turn_is_not_the_same_board(self) -> None:
        """The turn arrives on its own street; swapping it changes the node."""

        self.assertNotEqual(
            canonicalize_board(("2c", "7d", "Ts", "Ah")).canonical,
            canonicalize_board(("2c", "7d", "Ah", "Ts")).canonical,
        )

    def test_boards_that_only_swap_suit_labels_are_the_same(self) -> None:
        """Both are rainbow with the same ranks, so they are one board."""

        self.assertEqual(
            canonicalize_board(("2c", "7d", "Ts")).canonical,
            canonicalize_board(("2c", "7d", "Th")).canonical,
        )

    def test_different_boards_stay_different(self) -> None:
        rainbow = canonicalize_board(("2c", "7d", "Ts")).canonical
        # Different suit pattern: two-tone rather than rainbow.
        self.assertNotEqual(rainbow, canonicalize_board(("2c", "7c", "Ts")).canonical)
        # Different ranks.
        self.assertNotEqual(rainbow, canonicalize_board(("2c", "7d", "Js")).canonical)
        # Monotone.
        self.assertNotEqual(rainbow, canonicalize_board(("2c", "7c", "Tc")).canonical)

    def test_preflop_is_left_alone(self) -> None:
        canonical = canonicalize_board(())
        self.assertEqual(canonical.canonical, ())
        self.assertTrue(canonical.permutation.is_identity)
        self.assertTrue(canonical.is_canonical)

    def test_rejects_boards_that_cannot_exist(self) -> None:
        for board, pattern in (
            (("2c", "2c", "Ts"), "cannot repeat a card"),
            (("2x", "7d", "Ts"), "invalid card code"),
            (("2c", "7d"), "0, 3, 4 or 5 cards"),
            (("2c",), "0, 3, 4 or 5 cards"),
        ):
            with self.subTest(board=board):
                with self.assertRaisesRegex(IsomorphismError, pattern):
                    canonicalize_board(board)

    def test_reports_whether_the_flop_had_to_be_reordered(self) -> None:
        self.assertFalse(canonicalize_board(("2c", "7d", "Ts")).reorders_the_flop)
        self.assertTrue(canonicalize_board(("Ts", "7d", "2c")).reorders_the_flop)

    def test_serializes_its_own_decision(self) -> None:
        payload = canonicalize_board(("Ts", "7d", "2c")).to_dict()
        self.assertEqual(payload["isomorphism_version"], BOARD_ISOMORPHISM_VERSION)
        self.assertEqual(payload["board"], ["Ts", "7d", "2c"])
        self.assertFalse(payload["is_canonical"])


class SuitPermutationTest(unittest.TestCase):
    def test_round_trips_cards_and_combinations(self) -> None:
        rng = random.Random(5)
        for _ in range(2000):
            permutation = SuitPermutation.from_targets(rng.sample(SUITS, 4))
            card = rng.choice(DECK)
            self.assertEqual(permutation.unapply_card(permutation.apply_card(card)), card)
            first, second = rng.sample(DECK, 2)
            combo = "".join(
                sorted((first, second), key=lambda c: (-RANKS.index(c[0]), SUITS.index(c[1])))
            )
            self.assertEqual(
                permutation.unapply_combo(permutation.apply_combo(combo)), combo
            )

    def test_inverse_and_composition_behave(self) -> None:
        rng = random.Random(7)
        for _ in range(500):
            left = SuitPermutation.from_targets(rng.sample(SUITS, 4))
            right = SuitPermutation.from_targets(rng.sample(SUITS, 4))
            self.assertTrue(left.then(left.inverse()).is_identity)
            composed = left.then(right)
            for suit in SUITS:
                self.assertEqual(
                    composed.forward[suit], right.forward[left.forward[suit]]
                )

    def test_rejects_something_that_is_not_a_permutation(self) -> None:
        with self.assertRaisesRegex(IsomorphismError, "bijection"):
            SuitPermutation.from_targets("ccdh")

    def test_permutation_between_carries_one_board_onto_the_other(self) -> None:
        base = _node()
        rng = random.Random(3)
        for _ in range(500):
            left = dict(zip(SUITS, rng.sample(SUITS, 4)))
            right = dict(zip(SUITS, rng.sample(SUITS, 4)))
            observed = replace(
                base, board=tuple(c[0] + left[c[1]] for c in base.board)
            )
            solution = replace(
                base, board=tuple(c[0] + right[c[1]] for c in base.board)
            )
            bridge = permutation_between(observed, solution)
            self.assertEqual(
                set(bridge.relabel_board(observed.board)), set(solution.board)
            )

    def test_permutation_between_refuses_unrelated_boards(self) -> None:
        base = _node()
        with self.assertRaisesRegex(IsomorphismError, "not equivalent"):
            permutation_between(base, replace(base, board=("Ah", "Kh", "Qh")))

    def test_canonicalizing_a_node_touches_only_the_board(self) -> None:
        base = _node()
        shuffled = replace(base, board=("Ts", "7d", "2c"))
        canonical, permutation = canonicalize_game_spec(shuffled)
        self.assertEqual(canonical.pot_bb, shuffled.pot_bb)
        self.assertEqual(canonical.stacks, shuffled.stacks)
        self.assertEqual(canonical.action_history, shuffled.action_history)
        self.assertEqual(canonical.board, canonicalize_board(shuffled.board).canonical)
        self.assertEqual(
            canonical_board_fingerprint(shuffled), canonical.fingerprint
        )
        self.assertEqual(
            canonical_board_fingerprint(shuffled), canonical_board_fingerprint(base)
        )
        del permutation


class IsomorphicMatchingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.solution = _node()
        cls.catalog = _catalog([cls.solution])

    def _observed(self, board):
        return replace(self.solution, board=board)

    def test_board_equivalence_is_off_by_default(self) -> None:
        """A consumer checking for EXACT must keep failing closed until updated."""

        observed = self._observed(("2h", "7s", "Td"))
        result = match_game_spec(observed, self.catalog)
        self.assertEqual(result.status, MatchStatus.UNSUPPORTED)
        self.assertIsNone(result.suit_permutation)
        self.assertNotIn("board_isomorphism", result.to_dict())

    def test_a_relabelled_board_matches_when_asked(self) -> None:
        observed = self._observed(("2h", "7s", "Td"))
        result = match_game_spec(observed, self.catalog, board_isomorphism=True)
        self.assertEqual(result.status, MatchStatus.ISOMORPHIC)
        self.assertEqual(result.reason, MatchReason.ISOMORPHIC_NODE)
        self.assertEqual(result.solution.solution_id, "iso-0")
        payload = result.to_dict()["board_isomorphism"]
        self.assertEqual(payload["isomorphism_version"], BOARD_ISOMORPHISM_VERSION)
        self.assertEqual(
            payload["suit_permutation"], {"c": "h", "d": "s", "h": "c", "s": "d"}
        )

    def test_a_reordered_board_matches_when_asked(self) -> None:
        result = match_game_spec(
            self._observed(("Ts", "2c", "7d")), self.catalog, board_isomorphism=True
        )
        self.assertEqual(result.status, MatchStatus.ISOMORPHIC)
        self.assertTrue(result.suit_permutation.is_identity)

    def test_an_exact_hit_still_wins(self) -> None:
        result = match_game_spec(self.solution, self.catalog, board_isomorphism=True)
        self.assertEqual(result.status, MatchStatus.EXACT)
        self.assertIsNone(result.suit_permutation)
        self.assertNotIn("board_isomorphism", result.to_dict())

    def test_two_equivalent_catalog_nodes_are_ambiguous(self) -> None:
        catalog = _catalog(
            [self.solution, replace(self.solution, board=("2h", "7s", "Td"))]
        )
        result = match_game_spec(
            self._observed(("2d", "7c", "Th")), catalog, board_isomorphism=True
        )
        self.assertEqual(result.status, MatchStatus.UNSUPPORTED)
        self.assertEqual(result.reason, MatchReason.AMBIGUOUS_BEST_MATCH)
        self.assertEqual(list(result.candidate_solution_ids), ["iso-0", "iso-1"])

    def test_an_unrelated_board_still_does_not_match(self) -> None:
        result = match_game_spec(
            self._observed(("Ah", "Kh", "Qh")), self.catalog, board_isomorphism=True
        )
        self.assertEqual(result.status, MatchStatus.UNSUPPORTED)

    def test_the_index_builds_its_equivalence_map_lazily(self) -> None:
        index = SolutionCatalogIndex(self.catalog)
        self.assertIsNone(index._by_canonical_board)
        match_game_spec(self.solution, self.catalog, index=index)
        self.assertIsNone(index._by_canonical_board)
        match_game_spec(
            self._observed(("2h", "7s", "Td")),
            self.catalog,
            index=index,
            board_isomorphism=True,
        )
        self.assertIsNotNone(index._by_canonical_board)

    def test_the_index_gives_the_same_answer_as_an_inline_build(self) -> None:
        observed = self._observed(("2h", "7s", "Td"))
        index = SolutionCatalogIndex(self.catalog)
        self.assertEqual(
            match_game_spec(observed, self.catalog, board_isomorphism=True).to_dict(),
            match_game_spec(
                observed, self.catalog, index=index, board_isomorphism=True
            ).to_dict(),
        )


class IsomorphicStrategyEvidenceTest(unittest.TestCase):
    """A relabelled hit must answer in the frame of the hand actually played."""

    def setUp(self) -> None:
        import tempfile

        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.solution_spec = _node()

        document = {
            "schema_version": STRATEGY_ARTIFACT_SCHEMA_VERSION,
            "solution_id": "iso.artifact",
            "game_spec_fingerprint": self.solution_spec.fingerprint,
            "action_tree_version": "iso/1",
            "node_id": "iso.node",
            "ev_unit": "bb",
            "ev_semantics": "action_ev_from_node",
            "actions": [
                {"action_id": "bet_2bb", "kind": "bet", "size_bb": "2"},
                {"action_id": "check", "kind": "check", "size_bb": None},
            ],
            "entries": [
                {
                    "combo": combo,
                    "weight": "1",
                    "policies": [
                        {"action_id": "bet_2bb", "probability": probability, "ev": None},
                        {
                            "action_id": "check",
                            "probability": str(1 - Decimal(probability)),
                            "ev": None,
                        },
                    ],
                }
                for combo, probability in (("AcKc", "0.8"), ("AhKh", "0.2"))
            ],
            "provenance": {
                "solver_name": "x",
                "solver_version": "1",
                "solver_config_id": "c",
                "generated_at": "2026-08-18T00:00:00Z",
                "quality": "test_only",
                "quality_report_id": None,
                "license": "test",
            },
        }
        import hashlib

        path = self.root / "strategy" / "n.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path.write_bytes(raw)
        self.solution = SolutionSpec(
            solution_id="iso.artifact",
            game_spec=self.solution_spec,
            solver_name="x",
            solver_version="1",
            action_tree_version="iso/1",
            quality=SolutionQuality.TEST_ONLY,
            artifact_id="strategy/n.json",
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
        )
        self.verification = verify_solution_artifact(self.solution, root=self.root)

    def test_a_combo_is_asked_and_answered_in_the_observed_frame(self) -> None:
        # Observed board relabels c->h, so the observed AhKh is the solution's AcKc.
        observed = replace(self.solution_spec, board=("2h", "7s", "Td"))
        bridge = permutation_between(observed, self.solution_spec)
        evidence = build_strategy_evidence(
            self.verification, combo="AhKh", suit_permutation=bridge
        )
        payload = evidence.to_dict()
        self.assertEqual(payload["combo"], "AhKh")
        self.assertEqual(payload["board_isomorphism"]["solution_frame_combo"], "AcKc")
        self.assertEqual(payload["actions"][0]["probability"], "0.8")

    def test_the_permutation_is_recorded_with_its_assumption(self) -> None:
        observed = replace(self.solution_spec, board=("2h", "7s", "Td"))
        bridge = permutation_between(observed, self.solution_spec)
        payload = build_strategy_evidence(
            self.verification, suit_permutation=bridge
        ).to_dict()
        self.assertEqual(payload["scope"], "artifact_entries")
        self.assertIn("suit symmetric", payload["board_isomorphism"]["assumption"])

    def test_an_identity_permutation_is_not_reported(self) -> None:
        payload = build_strategy_evidence(
            self.verification,
            combo="AcKc",
            suit_permutation=SuitPermutation.identity(),
        ).to_dict()
        self.assertNotIn("board_isomorphism", payload)

    def test_an_uncovered_combo_names_both_frames(self) -> None:
        observed = replace(self.solution_spec, board=("2h", "7s", "Td"))
        bridge = permutation_between(observed, self.solution_spec)
        with self.assertRaisesRegex(ComboNotCoveredError, r"QhJh.*\(as QcJc\)"):
            build_strategy_evidence(
                self.verification, combo="QhJh", suit_permutation=bridge
            )


class IsomorphismCLITest(unittest.TestCase):
    def test_the_flag_is_what_turns_it_on(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
            twin = (
                source.replace("100000000001", "100000000002")
                .replace("[2c 7d Ts]", "[Td 7s 2h]")
                .replace("[2c 7d Ts] [As]", "[Td 7s 2h] [As]")
            )
            (root / "hands.txt").write_text(source + "\n\n" + twin, encoding="utf-8")

            database = root / "dev.db"
            catalog = root / "catalog.json"
            common = [
                "--database", os.fspath(database),
                "--catalog", os.fspath(catalog),
                "--rake-model", "pokerstars.cash.example",
                "--rake-percent", "5", "--rake-cap-bb", "3",
            ]
            self._run(["import", os.fspath(root / "hands.txt"), "--database", os.fspath(database)])
            self._run(
                ["gto-catalog-add", "pokerstars", "100000000001", "--before-action", "5"]
                + common
                + [
                    "--solution-id", "iso.demo",
                    "--solver-name", "x", "--solver-version", "1",
                    "--action-tree-version", "iso/1",
                    "--artifact-id", "strategy/n.json",
                    "--quality", "experimental",
                ]
            )
            base = ["gto-match", "pokerstars", "100000000002", "--before-action", "5"]
            _, off = self._run(base + common + ["--json"])
            _, on = self._run(base + common + ["--board-isomorphism", "--json"])

        self.assertEqual(json.loads(off)["match"]["status"], "unsupported")
        enabled = json.loads(on)["match"]
        self.assertEqual(enabled["status"], "isomorphic")
        self.assertEqual(
            enabled["board_isomorphism"]["suit_permutation"],
            {"c": "h", "d": "s", "h": "c", "s": "d"},
        )

    def _run(self, argv: list[str]) -> tuple[int, str]:
        from rivermind_core.cli import main

        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, buffer.getvalue()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
