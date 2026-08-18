from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.importer import HandHistoryImporter  # noqa: E402
from rivermind_core.leaks import (  # noqa: E402
    LeakDirection,
    LeakRule,
    LeakSeverity,
    LeakStatus,
    evaluate_leaks,
    wilson_interval_95,
)
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.reports import StatMetric  # noqa: E402
from rivermind_core.stats import PlayerStats, StatValue  # noqa: E402
from rivermind_core.storage import SQLiteHandStore  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _stats(metric: StatMetric, value: StatValue) -> PlayerStats:
    empty = StatValue(0, 0)
    stats = PlayerStats(
        player_name="Hero",
        hands=max(value.opportunities, 1),
        vpip=empty,
        pfr=empty,
        rfi=empty,
        three_bet=empty,
        call_open=empty,
        cold_call=empty,
        fold_to_three_bet=empty,
        flop_cbet=empty,
        fold_to_flop_cbet=empty,
    )
    return replace(stats, **{metric.value: value})


def _rule(
    *,
    metric: StatMetric = StatMetric.COLD_CALL,
    direction: LeakDirection = LeakDirection.ABOVE,
    trigger: float = 40,
    priority: float = 60,
    minimum: int = 20,
    evidence_occurred: bool = True,
) -> LeakRule:
    return LeakRule(
        rule_id="test_rule",
        title="Test rule",
        metric=metric,
        direction=direction,
        trigger_percentage=trigger,
        priority_percentage=priority,
        min_opportunities=minimum,
        evidence_occurred=evidence_occurred,
        rationale="Evidence-based test rationale",
        review_prompt="Review the matching hands",
    )


class LeakEngineTest(unittest.TestCase):
    def test_wilson_interval_is_deterministic_and_handles_no_opportunities(self) -> None:
        interval = wilson_interval_95(StatValue(50, 100))

        self.assertIsNone(wilson_interval_95(StatValue(0, 0)))
        assert interval is not None
        self.assertAlmostEqual(interval[0], 40.38, places=2)
        self.assertAlmostEqual(interval[1], 59.62, places=2)

    def test_above_rule_requires_sample_and_full_interval_crossing(self) -> None:
        rule = _rule()
        insufficient = evaluate_leaks(
            [_stats(StatMetric.COLD_CALL, StatValue(10, 10))], rules=(rule,)
        )[0]
        detected = evaluate_leaks(
            [_stats(StatMetric.COLD_CALL, StatValue(80, 100))], rules=(rule,)
        )[0]
        clear = evaluate_leaks(
            [_stats(StatMetric.COLD_CALL, StatValue(40, 100))], rules=(rule,)
        )[0]

        self.assertEqual(insufficient.status, LeakStatus.INSUFFICIENT_SAMPLE)
        self.assertEqual(insufficient.sample_shortfall, 10)
        self.assertEqual(detected.status, LeakStatus.DETECTED)
        self.assertEqual(detected.severity, LeakSeverity.PRIORITY)
        self.assertEqual(clear.status, LeakStatus.CLEAR)

    def test_below_rule_uses_upper_confidence_bound(self) -> None:
        rule = _rule(
            metric=StatMetric.THREE_BET,
            direction=LeakDirection.BELOW,
            trigger=10,
            priority=5,
            evidence_occurred=False,
        )
        assessment = evaluate_leaks(
            [_stats(StatMetric.THREE_BET, StatValue(0, 100))], rules=(rule,)
        )[0]

        self.assertEqual(assessment.status, LeakStatus.DETECTED)
        self.assertEqual(assessment.severity, LeakSeverity.PRIORITY)
        assert assessment.confidence_high is not None
        self.assertLess(assessment.confidence_high, 5)


class StoredLeakReportTest(unittest.TestCase):
    def test_detected_card_contains_metric_specific_evidence_hands(self) -> None:
        rule = _rule(
            metric=StatMetric.FLOP_CBET,
            trigger=10,
            priority=15,
            minimum=1,
        )
        with SQLiteHandStore(":memory:") as store:
            HandHistoryImporter(default_registry(), store).import_text(
                "cash.txt",
                (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8"),
            )
            report = store.query_leaks(
                heroes_only=True,
                rules=(rule,),
                evidence_limit=3,
            )

        self.assertEqual(report.detected_count, 1)
        card = report.cards[0]
        self.assertEqual(card.assessment.severity, LeakSeverity.PRIORITY)
        self.assertEqual(len(card.evidence_hands), 1)
        evidence = card.evidence_hands[0]
        self.assertEqual(evidence.stats.hand_id, "100000000001")
        self.assertTrue(evidence.stats.flop_cbet)
        self.assertTrue(evidence.stats.is_hero)


if __name__ == "__main__":
    unittest.main()
