from __future__ import annotations

import inspect
import json
import os
import unittest
from typing import Any
from unittest import mock

from twinmarket_kr import simulation
from twinmarket_kr.core import daily_cycle
from twinmarket_kr.core.daily_cycle import run_agent_turn

import config
from twinmarket_kr.community.posting import posting_decision
from twinmarket_kr.community.reading import community_reading_select
from twinmarket_kr.community.validation import CommunityValidationError
from twinmarket_kr.llm.analysis import AnalysisValidationError, analyze_market
from twinmarket_kr.llm.belief import (
    BeliefValidationError,
    generate_short_term_belief,
    update_long_term_belief,
    validate_stb_dim_6_scope,
)
from twinmarket_kr.llm.client import OpenRouterClient
from twinmarket_kr.llm.decision import (
    build_trading_constraints,
    make_decision,
)


def _dimensions(label: str) -> dict[str, str]:
    return {
        f"dim_{index}": f"{label} dimension {index}"
        for index in range(1, 7)
    }


def _stb_dimensions(label: str) -> dict[str, str]:
    """STB dim_6 must state today's information limits, not past performance."""

    dimensions = _dimensions(label)
    dimensions["dim_6"] = "정보 한계: 오늘은 요약만 제공됨 / 주의점: 제목만으로 단정하지 않기"
    return dimensions


def _evidence(
    *,
    dim_1_support: list[str] | None = None,
    dim_6_support: list[str] | None = None,
) -> dict[str, dict[str, list[str]]]:
    result = {
        f"dim_{index}": {"support": [], "contradict": []}
        for index in range(1, 7)
    }
    result["dim_1"]["support"] = list(dim_1_support or [])
    result["dim_6"]["support"] = list(dim_6_support or [])
    return result


def _stage_payload(prompt: str) -> dict[str, Any]:
    marker = "입력 정보(JSON):"
    if marker not in prompt:
        raise AssertionError("prompt does not contain the integrated stage payload marker")
    return json.loads(prompt.rsplit(marker, 1)[1].strip())


def _named_slot_json(prompt: str, first_key: str) -> dict[str, Any]:
    """Extract the JSON object a named slot rendered into the prompt.

    analysis/decision no longer carry a trailing stage payload, so their
    inputs are read back from the rendered slot itself.
    """

    anchor = prompt.index(f'"{first_key}"')
    start = prompt.rindex("{", 0, anchor)
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(prompt[start:])
    return value


class _CaptureClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = json.dumps(response, ensure_ascii=False)
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        self.prompts.append(messages[-1]["content"])
        self.kwargs.append(dict(kwargs))
        return self.response


class _SequenceClient:
    """Return a different response per attempt so retry prompts can be read back."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = [json.dumps(item, ensure_ascii=False) for item in responses]
        self.prompts: list[str] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> str:
        self.prompts.append(messages[-1]["content"])
        index = min(len(self.prompts), len(self.responses)) - 1
        return self.responses[index]


class IntegratedMemoryPromptWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_stb_uses_current_only_top_level_prompt_and_six_dimensions(
        self,
    ) -> None:
        client = _CaptureClient(
            {
                **_stb_dimensions("stb"),
                "dimension_evidence": _evidence(
                    dim_1_support=["news:current"],
                ),
            }
        )
        result = await generate_short_term_belief(
            {
                "agent_id": "agent-1",
                "news_depth": 1,
                "persona_prompt": "legacy persona",
            },
            event={
                "event_id": "2026-02-27/AM",
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
            },
            current_evidence={
                "news": [
                    {
                        "evidence_id": "news:current",
                        "title": "현재 턴 뉴스",
                    }
                ],
                "depth2_search_results": [],
                "community_claims": [],
            },
            allowed_evidence_ids={"news:current"},
            client=client,
            seed=7,
            validation_attempts=1,
        )

        self.assertEqual(client.kwargs[0]["audit_label"], "short_term_belief")
        self.assertIn(
            "이 Short-Term Belief 하나만으로 거래를 결정하지 않습니다.",
            client.prompts[0],
        )
        payload = _stage_payload(client.prompts[0])
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "persona",
                "event",
                "current_evidence",
                "sanitized_evidence_registry",
            },
        )
        forbidden = {
            "previous_ltb",
            "transaction_episode",
            "eligible_price_outcomes_dim_6_only",
            "belief_summary",
            "view_change",
        }
        self.assertTrue(forbidden.isdisjoint(payload))
        self.assertEqual(
            result["dimension_evidence"]["dim_1"]["support"],
            ["news:current"],
        )

    async def test_ltb_uses_recursive_post_fill_prompt_and_only_due_outcomes(
        self,
    ) -> None:
        due_outcome_id = "outcome:fill-previous:h1"
        integration = _evidence(dim_1_support=["news:current"])
        client = _CaptureClient(
            {
                **_dimensions("post-fill ltb"),
                "integration_evidence": integration,
                "outcome_verdicts": ["support"],
            }
        )
        result = await update_long_term_belief(
            {
                "agent_id": "agent-1",
                "news_depth": 1,
                "persona_prompt": "legacy persona",
            },
            event={
                "event_id": "2026-02-27/PM",
                "turn": 2,
                "date": "2026-02-27",
                "subturn": "pm",
            },
            previous_ltb={
                "dimensions": _dimensions("previous ltb"),
                "belief_summary": "HUMAN_SUMMARY_MUST_NOT_ENTER_LTB_PROMPT",
                "view_change": "HUMAN_CHANGE_MUST_NOT_ENTER_LTB_PROMPT",
            },
            current_stb={
                "dimensions": _dimensions("current stb"),
                "dimension_evidence": _evidence(
                    dim_1_support=["news:current"],
                ),
            },
            transaction_episode={
                "fill_id": "fill:current",
                "decision_id": "decision:current",
                "action": "buy",
                "requested_quantity": 2,
                "filled_quantity": 2,
                "executed_price": 100.0,
                "fee": 0.0,
            },
            advisor_note="이번에 받은 개인화 조언",
            eligible_price_outcomes_dim_6_only=[
                {
                    "outcome_id": due_outcome_id,
                    "horizon": "h1",
                    "observed_event_id": "2026-02-27/PM",
                    "action_aligned_markout": 0.05,
                }
            ],
            client=client,
            seed=8,
            validation_attempts=1,
        )

        self.assertEqual(
            client.kwargs[0]["audit_label"],
            "post_fill_long_term_belief",
        )
        self.assertIn(
            "유지되는 차원은 그 이유와 함께 정리하세요",
            client.prompts[0],
        )
        payload = _stage_payload(client.prompts[0])
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "persona",
                "event",
                "previous_ltb",
                "current_stb",
                "transaction_episode",
                "advisor_note",
                "eligible_price_outcomes_dim_6_only",
                "sanitized_evidence_registry",
            },
        )
        self.assertEqual(
            set(payload["previous_ltb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        self.assertEqual(
            payload["transaction_episode"]["fill_id"],
            "fill:current",
        )
        self.assertEqual(payload["advisor_note"], "이번에 받은 개인화 조언")
        # 가격 결과는 ID 대신 순번으로만 노출된다(환각 차단).
        self.assertEqual(
            [
                row["순번"]
                for row in payload["eligible_price_outcomes_dim_6_only"]
            ],
            [1],
        )
        self.assertNotIn(due_outcome_id, client.prompts[0])
        self.assertNotIn("HUMAN_SUMMARY_MUST_NOT_ENTER_LTB_PROMPT", client.prompts[0])
        self.assertNotIn("HUMAN_CHANGE_MUST_NOT_ENTER_LTB_PROMPT", client.prompts[0])
        self.assertEqual(
            result["integration_evidence"]["dim_6"]["support"],
            [due_outcome_id],
        )
        self.assertIn("belief_summary", result)
        self.assertIn("view_change", result)

    async def test_analysis_and_decision_compare_both_beliefs_without_human_logs(
        self,
    ) -> None:
        previous = {
            "dimensions": _dimensions("previous ltb"),
            "belief_summary": "HUMAN_SUMMARY_MUST_NOT_ENTER_DECISION",
            "view_change": "HUMAN_CHANGE_MUST_NOT_ENTER_DECISION",
        }
        current = {
            "dimensions": _dimensions("current stb"),
            "dimension_evidence": _evidence(),
            "belief_summary": "STB_SUMMARY_MUST_NOT_ENTER_DECISION",
        }
        analysis_client = _CaptureClient(
            {
                "market_view": "혼조",
                "valuation_view": "판단 어려움",
                "technical_view": "변동성 확인 필요",
                "news_view": "현재 신호가 엇갈림",
                "portfolio_view": "현금과 보유량을 함께 고려",
                "key_risks": ["정보 불확실성"],
                "opportunity": "제약 안의 제한적 대응",
                "caution": "과신 금지",
                "confidence": "medium",
                "directional_stance": "uncertain",
                "evidence_references": [
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_1"},
                    {"source": "market", "field": "close"},
                    {"source": "execution_state", "field": "available_cash"},
                ],
            }
        )
        constraints = build_trading_constraints(
            available_cash=1_000.0,
            current_quantity=5,
            current_price=100.0,
            max_single_trade_cash_ratio=0.5,
        )
        analysis = await analyze_market(
            {"agent_id": "agent-1", "persona_prompt": "legacy persona"},
            previous_ltb=previous,
            current_stb=current,
            market_features={"close": 100.0},
            portfolio_summary="cash=1000, quantity=5",
            execution_state=constraints,
            client=analysis_client,
            seed=9,
        )
        analysis_prompt = analysis_client.prompts[0]
        self.assertIn(
            "단순히 이어 붙이거나 어느 하나만 따르지 말고",
            analysis_prompt,
        )
        # analysis/decision은 이름 있는 슬롯 하나로만 입력을 받는다.
        # 같은 입력을 두 번 싣던 STAGE_PAYLOAD 블록은 제거됐다.
        self.assertNotIn("입력 정보(JSON):", analysis_prompt)
        self.assertNotIn("<<STAGE_PAYLOAD_JSON>>", analysis_prompt)
        analysis_belief = _named_slot_json(analysis_prompt, "previous_ltb")
        self.assertEqual(
            set(analysis_belief["previous_ltb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        self.assertEqual(
            set(analysis_belief["current_stb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        # evidence_references가 요구하는 네 source 이름이 프롬프트에 명시돼야 한다.
        for source in ("previous_ltb", "current_stb", "market", "execution_state"):
            self.assertIn(source, analysis_prompt)
        # execution_state는 슬롯으로 실제 전달돼야 한다.
        self.assertIn("available_cash", analysis_prompt)

        decision_client = _CaptureClient(
            {
                "action": "buy",
                "requested_quantity": 2,
                "reason": "LTB와 STB의 충돌을 시장 분석과 함께 종합했다.",
                "risk_control": "현금 제약 안에서 수량을 제한했다.",
            }
        )
        decision = await make_decision(
            {"agent_id": "agent-1", "persona_prompt": "legacy persona"},
            previous,
            current,
            analysis,
            "cash=1000, quantity=5",
            constraints,
            client=decision_client,
            seed=10,
            validation_attempts=1,
        )
        decision_prompt = decision_client.prompts[0]
        self.assertIn(
            "단순히 이어 붙이거나 Short-Term Belief만 따르지 말고",
            decision_prompt,
        )
        self.assertNotIn("입력 정보(JSON):", decision_prompt)
        self.assertNotIn("<<STAGE_PAYLOAD_JSON>>", decision_prompt)
        decision_belief = _named_slot_json(decision_prompt, "previous_ltb")
        self.assertEqual(
            set(decision_belief["previous_ltb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        self.assertEqual(
            set(decision_belief["current_stb"]),
            {f"dim_{index}" for index in range(1, 7)},
        )
        for sentinel in (
            "HUMAN_SUMMARY_MUST_NOT_ENTER_DECISION",
            "HUMAN_CHANGE_MUST_NOT_ENTER_DECISION",
            "STB_SUMMARY_MUST_NOT_ENTER_DECISION",
        ):
            self.assertNotIn(sentinel, analysis_prompt)
            self.assertNotIn(sentinel, decision_prompt)
        self.assertEqual((decision["action"], decision["quantity"]), ("buy", 2))

    async def test_posting_uses_post_fill_ltb_view_change_and_structured_pm_fill(
        self,
    ) -> None:
        client = _CaptureClient(
            {
                "will_post": True,
                "post_type": "impression",
                "title": "오늘 생각",
                "content": "장기 관점과 오늘 체결을 함께 돌아본 짧은 글입니다.",
            }
        )
        committed_ltb = {
            "ltb_id": "ltb:post-fill:pm",
            "dimensions": _dimensions("post-fill ltb"),
            "belief_summary": "HUMAN_SUMMARY_MUST_NOT_ENTER_POST",
            "view_change": {
                "dim_1": {
                    "before": "이전 전망",
                    "after": "체결 후 전망",
                }
            },
        }
        committed_fill = {
            "fill_id": "fill:pm",
            "decision_id": "decision:pm",
            "action": "sell",
            "requested_quantity": 2,
            "filled_quantity": 2,
            "executed_price": 105.0,
            "fee": 0.0,
            "private_extra": "PRIVATE_FILL_FIELD_MUST_NOT_ENTER_POST",
        }
        result = await posting_decision(
            {"agent_id": "agent-1", "persona_prompt": "legacy persona"},
            committed_ltb=committed_ltb,
            committed_fill=committed_fill,
            date="2026-02-27",
            client=client,
            seed=11,
        )

        prompt = client.prompts[0]
        self.assertIn(
            "현재 post-fill Long-Term Belief, 그 Belief와 연결된 관점 변화, "
            "오늘의 실제 PM 체결을 함께 활용",
            prompt,
        )
        self.assertIn("ltb:post-fill:pm", result["source_ltb_id"])
        self.assertEqual(result["source_fill_id"], "fill:pm")
        self.assertEqual(result["source_decision_id"], "decision:pm")
        self.assertEqual(result["source_view_change"], committed_ltb["view_change"])
        self.assertIn('"fill_id": "fill:pm"', prompt)
        self.assertIn('"after": "체결 후 전망"', prompt)
        self.assertNotIn("HUMAN_SUMMARY_MUST_NOT_ENTER_POST", prompt)
        self.assertNotIn("PRIVATE_FILL_FIELD_MUST_NOT_ENTER_POST", prompt)


class StbScopeAndAnalysisEvidenceContractTests(unittest.IsolatedAsyncioTestCase):
    """STB/LTB 역할 분리와 analysis evidence source 계약을 실제 호출로 검증한다."""

    async def _render_stb_prompt(self, dim_6: str | None = None) -> str:
        dimensions = _stb_dimensions("stb")
        if dim_6 is not None:
            dimensions["dim_6"] = dim_6
        client = _CaptureClient(
            {
                **dimensions,
                "dimension_evidence": _evidence(dim_1_support=["news:current"]),
            }
        )
        await generate_short_term_belief(
            {"agent_id": "agent-1", "news_depth": 1, "persona_prompt": "persona"},
            event={
                "event_id": "2026-02-27/AM",
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
            },
            current_evidence={
                "news": [{"evidence_id": "news:current", "title": "제목"}],
                "depth2_search_results": [],
                "community_claims": [],
            },
            allowed_evidence_ids={"news:current"},
            client=client,
            seed=7,
            validation_attempts=1,
        )
        return client.prompts[0]

    async def _run_stb_with(self, response: dict[str, Any]) -> None:
        client = _CaptureClient(response)
        await generate_short_term_belief(
            {"agent_id": "agent-1", "news_depth": 1, "persona_prompt": "persona"},
            event={
                "event_id": "2026-02-27/AM",
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
            },
            current_evidence={
                "news": [{"evidence_id": "news:current", "title": "제목"}],
                "depth2_search_results": [],
                "community_claims": [],
            },
            allowed_evidence_ids={"news:current"},
            client=client,
            seed=7,
            validation_attempts=1,
        )

    async def test_rendered_stb_prompt_drops_past_performance_recall(self) -> None:
        prompt = await self._render_stb_prompt()
        for banned in (
            "최근 나의 투자 판단들을 돌아본",
            "반복적으로 틀리는 패턴은 없는지",
            "잘 보고 있는 부분과 놓치고 있는 부분",
            "자기 투자 능력 평가",
        ):
            self.assertNotIn(banned, prompt)
        self.assertIn("오늘 정보의 한계와 오늘 판단에서 주의할 점", prompt)
        self.assertIn("정보 한계:", prompt)
        self.assertIn("주의점:", prompt)
        self.assertIn("Long-Term Belief 단계의 역할", prompt)

    async def test_rendered_stb_step_2_does_not_ask_for_previous_belief_comparison(
        self,
    ) -> None:
        prompt = await self._render_stb_prompt()
        self.assertNotIn("Step 2: 기존 Belief와 비교하면 무엇이 달라졌는가?", prompt)
        for banned in (
            "기존 관점을 확인해 주는 정보가 있었는가?",
            "기존 관점에 의문을 품게 만드는 정보가 있었는가?",
            "변화가 없다면, 기존 관점을 그대로 유지할 근거가 충분한가?",
        ):
            self.assertNotIn(banned, prompt)
        self.assertIn(
            "Step 2: 오늘의 새 신호는 현재 판단에 무엇을 시사하고, 어디가 불확실한가?",
            prompt,
        )
        self.assertIn("입력에 없는 과거 정보를 비교·복원·추정하지 마세요", prompt)
        # 슬롯이 전부 치환됐고 stage payload는 정확히 한 번만 존재한다.
        self.assertNotIn("<<STAGE_PAYLOAD_JSON>>", prompt)
        self.assertEqual(prompt.count("입력 정보(JSON):"), 1)
        payload = _stage_payload(prompt)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "persona",
                "event",
                "current_evidence",
                "sanitized_evidence_registry",
            },
        )

    async def test_stb_dim_6_requires_information_limit_form(self) -> None:
        ok = _stb_dimensions("stb")["dim_6"]
        self.assertEqual(validate_stb_dim_6_scope({"dim_6": ok}), [])
        for bad in (
            "최근 판단은 대체로 적절했고 반복 오류는 없었다",
            "정보 한계: 요약만 있음",
            "주의점: 단정하지 않기",
            "",
        ):
            with self.subTest(dim_6=bad):
                self.assertTrue(validate_stb_dim_6_scope({"dim_6": bad}))
        # 실제 생성 경로도 같은 계약으로 거부한다.
        with self.assertRaises(BeliefValidationError):
            await self._render_stb_prompt(
                dim_6="최근 판단을 돌아보면 방향은 맞았고 수량이 아쉬웠다"
            )

    async def test_stb_rejects_extra_key_and_broken_evidence_shape(self) -> None:
        valid = {
            **_stb_dimensions("stb"),
            "dimension_evidence": _evidence(dim_1_support=["news:current"]),
        }
        await self._run_stb_with(valid)  # 정상 JSON은 통과한다.
        with self.assertRaises(BeliefValidationError):
            await self._run_stb_with({**valid, "belief_summary": "사람용 요약"})
        broken = _evidence(dim_1_support=["news:current"])
        broken["dim_2"] = {"support": ["news:current"]}
        with self.assertRaises(BeliefValidationError):
            await self._run_stb_with({**valid, "dimension_evidence": broken})
        unknown = _evidence(dim_1_support=["news:does-not-exist"])
        with self.assertRaises(BeliefValidationError):
            await self._run_stb_with({**valid, "dimension_evidence": unknown})

    async def test_offline_stub_satisfies_the_stb_dim_6_contract(self) -> None:
        """무과금 smoke가 쓰는 stub이 STB 계약을 어기면 모든 offline 실행이 막힌다."""

        with mock.patch.dict(os.environ, {"TWINMARKET_OFFLINE_LLM": "1"}):
            client = OpenRouterClient()
            self.assertTrue(client.is_offline)
            stb = await generate_short_term_belief(
                {"agent_id": "A001", "news_depth": 1, "persona_prompt": "persona"},
                event={
                    "event_id": "2026-02-27/AM",
                    "turn": 1,
                    "date": "2026-02-27",
                    "subturn": "am",
                },
                current_evidence={
                    "news": [{"evidence_id": "news:x", "title": "제목"}],
                    "depth2_search_results": [],
                    "community_claims": [],
                },
                allowed_evidence_ids={"news:x"},
                client=client,
                seed=1,
                validation_attempts=1,
            )
        self.assertEqual(validate_stb_dim_6_scope(stb), [])
        self.assertEqual(stb["generation_attempts"], 1)

    async def test_rendered_ltb_prompt_keeps_fill_and_mature_outcome_self_assessment(
        self,
    ) -> None:
        due_outcome_id = "outcome:fill-previous:h1"
        client = _CaptureClient(
            {
                **_dimensions("post-fill ltb"),
                "integration_evidence": _evidence(
                    dim_1_support=["news:current"],
                ),
                "outcome_verdicts": ["support"],
            }
        )
        await update_long_term_belief(
            {"agent_id": "agent-1", "news_depth": 1, "persona_prompt": "persona"},
            event={
                "event_id": "2026-02-27/PM",
                "turn": 2,
                "date": "2026-02-27",
                "subturn": "pm",
            },
            previous_ltb={"dimensions": _dimensions("previous ltb")},
            current_stb={
                "dimensions": _stb_dimensions("current stb"),
                "dimension_evidence": _evidence(dim_1_support=["news:current"]),
            },
            transaction_episode={
                "fill_id": "fill:current",
                "decision_id": "decision:current",
                "action": "buy",
                "requested_quantity": 2,
                "filled_quantity": 2,
                "executed_price": 100.0,
                "fee": 0.0,
            },
            eligible_price_outcomes_dim_6_only=[
                {
                    "outcome_id": due_outcome_id,
                    "horizon": "h1",
                    "observed_event_id": "2026-02-27/PM",
                    "action_aligned_markout": 0.05,
                }
            ],
            client=client,
            seed=8,
            validation_attempts=1,
        )
        prompt = client.prompts[0]
        instructions = prompt.rsplit("입력 정보(JSON):", 1)[0]
        # LTB는 STB와 달리 실제 체결과 도래한 결과 기반 자기평가를 유지해야 한다.
        self.assertIn("최근 나의 투자 판단들을 돌아본 자기 평가", instructions)
        self.assertIn("관찰 시점 도래 과거 체결의 가격 결과", instructions)
        # 방향은 더 이상 강제하지 않는다. 대신 매수/매도의 손익 비대칭을
        # 설명해 에이전트가 스스로 판단할 맥락을 준다. 강제하던 시절에는
        # 모델이 매도 성공(가격 하락)을 실패로 읽는 오분류를 10회 내내
        # 반복해 실행을 죽였다(v4 소진 24건 중 22건).
        self.assertIn("어느 쪽인지는 **당신이 판단합니다**", instructions)
        self.assertIn("매도한 뒤 가격이 내렸다", instructions)
        self.assertIn("부분 매도", instructions)
        self.assertIn('"support" 또는 "contradict"', instructions)
        self.assertNotIn('"support", "contradict", "neutral"', instructions)
        self.assertNotIn("action_aligned_markout가 양수면 support", instructions)
        # STB dim_6 전용 형식 요구는 LTB 지시문으로 번지지 않는다. 다만 STB의
        # dim_6 문자열 자체는 current_stb payload로 정상 전달된다.
        self.assertNotIn("정보 한계:", instructions)
        self.assertIn("정보 한계:", prompt)
        payload = _stage_payload(prompt)
        self.assertIn("transaction_episode", payload)
        self.assertNotIn("advisor_note", payload)
        self.assertIn("eligible_price_outcomes_dim_6_only", payload)
        self.assertEqual(
            [item["순번"] for item in payload["eligible_price_outcomes_dim_6_only"]],
            [1],
        )

    async def _run_stb_sequence(self, responses: list[dict[str, Any]]) -> _SequenceClient:
        client = _SequenceClient(responses)
        await generate_short_term_belief(
            {"agent_id": "agent-1", "news_depth": 1, "persona_prompt": "persona"},
            event={
                "event_id": "2026-02-27/AM",
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
            },
            current_evidence={
                "news": [
                    {"evidence_id": "news:a", "title": "제목 a"},
                    {"evidence_id": "news:b", "title": "제목 b"},
                ],
                "depth2_search_results": [],
                "community_claims": [],
            },
            allowed_evidence_ids={"news:a", "news:b"},
            client=client,
            seed=7,
            validation_attempts=2,
        )
        return client

    async def test_same_id_in_both_relations_error_names_the_offending_ids(
        self,
    ) -> None:
        """라이브에서 재시도가 두더지잡기로 실패한 원인이 ID 미표시였다."""

        broken = _evidence(dim_1_support=["news:a"])
        broken["dim_4"] = {"support": ["news:a", "news:b"], "contradict": ["news:a"]}
        clean = _evidence(dim_1_support=["news:a"])
        client = await self._run_stb_sequence(
            [
                {**_stb_dimensions("stb"), "dimension_evidence": broken},
                {**_stb_dimensions("stb"), "dimension_evidence": clean},
            ]
        )
        self.assertEqual(len(client.prompts), 2)
        retry = client.prompts[1]
        # 어느 차원인지뿐 아니라 어느 ID인지까지 재시도 프롬프트에 들어가야 한다.
        self.assertIn("dimension_evidence.dim_4:same_id_in_both_relations", retry)
        self.assertIn("news:a", retry)
        # 겹치지 않은 news:b는 위반 목록에 들어가지 않는다.
        self.assertNotIn("same_id_in_both_relations:['news:a', 'news:b']", retry)

    async def test_retry_prompt_does_not_steer_evidence_direction(self) -> None:
        """재시도 힌트가 방향 기본값을 주면 연구자 편향이 데이터에 섞인다.

        evidence의 support/contradict는 관측 대상이므로, 규칙(한 쪽에만)은
        알려주되 어느 쪽인지는 지정하지 않는다.
        """

        broken = _evidence(dim_1_support=["news:a"])
        broken["dim_2"] = {"support": ["news:a"], "contradict": ["news:a"]}
        clean = _evidence(dim_1_support=["news:a"])
        client = await self._run_stb_sequence(
            [
                {**_stb_dimensions("stb"), "dimension_evidence": broken},
                {**_stb_dimensions("stb"), "dimension_evidence": clean},
            ]
        )
        retry = client.prompts[1]
        # 규칙은 전달된다.
        self.assertIn("한 쪽에만 넣으세요", retry)
        self.assertIn("dimension_evidence.dim_2:same_id_in_both_relations", retry)
        # 방향 계도 문구는 없어야 한다.
        for steering in (
            "support에만",
            "support 쪽",
            "support로 두세요",
            "contradict에만",
        ):
            self.assertNotIn(steering, retry)

    async def test_retry_prompt_states_the_scope_rule(self) -> None:
        broken = _evidence(dim_1_support=["news:a"])
        broken["dim_2"] = {"support": ["news:a"], "contradict": ["news:a"]}
        clean = _evidence(dim_1_support=["news:a"])
        client = await self._run_stb_sequence(
            [
                {**_stb_dimensions("stb"), "dimension_evidence": broken},
                {**_stb_dimensions("stb"), "dimension_evidence": clean},
            ]
        )
        retry = client.prompts[1]
        self.assertIn("한 쪽에만 넣으세요", retry)
        self.assertIn("페르소나의 판단에 따라", retry)
        self.assertIn("지적되지 않은 다른 차원은 그대로 두세요", retry)

    async def test_dimension_text_over_limit_is_accepted_without_retry(self) -> None:
        # 글자수 한도는 프롬프트의 목표치일 뿐 검증에서 강제하지 않는다(2026-07-31).
        # 한 턴에 outcome 3건이 동시에 도래하면 150자 안에 빠짐없이 요약하기가
        # 어려워 라이브에서 반복적으로 이벤트를 중단시켰다. 초과해도 그대로
        # 받아들여져 재시도가 발생하지 않아야 한다.
        over = _stb_dimensions("stb")
        over["dim_2"] = "가" * 187  # dim_2 한도(150자)를 넘긴 텍스트
        clean = _evidence(dim_1_support=["news:a"])
        client = _CaptureClient({**over, "dimension_evidence": clean})
        result = await generate_short_term_belief(
            {"agent_id": "agent-1", "news_depth": 1, "persona_prompt": "persona"},
            event={
                "event_id": "2026-02-27/AM",
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
            },
            current_evidence={
                "news": [{"evidence_id": "news:a", "title": "제목 a"}],
                "depth2_search_results": [],
                "community_claims": [],
            },
            allowed_evidence_ids={"news:a"},
            client=client,
            validation_attempts=1,
        )
        self.assertEqual(len(result["dim_2"]), 187)
        self.assertEqual(len(client.prompts), 1)

    async def _run_ltb(self, client: Any) -> dict[str, Any]:
        return await update_long_term_belief(
            {"agent_id": "agent-1", "news_depth": 1, "persona_prompt": "persona"},
            event={
                "event_id": "2026-02-27/PM",
                "turn": 2,
                "date": "2026-02-27",
                "subturn": "pm",
            },
            previous_ltb={"dimensions": _dimensions("previous ltb")},
            current_stb={
                "dimensions": _stb_dimensions("current stb"),
                "dimension_evidence": _evidence(dim_1_support=["news:current"]),
            },
            transaction_episode={
                "fill_id": "fill:current",
                "decision_id": "decision:current",
                "action": "buy",
                "requested_quantity": 2,
                "filled_quantity": 2,
                "executed_price": 100.0,
                "fee": 0.0,
            },
            eligible_price_outcomes_dim_6_only=[],
            client=client,
            seed=8,
            validation_attempts=2,
        )

    async def test_ltb_allows_per_dimension_carryover_text(self) -> None:
        """관점이 안 변한 차원의 문장 유지는 정당하다.

        차원별 강제 재서술은 임베딩 deviation 측정에 억지 패러프레이즈
        노이즈를 깔고, 라이브에서 dim_6 반복만으로 2 agent가 소진됐다.
        """

        carried = _dimensions("previous ltb")  # dim_6 포함 대부분 유지
        carried["dim_1"] = "오늘 신호를 통합해 방향 전망을 조정했다"
        client = _CaptureClient(
            {**carried, "integration_evidence": _evidence(dim_1_support=["news:current"])}
        )
        result = await self._run_ltb(client)
        self.assertEqual(result["dim_6"], "previous ltb dimension 6")
        self.assertEqual(len(client.prompts), 1)  # 첫 시도 수락

    async def test_ltb_allows_full_carryover_when_no_outcome_matured(self) -> None:
        """도래한 가격 결과가 없으면 여섯 차원 유지도 정당한 관측값이다.

        오늘 STB가 어제 관점과 사실상 같은 말을 하는 날이 있고, 그때 억지
        재서술을 강제하면 임베딩 기반 deviation에 가짜 변화량이 주입된다.
        """

        full_copy = {
            **_dimensions("previous ltb"),
            "integration_evidence": _evidence(dim_1_support=["news:current"]),
        }
        client = _SequenceClient([full_copy])
        result = await self._run_ltb(client)
        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(result["dim_1"], "previous ltb dimension 1")

    async def test_ltb_prompt_no_longer_demands_forced_rewrite(self) -> None:
        client = _CaptureClient(
            {
                **_dimensions("post-fill ltb"),
                "integration_evidence": _evidence(dim_1_support=["news:current"]),
            }
        )
        await self._run_ltb(client)
        instructions = client.prompts[0].rsplit("입력 정보(JSON):", 1)[0]
        for banned in (
            "새 문장으로 여섯 차원을 모두 재서술",
            "모두 다음 거래용 새 문장으로 작성",
            "이전 문장을 그대로 복사하거나",
            "`maintain`",
        ):
            self.assertNotIn(banned, instructions)

    def _analysis_response(self, **overrides: Any) -> dict[str, Any]:
        response = {
            "market_view": "혼조",
            "valuation_view": "판단 어려움",
            "technical_view": "변동성 확인 필요",
            "news_view": "신호가 엇갈림",
            "portfolio_view": "현금 여력과 보유량을 함께 고려",
            "key_risks": ["정보 불확실성"],
            "opportunity": "제한적 대응",
            "caution": "과신 금지",
            "confidence": "medium",
            "directional_stance": "uncertain",
            "evidence_references": [
                {"source": "previous_ltb", "field": "dim_1"},
                {"source": "current_stb", "field": "dim_4"},
                {"source": "market", "field": "close"},
                {"source": "execution_state", "field": "available_cash"},
            ],
        }
        response.update(overrides)
        return response

    async def _run_analysis(self, response: dict[str, Any]) -> tuple[dict[str, Any], str]:
        client = _CaptureClient(response)
        constraints = build_trading_constraints(
            available_cash=1_000.0,
            current_quantity=5,
            current_price=100.0,
            max_single_trade_cash_ratio=0.5,
        )
        result = await analyze_market(
            {"agent_id": "agent-1", "persona_prompt": "persona"},
            previous_ltb={"dimensions": _dimensions("previous ltb")},
            current_stb={
                "dimensions": _stb_dimensions("current stb"),
                "dimension_evidence": _evidence(),
            },
            market_features={"close": 100.0},
            portfolio_summary="cash=1000, quantity=5",
            execution_state=constraints,
            client=client,
            seed=9,
        )
        return result, client.prompts[0]

    async def test_analysis_prompt_names_four_sources_and_excludes_portfolio(
        self,
    ) -> None:
        _result, prompt = await self._run_analysis(self._analysis_response())
        self.assertIn("evidence_references가 인용할 수 있는 source", prompt)
        for source in ("previous_ltb", "current_stb", "market", "execution_state"):
            self.assertIn(source, prompt)
        self.assertIn(
            "판단을 위한 보조 상태이며 evidence_references의 source가 아닙니다",
            prompt,
        )
        self.assertIn('"source": "portfolio"', prompt)
        self.assertIn("execution_state의 실제 field", prompt)
        # portfolio_summary 자체는 판단용 컨텍스트로 계속 렌더링돼야 한다.
        self.assertIn("cash=1000, quantity=5", prompt)

    async def test_analysis_accepts_portfolio_view_backed_by_execution_state(
        self,
    ) -> None:
        result, _prompt = await self._run_analysis(self._analysis_response())
        self.assertEqual(result["portfolio_view"], "현금 여력과 보유량을 함께 고려")
        self.assertIn(
            {"source": "execution_state", "field": "available_cash"},
            result["evidence_references"],
        )

    async def test_analysis_rejects_portfolio_source_unknown_field_and_extra_key(
        self,
    ) -> None:
        cases = {
            "portfolio_source": self._analysis_response(
                evidence_references=[
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_4"},
                    {"source": "market", "field": "close"},
                    {"source": "execution_state", "field": "available_cash"},
                    {"source": "portfolio", "field": "cash"},
                ]
            ),
            "unknown_field": self._analysis_response(
                evidence_references=[
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_4"},
                    {"source": "market", "field": "field_that_does_not_exist"},
                    {"source": "execution_state", "field": "available_cash"},
                ]
            ),
            "bad_reference_shape": self._analysis_response(
                evidence_references=[
                    {"source": "previous_ltb", "field": "dim_1", "note": "extra"},
                    {"source": "current_stb", "field": "dim_4"},
                    {"source": "market", "field": "close"},
                    {"source": "execution_state", "field": "available_cash"},
                ]
            ),
            "missing_source": self._analysis_response(
                evidence_references=[
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_4"},
                    {"source": "market", "field": "close"},
                ]
            ),
            "extra_top_level_key": self._analysis_response(
                unexpected_key="프롬프트가 금지한 추가 key"
            ),
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(AnalysisValidationError):
                    await self._run_analysis(response)


class BeliefLimitAndRetryBudgetTests(unittest.IsolatedAsyncioTestCase):
    """길이 한도 상향과 재시도 예산 상향이 실제 호출 경로에 반영되는지 본다."""

    def test_every_belief_dimension_limit_is_150(self) -> None:
        self.assertEqual(
            config.BELIEF_LIMITS,
            {f"dim_{index}": 150 for index in range(1, 7)},
        )

    async def _stb_attempts(self, response: dict[str, Any]) -> _CaptureClient:
        client = _CaptureClient(response)
        with self.assertRaises(BeliefValidationError):
            await generate_short_term_belief(
                {"agent_id": "a", "news_depth": 1, "persona_prompt": "p"},
                event={
                    "event_id": "e",
                    "turn": 1,
                    "date": "2026-02-27",
                    "subturn": "am",
                },
                current_evidence={
                    "news": [],
                    "depth2_search_results": [],
                    "community_claims": [],
                },
                allowed_evidence_ids={"news:x"},
                client=client,
            )
        return client

    async def test_dimension_length_is_not_enforced_at_or_over_150(self) -> None:
        # 2026-07-31: 글자수는 프롬프트의 목표치일 뿐이다. 150자와 151자
        # 모두 재시도 없이 그대로 통과해야 한다.
        evidence = _evidence()
        for length in (150, 151):
            ok = {**_stb_dimensions("stb"), "dim_2": "가" * length}
            client = _CaptureClient({**ok, "dimension_evidence": evidence})
            result = await generate_short_term_belief(
                {"agent_id": "a", "news_depth": 1, "persona_prompt": "p"},
                event={"event_id": "e", "turn": 1, "date": "2026-02-27", "subturn": "am"},
                current_evidence={
                    "news": [],
                    "depth2_search_results": [],
                    "community_claims": [],
                },
                allowed_evidence_ids=set(),
                client=client,
                validation_attempts=1,
            )
            self.assertEqual(len(result["dim_2"]), length)
            self.assertEqual(len(client.prompts), 1)

    async def test_production_stages_retry_ten_times_before_failing(self) -> None:
        # 재시도 예산을 올려도 temperature/seed 스케줄 길이가 따라오지 않으면
        # journal 경계에서 죽는다. 실제 호출 횟수로 두 값이 함께 늘었는지 본다.
        failing = await self._stb_attempts(
            {**_stb_dimensions("stb"), "dimension_evidence": _evidence()
             | {"dim_1": {"support": ["news:missing"], "contradict": []}}}
        )
        self.assertEqual(len(failing.prompts), 10)

        select_client = _CaptureClient({"selected_post_ids": [999]})
        with self.assertRaises(CommunityValidationError):
            await community_reading_select(
                {"persona_prompt": "p"},
                [{"post_id": 1, "title": "t", "post_type": "analysis"}],
                5,
                client=select_client,
                seed=1,
            )
        self.assertEqual(len(select_client.prompts), 10)


if __name__ == "__main__":
    unittest.main()
