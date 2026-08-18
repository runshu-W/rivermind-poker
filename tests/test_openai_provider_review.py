from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(PROJECT_ROOT / "src"))

from rivermind_core.cli import main  # noqa: E402
from rivermind_core.coach import (  # noqa: E402
    CoachSource,
    build_candidate_skeleton,
    build_explanation_evidence,
    explain_leak_card,
)
from rivermind_core.coach_evals import build_eval_card  # noqa: E402
from rivermind_core.coach_prompt import (  # noqa: E402
    COACH_CANDIDATE_JSON_SCHEMA,
    COACH_PROMPT_VERSION,
    build_openai_responses_request,
    coach_prompt_hash,
)
from rivermind_core.coach_review import (  # noqa: E402
    build_blind_review_case,
    build_expert_review_document,
    score_expert_review_document,
)
from rivermind_core.coach_runtime import (  # noqa: E402
    CoachRuntimePolicy,
    CoachRuntimeStatus,
    coach_call_audit_to_dict,
    run_coach_provider,
)
from rivermind_core.openai_provider import (  # noqa: E402
    OPENAI_RESPONSES_ENDPOINT,
    OpenAIResponsesCoachProvider,
    OpenAIResponsesConfig,
)


class FakeOpenAITransport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0
        self.url = ""
        self.headers: dict[str, str] = {}
        self.payload: dict[str, object] = {}

    async def post_json(
        self,
        url: str,
        *,
        headers,
        payload,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, object]:
        self.calls += 1
        self.url = url
        self.headers = dict(headers)
        self.payload = dict(payload)
        return self.response


def _config(*, consent: bool = True) -> OpenAIResponsesConfig:
    return OpenAIResponsesConfig(
        model_id="test-structured-model",
        input_rate_microusd_per_million_tokens=1_000_000,
        output_rate_microusd_per_million_tokens=6_000_000,
        api_key="test-private-api-key",
        external_data_consent=consent,
    )


def _response_with_candidate(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "id": "resp_secret_AlicePrivate",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(candidate, ensure_ascii=False),
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


class CoachPromptTest(unittest.TestCase):
    def test_request_uses_strict_schema_and_only_deidentified_evidence(self) -> None:
        card = build_eval_card()
        evidence = build_explanation_evidence(card)

        request = build_openai_responses_request(
            evidence.model_payload(),
            model_id="test-model",
            max_output_tokens=500,
        )
        encoded = json.dumps(request, ensure_ascii=False)

        self.assertFalse(request["store"])
        self.assertEqual(request["model"], "test-model")
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertFalse(COACH_CANDIDATE_JSON_SCHEMA["additionalProperties"])
        self.assertNotIn("AlicePrivate", encoded)
        self.assertNotIn("pokerstars", encoded)
        self.assertNotIn("100000000001", encoded)
        self.assertNotIn("Secret Table", encoded)
        self.assertEqual(len(coach_prompt_hash()), 64)

    def test_api_key_is_redacted_and_explicit_consent_is_required(self) -> None:
        config = _config(consent=False)

        self.assertNotIn("test-private-api-key", repr(config))
        with self.assertRaisesRegex(ValueError, "external_data_consent"):
            OpenAIResponsesCoachProvider(config)


class OpenAIProviderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.card = build_eval_card()
        evidence = build_explanation_evidence(self.card)
        self.candidate = build_candidate_skeleton(evidence)

    async def test_adapter_parses_structured_output_and_keeps_secret_out_of_body(self) -> None:
        transport = FakeOpenAITransport(_response_with_candidate(self.candidate))
        provider = OpenAIResponsesCoachProvider(_config(), transport=transport)

        response = await provider.generate(
            build_explanation_evidence(self.card).model_payload(),
            timeout_seconds=1,
            max_output_chars=6_000,
        )
        body = json.dumps(transport.payload, ensure_ascii=False)

        self.assertEqual(transport.url, OPENAI_RESPONSES_ENDPOINT)
        self.assertEqual(
            transport.headers["Authorization"], "Bearer test-private-api-key"
        )
        self.assertNotIn("test-private-api-key", body)
        self.assertNotIn("AlicePrivate", body)
        self.assertEqual(response.candidate, self.candidate)
        self.assertEqual(response.cost_microusd, 400)
        self.assertEqual(response.request_id, "resp_secret_AlicePrivate")

    async def test_runtime_records_prompt_identity_without_request_id(self) -> None:
        transport = FakeOpenAITransport(_response_with_candidate(self.candidate))
        provider = OpenAIResponsesCoachProvider(_config(), transport=transport)

        result = await run_coach_provider(
            self.card,
            provider,
            policy=CoachRuntimePolicy(
                max_attempts=1,
                max_total_cost_microusd=100_000,
            ),
        )
        audit_json = json.dumps(coach_call_audit_to_dict(result.audit))

        self.assertEqual(result.audit.status, CoachRuntimeStatus.VALIDATED)
        self.assertEqual(result.audit.prompt_version, COACH_PROMPT_VERSION)
        self.assertEqual(result.audit.prompt_hash, coach_prompt_hash())
        self.assertEqual(result.item.explanation.source, CoachSource.LLM_VALIDATED)
        self.assertNotIn("resp_secret", audit_json)
        self.assertNotIn("test-private-api-key", audit_json)

    async def test_refusal_is_not_retried_and_refusal_text_is_not_audited(self) -> None:
        refusal = {
            "id": "resp_refusal",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "refusal",
                            "refusal": "AlicePrivate refusal body",
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        transport = FakeOpenAITransport(refusal)
        provider = OpenAIResponsesCoachProvider(_config(), transport=transport)

        result = await run_coach_provider(
            self.card,
            provider,
            policy=CoachRuntimePolicy(
                max_attempts=2,
                max_total_cost_microusd=100_000,
            ),
        )
        audit_json = json.dumps(coach_call_audit_to_dict(result.audit))

        self.assertEqual(result.audit.status, CoachRuntimeStatus.PROVIDER_REFUSAL)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(result.audit.cost_microusd, 22)
        self.assertIn("runtime_provider_refusal", result.audit.issue_codes)
        self.assertNotIn("AlicePrivate", audit_json)

    async def test_incomplete_response_is_not_retried(self) -> None:
        transport = FakeOpenAITransport(
            {
                "id": "resp_incomplete",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        )
        provider = OpenAIResponsesCoachProvider(_config(), transport=transport)

        result = await run_coach_provider(
            self.card,
            provider,
            policy=CoachRuntimePolicy(
                max_attempts=2,
                max_total_cost_microusd=100_000,
            ),
        )

        self.assertEqual(result.audit.status, CoachRuntimeStatus.PROVIDER_INCOMPLETE)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(result.audit.cost_microusd, 70)
        self.assertIn("runtime_provider_incomplete", result.audit.issue_codes)


class ExpertReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        card = build_eval_card()
        evidence = build_explanation_evidence(card)
        self.item = explain_leak_card(card, build_candidate_skeleton(evidence))

    def _passing_document(self) -> dict[str, object]:
        cases = []
        for index in range(50):
            case = build_blind_review_case(
                self.item,
                case_id=f"case-{index + 1}",
                variant_id="A",
            )
            case["evidence_hash"] = f"{index + 1:064x}"
            case["ratings"] = [
                {
                    "reviewer_id_hash": "a" * 16,
                    "expertise_attested": True,
                    "faithfulness": 5,
                    "teaching_value": 5,
                    "clarity": 5,
                    "uncertainty_quality": 5,
                    "fatal_error_codes": [],
                },
                {
                    "reviewer_id_hash": "b" * 16,
                    "expertise_attested": True,
                    "faithfulness": 5,
                    "teaching_value": 5,
                    "clarity": 5,
                    "uncertainty_quality": 5,
                    "fatal_error_codes": [],
                },
            ]
            cases.append(case)
        return build_expert_review_document(cases)

    def test_blind_export_omits_source_model_provider_and_private_identifiers(self) -> None:
        case = build_blind_review_case(
            self.item,
            case_id="case-one",
            variant_id="A",
        )
        encoded = json.dumps(case, ensure_ascii=False)

        self.assertNotIn("AlicePrivate", encoded)
        self.assertNotIn("pokerstars", encoded)
        self.assertNotIn("100000000001", encoded)
        self.assertNotIn("provider", encoded)
        self.assertNotIn("model", encoded)
        self.assertNotIn("source", encoded)

    def test_fifty_cases_with_two_expert_ratings_pass_quality_gate(self) -> None:
        report = score_expert_review_document(self._passing_document())

        self.assertTrue(report.passed)
        self.assertEqual(report.cases, 50)
        self.assertEqual(report.unique_evidence_cases, 50)
        self.assertEqual(report.ratings, 100)
        self.assertEqual(report.unique_reviewers, 2)
        self.assertEqual(report.fatal_errors, 0)
        self.assertEqual(report.dimension_means["faithfulness"], 5.0)

    def test_one_fatal_error_fails_quality_gate(self) -> None:
        document = self._passing_document()
        document["cases"][0]["ratings"][0]["fatal_error_codes"] = [
            "unsupported_strategy_claim"
        ]

        report = score_expert_review_document(document)

        self.assertFalse(report.passed)
        self.assertEqual(report.fatal_errors, 1)
        self.assertIn("fatal_errors_present", report.failure_reasons)

    def test_review_score_cli_returns_nonzero_for_draft_packet(self) -> None:
        draft = build_expert_review_document(
            (
                build_blind_review_case(
                    self.item,
                    case_id="draft-one",
                    variant_id="A",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "draft.json"
            path.write_text(json.dumps(draft), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(["coach-review-score", str(path), "--json"])
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertFalse(payload["summary"]["passed"])
        self.assertIn("insufficient_cases", payload["summary"]["failure_reasons"])

    def test_external_cli_requires_explicit_opt_in_before_database_or_key(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "coach-openai",
                    "--database",
                    "does-not-exist.db",
                    "--rule-id",
                    "some-rule",
                    "--model",
                    "some-model",
                    "--input-usd-per-million",
                    "1",
                    "--output-usd-per-million",
                    "1",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--allow-external-model is required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
