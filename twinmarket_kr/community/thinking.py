from __future__ import annotations

import copy
import json
import re
from typing import Any

import config
from twinmarket_kr.community.validation import CommunityValidationError
from twinmarket_kr.llm.analysis import parse_json_loose
from twinmarket_kr.llm.belief import render_prompt
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
    build_validation_retry_prompt,
    record_validation_failure,
    retry_temperature_schedule,
)


async def community_thinking(
    agent: dict[str, Any],
    community_log: dict[str, Any],
    *,
    delivery_turn: int,
    delivery_date: str,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    client = client or OpenRouterClient()
    source_turn = int(community_log.get("turn") or 0)
    source_date = str(community_log.get("date") or "")
    agent_id = str(agent["agent_id"])
    best_posts = community_log.get("best_posts_seen") or []
    posts_read = community_log.get("posts_read") or []
    selected_by_id = {
        int(post["post_id"]): post
        for post in posts_read
        if post.get("post_id") is not None
    }
    best_post_ids = {
        int(post["post_id"])
        for post in best_posts
        if post.get("post_id") is not None
    }
    quotable_registry: dict[int, dict[str, Any]] = {}
    best_summary, best_sources = _format_best_posts(
        best_posts,
        selected_by_id=selected_by_id,
        agent_id=agent_id,
        source_turn=source_turn,
        source_date=source_date,
        delivery_turn=delivery_turn,
        quotable_registry=quotable_registry,
    )
    read_summary, read_sources = _format_posts_read(
        posts_read,
        excluded_post_ids=best_post_ids,
        agent_id=agent_id,
        source_turn=source_turn,
        source_date=source_date,
        delivery_turn=delivery_turn,
        quotable_registry=quotable_registry,
    )
    allowed_sources = {
        **best_sources,
        **read_sources,
    }
    # 선택 화면의 title-only 후보 노출은 exposure 분석 로그 전용이며,
    # EXPERIMENT_DESIGN.md 8.4절에 따라 next-AM claim 입력에서 제외한다.
    # 따라서 이 프롬프트에는 full_body 노출 두 종류만 넣는다.
    prompt = render_prompt(
        "community_thinking.txt",
        persona_prompt=agent.get("persona_prompt", ""),
        best_posts_summary=best_summary,
        posts_read_summary=read_summary,
    )
    current_prompt = prompt
    stage = "community_thinking"
    validation_attempts = 10
    temperatures = retry_temperature_schedule(0.3, validation_attempts)
    seeds = [
        stable_llm_seed(seed or 0, "community_thinking_validation", attempt)
        for attempt in range(1, validation_attempts + 1)
    ]
    max_tokens = int(INTEGRATED_STAGE_MAX_TOKENS_V1[stage])
    journal_call = open_journal_call(
        stage=stage,
        schema_version=INTEGRATED_STAGE_SCHEMA_VERSIONS_V1[stage],
        base_prompt=prompt,
        client=client,
        model=config.OPENROUTER_COMMUNITY_MODEL,
        temperature_schedule=temperatures,
        seed_schedule=seeds,
        max_tokens=max_tokens,
        validation_attempts=validation_attempts,
        validation_procedure_version="community-thinking-validator-v4",
        response_format={"type": "json_object"},
        semantic_inputs={
            "allowed_sources": dict(sorted(allowed_sources.items())),
            "quotable_registry": {
                str(ref): [
                    sorted(entry["exposure_ids"]),
                    entry["sentence"],
                ]
                for ref, entry in sorted(quotable_registry.items())
            },
        },
    )
    if journal_call is not None and journal_call.replay is not None:
        content = dict(journal_call.replay.response)
        _canonicalize_claim_source_ids(
            content,
            allowed_sources=allowed_sources,
            quotable_registry=quotable_registry,
        )
        errors = _community_thinking_errors(
            content,
            allowed_sources=allowed_sources,
            quotable_registry=quotable_registry,
        )
        if errors:
            raise ResponseJournalError(
                f"accepted replay no longer passes community thinking: {errors}"
            )
        journal_call.repair_replay_acceptance(client=client)
        content = materialize_claim_quotes(
            content, quotable_registry=quotable_registry
        )
        content["source_turn"] = source_turn
        content["source_date"] = source_date
        content["delivery_turn"] = int(delivery_turn)
        content["delivery_date"] = str(delivery_date)
        return content
    for attempt in range(1, validation_attempts + 1):
        attempt_seed = seeds[attempt - 1]
        if journal_call is not None:
            journal_call.begin(attempt)
        try:
            response = await client.chat(
                [{"role": "user", "content": current_prompt}],
                model=config.OPENROUTER_COMMUNITY_MODEL,
                temperature=temperatures[attempt - 1],
                response_format={"type": "json_object"},
                seed=attempt_seed,
                max_tokens=max_tokens,
                audit_label=stage,
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
        journal_response = copy.deepcopy(raw) if raw is not None else None
        content = copy.deepcopy(raw) if raw is not None else {}
        _canonicalize_claim_source_ids(
            content,
            allowed_sources=allowed_sources,
            quotable_registry=quotable_registry,
        )
        errors = [
            *parse_errors,
            *_community_thinking_errors(
                content,
                allowed_sources=allowed_sources,
                quotable_registry=quotable_registry,
            ),
        ]
        if not errors:
            if journal_call is not None:
                if raw is None:
                    raise AssertionError("validated response has no raw JSON object")
                if journal_response is None:
                    raise AssertionError("validated response has no journal JSON object")
                journal_call.accept(journal_response, attempt, client=client)
            content = materialize_claim_quotes(
                content, quotable_registry=quotable_registry
            )
            content["source_turn"] = source_turn
            content["source_date"] = source_date
            content["delivery_turn"] = int(delivery_turn)
            content["delivery_date"] = str(delivery_date)
            return content
        if journal_call is not None:
            journal_call.rejected(
                attempt,
                response=raw,
                errors=errors,
            )
        record_validation_failure(
            label=stage,
            attempt=attempt,
            errors=errors,
            raw_content=raw_content,
            seed=attempt_seed,
        )
        current_prompt = build_validation_retry_prompt(
            prompt,
            errors=errors,
            schema_hint=(
                "observed_sentiment, claims, agreement_disagreement, uncertainty만 "
                "가진 JSON object를 출력하세요. 각 claim의 supporting_quote_ref는 "
                "위 게시글에 [인용 N]으로 표시된 번호 중 하나의 정수입니다. "
                "문장을 직접 쓰지 말고 번호만 고르세요."
                " claim_stance는 반드시 bullish, bearish, neutral, uncertain 중"
                " 하나입니다. mixed, anxious, optimistic 같은 observed_sentiment의"
                " 값을 claim_stance에 쓰지 마세요. 근거가 엇갈리는 주장이면"
                " neutral 또는 uncertain을 쓰세요."
            ),
        )
    raise CommunityValidationError(
        f"community thinking did not satisfy the structured claim contract after {validation_attempts} attempts"
    )


def _canonicalize_claim_source_ids(
    content: dict[str, Any],
    *,
    allowed_sources: dict[str, str],
    quotable_registry: dict[int, dict[str, Any]],
) -> None:
    """Expand shortened post IDs to canonical recipient exposure relations."""

    allowed = tuple(sorted(allowed_sources))
    claims = content.get("claims")
    if not isinstance(claims, list):
        return
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        raw_ids = claim.get("source_exposure_ids")
        canonical: list[str] = []
        if isinstance(raw_ids, list):
            for raw_id in raw_ids:
                if not isinstance(raw_id, str) or not raw_id:
                    continue
                matches = (
                    [raw_id]
                    if raw_id in allowed_sources
                    else [item for item in allowed if item.startswith(raw_id + ":")]
                )
                for exposure_id in matches:
                    if exposure_id not in canonical:
                        canonical.append(exposure_id)
        ref = claim.get("supporting_quote_ref")
        if isinstance(ref, int) and not isinstance(ref, bool) and ref in quotable_registry:
            for exposure_id in quotable_registry[ref]["exposure_ids"]:
                exposure_id = str(exposure_id)
                if exposure_id not in canonical:
                    canonical.append(exposure_id)
        if canonical:
            claim["source_exposure_ids"] = canonical


# 인용을 자유 문자열 복사로 받으면 reasoning-off 경량 모델이 구조적으로
# 실패한다(공백 통일, 조사 변경, 짜깁기 — 세 번의 유료 실행 실패 전부 이
# 지점). 파이프라인의 다른 모든 근거(뉴스·검색·outcome)는 ID 인용 + 시스템
# 조회 패턴이라 이 실패가 없다. 커뮤니티 인용도 같은 패턴으로 맞춘다:
# 게시글을 문장 단위로 쪼개 [인용 N] 번호를 붙여 보여주고, 모델은 번호만
# 고르고, 시스템이 번호로 원문 문장을 꺼내 기록한다. 인용문은 구조상 항상
# verbatim이고, 모델의 원시 응답은 journal에 남아 연구자가 사후 분석할 수
# 있다.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n+")


def _split_sentences(text: str) -> list[str]:
    return [
        part.strip()
        for part in _SENTENCE_BOUNDARY.split(text.strip())
        if part.strip()
    ]


def _register_quotables(
    registry: dict[int, dict[str, Any]],
    *,
    exposure_ids: list[str],
    post_id: int,
    title: str,
    body: str,
) -> tuple[int | None, list[tuple[int, str]]]:
    """Assign [인용 N] numbers to a post's title and body sentences."""

    title_ref: int | None = None
    if title.strip():
        title_ref = len(registry) + 1
        registry[title_ref] = {
            "exposure_ids": list(exposure_ids),
            "post_id": post_id,
            "kind": "title",
            "sentence": title.strip(),
        }
    body_refs: list[tuple[int, str]] = []
    for sentence in _split_sentences(body):
        ref = len(registry) + 1
        registry[ref] = {
            "exposure_ids": list(exposure_ids),
            "post_id": post_id,
            "kind": "body",
            "sentence": sentence,
        }
        body_refs.append((ref, sentence))
    return title_ref, body_refs


def _render_marked_body(body_refs: list[tuple[int, str]]) -> str:
    return " ".join(f"[인용 {ref}] {sentence}" for ref, sentence in body_refs)


def _format_best_posts(
    best_posts: list[dict[str, Any]],
    *,
    selected_by_id: dict[int, dict[str, Any]] | None = None,
    agent_id: str,
    source_turn: int,
    source_date: str,
    delivery_turn: int,
    quotable_registry: dict[int, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, str]]:
    quotable_registry = (
        quotable_registry if quotable_registry is not None else {}
    )
    if not best_posts:
        return "(어제 Best 게시글 없음)", {}
    selected_by_id = selected_by_id or {}
    lines: list[str] = []
    sources: dict[str, str] = {}
    for post in best_posts:
        post_id = int(post["post_id"]) if post.get("post_id") is not None else -1
        exposure_id = (
            f"community:{source_date}:t{source_turn}:post:{post_id}:"
            f"best_full_body:{agent_id}:delivered_t{delivery_turn}"
        )
        selected = selected_by_id.get(post_id)
        reaction = (
            f" | 전날 직접 읽음·내 반응: {selected.get('reaction', 'none')}"
            if selected is not None
            else ""
        )
        profile = _format_author_profile(post.get("author_profile"))
        title = str(post.get("title") or "")
        body = str(post.get("content") or "")
        body_text = f"{title}\n{body}"
        sources[exposure_id] = body_text
        exposure_ids = [exposure_id]
        exposure_lines = [f"- source_exposure_id: {exposure_id}"]
        if selected is not None:
            # 같은 글이 Best와 직접 선택 두 관계에 모두 해당하면 본문은 여기서 한 번만
            # 직렬화하되, 두 노출 관계 ID를 모두 인용 가능한 근거로 남긴다.
            replay_exposure_id = (
                f"community:{source_date}:t{source_turn}:post:{post_id}:"
                f"selected_full_body_replay:{agent_id}:delivered_t{delivery_turn}"
            )
            sources[replay_exposure_id] = body_text
            exposure_ids.append(replay_exposure_id)
            exposure_lines.append(
                f"- source_exposure_id: {replay_exposure_id} (같은 글을 전날 직접 선택해 읽은 관계)"
            )
        title_ref, body_refs = _register_quotables(
            quotable_registry,
            exposure_ids=exposure_ids,
            post_id=post_id,
            title=title,
            body=body,
        )
        title_marker = f"[인용 {title_ref}] " if title_ref is not None else ""
        lines.append(
            "\n".join(exposure_lines) + "\n"
            f"  Best {post.get('rank', '')} [{post.get('post_type', '')}] "
            f"{title_marker}{title} (score: {post.get('score', 0)})\n"
            f"  작성자: {post.get('anonymous_code', '')}{reaction}\n"
            f"  본문 전체: {_render_marked_body(body_refs)}{profile}"
        )
    return "\n\n".join(lines), sources


def _format_posts_read(
    posts_read: list[dict[str, Any]],
    *,
    excluded_post_ids: set[int] | None = None,
    agent_id: str,
    source_turn: int,
    source_date: str,
    delivery_turn: int,
    quotable_registry: dict[int, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, str]]:
    quotable_registry = (
        quotable_registry if quotable_registry is not None else {}
    )
    excluded_post_ids = excluded_post_ids or set()
    visible_posts = [
        post
        for post in posts_read
        if post.get("post_id") is None or int(post["post_id"]) not in excluded_post_ids
    ]
    if not visible_posts:
        return "(어제 직접 읽은 게시글 없음)", {}
    lines: list[str] = []
    sources: dict[str, str] = {}
    for post in visible_posts:
        post_id = int(post["post_id"])
        exposure_id = (
            f"community:{source_date}:t{source_turn}:post:{post_id}:"
            f"selected_full_body_replay:{agent_id}:delivered_t{delivery_turn}"
        )
        profile = _format_author_profile(post.get("author_profile"))
        title = str(post.get("title") or "")
        body = str(post.get("content") or "")
        sources[exposure_id] = f"{title}\n{body}"
        title_ref, body_refs = _register_quotables(
            quotable_registry,
            exposure_ids=[exposure_id],
            post_id=post_id,
            title=title,
            body=body,
        )
        title_marker = f"[인용 {title_ref}] " if title_ref is not None else ""
        lines.append(
            f"- source_exposure_id: {exposure_id}\n"
            f"  [{post.get('post_type', '')}] {title_marker}{title} | "
            f"내 반응: {post.get('reaction', 'none')}\n"
            f"  작성자: {post.get('anonymous_code', '')}\n"
            f"  본문 전체: {_render_marked_body(body_refs)}{profile}"
        )
    return "\n\n".join(lines), sources


def materialize_claim_quotes(
    content: dict[str, Any],
    *,
    quotable_registry: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Resolve each claim's [인용 N] reference into the verbatim source sentence.

    ``supporting_quote``는 모델 출력이 아니라 시스템이 번호로 조회한 원문
    문장이므로 구조상 항상 verbatim이다. 모델이 인용 번호의 글과 다른 글을
    출처로 적었으면 번호가 가리키는 실제 노출 ID를 출처 목록에 보충한다.
    모델의 원시 응답은 journal에 그대로 남아 연구자가 대조할 수 있다.
    """

    for claim in content.get("claims") or []:
        entry = quotable_registry[int(claim["supporting_quote_ref"])]
        claim["supporting_quote"] = str(entry["sentence"])
        source_ids = claim["source_exposure_ids"]
        if not any(
            exposure_id in source_ids for exposure_id in entry["exposure_ids"]
        ):
            source_ids.append(str(entry["exposure_ids"][0]))
    return content


def _community_thinking_errors(
    value: dict[str, Any],
    *,
    allowed_sources: dict[str, str],
    quotable_registry: dict[int, dict[str, Any]],
) -> list[str]:
    required = {
        "observed_sentiment",
        "claims",
        "agreement_disagreement",
        "uncertainty",
    }
    errors: list[str] = []
    if set(value) != required:
        errors.append(
            "top_level_keys:"
            f"missing={sorted(required - set(value))}:"
            f"unknown={sorted(set(value) - required)}"
        )
    if value.get("observed_sentiment") not in {
        "optimistic",
        "pessimistic",
        "mixed",
        "overheated",
        "anxious",
        "uncertain",
    }:
        errors.append("observed_sentiment:invalid")
    for field in ("agreement_disagreement", "uncertainty"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            errors.append(f"{field}:requires_nonempty_string")
    claims = value.get("claims")
    if not isinstance(claims, list):
        return [*errors, "claims:requires_array"]
    for index, claim in enumerate(claims):
        expected = {
            "claim_text",
            "claim_stance",
            "source_exposure_ids",
            "supporting_quote_ref",
        }
        if not isinstance(claim, dict) or set(claim) != expected:
            errors.append(f"claims[{index}]:invalid_schema")
            continue
        if not isinstance(claim["claim_text"], str) or not claim["claim_text"].strip():
            errors.append(f"claims[{index}].claim_text:requires_nonempty_string")
        if claim["claim_stance"] not in {
            "bullish",
            "bearish",
            "neutral",
            "uncertain",
        }:
            # 모델이 observed_sentiment의 어휘("mixed", "anxious" 등)를 인접한
            # claim_stance에 그대로 옮겨 쓰는 실수를 라이브에서 반복했다.
            # 실제로 받은 값과 허용 목록을 error 문자열 자체에 남겨, 재시도가
            # schema_hint 없이도 무엇이 잘못됐는지 바로 알 수 있게 한다.
            errors.append(
                f"claims[{index}].claim_stance:invalid:"
                f"got={claim['claim_stance']!r}:"
                "allowed=['bullish','bearish','neutral','uncertain']"
            )
        source_ids = claim["source_exposure_ids"]
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(item, str) or not item for item in source_ids)
            or len(source_ids) != len(set(source_ids))
        ):
            errors.append(
                f"claims[{index}].source_exposure_ids:requires_unique_nonempty_array"
            )
            continue
        unknown = set(source_ids) - set(allowed_sources)
        if unknown:
            errors.append(
                f"claims[{index}].source_exposure_ids:unknown:{sorted(unknown)}"
            )
        ref = claim["supporting_quote_ref"]
        if isinstance(ref, bool) or not isinstance(ref, int):
            errors.append(
                f"claims[{index}].supporting_quote_ref:requires_integer"
            )
        elif ref not in quotable_registry:
            errors.append(
                f"claims[{index}].supporting_quote_ref:unknown_ref:{ref}"
            )
    return errors


def _format_author_profile(profile: Any) -> str:
    if not isinstance(profile, dict) or not profile:
        return ""
    return "\n  동결된 공개 프로필: " + json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
