from __future__ import annotations

import asyncio
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from rivermind_core.coach_prompt import (
    COACH_PROMPT_VERSION,
    build_openai_responses_request,
    coach_prompt_hash,
)
from rivermind_core.coach_runtime import (
    CoachProviderIncomplete,
    CoachProviderRefusal,
    CoachProviderResponse,
)


OPENAI_RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"


class OpenAITransportError(RuntimeError):
    """Raised without retaining a provider response body."""


class OpenAIProviderProtocolError(RuntimeError):
    """Raised when a response does not match the expected Responses API shape."""


class OpenAIJSONTransport(Protocol):
    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class OpenAIResponsesConfig:
    model_id: str
    input_rate_microusd_per_million_tokens: int
    output_rate_microusd_per_million_tokens: int
    api_key: str = field(repr=False, compare=False)
    external_data_consent: bool = False
    endpoint: str = OPENAI_RESPONSES_ENDPOINT
    max_output_tokens: int = 2_048
    token_estimate_multiplier: int = 2
    max_response_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("model_id cannot be empty")
        if not self.api_key.strip() or "\n" in self.api_key or "\r" in self.api_key:
            raise ValueError("api_key must be a non-empty single-line secret")
        if self.endpoint != OPENAI_RESPONSES_ENDPOINT:
            raise ValueError("Only the official OpenAI Responses endpoint is allowed")
        rates = (
            self.input_rate_microusd_per_million_tokens,
            self.output_rate_microusd_per_million_tokens,
        )
        if any(type(item) is not int or item <= 0 for item in rates):
            raise ValueError("token rates must be positive integer microusd values")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if not 1 <= self.token_estimate_multiplier <= 8:
            raise ValueError("token_estimate_multiplier must be between one and eight")
        if not 1_024 <= self.max_response_bytes <= 10_000_000:
            raise ValueError("max_response_bytes must be between 1024 and 10000000")

    @classmethod
    def from_environment(
        cls,
        *,
        model_id: str,
        input_rate_microusd_per_million_tokens: int,
        output_rate_microusd_per_million_tokens: int,
        external_data_consent: bool,
        api_key_environment_variable: str = "OPENAI_API_KEY",
    ) -> OpenAIResponsesConfig:
        api_key = os.environ.get(api_key_environment_variable, "")
        if not api_key:
            raise ValueError(
                f"Missing API key environment variable: {api_key_environment_variable}"
            )
        return cls(
            model_id=model_id,
            input_rate_microusd_per_million_tokens=(
                input_rate_microusd_per_million_tokens
            ),
            output_rate_microusd_per_million_tokens=(
                output_rate_microusd_per_million_tokens
            ),
            api_key=api_key,
            external_data_consent=external_data_consent,
        )


class UrllibOpenAITransport:
    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, object]:
        return await asyncio.to_thread(
            self._post_json_sync,
            url,
            headers,
            payload,
            timeout_seconds,
            max_response_bytes,
        )

    @staticmethod
    def _post_json_sync(
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, object]:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise OpenAITransportError(f"OpenAI HTTP status {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OpenAITransportError("OpenAI transport failed") from None
        if len(raw) > max_response_bytes:
            raise OpenAITransportError("OpenAI response exceeded the byte limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenAITransportError("OpenAI returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise OpenAITransportError("OpenAI returned a non-object JSON response")
        return decoded


class OpenAIResponsesCoachProvider:
    provider_id = "openai-responses"
    prompt_version = COACH_PROMPT_VERSION
    prompt_hash = coach_prompt_hash()

    def __init__(
        self,
        config: OpenAIResponsesConfig,
        *,
        transport: OpenAIJSONTransport | None = None,
    ) -> None:
        if not config.external_data_consent:
            raise ValueError(
                "external_data_consent must be explicitly enabled before provider use"
            )
        self.config = config
        self.model_id = config.model_id
        self._transport = transport or UrllibOpenAITransport()

    def estimate_cost_microusd(
        self,
        evidence_payload: Mapping[str, object],
        max_output_chars: int,
    ) -> int:
        output_tokens = self._max_output_tokens(max_output_chars)
        request_payload = build_openai_responses_request(
            evidence_payload,
            model_id=self.model_id,
            max_output_tokens=output_tokens,
        )
        request_chars = len(
            json.dumps(
                request_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        input_tokens = request_chars * self.config.token_estimate_multiplier
        return _usage_cost_microusd(
            input_tokens,
            output_tokens,
            self.config,
        )

    async def generate(
        self,
        evidence_payload: Mapping[str, object],
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> CoachProviderResponse:
        request_payload = build_openai_responses_request(
            evidence_payload,
            model_id=self.model_id,
            max_output_tokens=self._max_output_tokens(max_output_chars),
        )
        response = await self._transport.post_json(
            self.config.endpoint,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "rivermind-poker/coach",
            },
            payload=request_payload,
            timeout_seconds=timeout_seconds,
            max_response_bytes=self.config.max_response_bytes,
        )
        return _parse_openai_response(response, self.config)

    def _max_output_tokens(self, max_output_chars: int) -> int:
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        char_bound = max_output_chars * self.config.token_estimate_multiplier
        return min(self.config.max_output_tokens, char_bound)


def _parse_openai_response(
    response: Mapping[str, object],
    config: OpenAIResponsesConfig,
) -> CoachProviderResponse:
    usage_response = _usage_only_response(response, config)
    if response.get("status") == "incomplete":
        raise CoachProviderIncomplete(usage_response)
    output = response.get("output")
    if not isinstance(output, list):
        raise OpenAIProviderProtocolError("OpenAI response is missing output")

    output_texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            content_type = content_item.get("type")
            if content_type == "refusal":
                raise CoachProviderRefusal(usage_response)
            if content_type == "output_text" and isinstance(
                content_item.get("text"), str
            ):
                output_texts.append(content_item["text"])
    if len(output_texts) != 1:
        raise OpenAIProviderProtocolError(
            "OpenAI response must contain exactly one output_text item"
        )
    try:
        candidate = json.loads(output_texts[0])
    except json.JSONDecodeError:
        raise OpenAIProviderProtocolError(
            "OpenAI output_text is not valid JSON"
        ) from None
    if not isinstance(candidate, dict):
        raise OpenAIProviderProtocolError(
            "OpenAI structured output is not an object"
        )

    return CoachProviderResponse(
        candidate=candidate,
        input_tokens=usage_response.input_tokens,
        output_tokens=usage_response.output_tokens,
        cost_microusd=usage_response.cost_microusd,
        request_id=usage_response.request_id,
    )


def _usage_only_response(
    response: Mapping[str, object],
    config: OpenAIResponsesConfig,
) -> CoachProviderResponse:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise OpenAIProviderProtocolError("OpenAI response is missing usage")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if type(input_tokens) is not int or type(output_tokens) is not int:
        raise OpenAIProviderProtocolError("OpenAI usage token counts are invalid")
    if input_tokens < 0 or output_tokens < 0:
        raise OpenAIProviderProtocolError("OpenAI usage token counts are negative")

    response_id = response.get("id")
    return CoachProviderResponse(
        candidate={},
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_microusd=_usage_cost_microusd(
            input_tokens,
            output_tokens,
            config,
        ),
        request_id=response_id if isinstance(response_id, str) else None,
    )


def _usage_cost_microusd(
    input_tokens: int,
    output_tokens: int,
    config: OpenAIResponsesConfig,
) -> int:
    numerator = (
        input_tokens * config.input_rate_microusd_per_million_tokens
        + output_tokens * config.output_rate_microusd_per_million_tokens
    )
    return math.ceil(numerator / 1_000_000)
