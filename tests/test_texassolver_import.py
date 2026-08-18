from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.cli import main  # noqa: E402
from rivermind_core.gto_matcher import extract_decision_game_spec  # noqa: E402
from rivermind_core.gto_specs import (  # noqa: E402
    RakeSpec,
    SolutionQuality,
    load_solution_catalog,
)
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.strategy_artifacts import (  # noqa: E402
    serialize_strategy_artifact,
    strategy_artifact_from_dict,
    verify_catalog_artifact,
)
from rivermind_core.texassolver_import import (  # noqa: E402
    SolverImportError,
    convert_texassolver_dump,
    parse_texassolver_range,
)


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"

#: A small BTN range whose expansion avoids the demo board (2c 7d Ts).
RANGE = "AA,KK,AKs,AKo,QQ:0.5,98s:0.75"

BASE_KWARGS = dict(
    solution_id="texassolver.test",
    action_tree_version="test.tree/0.0.1",
    node_id="test.node",
    solver_version="0.2.0",
    solver_config_id="test-config",
    generated_at="2026-08-18T00:00:00Z",
    license_text="TexasSolver AGPL v3 binary output.",
)


def _demo_spec():
    hand = default_registry().parse(
        (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8")
    )
    return extract_decision_game_spec(
        hand,
        before_action=5,
        rake=RakeSpec(
            model_id="pokerstars.cash.example",
            percent=Decimal("5"),
            cap_bb=Decimal("3"),
        ),
    )


def _dump(combos: list[str]) -> dict[str, Any]:
    """A structurally faithful TexasSolver dump: OOP root, IP node under CHECK."""

    ip_actions = ["CHECK", "BET 2.0", "BET 97.0"]
    ip = {combo: [0.5, 0.25, 0.25] for combo in combos}
    oop = {combo: [0.6, 0.4] for combo in combos}
    return {
        "node_type": "action_node",
        "player": 1,
        "actions": ["CHECK", "BET 2.0"],
        "strategy": {"actions": ["CHECK", "BET 2.0"], "strategy": oop},
        "childrens": {
            "CHECK": {
                "node_type": "action_node",
                "player": 0,
                "actions": ip_actions,
                "strategy": {"actions": ip_actions, "strategy": ip},
                "childrens": {
                    "CHECK": {
                        "node_type": "chance_node",
                        "deal_cards": {},
                        "deal_number": 45,
                    }
                },
            },
            "BET 2.0": {
                "node_type": "chance_node",
                "deal_cards": {},
                "deal_number": 45,
            },
        },
    }


class RangeParsingTest(unittest.TestCase):
    BOARD = ("2c", "7d", "Ts")

    def test_expands_pairs_suited_and_offsuit(self) -> None:
        pairs = parse_texassolver_range("AA")
        self.assertEqual(len(pairs), 6)
        self.assertIn("AhAs", pairs)

        suited = parse_texassolver_range("AKs")
        self.assertEqual(len(suited), 4)
        self.assertEqual(sorted(suited), ["AcKc", "AdKd", "AhKh", "AsKs"])

        offsuit = parse_texassolver_range("AKo")
        self.assertEqual(len(offsuit), 12)

        both = parse_texassolver_range("AK")
        self.assertEqual(len(both), 16)

    def test_canonicalizes_every_combination(self) -> None:
        for combo in parse_texassolver_range("AKo,99,JTs"):
            self.assertEqual(len(combo), 4)
            first, second = combo[:2], combo[2:]
            ranks = "23456789TJQKA"
            self.assertGreaterEqual(ranks.index(first[0]), ranks.index(second[0]))
            if first[0] == second[0]:
                self.assertLess("cdhs".index(first[1]), "cdhs".index(second[1]))

    def test_applies_weights_and_drops_negligible_ones(self) -> None:
        weights = parse_texassolver_range("AA,KK:0.5,QQ:0.005,JJ:0.001")
        self.assertEqual(weights["AhAs"], Decimal("1"))
        self.assertEqual(weights["KhKs"], Decimal("0.5"))
        self.assertNotIn("QhQs", weights)
        self.assertNotIn("JhJs", weights)

    def test_removes_combinations_blocked_by_the_board(self) -> None:
        weights = parse_texassolver_range("TT,98s", self.BOARD)
        self.assertNotIn("ThTs", weights)
        self.assertEqual(len([c for c in weights if c.startswith("T")]), 3)
        self.assertEqual(len(parse_texassolver_range("TT", self.BOARD)), 3)

    def test_rejects_notation_texassolver_does_not_accept(self) -> None:
        for token, pattern in (
            ("A2s+", "not valid notation"),
            ("AXs", "unknown rank"),
            ("AAs", "a pair cannot be suited"),
            ("AKx", "must end in"),
            ("AKs:1.5", "weight above 1"),
            ("A", "not valid notation"),
        ):
            with self.subTest(token=token):
                with self.assertRaisesRegex(SolverImportError, pattern):
                    parse_texassolver_range(token)

    def test_rejects_an_empty_expansion(self) -> None:
        with self.assertRaisesRegex(SolverImportError, "expands to no combinations"):
            parse_texassolver_range("AA:0.001")


class ConversionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _demo_spec()
        cls.weights = parse_texassolver_range(RANGE, cls.spec.board)
        cls.combos = sorted(cls.weights)

    def _convert(self, dump=None, **overrides):
        options = dict(BASE_KWARGS)
        options.update(overrides)
        options.setdefault("range_weights", self.weights)
        return convert_texassolver_dump(
            dump if dump is not None else _dump(self.combos),
            game_spec=self.spec,
            node_path=("CHECK",),
            **options,
        )

    def test_converts_a_node_into_a_valid_artifact(self) -> None:
        converted = self._convert()
        self.assertEqual(converted.solver_player, 0)
        self.assertEqual(converted.to_dict()["solver_player_seat"], "ip")
        self.assertEqual(converted.combo_count, len(self.combos))

        artifact = strategy_artifact_from_dict(
            converted.document, game_spec=self.spec
        )
        self.assertEqual(artifact.game_spec_fingerprint, self.spec.fingerprint)
        self.assertFalse(artifact.ev_present)
        self.assertEqual(
            artifact.action_ids, ("allin_97", "bet_2", "check")
        )
        # Serializing must succeed, which is what makes the hash reproducible.
        self.assertTrue(serialize_strategy_artifact(artifact))

    def test_detects_an_all_in_by_comparing_with_the_remaining_stack(self) -> None:
        artifact = strategy_artifact_from_dict(
            self._convert().document, game_spec=self.spec
        )
        kinds = {item.action_id: item.kind.value for item in artifact.actions}
        self.assertEqual(kinds["allin_97"], "all_in")
        self.assertEqual(kinds["bet_2"], "bet")
        self.assertEqual(kinds["check"], "check")

    def test_carries_range_weights_through(self) -> None:
        document = self._convert().document
        by_combo = {item["combo"]: item["weight"] for item in document["entries"]}
        self.assertEqual(by_combo["QcQd"], "0.5")
        self.assertEqual(by_combo["9c8c"], "0.75")
        self.assertEqual(by_combo["AhAs"], "1")

    def test_renormalizes_rows_to_exactly_one(self) -> None:
        dump = _dump(self.combos)
        node = dump["childrens"]["CHECK"]["strategy"]["strategy"]
        # float32 noise of the kind the solver actually emits
        for index, combo in enumerate(self.combos):
            node[combo] = [0.3333333432674408, 0.3333333432674408, 0.3333333134651184]
        converted = self._convert(dump)
        for entry in converted.document["entries"]:
            total = sum(Decimal(p["probability"]) for p in entry["policies"])
            self.assertEqual(total, Decimal("1"), entry["combo"])
        self.assertLessEqual(converted.max_renormalization, Decimal("0.000001"))

    def test_refuses_to_mint_verified(self) -> None:
        with self.assertRaisesRegex(SolverImportError, "never writes 'verified'"):
            self._convert(quality=SolutionQuality.VERIFIED)

    def test_refuses_to_guess_weights(self) -> None:
        with self.assertRaisesRegex(SolverImportError, "does not carry range weights"):
            self._convert(range_weights=None)
        with self.assertRaisesRegex(SolverImportError, "not both"):
            self._convert(assume_uniform_weights=True)

    def test_uniform_weights_must_be_stated_explicitly(self) -> None:
        converted = self._convert(range_weights=None, assume_uniform_weights=True)
        self.assertIn("uniform", converted.weights_source)
        self.assertTrue(
            all(item["weight"] == "1" for item in converted.document["entries"])
        )

    def test_refuses_a_range_that_does_not_match_the_solve(self) -> None:
        with self.assertRaisesRegex(SolverImportError, "the supplied range does not"):
            self._convert(range_weights=parse_texassolver_range("22", self.spec.board))

    def test_refuses_a_combination_that_uses_a_board_card(self) -> None:
        dump = _dump(self.combos)
        node = dump["childrens"]["CHECK"]["strategy"]["strategy"]
        node["Ts2c"] = [0.5, 0.25, 0.25]
        with self.assertRaisesRegex(SolverImportError, "uses a board card"):
            self._convert(dump)

    def test_refuses_a_bad_node_path(self) -> None:
        with self.assertRaisesRegex(SolverImportError, "no child 'RAISE'"):
            convert_texassolver_dump(
                _dump(self.combos),
                game_spec=self.spec,
                node_path=("RAISE",),
                range_weights=self.weights,
                **BASE_KWARGS,
            )

    def test_refuses_a_chance_node(self) -> None:
        with self.assertRaisesRegex(SolverImportError, "not an action node"):
            convert_texassolver_dump(
                _dump(self.combos),
                game_spec=self.spec,
                node_path=("CHECK", "CHECK"),
                range_weights=self.weights,
                **BASE_KWARGS,
            )

    def test_refuses_rows_that_are_not_distributions(self) -> None:
        dump = _dump(self.combos)
        node = dump["childrens"]["CHECK"]["strategy"]["strategy"]
        node[self.combos[0]] = [0.5, 0.25, 0.10]
        with self.assertRaisesRegex(SolverImportError, "not a probability distribution"):
            self._convert(dump)

    def test_refuses_malformed_probabilities(self) -> None:
        for row, pattern in (
            ([0.5, 0.25, "0.25"], "non-numeric probability"),
            ([1.5, -0.25, -0.25], "outside \\[0, 1\\]"),
            ([0.5, 0.25], "probabilities for 3 actions"),
        ):
            with self.subTest(row=row):
                dump = _dump(self.combos)
                dump["childrens"]["CHECK"]["strategy"]["strategy"][
                    self.combos[0]
                ] = row
                with self.assertRaisesRegex(SolverImportError, pattern):
                    self._convert(dump)

    def test_refuses_an_unrecognized_action_label(self) -> None:
        dump = _dump(self.combos)
        node = dump["childrens"]["CHECK"]
        node["strategy"]["actions"] = ["CHECK", "SHOVE", "BET 2.0"]
        with self.assertRaisesRegex(SolverImportError, "unrecognized solver action"):
            self._convert(dump)

    def test_refuses_a_node_with_fewer_than_two_actions(self) -> None:
        dump = _dump(self.combos)
        node = dump["childrens"]["CHECK"]
        node["strategy"]["actions"] = ["CHECK"]
        for combo in node["strategy"]["strategy"]:
            node["strategy"]["strategy"][combo] = [1.0]
        with self.assertRaisesRegex(SolverImportError, "not a decision with two actions"):
            self._convert(dump)

    def test_conversion_is_deterministic(self) -> None:
        first = self._convert().document
        second = self._convert(deepcopy(_dump(self.combos))).document
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )


class SolverIngestCLITest(unittest.TestCase):
    """The documented path: register -> import -> package -> verify."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.database = self.root / "dev.db"
        self.catalog = self.root / "catalog.json"
        self.solution_id = "texassolver.hu-cash.btn-flop-cbet"

        spec = _demo_spec()
        combos = sorted(parse_texassolver_range(RANGE, spec.board))
        (self.root / "dump.json").write_text(
            json.dumps(_dump(combos)), encoding="utf-8"
        )
        self._run(["import", os.fspath(FIXTURES), "--database", os.fspath(self.database)])

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, buffer.getvalue()

    def _register(self) -> tuple[int, str]:
        return self._run(
            [
                "gto-catalog-add", "pokerstars", "100000000001",
                "--before-action", "5",
                "--database", os.fspath(self.database),
                "--catalog", os.fspath(self.catalog),
                "--rake-model", "pokerstars.cash.example",
                "--rake-percent", "5", "--rake-cap-bb", "3",
                "--solution-id", self.solution_id,
                "--solver-name", "TexasSolver",
                "--solver-version", "0.2.0",
                "--action-tree-version", "rivermind.hu-flop-cbet/0.0.1",
                "--artifact-id", "strategy/btn_flop_cbet.json",
                "--quality", "experimental",
                "--json",
            ]
        )

    def _import(self, *extra: str) -> tuple[int, str]:
        return self._run(
            [
                "gto-import-texassolver", os.fspath(self.root / "dump.json"),
                "--catalog", os.fspath(self.catalog),
                "--solution-id", self.solution_id,
                "--node-path", "CHECK",
                "--node-id", "cash.hu.100bb.flop.btn-cbet-vs-bb-check",
                "--solver-version", "0.2.0",
                "--solver-config-id", "hu-flop-cbet-v1",
                "--generated-at", "2026-08-18T00:00:00Z",
                "--license", "TexasSolver AGPL v3 binary output.",
                "--range", RANGE,
                "--out", os.fspath(self.root / "draft.json"),
                "--json",
                *extra,
            ]
        )

    def test_registers_the_node_with_a_placeholder_hash(self) -> None:
        code, output = self._register()
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["artifact_sha256"], "0" * 64)
        self.assertEqual(
            payload["game_spec_fingerprint"],
            "85b7db3215c80f307bf4c745ab4a001b68f14ab3f0c938577b99e1c6bb469f55",
        )
        # A placeholder hash must fail verification, not pass it.
        code, _ = self._run(
            [
                "gto-artifact-verify",
                os.fspath(self.catalog),
                self.solution_id,
                "--json",
            ]
        )
        self.assertEqual(code, 2)

    def test_refuses_to_register_the_same_solution_twice(self) -> None:
        self.assertEqual(self._register()[0], 0)
        self.assertEqual(self._register()[0], 2)

    def test_full_ingest_path(self) -> None:
        self.assertEqual(self._register()[0], 0)

        code, output = self._import()
        self.assertEqual(code, 0)
        summary = json.loads(output)
        self.assertEqual(summary["solver_player_seat"], "ip")
        self.assertEqual(summary["quality"], "experimental")

        code, output = self._run(
            [
                "gto-artifact-package",
                os.fspath(self.root / "draft.json"),
                "--catalog", os.fspath(self.catalog),
                "--write", "--update-catalog", "--json",
            ]
        )
        self.assertEqual(code, 0)
        packaged = json.loads(output)
        self.assertTrue(packaged["written"])
        self.assertEqual(packaged["quality"], "experimental")

        verification = verify_catalog_artifact(
            load_solution_catalog(self.catalog),
            self.solution_id,
            root=self.catalog.parent,
        )
        self.assertEqual(
            verification.artifact_sha256, packaged["canonical_sha256"]
        )
        self.assertEqual(verification.quality, SolutionQuality.EXPERIMENTAL)

    def test_import_refuses_an_unregistered_solution(self) -> None:
        code, _ = self._import()
        self.assertEqual(code, 2)

    def test_import_refuses_when_weights_are_unspecified(self) -> None:
        """argparse enforces the choice, so the command cannot even start."""

        self.assertEqual(self._register()[0], 0)
        with self.assertRaises(SystemExit) as caught:
            self._run(
                [
                    "gto-import-texassolver", os.fspath(self.root / "dump.json"),
                    "--catalog", os.fspath(self.catalog),
                    "--solution-id", self.solution_id,
                    "--node-path", "CHECK",
                    "--node-id", "n",
                    "--solver-version", "0.2.0",
                    "--solver-config-id", "c",
                    "--generated-at", "2026-08-18T00:00:00Z",
                    "--license", "l",
                    "--json",
                ]
            )
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
