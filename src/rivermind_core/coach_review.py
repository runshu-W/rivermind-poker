from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Mapping, Sequence

from rivermind_core.coach import CoachItem


COACH_EXPERT_REVIEW_SCHEMA_VERSION = "coach-expert-review/1.0.0"
COACH_EXPERT_REVIEW_PROTOCOL_VERSION = "coach-expert-quality-gate/1.0.0"
REVIEWER_HASH_PATTERN = re.compile(r"^[a-f0-9]{16,64}$")
EVIDENCE_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
VARIANT_ID_PATTERN = re.compile(r"^[A-Z]$")
FATAL_ERROR_CODES = frozenset(
    {
        "fabricated_number",
        "incorrect_fact_binding",
        "unsupported_strategy_claim",
        "privacy_leak",
        "direct_action",
        "material_omission",
    }
)
RATING_DIMENSIONS = (
    "faithfulness",
    "teaching_value",
    "clarity",
    "uncertainty_quality",
)


@dataclass(frozen=True, slots=True)
class ExpertReviewThresholds:
    min_cases: int = 50
    min_unique_evidence_cases: int = 50
    min_reviews_per_case: int = 2
    min_unique_reviewers: int = 2
    min_faithfulness_mean: float = 4.8
    min_teaching_value_mean: float = 4.0
    min_clarity_mean: float = 4.0
    min_uncertainty_quality_mean: float = 4.5
    max_fatal_errors: int = 0

    def __post_init__(self) -> None:
        if self.min_cases <= 0:
            raise ValueError("min_cases must be positive")
        if self.min_unique_evidence_cases <= 0:
            raise ValueError("min_unique_evidence_cases must be positive")
        if self.min_reviews_per_case <= 0:
            raise ValueError("min_reviews_per_case must be positive")
        if self.min_unique_reviewers <= 0:
            raise ValueError("min_unique_reviewers must be positive")
        means = (
            self.min_faithfulness_mean,
            self.min_teaching_value_mean,
            self.min_clarity_mean,
            self.min_uncertainty_quality_mean,
        )
        if any(not 1 <= item <= 5 for item in means):
            raise ValueError("review mean thresholds must be between one and five")
        if self.max_fatal_errors < 0:
            raise ValueError("max_fatal_errors cannot be negative")


@dataclass(frozen=True, slots=True)
class ExpertRating:
    reviewer_id_hash: str
    expertise_attested: bool
    faithfulness: int
    teaching_value: int
    clarity: int
    uncertainty_quality: int
    fatal_error_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExpertReviewCase:
    case_id: str
    evidence_hash: str
    variant_id: str
    ratings: tuple[ExpertRating, ...]


@dataclass(frozen=True, slots=True)
class ExpertReviewReport:
    thresholds: ExpertReviewThresholds
    cases: int
    unique_evidence_cases: int
    ratings: int
    unique_reviewers: int
    fully_reviewed_cases: int
    unattested_ratings: int
    fatal_errors: int
    dimension_means: Mapping[str, float]
    passed: bool
    failure_reasons: tuple[str, ...]


def build_blind_review_case(
    item: CoachItem,
    *,
    case_id: str,
    variant_id: str,
) -> dict[str, object]:
    """Export explanation text without player, provider, model, or source labels."""
    if not case_id.strip():
        raise ValueError("case_id cannot be empty")
    if not VARIANT_ID_PATTERN.fullmatch(variant_id):
        raise ValueError("variant_id must be one uppercase blind label")
    explanation = item.explanation
    return {
        "case_id": case_id,
        "evidence_hash": item.evidence.evidence_hash,
        "variant_id": variant_id,
        "content": {
            "headline": explanation.headline,
            "observation": explanation.observation,
            "teaching_point": explanation.teaching_point,
            "review_plan": list(explanation.review_plan),
            "uncertainty": explanation.uncertainty,
        },
        "ratings": [],
    }


def build_expert_review_document(
    cases: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": COACH_EXPERT_REVIEW_SCHEMA_VERSION,
        "blinding": {
            "provider_hidden": True,
            "model_hidden": True,
            "source_hidden": True,
        },
        "cases": [dict(item) for item in cases],
    }


def load_and_score_expert_reviews(
    path: Path,
    *,
    thresholds: ExpertReviewThresholds = ExpertReviewThresholds(),
) -> ExpertReviewReport:
    document = json.loads(path.read_text(encoding="utf-8"))
    return score_expert_review_document(document, thresholds=thresholds)


def score_expert_review_document(
    document: object,
    *,
    thresholds: ExpertReviewThresholds = ExpertReviewThresholds(),
) -> ExpertReviewReport:
    cases = _parse_review_document(document)
    ratings = tuple(rating for case in cases for rating in case.ratings)
    reviewers = {item.reviewer_id_hash for item in ratings}
    unique_evidence = {item.evidence_hash for item in cases}
    fully_reviewed = sum(
        len(case.ratings) >= thresholds.min_reviews_per_case for case in cases
    )
    unattested = sum(not item.expertise_attested for item in ratings)
    fatal_errors = sum(len(item.fatal_error_codes) for item in ratings)
    means = {
        dimension: (
            round(fmean(getattr(item, dimension) for item in ratings), 3)
            if ratings
            else 0.0
        )
        for dimension in RATING_DIMENSIONS
    }

    failure_reasons: list[str] = []
    if len(cases) < thresholds.min_cases:
        failure_reasons.append("insufficient_cases")
    if len(unique_evidence) < thresholds.min_unique_evidence_cases:
        failure_reasons.append("insufficient_unique_evidence")
    if fully_reviewed != len(cases):
        failure_reasons.append("insufficient_reviews_per_case")
    if len(reviewers) < thresholds.min_unique_reviewers:
        failure_reasons.append("insufficient_unique_reviewers")
    if unattested:
        failure_reasons.append("unattested_expertise")
    if fatal_errors > thresholds.max_fatal_errors:
        failure_reasons.append("fatal_errors_present")
    dimension_thresholds = {
        "faithfulness": thresholds.min_faithfulness_mean,
        "teaching_value": thresholds.min_teaching_value_mean,
        "clarity": thresholds.min_clarity_mean,
        "uncertainty_quality": thresholds.min_uncertainty_quality_mean,
    }
    for dimension, minimum in dimension_thresholds.items():
        if means[dimension] < minimum:
            failure_reasons.append(f"{dimension}_below_threshold")

    return ExpertReviewReport(
        thresholds=thresholds,
        cases=len(cases),
        unique_evidence_cases=len(unique_evidence),
        ratings=len(ratings),
        unique_reviewers=len(reviewers),
        fully_reviewed_cases=fully_reviewed,
        unattested_ratings=unattested,
        fatal_errors=fatal_errors,
        dimension_means=means,
        passed=not failure_reasons,
        failure_reasons=tuple(failure_reasons),
    )


def expert_review_report_to_dict(report: ExpertReviewReport) -> dict[str, object]:
    return {
        "protocol": {
            "version": COACH_EXPERT_REVIEW_PROTOCOL_VERSION,
            "review_schema": COACH_EXPERT_REVIEW_SCHEMA_VERSION,
            "thresholds": {
                "min_cases": report.thresholds.min_cases,
                "min_unique_evidence_cases": (
                    report.thresholds.min_unique_evidence_cases
                ),
                "min_reviews_per_case": report.thresholds.min_reviews_per_case,
                "min_unique_reviewers": report.thresholds.min_unique_reviewers,
                "min_faithfulness_mean": (
                    report.thresholds.min_faithfulness_mean
                ),
                "min_teaching_value_mean": (
                    report.thresholds.min_teaching_value_mean
                ),
                "min_clarity_mean": report.thresholds.min_clarity_mean,
                "min_uncertainty_quality_mean": (
                    report.thresholds.min_uncertainty_quality_mean
                ),
                "max_fatal_errors": report.thresholds.max_fatal_errors,
            },
        },
        "summary": {
            "cases": report.cases,
            "unique_evidence_cases": report.unique_evidence_cases,
            "ratings": report.ratings,
            "unique_reviewers": report.unique_reviewers,
            "fully_reviewed_cases": report.fully_reviewed_cases,
            "unattested_ratings": report.unattested_ratings,
            "fatal_errors": report.fatal_errors,
            "dimension_means": dict(report.dimension_means),
            "passed": report.passed,
            "failure_reasons": list(report.failure_reasons),
        }
    }


def _parse_review_document(document: object) -> tuple[ExpertReviewCase, ...]:
    if not isinstance(document, dict):
        raise ValueError("Expert review document must be an object")
    _require_exact_keys(document, {"schema_version", "blinding", "cases"}, "document")
    if document.get("schema_version") != COACH_EXPERT_REVIEW_SCHEMA_VERSION:
        raise ValueError("Unsupported expert review schema version")
    blinding = document.get("blinding")
    if not isinstance(blinding, dict) or any(
        blinding.get(field) is not True
        for field in ("provider_hidden", "model_hidden", "source_hidden")
    ):
        raise ValueError("Expert review document must attest all blinding fields")
    _require_exact_keys(
        blinding,
        {"provider_hidden", "model_hidden", "source_hidden"},
        "blinding",
    )
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("Expert review cases must be an array")
    cases = tuple(_parse_case(item) for item in raw_cases)
    case_ids = [item.case_id for item in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Expert review case ids must be unique")
    return cases


def _parse_case(value: object) -> ExpertReviewCase:
    if not isinstance(value, dict):
        raise ValueError("Expert review case must be an object")
    _require_exact_keys(
        value,
        {"case_id", "evidence_hash", "variant_id", "content", "ratings"},
        "case",
    )
    case_id = _nonempty_string(value, "case_id")
    evidence_hash = _nonempty_string(value, "evidence_hash")
    variant_id = _nonempty_string(value, "variant_id")
    if not EVIDENCE_HASH_PATTERN.fullmatch(evidence_hash):
        raise ValueError(f"Invalid evidence_hash in {case_id}")
    if not VARIANT_ID_PATTERN.fullmatch(variant_id):
        raise ValueError(f"Invalid blind variant_id in {case_id}")
    _validate_content(value.get("content"), case_id)
    raw_ratings = value.get("ratings")
    if not isinstance(raw_ratings, list):
        raise ValueError(f"Ratings must be an array in {case_id}")
    ratings = tuple(_parse_rating(item, case_id) for item in raw_ratings)
    reviewer_ids = [item.reviewer_id_hash for item in ratings]
    if len(reviewer_ids) != len(set(reviewer_ids)):
        raise ValueError(f"Reviewers must be unique within {case_id}")
    return ExpertReviewCase(case_id, evidence_hash, variant_id, ratings)


def _parse_rating(value: object, case_id: str) -> ExpertRating:
    if not isinstance(value, dict):
        raise ValueError(f"Rating must be an object in {case_id}")
    _require_exact_keys(
        value,
        {
            "reviewer_id_hash",
            "expertise_attested",
            "faithfulness",
            "teaching_value",
            "clarity",
            "uncertainty_quality",
            "fatal_error_codes",
        },
        f"rating in {case_id}",
    )
    reviewer_id = _nonempty_string(value, "reviewer_id_hash")
    if not REVIEWER_HASH_PATTERN.fullmatch(reviewer_id):
        raise ValueError(f"Reviewer id must be a lowercase hex hash in {case_id}")
    expertise_attested = value.get("expertise_attested")
    if not isinstance(expertise_attested, bool):
        raise ValueError(f"expertise_attested must be boolean in {case_id}")
    dimension_values: dict[str, int] = {}
    for dimension in RATING_DIMENSIONS:
        rating = value.get(dimension)
        if type(rating) is not int or not 1 <= rating <= 5:
            raise ValueError(f"{dimension} must be an integer from one to five")
        dimension_values[dimension] = rating
    raw_fatal = value.get("fatal_error_codes")
    if not isinstance(raw_fatal, list) or not all(
        isinstance(item, str) for item in raw_fatal
    ):
        raise ValueError(f"fatal_error_codes must be a string array in {case_id}")
    fatal_codes = tuple(raw_fatal)
    if len(fatal_codes) != len(set(fatal_codes)):
        raise ValueError(f"fatal_error_codes must be unique in {case_id}")
    unknown_codes = set(fatal_codes) - FATAL_ERROR_CODES
    if unknown_codes:
        raise ValueError(f"Unknown fatal error codes in {case_id}")
    return ExpertRating(
        reviewer_id_hash=reviewer_id,
        expertise_attested=expertise_attested,
        faithfulness=dimension_values["faithfulness"],
        teaching_value=dimension_values["teaching_value"],
        clarity=dimension_values["clarity"],
        uncertainty_quality=dimension_values["uncertainty_quality"],
        fatal_error_codes=fatal_codes,
    )


def _validate_content(value: object, case_id: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"Review content must be an object in {case_id}")
    _require_exact_keys(
        value,
        {
            "headline",
            "observation",
            "teaching_point",
            "review_plan",
            "uncertainty",
        },
        f"content in {case_id}",
    )
    for field in (
        "headline",
        "observation",
        "teaching_point",
        "uncertainty",
    ):
        _nonempty_string(value, field)
    review_plan = value.get("review_plan")
    if not isinstance(review_plan, list) or not review_plan or not all(
        isinstance(item, str) and item.strip() for item in review_plan
    ):
        raise ValueError(f"review_plan must be a non-empty string array in {case_id}")


def _nonempty_string(value: Mapping[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return result


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    context: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"Unexpected or missing fields in expert review {context}")
