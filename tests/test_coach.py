from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.coach import (  # noqa: E402
    COACH_CANDIDATE_SCHEMA_VERSION,
    CoachReport,
    CoachSource,
    ValidationStatus,
    build_explanation_evidence,
    explain_leak_card,
)
from rivermind_core.importer import HandHistoryImporter  # noqa: E402
from rivermind_core.leaks import LeakCard, LeakDirection, LeakRule  # noqa: E402
from rivermind_core.leaks import build_leak_report  # noqa: E402
from rivermind_core.html_report import render_analysis_page  # noqa: E402
from rivermind_core.parsers import default_registry  # noqa: E402
from rivermind_core.reports import StatMetric  # noqa: E402
from rivermind_core.storage import SQLiteHandStore  # noqa: E402


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _detected_card() -> LeakCard:
    rule = LeakRule(
        rule_id="coach_test",
        title="Flop CBet 使用偏多",
        metric=StatMetric.FLOP_CBET,
        direction=LeakDirection.ABOVE,
        trigger_percentage=10,
        priority_percentage=15,
        min_opportunities=1,
        evidence_occurred=True,
        rationale="持续下注频率值得复查。",
        review_prompt="检查牌面、人数和位置。",
    )
    with SQLiteHandStore(":memory:") as store:
        HandHistoryImporter(default_registry(), store).import_text(
            "cash.txt",
            (FIXTURES / "pokerstars_cash.txt").read_text(encoding="utf-8"),
        )
        return store.query_leaks(
            heroes_only=True,
            rules=(rule,),
            evidence_limit=1,
        ).cards[0]


def _valid_candidate(card: LeakCard) -> dict[str, object]:
    evidence = build_explanation_evidence(card)
    return {
        "schema_version": COACH_CANDIDATE_SCHEMA_VERSION,
        "evidence_id": evidence.evidence_id,
        "evidence_hash": evidence.evidence_hash,
        "headline": "{{signal.title}}",
        "observation": (
            "观察值{{signal.observed_percentage}}，样本{{signal.sample}}，"
            "区间{{signal.confidence_interval_95}}，阈值{{rule.trigger}}。"
        ),
        "teaching_point": "{{rule.rationale}}{{limitations.context}}",
        "review_plan": ["{{rule.review_prompt}}", "先看{{hand.1}}。"],
        "uncertainty": "{{limitations.no_gto}}",
        "evidence_refs": [
            "signal.title",
            "signal.observed_percentage",
            "signal.sample",
            "signal.confidence_interval_95",
            "rule.trigger",
            "rule.rationale",
            "limitations.context",
            "rule.review_prompt",
            "hand.1",
            "limitations.no_gto",
        ],
    }


class CoachEvidenceTest(unittest.TestCase):
    def test_model_payload_is_stable_and_excludes_raw_source_identifiers(self) -> None:
        card = _detected_card()
        private_assessment = replace(card.assessment, player_name="AlicePrivate")
        private_hands = tuple(
            replace(
                hand,
                stats=replace(hand.stats, player_name="AlicePrivate"),
                table_name="Secret Table",
            )
            for hand in card.evidence_hands
        )
        private_card = LeakCard(private_assessment, private_hands)

        evidence = build_explanation_evidence(private_card)
        payload = json.dumps(evidence.model_payload(), ensure_ascii=False)

        self.assertEqual(evidence.evidence_hash, build_explanation_evidence(private_card).evidence_hash)
        self.assertNotIn("AlicePrivate", payload)
        self.assertNotIn("pokerstars", payload)
        self.assertNotIn("100000000001", payload)
        self.assertNotIn("Secret Table", payload)
        self.assertIn('"subject": "Hero"', payload)
        self.assertIn('"hand.1"', payload)

    def test_template_explanation_uses_only_deterministic_card_values(self) -> None:
        card = _detected_card()
        item = explain_leak_card(card)

        self.assertEqual(item.explanation.source, CoachSource.TEMPLATE)
        self.assertEqual(item.explanation.validation.status, ValidationStatus.NOT_REQUESTED)
        self.assertIn("100.0%", item.explanation.observation)
        self.assertIn("1/1", item.explanation.observation)
        self.assertIn("20.7%–100.0%", item.explanation.observation)
        self.assertIn("不包含 GTO", item.explanation.uncertainty)

    def test_valid_candidate_renders_fact_placeholders(self) -> None:
        card = _detected_card()
        item = explain_leak_card(card, _valid_candidate(card))

        self.assertEqual(item.explanation.source, CoachSource.LLM_VALIDATED)
        self.assertEqual(item.explanation.validation.status, ValidationStatus.ACCEPTED)
        self.assertNotIn("{{", item.explanation.observation)
        self.assertIn("100.0%", item.explanation.observation)
        self.assertIn("证据手牌 1", item.explanation.review_plan[1])

    def test_invalid_candidate_falls_back_with_reproducible_issue_codes(self) -> None:
        card = _detected_card()
        candidate = _valid_candidate(card)
        candidate["evidence_hash"] = "tampered"
        candidate["observation"] = "这个指标是 99%，所以具有 GTO 范围优势。"
        candidate["teaching_point"] = (
            "在 pokerstars 你应该立即下注。"
            "{{rule.rationale}}{{limitations.context}}"
        )
        candidate["action"] = "raise"

        item = explain_leak_card(card, candidate)
        issue_codes = {issue.code for issue in item.explanation.validation.issues}

        self.assertEqual(item.explanation.source, CoachSource.TEMPLATE_FALLBACK)
        self.assertEqual(item.explanation.validation.status, ValidationStatus.REJECTED)
        self.assertIn("unknown_field", issue_codes)
        self.assertIn("evidence_hash_mismatch", issue_codes)
        self.assertIn("unreferenced_number", issue_codes)
        self.assertIn("unsupported_claim", issue_codes)
        self.assertIn("action_directive", issue_codes)
        self.assertIn("sensitive_data", issue_codes)
        self.assertNotIn("99%", item.explanation.observation)

    def test_spelled_out_numbers_are_rejected_without_fact_references(self) -> None:
        card = _detected_card()
        for phrase in ("大约一半，", "about fifty percent, "):
            with self.subTest(phrase=phrase):
                candidate = _valid_candidate(card)
                candidate["observation"] = phrase + str(candidate["observation"])

                item = explain_leak_card(card, candidate)
                issue_codes = {
                    issue.code for issue in item.explanation.validation.issues
                }

                self.assertEqual(
                    item.explanation.source,
                    CoachSource.TEMPLATE_FALLBACK,
                )
                self.assertIn("unreferenced_number", issue_codes)

    def test_analysis_page_escapes_validated_candidate_text(self) -> None:
        card = _detected_card()
        candidate = _valid_candidate(card)
        candidate["headline"] = (
            "复盘 <img src=x onerror=alert('x')> {{signal.title}}"
        )
        item = explain_leak_card(card, candidate)
        leak_report = build_leak_report((card.assessment,), (card,))
        html = render_analysis_page(
            (),
            (),
            (),
            leak_report=leak_report,
            coach_report=CoachReport((item,)),
        )

        self.assertEqual(item.explanation.source, CoachSource.LLM_VALIDATED)
        self.assertIn("AI 教练", html)
        self.assertIn("已校验模型草稿", html)
        self.assertIn("&lt;img src=x onerror=alert(&#x27;x&#x27;)&gt;", html)
        self.assertNotIn("<img src=x", html)


if __name__ == "__main__":
    unittest.main()
