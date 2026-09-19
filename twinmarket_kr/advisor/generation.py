from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import config
from twinmarket_kr.advisor.artifact import canonical_sha256, validate_advice_body
from twinmarket_kr.llm.belief import render_prompt
from twinmarket_kr.llm.client import OpenRouterClient, response_content


ADVISOR_OUTPUT_KEYS = {
    "persona_basis",
    "observed_behavior",
    "persona_behavior_assessment",
    "performance_context",
    "advice_body",
}


def validate_advisor_output(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != ADVISOR_OUTPUT_KEYS:
        raise ValueError("advisor output keys differ from the sealed schema")
    result = dict(value)
    for key in ("persona_basis", "observed_behavior"):
        rows = result.get(key)
        if not isinstance(rows, list) or not rows or any(
            not isinstance(item, str) or not item.strip() for item in rows
        ):
            raise ValueError(f"advisor {key} must be a non-empty string list")
        result[key] = [item.strip() for item in rows]
    for key in ("persona_behavior_assessment", "performance_context", "advice_body"):
        item = result.get(key)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"advisor {key} must be non-empty")
        result[key] = item.strip()
    result["advice_body"] = validate_advice_body(result["advice_body"])
    return result


async def generate_advisor_output(
    case: Mapping[str, Any],
    *,
    client: OpenRouterClient,
    seed: int,
    agent_id: str,
) -> tuple[dict[str, Any], str]:
    prompt = render_prompt(
        "advisor_generation.txt",
        advisor_case_json=json.dumps(case, ensure_ascii=False, sort_keys=True, indent=2),
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    logical_call_id = f"advisor:{agent_id}:{prompt_sha256}"
    phase_attempt_id = "advisor-generation-attempt-1"
    response = await client.chat_advisor_reasoning_on(
        [{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.2,
        seed=int(seed),
        max_tokens=2048,
        audit_label="advisor_generation",
        logical_call_id=logical_call_id,
        phase_attempt_id=phase_attempt_id,
    )
    parsed = json.loads(response_content(response))
    if not isinstance(parsed, dict):
        raise ValueError("advisor provider response must be a JSON object")
    validated = validate_advisor_output(parsed)
    client.record_experiment_acceptance(
        logical_call_id=logical_call_id,
        phase_attempt_id=phase_attempt_id,
        accepted_response_sha256=canonical_sha256(validated),
    )
    return validated, prompt_sha256


def advisor_client(*, audit_path: Path) -> OpenRouterClient:
    return OpenRouterClient(
        model=config.PAPER_OPENROUTER_MODEL,
        audit_path=audit_path,
        audit_context={
            "artifact": "integrated_advisor_generation_attempt",
            "purpose": "advisor_generation",
        },
    )
