from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ADVISOR_ASSIGNMENT_NAMESPACE = "advisor-full-cohort-v1"
ADVISOR_ASSIGNMENT_SEED = 20260919
ADVISOR_COHORT_SIZE = 100
ADVISOR_MESSAGE_PREFIX = "당신의 담당 투자 어드바이저가 보낸 메모:"
ADVISOR_BODY_MAX_CHARS = 500
ADVISOR_ARTIFACT_TYPE = "integrated_advisor_messages_v1"
ADVISOR_MODEL = "qwen/qwen3.5-flash-02-23"
ADVISOR_PROVIDER = "alibaba"
ADVISOR_REASONING_POLICY = {"enabled": True, "exclude": False}
INTERACTIVE_ADVISOR_MODEL = "Codex (interactive authored advice)"
INTERACTIVE_ADVISOR_PROVIDER = "OpenAI"
CODEX_ADVISOR_MODEL = "Codex"
CODEX_ADVISOR_PROVIDER = "OpenAI"
GENERAL_ADVISOR_MODEL = "User (provided general advice)"
GENERAL_ADVISOR_PROVIDER = "manual"
ADVISOR_FORBIDDEN_BODY_PHRASES = (
    "매수하세요",
    "매도하세요",
    "사세요",
    "파세요",
    "상승할 것입니다",
    "하락할 것입니다",
    "오를 것입니다",
    "내릴 것입니다",
)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_advisor_agents(
    agent_ids: Iterable[str],
) -> tuple[str, ...]:
    """Return every sealed cohort member in deterministic agent-ID order."""

    ordered = sorted(str(value).strip() for value in agent_ids)
    if not ordered or any(not value for value in ordered):
        raise ValueError("advisor assignment requires non-empty agent IDs")
    if len(ordered) != len(set(ordered)):
        raise ValueError("advisor assignment agent IDs must be unique")
    if len(ordered) != ADVISOR_COHORT_SIZE:
        raise ValueError(
            f"advisor assignment requires the full {ADVISOR_COHORT_SIZE}-agent cohort"
        )
    return tuple(ordered)


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    rows = tuple(_nonempty_text(item, label) for item in value)
    return rows


def validate_advice_body(value: Any) -> str:
    body = _nonempty_text(value, "message_body")
    if len(body) > ADVISOR_BODY_MAX_CHARS:
        raise ValueError("advisor message body exceeds 500 characters")
    matched = [phrase for phrase in ADVISOR_FORBIDDEN_BODY_PHRASES if phrase in body]
    if matched:
        raise ValueError(
            f"advisor message contains forbidden direction/prediction: {matched}"
        )
    return body


@dataclass(frozen=True)
class AdvisorMessage:
    advisor_id: str
    agent_id: str
    source_run_id: str
    source_cutoff_event_id: str
    source_state_sha256: str
    advisor_prompt_sha256: str | None
    generation_seed: int | None
    persona_basis: tuple[str, ...]
    observed_behavior: tuple[str, ...]
    persona_behavior_assessment: str
    performance_context: str
    message_body: str
    message_body_sha256: str
    valid_from_event_id: str

    @property
    def rendered_message(self) -> str:
        return f"{ADVISOR_MESSAGE_PREFIX} {self.message_body}"

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        interactive_authored: bool = False,
        general_advice: bool = False,
    ) -> "AdvisorMessage":
        body = validate_advice_body(raw.get("message_body"))
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if raw.get("message_body_sha256") != body_sha256:
            raise ValueError("advisor message body hash differs")
        state_hash = _nonempty_text(raw.get("source_state_sha256"), "source_state_sha256")
        prompt_hash = raw.get("advisor_prompt_sha256")
        generation_seed = raw.get("generation_seed")
        if interactive_authored:
            if prompt_hash is not None or generation_seed is not None:
                raise ValueError("interactive advisor must not claim a generation prompt or seed")
        else:
            prompt_hash = _nonempty_text(prompt_hash, "advisor_prompt_sha256")
            generation_seed = int(generation_seed)
        hashes = [("source_state_sha256", state_hash)]
        if prompt_hash is not None:
            hashes.append(("advisor_prompt_sha256", prompt_hash))
        for label, value in hashes:
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{label} must be a lowercase SHA-256")
        return cls(
            advisor_id=_nonempty_text(raw.get("advisor_id"), "advisor_id"),
            agent_id=_nonempty_text(raw.get("agent_id"), "agent_id"),
            source_run_id=_nonempty_text(raw.get("source_run_id"), "source_run_id"),
            source_cutoff_event_id=_nonempty_text(raw.get("source_cutoff_event_id"), "source_cutoff_event_id"),
            source_state_sha256=state_hash,
            advisor_prompt_sha256=prompt_hash,
            generation_seed=generation_seed,
            persona_basis=(
                () if general_advice and raw.get("persona_basis") == []
                else _string_list(raw.get("persona_basis"), "persona_basis")
            ),
            observed_behavior=(
                () if general_advice and raw.get("observed_behavior") == []
                else _string_list(raw.get("observed_behavior"), "observed_behavior")
            ),
            persona_behavior_assessment=(
                "" if general_advice and raw.get("persona_behavior_assessment") == ""
                else _nonempty_text(raw.get("persona_behavior_assessment"), "persona_behavior_assessment")
            ),
            performance_context=(
                "" if general_advice and raw.get("performance_context") == ""
                else _nonempty_text(raw.get("performance_context"), "performance_context")
            ),
            message_body=body,
            message_body_sha256=body_sha256,
            valid_from_event_id=_nonempty_text(raw.get("valid_from_event_id"), "valid_from_event_id"),
        )


@dataclass(frozen=True)
class AdvisorArtifact:
    assignment_namespace: str
    assignment_seed: int
    model: str
    provider: str
    reasoning_policy: Mapping[str, Any] | None
    messages: tuple[AdvisorMessage, ...]
    artifact_sha256: str
    source_cases_sha256: str | None = None
    advice_kind: str = "personalized"

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(message.agent_id for message in self.messages)

    def message_for(self, agent_id: str, event_id: str) -> AdvisorMessage | None:
        for message in self.messages:
            if message.agent_id == agent_id and event_id >= message.valid_from_event_id:
                return message
        return None

    @classmethod
    def load(
        cls,
        path: Path | str,
        *,
        cohort_agent_ids: Sequence[str],
    ) -> "AdvisorArtifact":
        artifact_path = Path(path)
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("artifact_type") != ADVISOR_ARTIFACT_TYPE:
            raise ValueError("unknown advisor artifact type")
        claimed_hash = raw.get("artifact_sha256")
        payload = {key: value for key, value in raw.items() if key != "artifact_sha256"}
        actual_hash = canonical_sha256(payload)
        if claimed_hash != actual_hash:
            raise ValueError("advisor artifact hash differs")
        if raw.get("assignment_namespace") != ADVISOR_ASSIGNMENT_NAMESPACE:
            raise ValueError("advisor assignment namespace differs")
        if int(raw.get("assignment_seed")) != ADVISOR_ASSIGNMENT_SEED:
            raise ValueError("advisor assignment seed differs")
        general_advice = raw.get("advice_kind") == "general"
        if raw.get("advice_kind", "personalized") not in {"personalized", "general"}:
            raise ValueError("unknown advisor advice_kind")
        interactive_authored = (
            raw.get("model") == INTERACTIVE_ADVISOR_MODEL
            and raw.get("provider") == INTERACTIVE_ADVISOR_PROVIDER
        )
        codex_generated = (
            raw.get("model") == CODEX_ADVISOR_MODEL
            and raw.get("provider") == CODEX_ADVISOR_PROVIDER
        )
        user_general = (
            raw.get("model") == GENERAL_ADVISOR_MODEL
            and raw.get("provider") == GENERAL_ADVISOR_PROVIDER
        )
        if general_advice != user_general:
            raise ValueError("general advisor model/provider differs from advice_kind")
        authored = interactive_authored or user_general
        if authored:
            if raw.get("reasoning_policy") is not None:
                raise ValueError("authored advisor must not claim a generation reasoning policy")
        elif codex_generated:
            if raw.get("reasoning_policy") != ADVISOR_REASONING_POLICY:
                raise ValueError("Codex advisor reasoning policy differs")
        if authored or codex_generated:
            source_cases_hash = _nonempty_text(
                raw.get("source_cases_sha256"), "source_cases_sha256"
            )
            if len(source_cases_hash) != 64 or any(
                character not in "0123456789abcdef" for character in source_cases_hash
            ):
                raise ValueError("source_cases_sha256 must be a lowercase SHA-256")
        else:
            source_cases_hash = raw.get("source_cases_sha256")
            if raw.get("model") != ADVISOR_MODEL or raw.get("provider") != ADVISOR_PROVIDER:
                raise ValueError("advisor model/provider differs")
            if raw.get("reasoning_policy") != ADVISOR_REASONING_POLICY:
                raise ValueError("advisor reasoning policy differs")
        values = raw.get("messages")
        expected_ids = deterministic_advisor_agents(cohort_agent_ids)
        if not isinstance(values, list) or len(values) != len(expected_ids):
            raise ValueError(
                f"advisor artifact must contain exactly {len(expected_ids)} messages"
            )
        messages = tuple(
            AdvisorMessage.from_mapping(
                value,
                interactive_authored=authored,
                general_advice=general_advice,
            )
            for value in values
        )
        ids = tuple(message.agent_id for message in messages)
        if len(ids) != len(set(ids)):
            raise ValueError("advisor artifact contains duplicate agents")
        if tuple(sorted(ids)) != expected_ids:
            raise ValueError("advisor artifact agents differ from the full cohort")
        if authored and len({message.source_run_id for message in messages}) != 1:
            raise ValueError("authored advisor messages must share one source run")
        if general_advice:
            if len({message.message_body for message in messages}) != 1:
                raise ValueError("general advisor must deliver identical advice to all agents")
            if any(
                message.persona_basis or message.observed_behavior
                or message.persona_behavior_assessment or message.performance_context
                for message in messages
            ):
                raise ValueError("general advisor must not contain personalized assessments")
        if any(message.source_cutoff_event_id != "2026-05-04/PM" for message in messages):
            raise ValueError("advisor source cutoff must be 2026-05-04/PM")
        if any(message.valid_from_event_id != "2026-05-06/AM" for message in messages):
            raise ValueError("advisor valid-from event must be 2026-05-06/AM")
        return cls(
            assignment_namespace=ADVISOR_ASSIGNMENT_NAMESPACE,
            assignment_seed=ADVISOR_ASSIGNMENT_SEED,
            model=str(raw["model"]),
            provider=str(raw["provider"]),
            reasoning_policy=(
                None if authored else dict(ADVISOR_REASONING_POLICY)
            ),
            messages=messages,
            artifact_sha256=actual_hash,
            source_cases_sha256=source_cases_hash,
            advice_kind="general" if general_advice else "personalized",
        )

    def ensure_messages_installed(self, db_path: Path | str) -> None:
        """Install the sealed treatment once at turn 90, visible from turn 91."""

        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                """
                SELECT agent_id, turn, date, message
                FROM agent_system_messages
                WHERE message_type='advisor'
                ORDER BY agent_id
                """
            ).fetchall()
            expected = sorted(
                (message.agent_id, 90, "2026-05-04", message.rendered_message)
                for message in self.messages
            )
            if rows:
                observed = sorted(tuple(row) for row in rows)
                if observed != expected:
                    raise ValueError("runtime DB advisor messages differ from the sealed artifact")
                return
            connection.executemany(
                """
                INSERT INTO agent_system_messages(agent_id, turn, date, message_type, message)
                VALUES (?, 90, '2026-05-04', 'advisor', ?)
                """,
                [
                    (message.agent_id, message.rendered_message)
                    for message in self.messages
                ],
            )
            connection.commit()
