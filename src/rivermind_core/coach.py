from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence

from rivermind_core.leaks import LeakCard, LeakDirection, LeakReport, LeakSeverity


COACH_EVIDENCE_SCHEMA_VERSION = "explanation-evidence/1.0.0"
COACH_CANDIDATE_SCHEMA_VERSION = "coach-candidate/1.0.0"
COACH_TEMPLATE_VERSION = "zh-CN/1.0.0"


METRIC_LABELS = {
    "vpip": "VPIP",
    "pfr": "PFR",
    "rfi": "RFI",
    "three_bet": "3Bet",
    "call_open": "Call Open",
    "cold_call": "Cold Call",
    "fold_to_three_bet": "Fold to 3Bet",
    "flop_cbet": "Flop CBet",
    "fold_to_flop_cbet": "Fold to Flop CBet",
}


class CoachSource(StrEnum):
    TEMPLATE = "template"
    LLM_VALIDATED = "llm_validated"
    TEMPLATE_FALLBACK = "template_fallback"


class ValidationStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class CandidateValidation:
    status: ValidationStatus
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status == ValidationStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    fact_id: str
    label: str
    value: str | int | bool
    display_value: str


@dataclass(frozen=True, slots=True)
class ExplanationEvidence:
    source_fingerprint: str
    rule_id: str
    rule_version: str
    subject_label: str
    facts: tuple[EvidenceFact, ...]
    hand_fact_ids: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    constraints: tuple[str, ...]

    @property
    def evidence_id(self) -> str:
        return f"evidence:{self.source_fingerprint[:16]}"

    @property
    def fact_map(self) -> dict[str, EvidenceFact]:
        return {item.fact_id: item for item in self.facts}

    @property
    def evidence_hash(self) -> str:
        return _sha256_json(self.model_payload(include_hash=False))

    def model_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": COACH_EVIDENCE_SCHEMA_VERSION,
            "candidate_schema_version": COACH_CANDIDATE_SCHEMA_VERSION,
            "evidence_id": self.evidence_id,
            "subject": self.subject_label,
            "rule": {
                "id": self.rule_id,
                "version": self.rule_version,
            },
            "facts": [
                {
                    "fact_id": item.fact_id,
                    "label": item.label,
                    "value": item.value,
                    "display_value": item.display_value,
                }
                for item in self.facts
            ],
            "anonymous_hand_refs": list(self.hand_fact_ids),
            "missing_evidence": list(self.missing_evidence),
            "constraints": list(self.constraints),
        }
        if include_hash:
            payload["evidence_hash"] = self.evidence_hash
        return payload


@dataclass(frozen=True, slots=True)
class CoachCandidate:
    schema_version: str
    evidence_id: str
    evidence_hash: str
    headline: str
    observation: str
    teaching_point: str
    review_plan: tuple[str, ...]
    uncertainty: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoachExplanation:
    rule_id: str
    player_name: str
    evidence_id: str
    evidence_hash: str
    source: CoachSource
    template_version: str
    headline: str
    observation: str
    teaching_point: str
    review_plan: tuple[str, ...]
    uncertainty: str
    evidence_hand_refs: tuple[str, ...]
    validation: CandidateValidation


@dataclass(frozen=True, slots=True)
class CoachItem:
    evidence: ExplanationEvidence
    explanation: CoachExplanation


@dataclass(frozen=True, slots=True)
class CoachReport:
    items: tuple[CoachItem, ...]

    @property
    def explanation_count(self) -> int:
        return len(self.items)

    @property
    def fallback_count(self) -> int:
        return sum(
            item.explanation.source == CoachSource.TEMPLATE_FALLBACK
            for item in self.items
        )


PLACEHOLDER_PATTERN = re.compile(r"\{\{([a-z0-9_.-]+)\}\}")
ASCII_NUMBER_PATTERN = re.compile(r"\d")
CHINESE_QUANTITY_PATTERN = re.compile(
    r"(?:百分之[零〇一二三四五六七八九十百千万亿两]+|一半|半数|"
    r"[零〇一二三四五六七八九十百千万亿两]+"
    r"(?:%|％|次|手|个|张|条|项|点|成|分|倍))"
)
ENGLISH_NUMBER_PATTERN = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|thousand|million|billion|half|quarter|"
    r"double|twice)\b",
    re.IGNORECASE,
)
UNSUPPORTED_CLAIM_TERMS = (
    "GTO",
    "EV",
    "均衡频率",
    "最优频率",
    "范围优势",
    "坚果优势",
    "对手底牌",
    "底牌是",
    "翻牌是",
    "转牌是",
    "河牌是",
    "未来公共牌",
    "必然盈利",
)
DIRECT_ACTION_PATTERN = re.compile(
    r"(?:应该|必须|务必|总是|每次都|直接|立即).{0,12}"
    r"(?:弃牌|跟注|加注|下注|全下|fold|call|raise|bet|all.?in)",
    re.IGNORECASE,
)
REQUIRED_CANDIDATE_KEYS = frozenset(
    {
        "schema_version",
        "evidence_id",
        "evidence_hash",
        "headline",
        "observation",
        "teaching_point",
        "review_plan",
        "uncertainty",
        "evidence_refs",
    }
)
REQUIRED_FACT_REFS = frozenset(
    {
        "signal.title",
        "signal.observed_percentage",
        "signal.sample",
        "signal.confidence_interval_95",
        "rule.trigger",
        "rule.rationale",
        "rule.review_prompt",
        "limitations.context",
    }
)


def build_explanation_evidence(card: LeakCard) -> ExplanationEvidence:
    assessment = card.assessment
    assert assessment.severity is not None
    assert assessment.observed_percentage is not None
    assert assessment.confidence_low is not None
    assert assessment.confidence_high is not None
    direction = "≥" if assessment.direction == LeakDirection.ABOVE else "≤"
    severity = "优先复盘" if assessment.severity == LeakSeverity.PRIORITY else "建议复盘"
    metric_label = METRIC_LABELS[assessment.metric.value]
    hand_facts = tuple(
        EvidenceFact(
            fact_id=f"hand.{index}",
            label=f"匿名证据手牌 {index}",
            value=f"{hand.stats.position.value}|{hand.stats.effective_stack_bb}|{hand.net_result_bb}",
            display_value=(
                f"证据手牌 {index}（{hand.stats.position.value}，"
                f"{hand.stats.effective_stack_bb} BB，净结果 {hand.net_result_bb} BB）"
            ),
        )
        for index, hand in enumerate(card.evidence_hands, start=1)
    )
    facts = (
        EvidenceFact("signal.title", "复盘信号", assessment.title, assessment.title),
        EvidenceFact("signal.metric", "统计指标", assessment.metric.value, metric_label),
        EvidenceFact(
            "signal.observed_percentage",
            "观察比例",
            f"{assessment.observed_percentage:.6f}",
            f"{assessment.observed_percentage:.1f}%",
        ),
        EvidenceFact(
            "signal.sample",
            "发生数/机会数",
            f"{assessment.occurrences}/{assessment.opportunities}",
            f"{assessment.occurrences}/{assessment.opportunities}",
        ),
        EvidenceFact(
            "signal.confidence_interval_95",
            "95% Wilson 区间",
            f"{assessment.confidence_low:.6f}/{assessment.confidence_high:.6f}",
            f"{assessment.confidence_low:.1f}%–{assessment.confidence_high:.1f}%",
        ),
        EvidenceFact(
            "rule.trigger",
            "保守触发阈值",
            f"{assessment.direction.value}:{assessment.trigger_percentage:.6f}",
            f"{direction}{assessment.trigger_percentage:g}%",
        ),
        EvidenceFact("rule.severity", "复盘优先级", assessment.severity.value, severity),
        EvidenceFact("rule.rationale", "规则依据", assessment.rationale, assessment.rationale),
        EvidenceFact(
            "rule.review_prompt",
            "复盘问题",
            assessment.review_prompt,
            assessment.review_prompt,
        ),
        EvidenceFact(
            "evidence.count",
            "证据手牌数量",
            len(card.evidence_hands),
            f"{len(card.evidence_hands)} 手",
        ),
        EvidenceFact(
            "limitations.context",
            "适用边界",
            "review_signal_not_gto",
            "这是玩家级复盘筛选信号，不是具体节点的 GTO 定论。",
        ),
        EvidenceFact(
            "limitations.no_gto",
            "缺失的 GTO 证据",
            "missing",
            "当前证据不包含策略频率、范围或求解器结果。",
        ),
        EvidenceFact(
            "limitations.no_ev",
            "缺失的 EV 证据",
            "missing",
            "当前证据不包含动作 EV 或 EV 损失。",
        ),
        *hand_facts,
    )
    source_payload = {
        "rule_id": assessment.rule_id,
        "rule_version": assessment.profile_version,
        "player_name": assessment.player_name,
        "statistics": {
            "occurrences": assessment.occurrences,
            "opportunities": assessment.opportunities,
            "confidence_low": assessment.confidence_low,
            "confidence_high": assessment.confidence_high,
            "trigger": assessment.trigger_percentage,
            "severity": assessment.severity.value,
        },
        "hands": [
            {
                "site": hand.stats.site,
                "hand_id": hand.stats.hand_id,
                "player": hand.stats.player_name,
                "table": hand.table_name,
            }
            for hand in card.evidence_hands
        ],
    }
    return ExplanationEvidence(
        source_fingerprint=_sha256_json(source_payload),
        rule_id=assessment.rule_id,
        rule_version=assessment.profile_version,
        subject_label=(
            "Hero"
            if card.evidence_hands
            and all(hand.stats.is_hero for hand in card.evidence_hands)
            else "Player"
        ),
        facts=facts,
        hand_fact_ids=tuple(item.fact_id for item in hand_facts),
        missing_evidence=(
            "gto_strategy_frequencies",
            "action_ev_and_ev_loss",
            "range_and_board_analysis",
            "opponent_model",
        ),
        constraints=(
            "Return only the candidate JSON schema; do not add fields.",
            "Reference every numeric value with a {{fact_id}} placeholder.",
            "Do not include raw numeric literals in narrative fields.",
            "Do not infer GTO strategy, EV, ranges, private cards, or future cards.",
            "Treat the signal as an offline review heuristic, not a live action recommendation.",
        ),
    )


def explain_leak_card(
    card: LeakCard,
    candidate_payload: Mapping[str, object] | None = None,
) -> CoachItem:
    evidence = build_explanation_evidence(card)
    if candidate_payload is None:
        explanation = _template_explanation(
            card,
            evidence,
            source=CoachSource.TEMPLATE,
            validation=CandidateValidation(ValidationStatus.NOT_REQUESTED),
        )
        return CoachItem(evidence, explanation)

    candidate, validation = validate_coach_candidate(
        candidate_payload,
        evidence,
        sensitive_terms=_sensitive_terms(card),
    )
    if candidate is None or not validation.accepted:
        explanation = _template_explanation(
            card,
            evidence,
            source=CoachSource.TEMPLATE_FALLBACK,
            validation=validation,
        )
        return CoachItem(evidence, explanation)
    explanation = _candidate_explanation(card, evidence, candidate, validation)
    return CoachItem(evidence, explanation)


def build_candidate_skeleton(evidence: ExplanationEvidence) -> dict[str, object]:
    """Return the smallest valid, evidence-bound candidate for an evidence packet."""
    review_plan = ["{{rule.review_prompt}}"]
    evidence_refs = [
        "signal.title",
        "signal.observed_percentage",
        "signal.sample",
        "signal.confidence_interval_95",
        "rule.trigger",
        "rule.rationale",
        "limitations.context",
        "rule.review_prompt",
        "limitations.no_gto",
    ]
    if evidence.hand_fact_ids:
        review_plan.append(f"先看{{{{{evidence.hand_fact_ids[0]}}}}}。")
        evidence_refs.append(evidence.hand_fact_ids[0])
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
        "review_plan": review_plan,
        "uncertainty": "{{limitations.no_gto}}",
        "evidence_refs": evidence_refs,
    }


def fallback_coach_item(
    card: LeakCard,
    issues: Sequence[ValidationIssue],
) -> CoachItem:
    """Build a deterministic fallback without accepting provider-authored text."""
    evidence = build_explanation_evidence(card)
    explanation = _template_explanation(
        card,
        evidence,
        source=CoachSource.TEMPLATE_FALLBACK,
        validation=CandidateValidation(ValidationStatus.REJECTED, tuple(issues)),
    )
    return CoachItem(evidence, explanation)


def explain_leak_report(
    report: LeakReport,
    *,
    candidates: Mapping[str, Mapping[str, object]] | None = None,
) -> CoachReport:
    candidate_map = candidates or {}
    items = []
    for card in report.cards:
        evidence = build_explanation_evidence(card)
        items.append(explain_leak_card(card, candidate_map.get(evidence.evidence_id)))
    return CoachReport(tuple(items))


def validate_coach_candidate(
    payload: Mapping[str, object],
    evidence: ExplanationEvidence,
    *,
    sensitive_terms: Sequence[str] = (),
) -> tuple[CoachCandidate | None, CandidateValidation]:
    issues: list[ValidationIssue] = []
    keys = frozenset(payload)
    for key in sorted(REQUIRED_CANDIDATE_KEYS - keys):
        issues.append(ValidationIssue("missing_field", key, "Required field is missing"))
    for key in sorted(keys - REQUIRED_CANDIDATE_KEYS):
        issues.append(ValidationIssue("unknown_field", key, "Field is not allowed"))

    scalar_fields = (
        "schema_version",
        "evidence_id",
        "evidence_hash",
        "headline",
        "observation",
        "teaching_point",
        "uncertainty",
    )
    scalar_values: dict[str, str] = {}
    for field in scalar_fields:
        value = payload.get(field)
        if not isinstance(value, str):
            issues.append(ValidationIssue("invalid_type", field, "Expected a string"))
        else:
            scalar_values[field] = value

    review_plan = _string_list(payload.get("review_plan"), "review_plan", issues)
    evidence_refs = _string_list(payload.get("evidence_refs"), "evidence_refs", issues)
    if any(issue.code in {"missing_field", "invalid_type"} for issue in issues):
        return None, CandidateValidation(ValidationStatus.REJECTED, tuple(issues))

    candidate = CoachCandidate(
        schema_version=scalar_values["schema_version"],
        evidence_id=scalar_values["evidence_id"],
        evidence_hash=scalar_values["evidence_hash"],
        headline=scalar_values["headline"],
        observation=scalar_values["observation"],
        teaching_point=scalar_values["teaching_point"],
        review_plan=tuple(review_plan),
        uncertainty=scalar_values["uncertainty"],
        evidence_refs=tuple(evidence_refs),
    )
    _validate_candidate_content(candidate, evidence, sensitive_terms, issues)
    status = ValidationStatus.ACCEPTED if not issues else ValidationStatus.REJECTED
    return candidate, CandidateValidation(status, tuple(issues))


def coach_report_to_dict(report: CoachReport) -> dict[str, object]:
    return {
        "protocol": {
            "evidence_schema": COACH_EVIDENCE_SCHEMA_VERSION,
            "candidate_schema": COACH_CANDIDATE_SCHEMA_VERSION,
            "template_version": COACH_TEMPLATE_VERSION,
        },
        "summary": {
            "explanations": report.explanation_count,
            "fallbacks": report.fallback_count,
        },
        "items": [
            {
                "model_input": item.evidence.model_payload(),
                "explanation": _explanation_to_dict(item.explanation),
            }
            for item in report.items
        ],
    }


def _validate_candidate_content(
    candidate: CoachCandidate,
    evidence: ExplanationEvidence,
    sensitive_terms: Sequence[str],
    issues: list[ValidationIssue],
) -> None:
    if candidate.schema_version != COACH_CANDIDATE_SCHEMA_VERSION:
        issues.append(ValidationIssue("schema_mismatch", "schema_version", "Unsupported candidate schema"))
    if candidate.evidence_id != evidence.evidence_id:
        issues.append(ValidationIssue("evidence_id_mismatch", "evidence_id", "Candidate targets different evidence"))
    if candidate.evidence_hash != evidence.evidence_hash:
        issues.append(ValidationIssue("evidence_hash_mismatch", "evidence_hash", "Evidence changed after generation"))
    if not 1 <= len(candidate.review_plan) <= 4:
        issues.append(ValidationIssue("invalid_length", "review_plan", "Use between one and four review steps"))
    if len(candidate.evidence_refs) != len(set(candidate.evidence_refs)):
        issues.append(ValidationIssue("duplicate_reference", "evidence_refs", "Evidence references must be unique"))

    text_fields = {
        "headline": candidate.headline,
        "observation": candidate.observation,
        "teaching_point": candidate.teaching_point,
        "uncertainty": candidate.uncertainty,
        **{f"review_plan[{index}]": value for index, value in enumerate(candidate.review_plan)},
    }
    fact_map = evidence.fact_map
    used_refs: set[str] = set()
    for field, value in text_fields.items():
        if not value.strip():
            issues.append(ValidationIssue("empty_text", field, "Narrative field cannot be empty"))
            continue
        limit = 120 if field == "headline" else 800
        if len(value) > limit:
            issues.append(ValidationIssue("text_too_long", field, f"Text exceeds {limit} characters"))
        refs = set(PLACEHOLDER_PATTERN.findall(value))
        used_refs.update(refs)
        for reference in sorted(refs - fact_map.keys()):
            issues.append(ValidationIssue("unknown_fact", field, f"Unknown fact reference: {reference}"))
        without_placeholders = PLACEHOLDER_PATTERN.sub("", value)
        if _contains_unreferenced_number(without_placeholders):
            issues.append(ValidationIssue("unreferenced_number", field, "Numeric claims require fact placeholders"))
        lowered = without_placeholders.casefold()
        for term in UNSUPPORTED_CLAIM_TERMS:
            if term.casefold() in lowered:
                issues.append(ValidationIssue("unsupported_claim", field, f"Unsupported claim term: {term}"))
        if DIRECT_ACTION_PATTERN.search(without_placeholders):
            issues.append(ValidationIssue("action_directive", field, "Direct action instructions are not allowed"))
        for term in sensitive_terms:
            if len(term) >= 3 and term.casefold() in lowered:
                issues.append(ValidationIssue("sensitive_data", field, "Narrative contains redacted source data"))

    declared_refs = set(candidate.evidence_refs)
    for reference in sorted(declared_refs - fact_map.keys()):
        issues.append(ValidationIssue("unknown_fact", "evidence_refs", f"Unknown fact reference: {reference}"))
    if declared_refs != used_refs:
        issues.append(ValidationIssue("reference_mismatch", "evidence_refs", "Declared references must exactly match placeholders"))
    for reference in sorted(REQUIRED_FACT_REFS - used_refs):
        issues.append(ValidationIssue("missing_required_fact", "evidence_refs", f"Required fact was not used: {reference}"))
    if evidence.hand_fact_ids and not used_refs.intersection(evidence.hand_fact_ids):
        issues.append(ValidationIssue("missing_hand_evidence", "evidence_refs", "At least one anonymous hand reference is required"))


def _template_explanation(
    card: LeakCard,
    evidence: ExplanationEvidence,
    *,
    source: CoachSource,
    validation: CandidateValidation,
) -> CoachExplanation:
    assessment = card.assessment
    assert assessment.severity is not None
    assert assessment.observed_percentage is not None
    assert assessment.confidence_low is not None
    assert assessment.confidence_high is not None
    direction = "≥" if assessment.direction == LeakDirection.ABOVE else "≤"
    severity = "优先复盘" if assessment.severity == LeakSeverity.PRIORITY else "建议复盘"
    metric_label = METRIC_LABELS[assessment.metric.value]
    return CoachExplanation(
        rule_id=assessment.rule_id,
        player_name=assessment.player_name,
        evidence_id=evidence.evidence_id,
        evidence_hash=evidence.evidence_hash,
        source=source,
        template_version=COACH_TEMPLATE_VERSION,
        headline=f"{assessment.title} · {severity}",
        observation=(
            f"{metric_label} 为 {assessment.observed_percentage:.1f}% "
            f"（{assessment.occurrences}/{assessment.opportunities}），95% Wilson 区间为 "
            f"{assessment.confidence_low:.1f}%–{assessment.confidence_high:.1f}%，"
            f"整体越过复盘阈值 {direction}{assessment.trigger_percentage:g}%。"
        ),
        teaching_point=(
            f"{assessment.rationale} 这是一条玩家级筛选信号，仍需结合位置、"
            "有效筹码、人数和牌面逐手判断。"
        ),
        review_plan=(
            assessment.review_prompt,
            f"从 {len(card.evidence_hands)} 手匿名证据中按位置和有效筹码分组，记录重复出现的决策环境。",
        ),
        uncertainty=(
            "当前证据不包含 GTO 策略频率、范围或动作 EV，因此不能据此断言某一手的最优行动。"
        ),
        evidence_hand_refs=evidence.hand_fact_ids,
        validation=validation,
    )


def _candidate_explanation(
    card: LeakCard,
    evidence: ExplanationEvidence,
    candidate: CoachCandidate,
    validation: CandidateValidation,
) -> CoachExplanation:
    fact_map = evidence.fact_map
    render = lambda value: PLACEHOLDER_PATTERN.sub(  # noqa: E731
        lambda match: fact_map[match.group(1)].display_value,
        value,
    )
    return CoachExplanation(
        rule_id=card.assessment.rule_id,
        player_name=card.assessment.player_name,
        evidence_id=evidence.evidence_id,
        evidence_hash=evidence.evidence_hash,
        source=CoachSource.LLM_VALIDATED,
        template_version=COACH_TEMPLATE_VERSION,
        headline=render(candidate.headline),
        observation=render(candidate.observation),
        teaching_point=render(candidate.teaching_point),
        review_plan=tuple(render(item) for item in candidate.review_plan),
        uncertainty=render(candidate.uncertainty),
        evidence_hand_refs=tuple(
            item for item in evidence.hand_fact_ids if item in candidate.evidence_refs
        ),
        validation=validation,
    )


def _explanation_to_dict(explanation: CoachExplanation) -> dict[str, object]:
    return {
        "rule_id": explanation.rule_id,
        "player": explanation.player_name,
        "evidence_id": explanation.evidence_id,
        "evidence_hash": explanation.evidence_hash,
        "source": explanation.source.value,
        "template_version": explanation.template_version,
        "headline": explanation.headline,
        "observation": explanation.observation,
        "teaching_point": explanation.teaching_point,
        "review_plan": list(explanation.review_plan),
        "uncertainty": explanation.uncertainty,
        "evidence_hand_refs": list(explanation.evidence_hand_refs),
        "validation": {
            "status": explanation.validation.status.value,
            "issues": [
                {
                    "code": issue.code,
                    "field": issue.field,
                    "message": issue.message,
                }
                for issue in explanation.validation.issues
            ],
        },
    }


def _string_list(
    value: object,
    field: str,
    issues: list[ValidationIssue],
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        issues.append(ValidationIssue("invalid_type", field, "Expected an array of strings"))
        return []
    return value


def _contains_unreferenced_number(value: str) -> bool:
    return bool(
        ASCII_NUMBER_PATTERN.search(value)
        or CHINESE_QUANTITY_PATTERN.search(value)
        or ENGLISH_NUMBER_PATTERN.search(value)
    )


def _sensitive_terms(card: LeakCard) -> tuple[str, ...]:
    terms = {
        card.assessment.player_name,
        *(hand.stats.site for hand in card.evidence_hands),
        *(hand.stats.hand_id for hand in card.evidence_hands),
        *(hand.table_name for hand in card.evidence_hands),
    }
    subject_label = (
        "Hero"
        if card.evidence_hands
        and all(hand.stats.is_hero for hand in card.evidence_hands)
        else "Player"
    )
    return tuple(sorted(term for term in terms if term and term != subject_label))


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
