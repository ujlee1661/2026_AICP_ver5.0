from __future__ import annotations

import hashlib
import json
import re
from datetime import date
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
  "observed_behavior": ["fill_A001_t090: 관찰된 원장 근거"],
  "persona_behavior_assessment": "페르소나와 행동의 관계 평가",
  "performance_context": "과거 성과 맥락",
  "advice_body": "1~500자의 비지시적 맞춤형 진단"
}
persona_basis와 observed_behavior는 비어 있지 않은 문자열 배열이어야 합니다.
나머지 값은 비어 있지 않은 문자열이어야 하며, advice_body에는 매수·매도 지시나
미래 가격 예측을 포함하지 마세요."""

ADVISOR_CUTOFF_DATE = date(2026, 5, 4)


def validate_advisor_case(case: Mapping[str, Any]) -> dict[str, Any]:
    if str(case.get("cutoff_event_id") or "") != "2026-05-04/PM":
        raise ValueError("advisor case cutoff must be 2026-05-04/PM")
    constraints = case.get("runtime_constraints")
    expected_constraints = {
        "decision_space": ["buy", "sell"],
        "allow_hold": False,
        "minimum_order_quantity": 1,
        "transaction_fee_rate": 0.0,
    }
    if constraints != expected_constraints:
        raise ValueError("advisor case runtime constraints differ")
    episodes = case.get("representative_episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("advisor case requires representative fill episodes")
    fill_ids: set[str] = set()
    for episode in episodes:
        if not isinstance(episode, Mapping):
            raise ValueError("advisor representative episode must be an object")
        fill_id = str(episode.get("fill_id") or "").strip()
        event_id = str(episode.get("event_id") or "").strip()
        if not fill_id or fill_id in fill_ids:
            raise ValueError("advisor representative fill IDs must be unique")
        if event_id > "2026-05-04/PM":
            raise ValueError("advisor representative episode exceeds cutoff")
        fill_ids.add(fill_id)
    return {"fill_ids": fill_ids}


def _reject_post_cutoff_dates(value: Mapping[str, Any]) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for year, month, day in re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", serialized):
        try:
            observed = date(int(year), int(month), int(day))
        except ValueError as exc:
            raise ValueError("advisor output contains an invalid date") from exc
        if observed > ADVISOR_CUTOFF_DATE:
            raise ValueError("advisor output contains a post-cutoff date")
    for month, day in re.findall(r"(?<!\d)(\d{1,2})월\s*(\d{1,2})일", serialized):
        try:
            observed = date(2026, int(month), int(day))
        except ValueError as exc:
            raise ValueError("advisor output contains an invalid Korean date") from exc
        if observed > ADVISOR_CUTOFF_DATE:
            raise ValueError("advisor output contains a post-cutoff Korean date")


def validate_advisor_output(
    value: Mapping[str, Any],
    *,
    case: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
    if case is not None:
        case_index = validate_advisor_case(case)
        available_fill_ids = case_index["fill_ids"]
        for observation in result["observed_behavior"]:
            cited = set(re.findall(r"\bfill_[A-Za-z0-9_-]+_t\d+\b", observation))
            if not cited:
                raise ValueError("advisor observed_behavior must cite a fill_id")
            if not cited <= available_fill_ids:
                raise ValueError("advisor observed_behavior cites an unavailable fill_id")
        _reject_post_cutoff_dates(result)
    return result


def parse_advisor_response(
    raw: str,
    *,
    case: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Find one exact advisor object in a reasoning-on text response."""

    candidates = [raw]
    candidates.extend(
        match.group(1)
        for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    )
    decoder = json.JSONDecoder()
    last_validation_error: ValueError | None = None
    for text in candidates:
        stripped = text.strip()
        try:
            parsed = json.loads(stripped)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            try:
                return validate_advisor_output(parsed, case=case)
            except (TypeError, ValueError) as exc:
                last_validation_error = ValueError(str(exc))
        for match in re.finditer(r"\{", text):
            try:
                embedded, _ = decoder.raw_decode(text, match.start())
            except json.JSONDecodeError:
                continue
            if not isinstance(embedded, dict):
                continue
            try:
                return validate_advisor_output(embedded, case=case)
            except (TypeError, ValueError) as exc:
                last_validation_error = ValueError(str(exc))
                continue
    if last_validation_error is not None:
        raise last_validation_error
    raise ValueError("advisor response contains no object matching the sealed schema")


async def generate_advisor_output(
    case: Mapping[str, Any],
    *,
    client: OpenRouterClient,
    seed: int,
    agent_id: str,
) -> tuple[dict[str, Any], str]:
    case_index = validate_advisor_case(case)
    schema_fill_id = sorted(case_index["fill_ids"])[0]
    schema_hint = ADVISOR_SCHEMA_HINT.replace("fill_A001_t090", schema_fill_id)
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
                schema_hint=schema_hint,
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
            validated = parse_advisor_response(raw, case=case)
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
