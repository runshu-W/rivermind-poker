from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import sqrt
from typing import Sequence

from rivermind_core.reports import PlayerHandReport, StatMetric
from rivermind_core.stats import PlayerStats, StatValue


LEAK_PROFILE_ID = "broad-review-signals"
LEAK_PROFILE_VERSION = "0.1.0"
WILSON_Z_95 = 1.959963984540054


class LeakDirection(StrEnum):
    ABOVE = "above"
    BELOW = "below"


class LeakStatus(StrEnum):
    DETECTED = "detected"
    CLEAR = "clear"
    INSUFFICIENT_SAMPLE = "insufficient_sample"


class LeakSeverity(StrEnum):
    REVIEW = "review"
    PRIORITY = "priority"


@dataclass(frozen=True, slots=True)
class LeakRule:
    rule_id: str
    title: str
    metric: StatMetric
    direction: LeakDirection
    trigger_percentage: float
    priority_percentage: float
    min_opportunities: int
    evidence_occurred: bool
    rationale: str
    review_prompt: str

    def __post_init__(self) -> None:
        if not self.rule_id or not self.title:
            raise ValueError("Leak rules require an id and title")
        if not 0 <= self.trigger_percentage <= 100:
            raise ValueError("trigger_percentage must be between 0 and 100")
        if not 0 <= self.priority_percentage <= 100:
            raise ValueError("priority_percentage must be between 0 and 100")
        if self.min_opportunities <= 0:
            raise ValueError("min_opportunities must be positive")
        if (
            self.direction == LeakDirection.ABOVE
            and self.priority_percentage < self.trigger_percentage
        ):
            raise ValueError("An above rule priority threshold cannot be lower")
        if (
            self.direction == LeakDirection.BELOW
            and self.priority_percentage > self.trigger_percentage
        ):
            raise ValueError("A below rule priority threshold cannot be higher")


@dataclass(frozen=True, slots=True)
class LeakAssessment:
    rule_id: str
    profile_version: str
    player_name: str
    title: str
    metric: StatMetric
    direction: LeakDirection
    status: LeakStatus
    severity: LeakSeverity | None
    occurrences: int
    opportunities: int
    observed_percentage: float | None
    confidence_low: float | None
    confidence_high: float | None
    trigger_percentage: float
    priority_percentage: float
    min_opportunities: int
    evidence_occurred: bool
    rationale: str
    review_prompt: str

    @property
    def sample_shortfall(self) -> int:
        return max(0, self.min_opportunities - self.opportunities)


@dataclass(frozen=True, slots=True)
class LeakCard:
    assessment: LeakAssessment
    evidence_hands: tuple[PlayerHandReport, ...]

    def __post_init__(self) -> None:
        if self.assessment.status != LeakStatus.DETECTED:
            raise ValueError("Leak cards require a detected assessment")
        if self.assessment.severity is None:
            raise ValueError("Leak cards require a severity")
        if self.assessment.observed_percentage is None:
            raise ValueError("Leak cards require an observed percentage")
        if (
            self.assessment.confidence_low is None
            or self.assessment.confidence_high is None
        ):
            raise ValueError("Leak cards require a confidence interval")


@dataclass(frozen=True, slots=True)
class LeakReport:
    profile_id: str
    profile_version: str
    assessments: tuple[LeakAssessment, ...]
    cards: tuple[LeakCard, ...]

    @property
    def detected_count(self) -> int:
        return len(self.cards)

    @property
    def clear_count(self) -> int:
        return sum(item.status == LeakStatus.CLEAR for item in self.assessments)

    @property
    def insufficient_sample_count(self) -> int:
        return sum(
            item.status == LeakStatus.INSUFFICIENT_SAMPLE
            for item in self.assessments
        )


DEFAULT_LEAK_RULES = (
    LeakRule(
        rule_id="three_bet_underuse",
        title="3Bet 使用偏少",
        metric=StatMetric.THREE_BET,
        direction=LeakDirection.BELOW,
        trigger_percentage=4,
        priority_percentage=2,
        min_opportunities=40,
        evidence_occurred=False,
        rationale="面对一次加注时很少 3Bet，可能让跟注范围承受过多压力。",
        review_prompt="复查错过 3Bet 的位置、有效筹码和对手开池位置，区分合理跟注与过度被动。",
    ),
    LeakRule(
        rule_id="cold_call_overuse",
        title="非盲位 Cold Call 偏多",
        metric=StatMetric.COLD_CALL,
        direction=LeakDirection.ABOVE,
        trigger_percentage=25,
        priority_percentage=35,
        min_opportunities=30,
        evidence_occurred=True,
        rationale="非盲位面对开池时频繁跟注，可能形成封顶范围并增加多人底池比例。",
        review_prompt="检查这些跟注是否更适合 3Bet 或弃牌，并按位置与有效筹码分别复盘。",
    ),
    LeakRule(
        rule_id="fold_to_three_bet_overfold",
        title="面对 3Bet 弃牌偏多",
        metric=StatMetric.FOLD_TO_THREE_BET,
        direction=LeakDirection.ABOVE,
        trigger_percentage=70,
        priority_percentage=80,
        min_opportunities=25,
        evidence_occurred=True,
        rationale="开池后面对 3Bet 经常弃牌，可能让开池范围被对手低成本施压。",
        review_prompt="按开池位置、3Bet 位置和尺度检查弃牌，优先寻找可跟注或 4Bet 的边缘组合。",
    ),
    LeakRule(
        rule_id="flop_cbet_underuse",
        title="Flop CBet 使用偏少",
        metric=StatMetric.FLOP_CBET,
        direction=LeakDirection.BELOW,
        trigger_percentage=45,
        priority_percentage=30,
        min_opportunities=30,
        evidence_occurred=False,
        rationale="作为翻牌前最后进攻者时持续下注偏少，可能错过范围和牌面优势。",
        review_prompt="检查未 CBet 的牌面结构、人数和位置，寻找应使用小尺度高频下注的节点。",
    ),
    LeakRule(
        rule_id="flop_cbet_overuse",
        title="Flop CBet 使用偏多",
        metric=StatMetric.FLOP_CBET,
        direction=LeakDirection.ABOVE,
        trigger_percentage=80,
        priority_percentage=90,
        min_opportunities=30,
        evidence_occurred=True,
        rationale="作为翻牌前最后进攻者时几乎总是持续下注，可能在不利牌面缺少过牌保护。",
        review_prompt="检查高频 CBet 是否集中在多人底池、不利牌面或缺少后门权益的组合。",
    ),
    LeakRule(
        rule_id="fold_to_flop_cbet_overfold",
        title="面对 Flop CBet 弃牌偏多",
        metric=StatMetric.FOLD_TO_FLOP_CBET,
        direction=LeakDirection.ABOVE,
        trigger_percentage=65,
        priority_percentage=75,
        min_opportunities=30,
        evidence_occurred=True,
        rationale="面对翻牌持续下注经常弃牌，可能没有充分保护跟注到翻牌的范围。",
        review_prompt="检查弃牌组合的后门权益、下注尺度和牌面连接度，寻找可跟注或加注的候选。",
    ),
)


def evaluate_leaks(
    stats: Sequence[PlayerStats],
    *,
    rules: Sequence[LeakRule] = DEFAULT_LEAK_RULES,
) -> tuple[LeakAssessment, ...]:
    assessments = [
        _evaluate_rule(player_stats, rule)
        for player_stats in stats
        for rule in rules
    ]
    return tuple(
        sorted(
            assessments,
            key=lambda item: (item.player_name.casefold(), item.rule_id),
        )
    )


def build_leak_report(
    assessments: Sequence[LeakAssessment],
    cards: Sequence[LeakCard] = (),
) -> LeakReport:
    ordered_cards = tuple(
        sorted(
            cards,
            key=lambda item: (
                0 if item.assessment.severity == LeakSeverity.PRIORITY else 1,
                item.assessment.player_name.casefold(),
                item.assessment.rule_id,
            ),
        )
    )
    return LeakReport(
        profile_id=LEAK_PROFILE_ID,
        profile_version=LEAK_PROFILE_VERSION,
        assessments=tuple(assessments),
        cards=ordered_cards,
    )


def wilson_interval_95(value: StatValue) -> tuple[float, float] | None:
    if value.opportunities == 0:
        return None
    n = value.opportunities
    proportion = value.occurrences / n
    z_squared = WILSON_Z_95**2
    denominator = 1 + z_squared / n
    center = (proportion + z_squared / (2 * n)) / denominator
    margin = (
        WILSON_Z_95
        * sqrt(proportion * (1 - proportion) / n + z_squared / (4 * n**2))
        / denominator
    )
    return max(0.0, (center - margin) * 100), min(100.0, (center + margin) * 100)


def _evaluate_rule(stats: PlayerStats, rule: LeakRule) -> LeakAssessment:
    value: StatValue = getattr(stats, rule.metric.value)
    interval = wilson_interval_95(value)
    percentage = value.percentage
    status = LeakStatus.CLEAR
    severity: LeakSeverity | None = None

    if value.opportunities < rule.min_opportunities:
        status = LeakStatus.INSUFFICIENT_SAMPLE
    elif interval is not None:
        low, high = interval
        if rule.direction == LeakDirection.ABOVE and low >= rule.trigger_percentage:
            status = LeakStatus.DETECTED
            severity = (
                LeakSeverity.PRIORITY
                if low >= rule.priority_percentage
                else LeakSeverity.REVIEW
            )
        elif rule.direction == LeakDirection.BELOW and high <= rule.trigger_percentage:
            status = LeakStatus.DETECTED
            severity = (
                LeakSeverity.PRIORITY
                if high <= rule.priority_percentage
                else LeakSeverity.REVIEW
            )

    return LeakAssessment(
        rule_id=rule.rule_id,
        profile_version=LEAK_PROFILE_VERSION,
        player_name=stats.player_name,
        title=rule.title,
        metric=rule.metric,
        direction=rule.direction,
        status=status,
        severity=severity,
        occurrences=value.occurrences,
        opportunities=value.opportunities,
        observed_percentage=percentage,
        confidence_low=None if interval is None else interval[0],
        confidence_high=None if interval is None else interval[1],
        trigger_percentage=rule.trigger_percentage,
        priority_percentage=rule.priority_percentage,
        min_opportunities=rule.min_opportunities,
        evidence_occurred=rule.evidence_occurred,
        rationale=rule.rationale,
        review_prompt=rule.review_prompt,
    )
