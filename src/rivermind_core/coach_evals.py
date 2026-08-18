from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from rivermind_core.coach import (
    ValidationStatus,
    build_candidate_skeleton,
    build_explanation_evidence,
    explain_leak_card,
)
from rivermind_core.leaks import (
    LeakAssessment,
    LeakCard,
    LeakDirection,
    LeakSeverity,
    LeakStatus,
)
from rivermind_core.models import GameType, PlayerPosition
from rivermind_core.reports import PlayerHandReport, StatMetric
from rivermind_core.stats import PlayerHandStatRow


COACH_EVAL_SCHEMA_VERSION = "coach-eval-corpus/1.0.0"


@dataclass(frozen=True, slots=True)
class CoachEvalCaseResult:
    case_id: str
    category: str
    description: str
    passed: bool
    expected_status: str
    actual_status: str
    expected_issue_codes: tuple[str, ...]
    actual_issue_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoachEvalReport:
    corpus_schema: str
    corpus_path: str
    results: tuple[CoachEvalCaseResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(item.passed for item in self.results)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def categories(self) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for item in self.results:
            counts = summary.setdefault(item.category, {"total": 0, "passed": 0})
            counts["total"] += 1
            counts["passed"] += int(item.passed)
        return summary


def build_eval_card() -> LeakCard:
    """Build private-looking source data to exercise deidentification boundaries."""
    assessment = LeakAssessment(
        rule_id="coach_eval_flop_cbet",
        profile_version="eval/1.0.0",
        player_name="AlicePrivate",
        title="Flop CBet 使用偏多",
        metric=StatMetric.FLOP_CBET,
        direction=LeakDirection.ABOVE,
        status=LeakStatus.DETECTED,
        severity=LeakSeverity.PRIORITY,
        occurrences=42,
        opportunities=50,
        observed_percentage=84.0,
        confidence_low=71.5,
        confidence_high=91.7,
        trigger_percentage=80.0,
        priority_percentage=90.0,
        min_opportunities=30,
        evidence_occurred=True,
        rationale="持续下注频率值得结合牌面、人数和位置复查。",
        review_prompt="检查牌面、人数、位置和有效筹码。",
    )
    stats = PlayerHandStatRow(
        site="pokerstars",
        hand_id="100000000001",
        game_type=GameType.CASH,
        tournament_id=None,
        player_name="AlicePrivate",
        is_hero=True,
        position=PlayerPosition.BUTTON,
        starting_stack_bb=Decimal("100"),
        effective_stack_bb=Decimal("87.5"),
        vpip=True,
        pfr=True,
        rfi_opportunity=True,
        rfi=True,
        three_bet_opportunity=False,
        three_bet=False,
        call_open_opportunity=False,
        call_open=False,
        cold_call_opportunity=False,
        cold_call=False,
        fold_to_three_bet_opportunity=False,
        fold_to_three_bet=False,
        flop_cbet_opportunity=True,
        flop_cbet=True,
        fold_to_flop_cbet_opportunity=False,
        fold_to_flop_cbet=False,
    )
    hand = PlayerHandReport(
        stats=stats,
        played_at=datetime(2026, 8, 18, 20, 0),
        currency="USD",
        table_name="Secret Table",
        invested=Decimal("12.50"),
        returned=Decimal("0"),
        collected=Decimal("25"),
        net_result=Decimal("12.50"),
        net_result_bb=Decimal("25"),
        accounting_balanced=True,
    )
    return LeakCard(assessment, (hand,))


def run_coach_eval(corpus_path: Path) -> CoachEvalReport:
    document = json.loads(corpus_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Coach eval corpus must be a JSON object")
    schema_version = document.get("schema_version")
    if schema_version != COACH_EVAL_SCHEMA_VERSION:
        raise ValueError(f"Unsupported coach eval corpus schema: {schema_version}")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("Coach eval corpus cases must be an array")
    expected_cases = document.get("expected_cases")
    if type(expected_cases) is not int or expected_cases != len(cases):
        raise ValueError("Coach eval corpus case count does not match expected_cases")
    case_ids = [_required_string(case, "id") for case in cases if isinstance(case, dict)]
    if len(case_ids) != len(cases) or len(case_ids) != len(set(case_ids)):
        raise ValueError("Coach eval corpus case ids must be present and unique")

    card = build_eval_card()
    evidence = build_explanation_evidence(card)
    base_candidate = build_candidate_skeleton(evidence)
    results = tuple(
        _run_case(case, card, base_candidate)
        for case in cases
    )
    return CoachEvalReport(
        corpus_schema=schema_version,
        corpus_path=str(corpus_path.resolve()),
        results=results,
    )


def coach_eval_report_to_dict(report: CoachEvalReport) -> dict[str, object]:
    return {
        "corpus_schema": report.corpus_schema,
        "corpus_path": report.corpus_path,
        "summary": {
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "categories": report.categories,
        },
        "results": [
            {
                "id": item.case_id,
                "category": item.category,
                "description": item.description,
                "passed": item.passed,
                "expected_status": item.expected_status,
                "actual_status": item.actual_status,
                "expected_issue_codes": list(item.expected_issue_codes),
                "actual_issue_codes": list(item.actual_issue_codes),
            }
            for item in report.results
        ],
    }


def _run_case(
    raw_case: object,
    card: LeakCard,
    base_candidate: Mapping[str, object],
) -> CoachEvalCaseResult:
    if not isinstance(raw_case, dict):
        raise ValueError("Each coach eval case must be an object")
    case_id = _required_string(raw_case, "id")
    category = _required_string(raw_case, "category")
    description = _required_string(raw_case, "description")
    expected_status = _required_string(raw_case, "expected_status")
    if expected_status not in {item.value for item in ValidationStatus}:
        raise ValueError(f"Invalid expected_status in {case_id}: {expected_status}")
    expected_codes_value = raw_case.get("expected_issue_codes", [])
    if not isinstance(expected_codes_value, list) or not all(
        isinstance(item, str) for item in expected_codes_value
    ):
        raise ValueError(f"Invalid expected_issue_codes in {case_id}")

    candidate = copy.deepcopy(dict(base_candidate))
    _apply_case_mutations(candidate, raw_case, case_id)
    item = explain_leak_card(card, candidate)
    actual_status = item.explanation.validation.status.value
    actual_codes = tuple(
        sorted({issue.code for issue in item.explanation.validation.issues})
    )
    expected_codes = tuple(expected_codes_value)
    passed = (
        actual_status == expected_status
        and set(expected_codes).issubset(actual_codes)
        and (expected_status != ValidationStatus.ACCEPTED.value or not actual_codes)
    )
    return CoachEvalCaseResult(
        case_id=case_id,
        category=category,
        description=description,
        passed=passed,
        expected_status=expected_status,
        actual_status=actual_status,
        expected_issue_codes=expected_codes,
        actual_issue_codes=actual_codes,
    )


def _apply_case_mutations(
    candidate: dict[str, object],
    raw_case: Mapping[str, object],
    case_id: str,
) -> None:
    remove = raw_case.get("remove", [])
    if not isinstance(remove, list) or not all(isinstance(item, str) for item in remove):
        raise ValueError(f"Invalid remove mutation in {case_id}")
    for field in remove:
        candidate.pop(field, None)

    set_values = raw_case.get("set", {})
    if not isinstance(set_values, dict):
        raise ValueError(f"Invalid set mutation in {case_id}")
    candidate.update(set_values)

    append_values = raw_case.get("append", {})
    if not isinstance(append_values, dict):
        raise ValueError(f"Invalid append mutation in {case_id}")
    for field, value in append_values.items():
        current = candidate.get(field)
        if isinstance(current, str) and isinstance(value, str):
            candidate[field] = current + value
        elif isinstance(current, list) and isinstance(value, list):
            current.extend(value)
        else:
            raise ValueError(f"Incompatible append mutation for {field} in {case_id}")


def _required_string(value: Mapping[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Coach eval case requires a non-empty {field}")
    return result
