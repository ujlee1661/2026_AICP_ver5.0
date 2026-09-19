from __future__ import annotations

import json
from typing import Any, Callable

from twinmarket_kr.agents.news_agent import (
    DEPTH2_MAX_KEYWORDS,
    DEPTH2_MAX_SEARCH_ARTICLES,
)
from twinmarket_kr.llm.belief import BELIEF_DIMENSION_KEYS, belief_dimensions, render_prompt
from twinmarket_kr.llm.call_policy import (
    INTEGRATED_STAGE_MAX_TOKENS_V1,
    INTEGRATED_STAGE_SCHEMA_VERSIONS_V1,
)
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed
from twinmarket_kr.llm.response_journal import (
    ResponseJournalError,
    open_journal_call,
    parse_strict_json_object,
)
from twinmarket_kr.llm.validation import (
    LLMValidationError,
    build_validation_retry_prompt,
    normalize_string_list,
    record_validation_failure,
    retry_temperature_schedule,
    valid_string_list,
)


NEWS_INTERPRETATION_KEYS = (
    "selected_news",
    "news_sentiment",
    "short_term_impact",
    "long_term_impact",
    "persona_interpretation",
    "confidence",
    "reason",
)

MARKET_ANALYSIS_KEYS = (
    "market_view",
    "valuation_view",
    "technical_view",
    "news_view",
    "portfolio_view",
    "key_risks",
    "opportunity",
    "caution",
    "confidence",
    "directional_stance",
    "evidence_references",
)

DEPTH2_PRE_SEARCH_KEYS = (
    "key_findings",
    "curiosity_points",
    "search_rationale",
    "search_keywords",
)

DEPTH2_POST_SEARCH_KEYS = (
    "new_findings",
    "view_change",
    "view_change_detail",
    "unresolved_questions",
)


class AnalysisValidationError(LLMValidationError):
    pass


DEPTH2_PRE_SEARCH_SCHEMA = """
{
  "key_findings": ["비어 있지 않은 문자열", "..."],
  "curiosity_points": ["비어 있지 않은 문자열", "..."],
  "search_rationale": "비어 있지 않은 문자열",
  "search_keywords": ["0~5개의 검색 키워드", "...", "..."]
}
"""

DEPTH2_POST_SEARCH_SCHEMA = """
{
  "new_findings": ["새로 확인한 내용"],
  "view_change": "강화 또는 수정 또는 반전 또는 유지",
  "view_change_detail": "비어 있지 않은 문자열",
  "unresolved_questions": ["아직 불명확한 쟁점"]
}
새로운 내용이나 미해결 쟁점이 없으면 해당 배열은 []로 출력할 수 있습니다.
"""


def _nonempty_text_or_string_list(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value) and isinstance(value, list) and all(
        isinstance(item, str) and item.strip() for item in value
    )


def parse_json_object(content: str, required_keys: tuple[str, ...], label: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    missing = [key for key in required_keys if key not in data]
    if missing:
        raise ValueError(f"{label} JSON missing keys: {missing}")
    return data


def parse_json_loose(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def _required_json_response(
    *,
    client: OpenRouterClient,
    prompt: str,
    required_keys: tuple[str, ...],
    label: str,
    seed: int | None,
    validation_attempts: int = 10,
    validator: Callable[[dict[str, Any]], list[str]] | None = None,
    normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    schema_hint: str = "요청된 모든 JSON 키와 자료형을 정확히 지키세요.",
    validation_procedure_version: str | None = None,
) -> dict[str, Any]:
    if validation_attempts < 1:
        raise ValueError("validation_attempts must be at least 1")
    invalid_history: list[list[str]] = []
    current_prompt = prompt
    temperatures = retry_temperature_schedule(0.2, validation_attempts)
    seeds = [
        stable_llm_seed(seed or 0, label, attempt)
        for attempt in range(1, validation_attempts + 1)
    ]
    max_tokens = int(INTEGRATED_STAGE_MAX_TOKENS_V1[label])
    journal_call = open_journal_call(
        stage=label,
        schema_version=INTEGRATED_STAGE_SCHEMA_VERSIONS_V1[label],
        base_prompt=prompt,
        client=client,
        temperature_schedule=temperatures,
        seed_schedule=seeds,
        max_tokens=max_tokens,
        validation_attempts=validation_attempts,
        validation_procedure_version=(
            validation_procedure_version or f"{label}-validator-v1"
        ),
        response_format={"type": "json_object"},
        semantic_inputs={
            "required_keys": list(required_keys),
            "schema_hint": schema_hint,
        },
    )
    if journal_call is not None and journal_call.replay is not None:
        raw = dict(journal_call.replay.response)
        data = normalizer(dict(raw)) if normalizer is not None else dict(raw)
        missing = [key for key in required_keys if key not in data]
        validation_errors = (
            validator(data)
            if not missing and validator is not None
            else []
        )
        if missing or validation_errors:
            raise ResponseJournalError(
                f"accepted replay no longer passes {label} validator: "
                f"{[*[f'missing:{key}' for key in missing], *validation_errors]}"
            )
        journal_call.repair_replay_acceptance(client=client)
        data["generation_attempts"] = journal_call.replay.validation_attempt
        return data
    for attempt in range(1, validation_attempts + 1):
        attempt_seed = seeds[attempt - 1]
        if journal_call is not None:
            journal_call.begin(attempt)
        try:
            response = await client.chat(
                [{"role": "user", "content": current_prompt}],
                response_format={"type": "json_object"},
                temperature=temperatures[attempt - 1],
                seed=attempt_seed,
                max_tokens=max_tokens,
                audit_label=label,
                logical_call_id=(
                    journal_call.logical_call_id
                    if journal_call is not None
                    else None
                ),
                phase_attempt_id=(
                    journal_call.phase_attempt_id
                    if journal_call is not None
                    else None
                ),
            )
        except BaseException as exc:
            if journal_call is not None:
                journal_call.error(attempt, exc)
            raise
        raw_content = response_content(response) or "{}"
        parse_errors: list[str] = []
        raw: dict[str, Any] | None
        try:
            raw = (
                parse_strict_json_object(raw_content)
                if journal_call is not None
                else parse_json_loose(raw_content)
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raw = None
            parse_errors.append(f"strict_json_object:{exc}")
        data = dict(raw or {})
        if normalizer is not None:
            data = normalizer(data)
        missing = [key for key in required_keys if key not in data]
        validation_errors = validator(data) if not missing and validator is not None else []
        if not parse_errors and not missing and not validation_errors:
            if journal_call is not None:
                if raw is None:
                    raise AssertionError("validated response has no raw JSON object")
                journal_call.accept(raw, attempt, client=client)
            data["generation_attempts"] = attempt
            return data
        errors = [
            *parse_errors,
            *[f"missing:{key}" for key in missing],
            *validation_errors,
        ]
        if journal_call is not None:
            journal_call.rejected(
                attempt,
                response=raw,
                errors=errors,
            )
        invalid_history.append(errors)
        record_validation_failure(
            label=label,
            attempt=attempt,
            errors=errors,
            raw_content=raw_content,
            seed=attempt_seed,
        )
        current_prompt = build_validation_retry_prompt(
            prompt,
            errors=errors,
            schema_hint=schema_hint,
        )
    raise AnalysisValidationError(
        f"{label} did not produce all required JSON keys after "
        f"{validation_attempts} attempts: {invalid_history}"
    )


def normalize_depth2_pre_search(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    for key in ("key_findings", "curiosity_points", "search_keywords"):
        if key in normalized:
            normalized[key] = normalize_string_list(normalized[key])
    if isinstance(normalized.get("search_keywords"), list) and all(
        isinstance(item, str) for item in normalized["search_keywords"]
    ):
        normalized["search_keywords"] = normalized["search_keywords"][:DEPTH2_MAX_KEYWORDS]
    return normalized


def validate_depth2_pre_search(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("key_findings", "curiosity_points"):
        if not valid_string_list(value.get(key), allow_empty=False):
            errors.append(f"{key}:requires_nonempty_string_list")
    if not isinstance(value.get("search_rationale"), str) or not value[
        "search_rationale"
    ].strip():
        errors.append("search_rationale:requires_nonempty_string")
    keywords = value.get("search_keywords")
    # 0개는 "이번 턴에는 추가 검색을 하지 않겠다"는 유효한 판단이다.
    if not valid_string_list(keywords, allow_empty=True) or len(keywords) > DEPTH2_MAX_KEYWORDS:
        errors.append(f"search_keywords:requires_0_to_{DEPTH2_MAX_KEYWORDS}_strings")
    return errors


def normalize_depth2_post_search(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    for key in ("new_findings", "unresolved_questions"):
        if key in normalized:
            normalized[key] = normalize_string_list(normalized[key])
    return normalized


def validate_depth2_post_search(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not valid_string_list(value.get("new_findings"), allow_empty=True):
        errors.append("new_findings:requires_string_list")
    if value.get("view_change") not in {"강화", "수정", "반전", "유지"}:
        errors.append("view_change:invalid")
    if not isinstance(value.get("view_change_detail"), str) or not value[
        "view_change_detail"
    ].strip():
        errors.append("view_change_detail:requires_nonempty_string")
    if not valid_string_list(value.get("unresolved_questions"), allow_empty=True):
        errors.append("unresolved_questions:requires_string_list")
    return errors


async def interpret_news(
    agent: dict[str, Any],
    news_context: dict[str, Any],
    *,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    prompt = render_prompt(
        "news_interpretation.txt",
        persona_prompt=agent["persona_prompt"],
        news_context=json.dumps(news_context, ensure_ascii=False, indent=2),
    )
    data = await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=NEWS_INTERPRETATION_KEYS,
        label="news_interpretation",
        seed=seed,
        validator=lambda value: [
            *([] if isinstance(value.get("selected_news"), list) else ["selected_news:not_list"]),
            *(
                []
                if value.get("news_sentiment")
                in {"positive", "negative", "neutral", "mixed", "insufficient"}
                else ["news_sentiment:invalid"]
            ),
            *(
                []
                if value.get("confidence") in {"high", "medium", "low"}
                else ["confidence:invalid"]
            ),
            *[
                f"{key}:empty"
                for key in ("short_term_impact", "long_term_impact", "persona_interpretation", "reason")
                if not isinstance(value.get(key), str) or not value[key].strip()
            ],
        ],
    )
    return data


async def depth2_pre_search(
    agent: dict[str, Any],
    base_news_context: dict[str, Any],
    *,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    prompt = render_prompt(
        "news_agent_pre_search.txt",
        persona_prompt=agent["persona_prompt"],
        base_news_context=json.dumps(base_news_context, ensure_ascii=False, indent=2),
    )
    data = await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=DEPTH2_PRE_SEARCH_KEYS,
        label="depth2_pre_search",
        seed=seed,
        normalizer=normalize_depth2_pre_search,
        validator=validate_depth2_pre_search,
        schema_hint=DEPTH2_PRE_SEARCH_SCHEMA,
    )
    return data


async def depth2_post_search(
    agent: dict[str, Any],
    base_news_context: dict[str, Any],
    search_results: list[dict[str, Any]],
    pre_thinking: dict[str, Any],
    *,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    prompt = render_prompt(
        "news_agent_post_search.txt",
        persona_prompt=agent["persona_prompt"],
        base_news_context=json.dumps(base_news_context, ensure_ascii=False, indent=2),
        search_results=json.dumps(search_results, ensure_ascii=False, indent=2),
        pre_search_thinking=json.dumps(pre_thinking, ensure_ascii=False, indent=2),
    )
    data = await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=DEPTH2_POST_SEARCH_KEYS,
        label="depth2_post_search",
        seed=seed,
        normalizer=normalize_depth2_post_search,
        validator=validate_depth2_post_search,
        schema_hint=DEPTH2_POST_SEARCH_SCHEMA,
    )
    return data


async def analyze_market(
    agent: dict[str, Any],
    *,
    previous_ltb: dict[str, Any],
    current_stb: dict[str, Any],
    market_features: dict[str, Any],
    portfolio_summary: str,
    execution_state: dict[str, Any],
    advisor_note: str | None = None,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    previous_dimensions = belief_dimensions(
        previous_ltb.get("dimensions", previous_ltb),
        label="analysis.previous_ltb",
    )
    current_dimensions = belief_dimensions(
        current_stb.get("dimensions", current_stb),
        label="analysis.current_stb",
    )
    # 이름 있는 슬롯 하나만으로 입력을 전달한다. 예전에는 같은 내용을
    # <<STAGE_PAYLOAD_JSON>>으로 한 번 더 실어 보내 프롬프트의 약 1/3이
    # 동일 입력의 사본이었다. evidence_references가 참조해야 하는 source
    # 이름(previous_ltb/current_stb/market/execution_state)은 이제 payload
    # key가 아니라 프롬프트 본문이 명시한다.
    prompt = render_prompt(
        "market_analysis.txt",
        persona_prompt=agent["persona_prompt"],
        today_belief=json.dumps(
            {
                "previous_ltb": previous_dimensions,
                "current_stb": current_dimensions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        market_features=json.dumps(market_features, ensure_ascii=False, indent=2),
        execution_state=json.dumps(execution_state, ensure_ascii=False, indent=2),
        portfolio_summary=portfolio_summary,
        advisor_note_block=(
            "\n담당 투자 어드바이저의 행동 규율 메모:\n" + advisor_note + "\n"
            if advisor_note
            else ""
        ),
    )
    allowed_reference_fields = {
        "previous_ltb": set(BELIEF_DIMENSION_KEYS),
        "current_stb": set(BELIEF_DIMENSION_KEYS),
        "market": set(market_features),
        "execution_state": set(execution_state),
    }

    def validate_analysis(value: dict[str, Any]) -> list[str]:
        # 프롬프트가 "위 필수 key만 가진 JSON object"를 요구하므로 STB/LTB·decision
        # 단계와 같은 강도로 extra key를 거부한다. 검증을 통과한 뒤에야 호출부가
        # generation_attempts를 덧붙이므로 이 시점의 value에는 추가되지 않는다.
        unknown_keys = sorted(set(value) - set(MARKET_ANALYSIS_KEYS))
        errors = [
            *(
                [f"top_level_keys:unknown={unknown_keys}"]
                if unknown_keys
                else []
            ),
            *(
                []
                if value.get("confidence") in {"high", "medium", "low"}
                else ["confidence:invalid"]
            ),
            *(
                []
                if value.get("directional_stance") in {"buy", "sell", "uncertain"}
                else ["directional_stance:invalid"]
            ),
            *[
                f"{key}:empty"
                for key in (
                    "market_view",
                    "valuation_view",
                    "technical_view",
                    "news_view",
                    "portfolio_view",
                )
                if not isinstance(value.get(key), str) or not value[key].strip()
            ],
            *[
                f"{key}:requires_nonempty_text_or_string_list"
                for key in ("key_risks", "opportunity", "caution")
                if not _nonempty_text_or_string_list(value.get(key))
            ],
        ]
        references = value.get("evidence_references")
        if not isinstance(references, list):
            return [*errors, "evidence_references:requires_array"]
        observed_sources: set[str] = set()
        observed_pairs: set[tuple[str, str]] = set()
        for index, reference in enumerate(references):
            if not isinstance(reference, dict) or set(reference) != {"source", "field"}:
                errors.append(f"evidence_references[{index}]:invalid_schema")
                continue
            source = str(reference.get("source") or "")
            field = str(reference.get("field") or "")
            if source not in allowed_reference_fields:
                errors.append(f"evidence_references[{index}]:invalid_source")
                continue
            if field not in allowed_reference_fields[source]:
                errors.append(f"evidence_references[{index}]:invalid_field")
            pair = (source, field)
            if pair in observed_pairs:
                errors.append(f"evidence_references[{index}]:duplicate")
            observed_pairs.add(pair)
            observed_sources.add(source)
        missing_sources = set(allowed_reference_fields) - observed_sources
        if missing_sources:
            errors.append(
                "evidence_references:missing_sources:"
                + ",".join(sorted(missing_sources))
            )
        return errors

    return await _required_json_response(
        client=client,
        prompt=prompt,
        required_keys=MARKET_ANALYSIS_KEYS,
        label="market_analysis",
        seed=seed,
        validator=validate_analysis,
        validation_procedure_version="market_analysis-validator-v2",
    )
