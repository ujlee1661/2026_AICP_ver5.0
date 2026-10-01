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
    GENERAL_ADVISOR_MODEL,
    GENERAL_ADVISOR_PROVIDER,
    AdvisorArtifact,
    canonical_sha256,
    deterministic_advisor_agents,
)
from twinmarket_kr.advisor.generation import (
    ADVISOR_VALIDATION_ATTEMPTS,
    generate_advisor_output,
    parse_advisor_response,
    validate_advisor_case,
    validate_advisor_output,
)
from twinmarket_kr.advisor.case_builder import normalize_runtime_constraints
from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.db.connection import init_sim_db
from twinmarket_kr.llm.belief import render_prompt


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

    def _general_payload(self, body: str = "매매 전 정보를 확인하고 신중하게 판단하세요.") -> dict:
        payload = self._payload(body)
        payload.update(
            advice_kind="general",
            model=GENERAL_ADVISOR_MODEL,
            provider=GENERAL_ADVISOR_PROVIDER,
            reasoning_policy=None,
            source_cases_sha256="c" * 64,
        )
        for message in payload["messages"]:
            message.update(
                advisor_prompt_sha256=None,
                generation_seed=None,
                persona_basis=[],
                observed_behavior=[],
                persona_behavior_assessment="",
                performance_context="",
            )
        payload["artifact_sha256"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "artifact_sha256"}
        )
        return payload

    def test_general_advice_is_identical_and_has_no_personal_assessment(self) -> None:
        artifact = self._load(self._general_payload())
        self.assertEqual(artifact.advice_kind, "general")
        self.assertEqual(len({message.message_body for message in artifact.messages}), 1)

        payload = self._general_payload()
        payload["messages"][0]["persona_basis"] = ["개인 성향"]
        payload["artifact_sha256"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "artifact_sha256"}
        )
        with self.assertRaisesRegex(ValueError, "personalized assessments"):
            self._load(payload)

    def test_general_advice_rejects_different_body_for_one_agent(self) -> None:
        payload = self._general_payload()
        body = "다른 조언"
        payload["messages"][0]["message_body"] = body
        payload["messages"][0]["message_body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        payload["artifact_sha256"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "artifact_sha256"}
        )
        with self.assertRaisesRegex(ValueError, "identical advice"):
            self._load(payload)

    def test_assignment_is_reproducible_and_contains_full_unique_cohort(self) -> None:
        first = deterministic_advisor_agents(self.agent_ids)
        second = deterministic_advisor_agents(reversed(self.agent_ids))
        self.assertEqual(first, second)
        self.assertEqual(first, tuple(self.agent_ids))
        self.assertEqual(len(first), 100)
        self.assertEqual(len(set(first)), 100)

    def test_assignment_rejects_a_partial_cohort(self) -> None:
        with self.assertRaisesRegex(ValueError, "full 100-agent cohort"):
            deterministic_advisor_agents(self.agent_ids[:-1])

    def test_artifact_rejects_a_missing_cohort_message(self) -> None:
        payload = self._payload()
        payload["messages"].pop()
        payload["artifact_sha256"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "artifact_sha256"}
        )
        with self.assertRaisesRegex(ValueError, "exactly 100 messages"):
            self._load(payload)

    def test_500_character_body_passes_and_501_fails(self) -> None:
        artifact = self._load(self._payload("가" * 500))
        self.assertEqual(len(artifact.messages), 100)
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
        self.assertIsNone(artifact.message_for(agent_id, "2026-05-06/PM"))
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
            self.assertIsNone(memory.get_advisor_note(agent_id, current_turn=92))
            self.assertIsNone(memory.get_recent_system_message(agent_id, current_turn=92))
            self.assertIsNone(memory.get_advisor_note("A999", current_turn=91))


class _AdvisorSequenceClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = [json.dumps(value, ensure_ascii=False) for value in responses]
        self.calls: list[dict] = []
        self.acceptances: list[dict] = []

    async def chat_advisor_reasoning_on(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": self.responses.pop(0),
                        "reasoning": "검토 완료",
                    },
                }
            ],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 1}},
        }

    def record_advisor_acceptance(self, **kwargs) -> None:
        self.acceptances.append(kwargs)


class AdvisorGenerationRetryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _valid_case() -> dict:
        return {
            "agent_id": "A003",
            "cutoff_event_id": "2026-05-04/PM",
            "runtime_constraints": {
                "decision_space": ["buy", "sell"],
                "allow_hold": False,
                "minimum_order_quantity": 1,
                "transaction_fee_rate": 0.0,
            },
            "representative_episodes": [
                {
                    "fill_id": "fill_A003_t090",
                    "event_id": "2026-05-04/PM",
                }
            ],
        }

    @staticmethod
    def _valid_output() -> dict:
        return {
            "persona_basis": ["장기 투자 성향"],
            "observed_behavior": ["fill_A003_t090: 최근 거래 규모가 커짐"],
            "persona_behavior_assessment": "성향과 행동을 함께 점검함",
            "performance_context": "수익률은 보조 근거로 사용함",
            "advice_body": "거래 규모와 판단 근거를 함께 점검하세요.",
        }

    def test_generation_prompt_prioritizes_optimal_process_over_persona_compliance(self) -> None:
        prompt = render_prompt("advisor_generation.txt", advisor_case_json="{}")
        optimal_process = "앞으로 가장 합리적인 판단 절차가 무엇인지 먼저 결정하세요"
        persona_reference = "persona는 그 절차의"
        self.assertIn(optimal_process, prompt)
        self.assertIn(persona_reference, prompt)
        self.assertLess(prompt.index(optimal_process), prompt.index(persona_reference))
        self.assertIn("참고 자료이지, 지켜야 할 최상위", prompt)
        self.assertIn("상충하더라도 더 나은 판단", prompt)
        self.assertIn("부적합한 persona를 무조건 따르라고", prompt)
        self.assertIn("persona의 방식이 현재 상황에도 합리적인데 실제 행동만 이탈", prompt)
        self.assertIn("조언은 반드시 이 투자자에게 개인화하세요", prompt)
        self.assertIn("조언 내용 개인화의 핵심 근거입니다", prompt)
        self.assertIn("persona가 달라도 동일하게 나올 수 있는 비개인화된 일반론", prompt)
        self.assertIn("persona의 말투·어휘·캐릭터를 흉내 내지는 말고", prompt)
        self.assertIn("명료하고 중립적인 일반적 어드바이저 문체", prompt)

    def test_reasoning_text_with_fenced_json_is_parsed_without_coercion(self) -> None:
        expected = self._valid_output()
        raw = "검토 과정입니다.\n```json\n" + json.dumps(expected, ensure_ascii=False) + "\n```"
        self.assertEqual(parse_advisor_response(raw), expected)

    async def test_non_object_is_retried_with_error_and_new_seed(self) -> None:
        client = _AdvisorSequenceClient([[], self._valid_output()])
        output, prompt_sha256 = await generate_advisor_output(
            self._valid_case(), client=client, seed=7, agent_id="A003"
        )
        self.assertEqual(output, self._valid_output())
        self.assertEqual(len(prompt_sha256), 64)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["seed"], 7)
        self.assertNotEqual(client.calls[0]["seed"], client.calls[1]["seed"])
        self.assertEqual(client.calls[0]["temperature"], 0.2)
        self.assertEqual(client.calls[1]["temperature"], 0.3)
        retry_prompt = client.calls[1]["messages"][0]["content"]
        self.assertIn("contains no object matching the sealed schema", retry_prompt)
        self.assertIn('"persona_basis"', retry_prompt)
        self.assertIn("fill_A003_t090", retry_prompt)
        self.assertEqual(len(client.acceptances), 1)
        self.assertEqual(
            client.acceptances[0]["phase_attempt_id"],
            "advisor-generation-attempt-2",
        )

    async def test_invalid_output_exhausts_bounded_attempts(self) -> None:
        client = _AdvisorSequenceClient([[]] * ADVISOR_VALIDATION_ATTEMPTS)
        with self.assertRaisesRegex(ValueError, "after 3 attempts"):
            await generate_advisor_output(
                self._valid_case(), client=client, seed=7, agent_id="A003"
            )
        self.assertEqual(len(client.calls), ADVISOR_VALIDATION_ATTEMPTS)
        self.assertFalse(client.acceptances)

    def test_runtime_constraints_are_fail_closed(self) -> None:
        self.assertEqual(
            normalize_runtime_constraints(
                {
                    "decision_space": "buy_sell_only",
                    "allow_hold": False,
                    "minimum_order_quantity": 1,
                    "transaction_fee_rate": 0.0,
                }
            ),
            self._valid_case()["runtime_constraints"],
        )
        for field, invalid in (
            ("decision_space", ["buy", "sell", "hold"]),
            ("allow_hold", True),
            ("minimum_order_quantity", 10),
            ("transaction_fee_rate", 0.001),
        ):
            constraints = dict(self._valid_case()["runtime_constraints"])
            constraints[field] = invalid
            with self.subTest(field=field), self.assertRaises(ValueError):
                normalize_runtime_constraints(constraints)

    def test_grounding_requires_available_fill_id(self) -> None:
        case = self._valid_case()
        self.assertEqual(validate_advisor_case(case)["fill_ids"], {"fill_A003_t090"})
        missing = self._valid_output()
        missing["observed_behavior"] = ["최근 거래 규모가 커짐"]
        with self.assertRaisesRegex(ValueError, "must cite a fill_id"):
            validate_advisor_output(missing, case=case)
        unavailable = self._valid_output()
        unavailable["observed_behavior"] = ["fill_A003_t091: 거래 규모가 커짐"]
        with self.assertRaisesRegex(ValueError, "unavailable fill_id"):
            validate_advisor_output(unavailable, case=case)

    def test_post_cutoff_date_is_rejected(self) -> None:
        output = self._valid_output()
        output["advice_body"] = "5월 6일 행동을 근거로 판단 절차를 점검하세요."
        with self.assertRaisesRegex(ValueError, "post-cutoff Korean date"):
            validate_advisor_output(output, case=self._valid_case())

    async def test_rendered_prompt_contains_constraints_and_improved_contract(self) -> None:
        client = _AdvisorSequenceClient([self._valid_output()])
        await generate_advisor_output(
            self._valid_case(), client=client, seed=7, agent_id="A003"
        )
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn('"minimum_order_quantity": 1', prompt)
        self.assertIn("가장 중요한 판단 문제 하나", prompt)
        self.assertIn("명확한 오류나 충돌이 없으면 문제를 만들어내지 마세요", prompt)


if __name__ == "__main__":
    unittest.main()
