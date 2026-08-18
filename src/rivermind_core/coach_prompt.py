from __future__ import annotations

import hashlib
import json
from typing import Mapping

from rivermind_core.coach import COACH_CANDIDATE_SCHEMA_VERSION


COACH_PROMPT_VERSION = "coach-prompt/zh-CN/1.0.0"
COACH_SCHEMA_NAME = "rivermind_coach_candidate"

COACH_SYSTEM_PROMPT = """你是 RiverMind 的离线复盘解释器，不是扑克行动决策器。
只根据用户消息中的 ExplanationEvidence 生成中文复盘说明，并严格遵守输出 JSON Schema。

硬性规则：
- 叙述中的事实必须使用原样的 {{fact_id}} 占位符；不要把 display_value 或任何数字直接抄进叙述。
- evidence_refs 必须与叙述实际出现的占位符完全一致，不能多报、漏报或重复。
- 必须覆盖信号标题、观察比例、样本、置信区间、规则阈值、规则依据、复盘问题和适用边界。
- 如果提供匿名证据手牌，至少引用一手。
- 不推断 GTO 频率、范围、EV、范围优势、坚果优势、对手底牌或未来公共牌。
- 不给出某一手必须下注、跟注、加注、弃牌或全下的直接指令。
- 不输出玩家名、站点、桌名、原始手牌 ID、原始牌谱或其他来源标识。
- 不添加 schema 以外的字段，不写 Markdown，不解释你的推理过程。
"""

COACH_CANDIDATE_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string"},
        "evidence_id": {"type": "string"},
        "evidence_hash": {"type": "string"},
        "headline": {"type": "string"},
        "observation": {"type": "string"},
        "teaching_point": {"type": "string"},
        "review_plan": {
            "type": "array",
            "items": {"type": "string"},
        },
        "uncertainty": {"type": "string"},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "schema_version",
        "evidence_id",
        "evidence_hash",
        "headline",
        "observation",
        "teaching_point",
        "review_plan",
        "uncertainty",
        "evidence_refs",
    ],
    "additionalProperties": False,
}


def coach_prompt_hash() -> str:
    payload = {
        "version": COACH_PROMPT_VERSION,
        "system": COACH_SYSTEM_PROMPT,
        "candidate_schema_version": COACH_CANDIDATE_SCHEMA_VERSION,
        "schema": COACH_CANDIDATE_JSON_SCHEMA,
    }
    return hashlib.sha256(_compact_json(payload).encode("utf-8")).hexdigest()


def build_openai_responses_request(
    evidence_payload: Mapping[str, object],
    *,
    model_id: str,
    max_output_tokens: int,
) -> dict[str, object]:
    if not model_id.strip():
        raise ValueError("model_id cannot be empty")
    if max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")
    return {
        "model": model_id,
        "store": False,
        "input": [
            {"role": "system", "content": COACH_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _compact_json(evidence_payload),
            },
        ],
        "max_output_tokens": max_output_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": COACH_SCHEMA_NAME,
                "strict": True,
                "schema": COACH_CANDIDATE_JSON_SCHEMA,
            }
        },
    }


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
