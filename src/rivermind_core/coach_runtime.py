from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping, Protocol

from rivermind_core.coach import (
    CoachItem,
    CoachSource,
    ValidationIssue,
    build_explanation_evidence,
    explain_leak_card,
    fallback_coach_item,
)
from rivermind_core.leaks import LeakCard


class CoachRuntimeStatus(StrEnum):
    VALIDATED = "validated"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_REFUSAL = "provider_refusal"
    PROVIDER_INCOMPLETE = "provider_incomplete"
    INVALID_CANDIDATE = "invalid_candidate"
    BUDGET_BLOCKED = "budget_blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    INPUT_TOO_LARGE = "input_too_large"
    OUTPUT_TOO_LARGE = "output_too_large"


@dataclass(frozen=True, slots=True)
class CoachProviderResponse:
    candidate: Mapping[str, object]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microusd: int = 0
    request_id: str | None = None


class CoachProviderRefusal(RuntimeError):
    """Raised when a provider returns a model refusal without retaining its text."""

    def __init__(self, usage: CoachProviderResponse | None = None) -> None:
        super().__init__()
        self.usage = usage


class CoachProviderIncomplete(RuntimeError):
    """Raised when a provider cannot produce a complete structured candidate."""

    def __init__(self, usage: CoachProviderResponse | None = None) -> None:
        super().__init__()
        self.usage = usage


class CoachProvider(Protocol):
    provider_id: str
    model_id: str

    def estimate_cost_microusd(
        self,
        evidence_payload: Mapping[str, object],
        max_output_chars: int,
    ) -> int: ...

    async def generate(
        self,
        evidence_payload: Mapping[str, object],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CoachProviderResponse: ...


@dataclass(frozen=True, slots=True)
class CoachRuntimePolicy:
    timeout_seconds: float = 5.0
    max_attempts: int = 2
    max_input_chars: int = 20_000
    max_output_chars: int = 6_000
    max_total_cost_microusd: int = 5_000

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between one and five")
        if self.max_input_chars <= 0:
            raise ValueError("max_input_chars must be positive")
        if self.max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        if self.max_total_cost_microusd < 0:
            raise ValueError("max_total_cost_microusd cannot be negative")


@dataclass(frozen=True, slots=True)
class CoachCallAudit:
    call_id: str
    status: CoachRuntimeStatus
    provider_id: str
    model_id: str
    prompt_version: str
    prompt_hash: str
    evidence_id: str
    evidence_hash: str
    attempts: int
    input_chars: int
    output_chars: int
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    latency_ms: int
    validation_status: str
    issue_codes: tuple[str, ...]
    started_at: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class CoachRuntimeResult:
    item: CoachItem
    audit: CoachCallAudit


async def run_coach_provider(
    card: LeakCard,
    provider: CoachProvider,
    *,
    policy: CoachRuntimePolicy = CoachRuntimePolicy(),
) -> CoachRuntimeResult:
    """Generate one candidate behind strict resource, evidence, and fallback gates."""
    evidence = build_explanation_evidence(card)
    evidence_payload = evidence.model_payload()
    input_chars = len(_compact_json(evidence_payload))
    started_at = datetime.now(timezone.utc)
    started_clock = time.perf_counter()
    provider_id = _provider_label(provider, "provider_id")
    model_id = _provider_label(provider, "model_id")
    prompt_version = _provider_label(provider, "prompt_version")
    prompt_hash = _provider_label(provider, "prompt_hash")

    if input_chars > policy.max_input_chars:
        issue = ValidationIssue(
            "runtime_input_too_large",
            "model_input",
            "Evidence payload exceeds the configured input limit",
        )
        item = fallback_coach_item(card, (issue,))
        return _result(
            item=item,
            status=CoachRuntimeStatus.INPUT_TOO_LARGE,
            provider_id=provider_id,
            model_id=model_id,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            attempts=0,
            input_chars=input_chars,
            started_at=started_at,
            started_clock=started_clock,
        )

    try:
        estimated_cost = provider.estimate_cost_microusd(
            evidence_payload,
            policy.max_output_chars,
        )
        if type(estimated_cost) is not int or estimated_cost < 0:
            raise ValueError("Provider returned an invalid cost estimate")
    except Exception:
        issue = ValidationIssue(
            "runtime_provider_error",
            "cost_estimate",
            "Provider cost estimation failed",
        )
        item = fallback_coach_item(card, (issue,))
        return _result(
            item=item,
            status=CoachRuntimeStatus.PROVIDER_ERROR,
            provider_id=provider_id,
            model_id=model_id,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            attempts=0,
            input_chars=input_chars,
            started_at=started_at,
            started_clock=started_clock,
        )

    if estimated_cost * policy.max_attempts > policy.max_total_cost_microusd:
        issue = ValidationIssue(
            "runtime_budget_blocked",
            "cost_budget",
            "Worst-case planned attempts exceed the configured cost budget",
        )
        item = fallback_coach_item(card, (issue,))
        return _result(
            item=item,
            status=CoachRuntimeStatus.BUDGET_BLOCKED,
            provider_id=provider_id,
            model_id=model_id,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            attempts=0,
            input_chars=input_chars,
            started_at=started_at,
            started_clock=started_clock,
        )

    last_status = CoachRuntimeStatus.PROVIDER_ERROR
    outcome_usage: CoachProviderResponse | None = None
    attempts = 0
    for attempts in range(1, policy.max_attempts + 1):
        try:
            response = await asyncio.wait_for(
                provider.generate(
                    evidence_payload,
                    timeout_seconds=policy.timeout_seconds,
                    max_output_chars=policy.max_output_chars,
                ),
                timeout=policy.timeout_seconds,
            )
        except CoachProviderRefusal as exc:
            last_status = CoachRuntimeStatus.PROVIDER_REFUSAL
            outcome_usage = exc.usage
            break
        except CoachProviderIncomplete as exc:
            last_status = CoachRuntimeStatus.PROVIDER_INCOMPLETE
            outcome_usage = exc.usage
            break
        except TimeoutError:
            last_status = CoachRuntimeStatus.TIMEOUT
            continue
        except Exception:
            last_status = CoachRuntimeStatus.PROVIDER_ERROR
            continue

        if not isinstance(response, CoachProviderResponse):
            issue = ValidationIssue(
                "runtime_provider_error",
                "provider_response",
                "Provider returned an unsupported response object",
            )
            item = fallback_coach_item(card, (issue,))
            return _result(
                item=item,
                status=CoachRuntimeStatus.PROVIDER_ERROR,
                provider_id=provider_id,
                model_id=model_id,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                attempts=attempts,
                input_chars=input_chars,
                started_at=started_at,
                started_clock=started_clock,
            )

        metrics_issue = _response_metrics_issue(response)
        if metrics_issue is not None:
            item = fallback_coach_item(card, (metrics_issue,))
            return _result(
                item=item,
                status=CoachRuntimeStatus.PROVIDER_ERROR,
                provider_id=provider_id,
                model_id=model_id,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                attempts=attempts,
                input_chars=input_chars,
                started_at=started_at,
                started_clock=started_clock,
            )

        if not isinstance(response.candidate, Mapping):
            issue = ValidationIssue(
                "runtime_provider_error",
                "candidate",
                "Provider candidate must be a JSON object",
            )
            item = fallback_coach_item(card, (issue,))
            return _result(
                item=item,
                status=CoachRuntimeStatus.PROVIDER_ERROR,
                provider_id=provider_id,
                model_id=model_id,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                attempts=attempts,
                input_chars=input_chars,
                response=response,
                started_at=started_at,
                started_clock=started_clock,
            )

        try:
            output_chars = len(_compact_json(response.candidate))
        except (TypeError, ValueError):
            issue = ValidationIssue(
                "runtime_provider_error",
                "candidate",
                "Provider candidate is not JSON serializable",
            )
            item = fallback_coach_item(card, (issue,))
            return _result(
                item=item,
                status=CoachRuntimeStatus.PROVIDER_ERROR,
                provider_id=provider_id,
                model_id=model_id,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                attempts=attempts,
                input_chars=input_chars,
                started_at=started_at,
                started_clock=started_clock,
            )

        if output_chars > policy.max_output_chars:
            issue = ValidationIssue(
                "runtime_output_too_large",
                "candidate",
                "Provider candidate exceeds the configured output limit",
            )
            item = fallback_coach_item(card, (issue,))
            return _result(
                item=item,
                status=CoachRuntimeStatus.OUTPUT_TOO_LARGE,
                provider_id=provider_id,
                model_id=model_id,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                attempts=attempts,
                input_chars=input_chars,
                output_chars=output_chars,
                response=response,
                started_at=started_at,
                started_clock=started_clock,
            )

        if response.cost_microusd > policy.max_total_cost_microusd:
            issue = ValidationIssue(
                "runtime_budget_exceeded",
                "cost_budget",
                "Reported provider cost exceeds the configured budget",
            )
            item = fallback_coach_item(card, (issue,))
            return _result(
                item=item,
                status=CoachRuntimeStatus.BUDGET_EXCEEDED,
                provider_id=provider_id,
                model_id=model_id,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                attempts=attempts,
                input_chars=input_chars,
                output_chars=output_chars,
                response=response,
                started_at=started_at,
                started_clock=started_clock,
            )

        item = explain_leak_card(card, response.candidate)
        status = (
            CoachRuntimeStatus.VALIDATED
            if item.explanation.source == CoachSource.LLM_VALIDATED
            else CoachRuntimeStatus.INVALID_CANDIDATE
        )
        return _result(
            item=item,
            status=status,
            provider_id=provider_id,
            model_id=model_id,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            attempts=attempts,
            input_chars=input_chars,
            output_chars=output_chars,
            response=response,
            started_at=started_at,
            started_clock=started_clock,
        )

    issue_code = {
        CoachRuntimeStatus.TIMEOUT: "runtime_timeout",
        CoachRuntimeStatus.PROVIDER_REFUSAL: "runtime_provider_refusal",
        CoachRuntimeStatus.PROVIDER_INCOMPLETE: "runtime_provider_incomplete",
    }.get(last_status, "runtime_provider_error")
    issue = ValidationIssue(
        issue_code,
        "provider",
        "Provider attempts ended without a candidate",
    )
    item = fallback_coach_item(card, (issue,))
    return _result(
        item=item,
        status=last_status,
        provider_id=provider_id,
        model_id=model_id,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        attempts=attempts,
        input_chars=input_chars,
        response=outcome_usage,
        started_at=started_at,
        started_clock=started_clock,
    )


def coach_call_audit_to_dict(audit: CoachCallAudit) -> dict[str, object]:
    """Serialize metadata only; raw prompts, candidates, and request IDs are absent."""
    return {
        "call_id": audit.call_id,
        "status": audit.status.value,
        "provider_id": audit.provider_id,
        "model_id": audit.model_id,
        "prompt_version": audit.prompt_version,
        "prompt_hash": audit.prompt_hash,
        "evidence_id": audit.evidence_id,
        "evidence_hash": audit.evidence_hash,
        "attempts": audit.attempts,
        "input_chars": audit.input_chars,
        "output_chars": audit.output_chars,
        "input_tokens": audit.input_tokens,
        "output_tokens": audit.output_tokens,
        "cost_microusd": audit.cost_microusd,
        "latency_ms": audit.latency_ms,
        "validation_status": audit.validation_status,
        "issue_codes": list(audit.issue_codes),
        "started_at": audit.started_at,
        "completed_at": audit.completed_at,
    }


def _result(
    *,
    item: CoachItem,
    status: CoachRuntimeStatus,
    provider_id: str,
    model_id: str,
    prompt_version: str,
    prompt_hash: str,
    attempts: int,
    input_chars: int,
    started_at: datetime,
    started_clock: float,
    output_chars: int = 0,
    response: CoachProviderResponse | None = None,
) -> CoachRuntimeResult:
    completed_at = datetime.now(timezone.utc)
    issue_codes = tuple(
        sorted({issue.code for issue in item.explanation.validation.issues})
    )
    audit = CoachCallAudit(
        call_id=str(uuid.uuid4()),
        status=status,
        provider_id=provider_id,
        model_id=model_id,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        evidence_id=item.evidence.evidence_id,
        evidence_hash=item.evidence.evidence_hash,
        attempts=attempts,
        input_chars=input_chars,
        output_chars=output_chars,
        input_tokens=0 if response is None else response.input_tokens,
        output_tokens=0 if response is None else response.output_tokens,
        cost_microusd=0 if response is None else response.cost_microusd,
        latency_ms=round((time.perf_counter() - started_clock) * 1000),
        validation_status=item.explanation.validation.status.value,
        issue_codes=issue_codes,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
    )
    return CoachRuntimeResult(item=item, audit=audit)


def _response_metrics_issue(
    response: CoachProviderResponse,
) -> ValidationIssue | None:
    metrics = (
        ("input_tokens", response.input_tokens),
        ("output_tokens", response.output_tokens),
        ("cost_microusd", response.cost_microusd),
    )
    if any(type(value) is not int or value < 0 for _, value in metrics):
        return ValidationIssue(
            "runtime_provider_error",
            "provider_metrics",
            "Provider usage metrics must be non-negative integers",
        )
    return None


def _provider_label(provider: CoachProvider, field: str) -> str:
    value = getattr(provider, field, None)
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    return value[:120]


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
