from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ADVISOR_ASSIGNMENT_NAMESPACE = "advisor-assignment-v1"
ADVISOR_ASSIGNMENT_SEED = 20260919
ADVISOR_MESSAGE_PREFIX = "당신의 담당 투자 어드바이저가 보낸 메모:"
ADVISOR_BODY_MAX_CHARS = 500
ADVISOR_ARTIFACT_TYPE = "integrated_advisor_messages_v1"
ADVISOR_MODEL = "qwen/qwen3.5-flash-02-23"
ADVISOR_PROVIDER = "alibaba"
ADVISOR_REASONING_POLICY = {"enabled": True, "exclude": False}
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
    *,
    count: int = 10,
    seed: int = ADVISOR_ASSIGNMENT_SEED,
    namespace: str = ADVISOR_ASSIGNMENT_NAMESPACE,
) -> tuple[str, ...]:
    ordered = sorted(str(value).strip() for value in agent_ids)
    if not namespace.strip():
        raise ValueError("advisor assignment namespace must not be empty")
    if not ordered or any(not value for value in ordered):
        raise ValueError("advisor assignment requires non-empty agent IDs")
    if len(ordered) != len(set(ordered)):
        raise ValueError("advisor assignment agent IDs must be unique")
    if isinstance(count, bool) or count < 1 or count > len(ordered):
        raise ValueError("advisor assignment count is outside the cohort")
    derived_seed = int.from_bytes(
        hashlib.sha256(f"{namespace}|{int(seed)}".encode("utf-8")).digest()[:8],
        "big",
    )
    return tuple(sorted(random.Random(derived_seed).sample(ordered, count)))


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
    advisor_prompt_sha256: str
    generation_seed: int
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
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AdvisorMessage":
        body = validate_advice_body(raw.get("message_body"))
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if raw.get("message_body_sha256") != body_sha256:
            raise ValueError("advisor message body hash differs")
        state_hash = _nonempty_text(raw.get("source_state_sha256"), "source_state_sha256")
        prompt_hash = _nonempty_text(raw.get("advisor_prompt_sha256"), "advisor_prompt_sha256")
        for label, value in (("source_state_sha256", state_hash), ("advisor_prompt_sha256", prompt_hash)):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{label} must be a lowercase SHA-256")
        return cls(
            advisor_id=_nonempty_text(raw.get("advisor_id"), "advisor_id"),
            agent_id=_nonempty_text(raw.get("agent_id"), "agent_id"),
            source_run_id=_nonempty_text(raw.get("source_run_id"), "source_run_id"),
            source_cutoff_event_id=_nonempty_text(raw.get("source_cutoff_event_id"), "source_cutoff_event_id"),
            source_state_sha256=state_hash,
            advisor_prompt_sha256=prompt_hash,
            generation_seed=int(raw.get("generation_seed")),
            persona_basis=_string_list(raw.get("persona_basis"), "persona_basis"),
            observed_behavior=_string_list(raw.get("observed_behavior"), "observed_behavior"),
            persona_behavior_assessment=_nonempty_text(
                raw.get("persona_behavior_assessment"), "persona_behavior_assessment"
            ),
            performance_context=_nonempty_text(raw.get("performance_context"), "performance_context"),
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
    reasoning_policy: Mapping[str, Any]
    messages: tuple[AdvisorMessage, ...]
    artifact_sha256: str

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
        expected_count: int = 10,
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
        if raw.get("model") != ADVISOR_MODEL or raw.get("provider") != ADVISOR_PROVIDER:
            raise ValueError("advisor model/provider differs")
        if raw.get("reasoning_policy") != ADVISOR_REASONING_POLICY:
            raise ValueError("advisor reasoning policy differs")
        values = raw.get("messages")
        if not isinstance(values, list) or len(values) != expected_count:
            raise ValueError(f"advisor artifact must contain exactly {expected_count} messages")
        messages = tuple(AdvisorMessage.from_mapping(value) for value in values)
        ids = tuple(message.agent_id for message in messages)
        if len(ids) != len(set(ids)):
            raise ValueError("advisor artifact contains duplicate agents")
        cohort = tuple(str(value) for value in cohort_agent_ids)
        expected_ids = deterministic_advisor_agents(cohort, count=expected_count)
        if tuple(sorted(ids)) != expected_ids:
            raise ValueError("advisor artifact agents differ from deterministic assignment")
        if any(message.source_cutoff_event_id != "2026-05-04/PM" for message in messages):
            raise ValueError("advisor source cutoff must be 2026-05-04/PM")
        if any(message.valid_from_event_id != "2026-05-06/AM" for message in messages):
            raise ValueError("advisor valid-from event must be 2026-05-06/AM")
        return cls(
            assignment_namespace=ADVISOR_ASSIGNMENT_NAMESPACE,
            assignment_seed=ADVISOR_ASSIGNMENT_SEED,
            model=ADVISOR_MODEL,
            provider=ADVISOR_PROVIDER,
            reasoning_policy=dict(ADVISOR_REASONING_POLICY),
            messages=messages,
            artifact_sha256=actual_hash,
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
