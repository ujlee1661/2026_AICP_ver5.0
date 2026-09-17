from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import config
from twinmarket_kr.agents.memory_agent import load_agents_from_sys100
from twinmarket_kr.persona.select import (
    generate_persona_prompt,
    save_sys_100,
    verify_distribution,
)
from twinmarket_kr.simulation import select_simulation_agents


def _cohort() -> list[dict[str, object]]:
    depths = ([0] * 30) + ([1] * 55) + ([2] * 15)
    return [
        {
            "agent_id": f"A{index:03d}",
            "news_depth": depth,
        }
        for index, depth in enumerate(depths, start=1)
    ]


class SimulationCohortSelectionTests(unittest.TestCase):
    def test_persona_instrument_is_profile_driven_without_changing_features(self) -> None:
        agent = load_agents_from_sys100(config.SYS_100_DB)[0]
        prompt = generate_persona_prompt(
            agent,
            instrument_name="테스트 종목",
        )

        self.assertIn("테스트 종목 개인투자자", prompt)
        self.assertIn("테스트 종목 단일 자산", prompt)
        self.assertNotIn("삼성전자", prompt)
        self.assertNotIn("나이는", prompt)
        self.assertIn("기본 뉴스의 기사 제목(헤드라인)", prompt)

    def test_loader_regenerates_persona_from_structured_depth_without_mutating_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "sys_100.sqlite"
            source_prompt = "오염된 prompt: 최근 7일 뉴스까지 추가 검색"
            save_sys_100(
                [
                    {
                        "agent_id": "A001",
                        "source_user_id": "source-1",
                        "user_type": "ordinary",
                        "gender": "male",
                        "age": 35,
                        "age_group": "30대",
                        "location": "서울",
                        "bh_disposition_effect_category": "medium",
                        "bh_lottery_preference_category": "low",
                        "bh_total_return_category": "medium",
                        "bh_underdiversification_category": "low",
                        "strategy": "value",
                        "trad_pro": 0,
                        "fol_ind": '["전기전자", "반도체"]',
                        "ini_cash": 100_000_000,
                        "news_depth": 0,
                        "momentum_contrarian": "neutral",
                        "segment_key": "fixture",
                        "match_score": 1,
                        "persona_prompt": source_prompt,
                    }
                ],
                output_db=db_path,
            )

            loaded = load_agents_from_sys100(db_path)
            with sqlite3.connect(db_path) as connection:
                persisted_prompt = connection.execute(
                    "SELECT persona_prompt FROM agents WHERE agent_id = 'A001'"
                ).fetchone()[0]

        self.assertEqual(persisted_prompt, source_prompt)
        self.assertTrue(loaded[0]["persona_prompt_regenerated"])
        self.assertIn(
            "기본 뉴스의 기사 제목(헤드라인)을 모두 확인",
            loaded[0]["persona_prompt"],
        )
        self.assertNotIn("성별은", loaded[0]["persona_prompt"])
        self.assertNotIn("거주 지역은", loaded[0]["persona_prompt"])
        self.assertNotIn("[기본 정보]", loaded[0]["persona_prompt"])
        self.assertNotIn("거래 경험 지표", loaded[0]["persona_prompt"])
        self.assertNotIn("관심 산업", loaded[0]["persona_prompt"])
        self.assertNotIn("최근 7일 뉴스까지 추가 검색", loaded[0]["persona_prompt"])
        self.assertNotEqual(
            loaded[0]["source_persona_prompt_sha256"],
            loaded[0]["persona_prompt_sha256"],
        )
        self.assertEqual(loaded[0]["can_post_community"], 0)

    def test_all_100_prompts_keep_structured_non_demographic_feature_set(self) -> None:
        agents = load_agents_from_sys100(config.SYS_100_DB)
        self.assertEqual(len(agents), 100)
        for agent in agents:
            prompt = str(agent["persona_prompt"])
            self.assertEqual(len(prompt.rstrip("\n").split("\n")), 10)
            self.assertTrue(prompt.endswith("\n"))
            self.assertFalse(prompt.endswith("\n\n"))
            self.assertNotIn("나이는", prompt)
            self.assertNotIn("거주 지역은", prompt)
            self.assertIn(
                f"현금 {int(agent['ini_cash']):,}원",
                prompt,
            )
            self.assertIn(
                "기본 뉴스의 기사 제목(헤드라인)",
                prompt,
            )
            self.assertNotIn("[기본 정보]", prompt)
            self.assertNotIn("거래 경험 지표", prompt)
            self.assertNotIn("관심 산업", prompt)
            self.assertNotIn("source_user_id", prompt)
            self.assertNotIn("segment_key", prompt)
            self.assertNotIn("match_score", prompt)
            depth = int(agent["news_depth"])
            if depth == 0:
                self.assertIn("기사 요약은 확인하지 않는", prompt)
                self.assertNotIn("추가 뉴스 최대 5건", prompt)
            elif depth == 1:
                self.assertIn("기사 요약을 모두 확인", prompt)
                self.assertNotIn("추가 뉴스 최대 5건", prompt)
            else:
                self.assertIn("최근 7일", prompt)
                self.assertIn("추가 뉴스 최대 5건", prompt)

    def test_full_100_agent_cohort_preserves_30_55_15_distribution(self) -> None:
        cohort = _cohort()

        with patch(
            "twinmarket_kr.simulation.load_agents_from_sys100",
            return_value=cohort,
        ):
            selected = select_simulation_agents(max_agents=100)

        self.assertEqual(
            Counter(int(agent["news_depth"]) for agent in selected),
            {0: 30, 1: 55, 2: 15},
        )
        self.assertEqual(
            [agent["agent_id"] for agent in selected],
            [agent["agent_id"] for agent in cohort],
        )

    def test_checked_in_persona_report_matches_current_cohort(self) -> None:
        agents = load_agents_from_sys100(config.SYS_100_DB)
        expected = json.loads(
            json.dumps(verify_distribution(agents), ensure_ascii=False)
        )
        actual = json.loads(
            (
                config.OUTPUT_DIR / "persona_validation_report.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(expected["distribution_pass"])
        self.assertEqual(expected, actual)

    def test_reduced_cohort_is_prefix_without_forced_depth2_replacement(self) -> None:
        cohort = [
            {"agent_id": "A001", "news_depth": 1},
            {"agent_id": "A002", "news_depth": 0},
            {"agent_id": "A003", "news_depth": 1},
            {"agent_id": "A004", "news_depth": 1},
            {"agent_id": "A005", "news_depth": 0},
            {"agent_id": "A006", "news_depth": 2},
        ]

        with patch(
            "twinmarket_kr.simulation.load_agents_from_sys100",
            return_value=cohort,
        ):
            selected = select_simulation_agents(max_agents=5)

        self.assertEqual(
            [agent["agent_id"] for agent in selected],
            ["A001", "A002", "A003", "A004", "A005"],
        )
        self.assertNotIn(2, {int(agent["news_depth"]) for agent in selected})

    def test_sealed_agent_ids_select_exact_members_in_manifest_order(self) -> None:
        cohort = [
            {"agent_id": "A001", "news_depth": 1},
            {"agent_id": "A002", "news_depth": 0},
            {"agent_id": "A003", "news_depth": 2},
        ]
        with patch(
            "twinmarket_kr.simulation.load_agents_from_sys100",
            return_value=cohort,
        ):
            selected = select_simulation_agents(
                agent_ids=["A003", "A001"],
            )

        self.assertEqual(
            [agent["agent_id"] for agent in selected],
            ["A003", "A001"],
        )


if __name__ == "__main__":
    unittest.main()
