from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

import config
from twinmarket_kr.advisor.artifact import canonical_sha256, validate_advice_body
from twinmarket_kr.llm.belief import render_prompt
from twinmarket_kr.llm.client import (
    OpenRouterClient,
    response_content,
    response_finish_reason,
    stable_llm_seed,
)
from twinmarket_kr.llm.validation import (
    build_validation_retry_prompt,
    retry_temperature_schedule,
)


ADVISOR_OUTPUT_KEYS = {
    "persona_basis",
    "observed_behavior",
    "persona_behavior_assessment",
    "performance_context",
    "advice_body",
}
ADVISOR_VALIDATION_ATTEMPTS = 3
ADVISOR_MAX_TOKENS = 8192
ADVISOR_SCHEMA_HINT = """{
  "persona_basis": ["페르소나 근거"],
  "observed_behavior": ["관찰된 행동 근거"],
  "persona_behavior_assessment": "페르소나와 행동의 관계 평가",
  "performance_context": "과거 성과 맥락",
  "advice_body": "1~500자의 비지시적 맞춤형 진단"
}
persona_basis와 observed_behavior는 비어 있지 않은 문자열 배열이어야 합니다.
나머지 값은 비어 있지 않은 문자열이어야 하며, advice_body에는 매수·매도 지시나
미래 가격 예측을 포함하지 마세요."""


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


def parse_advisor_response(raw: str) -> dict[str, Any]:
    """Find one exact advisor object in a reasoning-on text response."""

    candidates = [raw]
    candidates.extend(
        match.group(1)
        for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    )
    decoder = json.JSONDecoder()
    for text in candidates:
        stripped = text.strip()
        try:
            parsed = json.loads(stripped)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            try:
                return validate_advisor_output(parsed)
            except (TypeError, ValueError):
                pass
        for match in re.finditer(r"\{", text):
            try:
                embedded, _ = decoder.raw_decode(text, match.start())
            except json.JSONDecodeError:
                continue
            if not isinstance(embedded, dict):
                continue
            try:
                return validate_advisor_output(embedded)
            except (TypeError, ValueError):
                continue
    raise ValueError("advisor response contains no object matching the sealed schema")


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
    temperatures = retry_temperature_schedule(0.2, ADVISOR_VALIDATION_ATTEMPTS)
    errors: list[str] = []
    for attempt, temperature in enumerate(temperatures, start=1):
        phase_attempt_id = f"advisor-generation-attempt-{attempt}"
        attempt_prompt = prompt
        if errors:
            attempt_prompt = build_validation_retry_prompt(
                prompt,
                errors=errors,
                schema_hint=ADVISOR_SCHEMA_HINT,
            )
        attempt_seed = (
            int(seed)
            if attempt == 1
            else stable_llm_seed(seed, "advisor_validation", agent_id, attempt)
        )
        response = await client.chat_advisor_reasoning_on(
            [{"role": "user", "content": attempt_prompt}],
            response_format=None,
            temperature=temperature,
            seed=attempt_seed,
            max_tokens=ADVISOR_MAX_TOKENS,
            audit_label="advisor_generation",
            logical_call_id=logical_call_id,
            phase_attempt_id=phase_attempt_id,
        )
        raw = response_content(response)
        if response_finish_reason(response) != "stop":
            errors = [
                "provider_finish_reason_must_be_stop:"
                f"got={response_finish_reason(response)!r}"
            ]
            continue
        try:
            validated = parse_advisor_response(raw)
        except (TypeError, ValueError) as exc:
            errors = [str(exc)]
            continue
        client.record_advisor_acceptance(
            logical_call_id=logical_call_id,
            phase_attempt_id=phase_attempt_id,
            accepted_response_sha256=canonical_sha256(validated),
        )
        return validated, prompt_sha256
    raise ValueError(
        "advisor provider response did not satisfy the sealed schema after "
        f"{ADVISOR_VALIDATION_ATTEMPTS} attempts: {errors}"
    )


def advisor_client(*, audit_path: Path) -> OpenRouterClient:
    return OpenRouterClient(
        model=config.PAPER_OPENROUTER_MODEL,
        audit_path=audit_path,
        audit_context={
            "artifact": "integrated_advisor_generation_attempt",
            "purpose": "advisor_generation",
        },
    )
