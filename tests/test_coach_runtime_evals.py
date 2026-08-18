from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.coach import (  # noqa: E402
    CoachSource,
    build_candidate_skeleton,
    build_explanation_evidence,
)
from rivermind_core.coach_evals import (  # noqa: E402
    build_eval_card,
    coach_eval_report_to_dict,
    run_coach_eval,
)
from rivermind_core.coach_runtime import (  # noqa: E402
    CoachProviderResponse,
    CoachRuntimePolicy,
    CoachRuntimeStatus,
    coach_call_audit_to_dict,
    run_coach_provider,
)
from rivermind_core.cli import main  # noqa: E402


CORPUS = PROJECT_ROOT / "evals" / "coach_candidate_cases.json"


class StubProvider:
    provider_id = "stub"
    model_id = "offline-test"

    def __init__(
        self,
        candidate: dict[str, object],
        *,
        mode: str = "success",
        estimate: int = 100,
        actual_cost: int = 80,
    ) -> None:
        self.candidate = candidate
        self.mode = mode
        self.estimate = estimate
        self.actual_cost = actual_cost
        self.calls = 0
        self.last_payload: object = None

    def estimate_cost_microusd(
        self,
        evidence_payload: dict[str, object],
        max_output_chars: int,
    ) -> int:
        return self.estimate

    async def generate(
        self,
        evidence_payload: dict[str, object],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CoachProviderResponse:
        self.calls += 1
        self.last_payload = evidence_payload
        if self.mode == "timeout":
            await asyncio.sleep(timeout_seconds * 10)
        if self.mode == "error":
            raise RuntimeError("private provider error must not enter the audit")
        return CoachProviderResponse(
            candidate=self.candidate,
            input_tokens=240,
            output_tokens=120,
            cost_microusd=self.actual_cost,
            request_id="secret-request-AlicePrivate",
        )


class CoachEvalTest(unittest.TestCase):
    def test_committed_contract_corpus_has_fifty_passing_cases(self) -> None:
        report = run_coach_eval(CORPUS)

        self.assertEqual(report.total, 50)
        self.assertEqual(report.passed, 50)
        self.assertEqual(report.failed, 0)
        self.assertEqual(
            report.categories,
            {
                "valid": {"total": 8, "passed": 8},
                "schema": {"total": 7, "passed": 7},
                "evidence": {"total": 8, "passed": 8},
                "numeric": {"total": 10, "passed": 10},
                "unsupported": {"total": 8, "passed": 8},
                "privacy": {"total": 5, "passed": 5},
                "action": {"total": 4, "passed": 4},
            },
        )

    def test_eval_report_is_json_serializable(self) -> None:
        payload = coach_eval_report_to_dict(run_coach_eval(CORPUS))
        encoded = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["summary"]["failed"], 0)
        self.assertIn("numeric_fullwidth_digits", encoded)

    def test_coach_eval_cli_returns_machine_readable_gate_result(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(
                ["coach-eval", "--corpus", str(CORPUS), "--json"]
            )
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["summary"]["total"], 50)
        self.assertEqual(payload["summary"]["failed"], 0)


class CoachRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.card = build_eval_card()
        evidence = build_explanation_evidence(self.card)
        self.candidate = build_candidate_skeleton(evidence)

    async def test_valid_provider_candidate_is_accepted_and_audit_is_metadata_only(self) -> None:
        provider = StubProvider(self.candidate)

        result = await run_coach_provider(self.card, provider)
        audit_json = json.dumps(
            coach_call_audit_to_dict(result.audit),
            ensure_ascii=False,
        )
        provider_payload = json.dumps(provider.last_payload, ensure_ascii=False)

        self.assertEqual(result.audit.status, CoachRuntimeStatus.VALIDATED)
        self.assertEqual(result.item.explanation.source, CoachSource.LLM_VALIDATED)
        self.assertEqual(result.audit.attempts, 1)
        self.assertEqual(result.audit.cost_microusd, 80)
        self.assertNotIn("AlicePrivate", provider_payload)
        self.assertNotIn("pokerstars", provider_payload)
        self.assertNotIn("100000000001", provider_payload)
        self.assertNotIn("Secret Table", provider_payload)
        self.assertNotIn("AlicePrivate", audit_json)
        self.assertNotIn("secret-request", audit_json)
        self.assertNotIn("candidate", audit_json)

    async def test_timeout_retries_then_falls_back(self) -> None:
        provider = StubProvider(self.candidate, mode="timeout")
        policy = CoachRuntimePolicy(timeout_seconds=0.01, max_attempts=2)

        result = await run_coach_provider(self.card, provider, policy=policy)

        self.assertEqual(result.audit.status, CoachRuntimeStatus.TIMEOUT)
        self.assertEqual(result.audit.attempts, 2)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(result.item.explanation.source, CoachSource.TEMPLATE_FALLBACK)
        self.assertIn("runtime_timeout", result.audit.issue_codes)

    async def test_provider_errors_retry_without_exposing_exception_text(self) -> None:
        provider = StubProvider(self.candidate, mode="error")

        result = await run_coach_provider(self.card, provider)
        audit_json = json.dumps(coach_call_audit_to_dict(result.audit))

        self.assertEqual(result.audit.status, CoachRuntimeStatus.PROVIDER_ERROR)
        self.assertEqual(provider.calls, 2)
        self.assertIn("runtime_provider_error", result.audit.issue_codes)
        self.assertNotIn("private provider error", audit_json)

    async def test_invalid_candidate_is_not_retried_and_falls_back(self) -> None:
        candidate = dict(self.candidate)
        candidate["evidence_hash"] = "tampered"
        provider = StubProvider(candidate)

        result = await run_coach_provider(self.card, provider)

        self.assertEqual(result.audit.status, CoachRuntimeStatus.INVALID_CANDIDATE)
        self.assertEqual(provider.calls, 1)
        self.assertIn("evidence_hash_mismatch", result.audit.issue_codes)
        self.assertEqual(result.item.explanation.source, CoachSource.TEMPLATE_FALLBACK)

    async def test_worst_case_estimate_blocks_call_before_provider_execution(self) -> None:
        provider = StubProvider(self.candidate, estimate=600)
        policy = CoachRuntimePolicy(max_attempts=2, max_total_cost_microusd=1_000)

        result = await run_coach_provider(self.card, provider, policy=policy)

        self.assertEqual(result.audit.status, CoachRuntimeStatus.BUDGET_BLOCKED)
        self.assertEqual(result.audit.attempts, 0)
        self.assertEqual(provider.calls, 0)
        self.assertIn("runtime_budget_blocked", result.audit.issue_codes)

    async def test_reported_cost_above_budget_rejects_candidate(self) -> None:
        provider = StubProvider(self.candidate, estimate=100, actual_cost=2_000)
        policy = CoachRuntimePolicy(max_attempts=2, max_total_cost_microusd=1_000)

        result = await run_coach_provider(self.card, provider, policy=policy)

        self.assertEqual(result.audit.status, CoachRuntimeStatus.BUDGET_EXCEEDED)
        self.assertEqual(result.audit.cost_microusd, 2_000)
        self.assertEqual(result.item.explanation.source, CoachSource.TEMPLATE_FALLBACK)

    async def test_input_and_output_limits_fail_closed(self) -> None:
        input_provider = StubProvider(self.candidate)
        input_result = await run_coach_provider(
            self.card,
            input_provider,
            policy=CoachRuntimePolicy(max_input_chars=1),
        )
        output_provider = StubProvider(self.candidate)
        output_result = await run_coach_provider(
            self.card,
            output_provider,
            policy=CoachRuntimePolicy(max_output_chars=10),
        )

        self.assertEqual(input_result.audit.status, CoachRuntimeStatus.INPUT_TOO_LARGE)
        self.assertEqual(input_provider.calls, 0)
        self.assertEqual(output_result.audit.status, CoachRuntimeStatus.OUTPUT_TOO_LARGE)
        self.assertEqual(output_provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
