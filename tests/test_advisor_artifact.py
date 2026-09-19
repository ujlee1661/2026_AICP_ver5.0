from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.advisor.artifact import (
    ADVISOR_ARTIFACT_TYPE,
    ADVISOR_ASSIGNMENT_NAMESPACE,
    ADVISOR_ASSIGNMENT_SEED,
    ADVISOR_MODEL,
    ADVISOR_PROVIDER,
    ADVISOR_REASONING_POLICY,
    AdvisorArtifact,
    canonical_sha256,
    deterministic_advisor_agents,
)
from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.db.connection import init_sim_db


class AdvisorArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent_ids = [f"A{index:03d}" for index in range(1, 101)]

    def _payload(self, body: str = "기록과 투자 성향을 함께 고려해 거래 규모를 점검하세요.") -> dict:
        selected = deterministic_advisor_agents(self.agent_ids)
        messages = []
        for index, agent_id in enumerate(selected):
            messages.append(
                {
                    "advisor_id": f"advisor-{index:02d}",
                    "agent_id": agent_id,
                    "source_run_id": "parent-run",
                    "source_cutoff_event_id": "2026-05-04/PM",
                    "source_state_sha256": "a" * 64,
                    "advisor_prompt_sha256": "b" * 64,
                    "generation_seed": index + 1,
                    "persona_basis": ["장기 투자 성향"],
                    "observed_behavior": ["최근 거래 규모가 커짐"],
                    "persona_behavior_assessment": "성향과 최근 행동을 함께 점검함",
                    "performance_context": "수익률은 보조 근거로만 사용함",
                    "message_body": body,
                    "message_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "valid_from_event_id": "2026-05-06/AM",
                }
            )
        payload = {
            "artifact_type": ADVISOR_ARTIFACT_TYPE,
            "assignment_namespace": ADVISOR_ASSIGNMENT_NAMESPACE,
            "assignment_seed": ADVISOR_ASSIGNMENT_SEED,
            "model": ADVISOR_MODEL,
            "provider": ADVISOR_PROVIDER,
            "reasoning_policy": ADVISOR_REASONING_POLICY,
            "messages": messages,
        }
        payload["artifact_sha256"] = canonical_sha256(payload)
        return payload

    def _load(self, payload: dict) -> AdvisorArtifact:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advisor.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return AdvisorArtifact.load(path, cohort_agent_ids=self.agent_ids)

    def test_assignment_is_reproducible_and_contains_ten_unique_agents(self) -> None:
        first = deterministic_advisor_agents(self.agent_ids)
        second = deterministic_advisor_agents(reversed(self.agent_ids))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(set(first)), 10)

    def test_500_character_body_passes_and_501_fails(self) -> None:
        artifact = self._load(self._payload("가" * 500))
        self.assertEqual(len(artifact.messages), 10)
        with self.assertRaisesRegex(ValueError, "exceeds 500"):
            self._load(self._payload("가" * 501))

    def test_direction_instruction_and_future_prediction_are_rejected(self) -> None:
        for body in ("삼성전자를 매수하세요.", "주가는 상승할 것입니다."):
            with self.subTest(body=body), self.assertRaisesRegex(
                ValueError, "forbidden direction/prediction"
            ):
                self._load(self._payload(body))

    def test_message_is_visible_only_from_the_sealed_event(self) -> None:
        artifact = self._load(self._payload())
        agent_id = artifact.agent_ids[0]
        self.assertIsNone(artifact.message_for(agent_id, "2026-05-04/PM"))
        self.assertIsNotNone(artifact.message_for(agent_id, "2026-05-06/AM"))
        self.assertIsNone(artifact.message_for("A999", "2026-05-06/AM"))

    def test_install_is_idempotent_and_reader_hides_note_until_turn_91(self) -> None:
        artifact = self._load(self._payload())
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "runtime.db"
            init_sim_db(db_path)
            artifact.ensure_messages_installed(db_path)
            artifact.ensure_messages_installed(db_path)
            memory = MemoryAgent(db_path)
            agent_id = artifact.agent_ids[0]
            self.assertIsNone(memory.get_advisor_note(agent_id, current_turn=90))
            note = memory.get_advisor_note(agent_id, current_turn=91)
            self.assertIn("담당 투자 어드바이저", note or "")
            self.assertIsNone(memory.get_advisor_note("A999", current_turn=91))


if __name__ == "__main__":
    unittest.main()
