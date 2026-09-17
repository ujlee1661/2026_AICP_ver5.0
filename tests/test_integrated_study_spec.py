from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import config
from twinmarket_kr.agents.memory_agent import load_agents_from_sys100
from twinmarket_kr.experiment_runtime import canonical_sha256
from twinmarket_kr.study_spec import (
    IntegratedStudySpecError,
    integrated_prompt_bundle_sha256,
    validate_integrated_study_profile,
)


class IntegratedStudySpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sealed_root = config.SEALED_PROFILE_ROOT
        self.agents = load_agents_from_sys100(config.SYS_100_DB)

    def _validate(
        self,
        root: Path,
        agents: list[dict] | None = None,
    ):
        return validate_integrated_study_profile(
            root,
            agents=self.agents if agents is None else agents,
            condition_id="RN_COMM_ON",
            prompt_dir=config.PROMPT_DIR,
            expected_stock_code=config.STOCK_CODE,
            expected_model=config.PAPER_OPENROUTER_MODEL,
            expected_provider=config.PAPER_OPENROUTER_PROVIDER,
        )

    def _copy_json_bundle(self, target: Path) -> None:
        target.mkdir()
        for filename in (
            "study_spec.json",
            "cohort.json",
            "persona_projection.json",
            "calendar.json",
            "prices.json",
            "stage_inputs.json",
            "known_injection.json",
            "review.json",
            "news.json",
        ):
            shutil.copyfile(
                self.sealed_root / filename,
                target / filename,
            )

    def test_current_sealed_profile_matches_common_runtime(self) -> None:
        profile = self._validate(self.sealed_root)
        self.assertEqual(profile.required_agent_count, 100)
        self.assertEqual(profile.stock_code, config.STOCK_CODE)
        self.assertEqual(profile.instrument_name, "삼성전자")
        self.assertEqual(len(profile.schedule_date_ids), 45)
        self.assertEqual(profile.per_arm_concurrency, 8)
        self.assertEqual(len(profile.burn_in_dates), 3)

    def test_sealed_belief_limits_track_config_as_the_single_source(self) -> None:
        """한도 정본은 config 하나여야 한다.

        예전에는 이 검증기와 ``scripts/15_seal_study.py``가 각각 같은 값을
        literal로 들고 있어서, config만 바꾸면 봉인 spec은 옛 값을 유지한 채
        런타임 프롬프트와 ``belief_dimensions``만 새 값으로 도는 조용한
        불일치가 가능했다.
        """

        spec = json.loads(
            (self.sealed_root / "study_spec.json").read_text(encoding="utf-8")
        )
        self.assertEqual(spec["belief_limits"], dict(config.BELIEF_LIMITS))

        # config를 바꾸면 기존 봉인 spec은 반드시 거부돼야 한다. 검증기가
        # literal을 들고 있으면 이 단언이 실패한다.
        drifted = {**config.BELIEF_LIMITS, "dim_3": config.BELIEF_LIMITS["dim_3"] + 10}
        with mock.patch.object(config, "BELIEF_LIMITS", drifted):
            with self.assertRaisesRegex(
                IntegratedStudySpecError,
                "belief_limits",
            ):
                self._validate(self.sealed_root)

    def test_rejects_community_depth_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sealed"
            self._copy_json_bundle(root)
            spec_path = root / "study_spec.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["community_policy"]["depth2_selective_read_cap"] = 10
            spec_path.write_text(
                json.dumps(spec, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                IntegratedStudySpecError,
                "community D2 cap",
            ):
                self._validate(root)

    def test_rejects_news_cross_category_backfill_policy_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sealed"
            self._copy_json_bundle(root)
            spec_path = root / "study_spec.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["news_exposure_policy"]["cross_category_backfill"] = True
            spec_path.write_text(
                json.dumps(spec, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                IntegratedStudySpecError,
                "cross-category backfill",
            ):
                self._validate(root)

    def test_rejects_d2_frozen_author_profile_policy_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sealed"
            self._copy_json_bundle(root)
            spec_path = root / "study_spec.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["context_window_policy"][
                "community_public_author_private_portfolio_or_trade_visibility"
            ] = "forbidden"
            spec_path.write_text(
                json.dumps(spec, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                IntegratedStudySpecError,
                "D2 frozen author profile",
            ):
                self._validate(root)

    def test_rejects_structured_persona_depth_mismatch(self) -> None:
        changed = [dict(agent) for agent in self.agents]
        changed[0]["news_depth"] = (
            int(changed[0]["news_depth"]) + 1
        ) % 3
        with self.assertRaisesRegex(
            IntegratedStudySpecError,
            "news_depth",
        ):
            self._validate(self.sealed_root, changed)

    def test_rejects_structured_community_post_permission_mismatch(self) -> None:
        changed = [dict(agent) for agent in self.agents]
        changed[0]["can_post_community"] = 1 - int(
            changed[0]["can_post_community"]
        )
        with self.assertRaisesRegex(
            IntegratedStudySpecError,
            "can_post_community",
        ):
            self._validate(self.sealed_root, changed)

    def test_rejects_non_prompt_matching_metadata_drift(self) -> None:
        changed = [dict(agent) for agent in self.agents]
        changed[0]["match_score"] = int(changed[0]["match_score"]) + 1
        with self.assertRaisesRegex(
            IntegratedStudySpecError,
            "structured_persona_sha256",
        ):
            self._validate(self.sealed_root, changed)

    def test_profile_can_seal_an_explicit_runtime_cohort_subset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sealed"
            self._copy_json_bundle(root)
            cohort_path = root / "cohort.json"
            projection_path = root / "persona_projection.json"
            spec_path = root / "study_spec.json"
            cohort = json.loads(cohort_path.read_text(encoding="utf-8"))
            projection = json.loads(
                projection_path.read_text(encoding="utf-8")
            )
            selected_ids = ("A002", "A001", "A100")
            cohort_by_id = {
                str(row["agent_id"]): row
                for row in cohort["agents"]
            }
            projection_by_id = {
                str(row["agent_id"]): row
                for row in projection["agents"]
            }
            cohort["agents"] = [
                {
                    **cohort_by_id[agent_id],
                    "ordinal": ordinal,
                }
                for ordinal, agent_id in enumerate(selected_ids, start=1)
            ]
            projection["agents"] = [
                {
                    **projection_by_id[agent_id],
                    "ordinal": ordinal,
                }
                for ordinal, agent_id in enumerate(selected_ids, start=1)
            ]
            cohort_path.write_text(
                json.dumps(cohort, ensure_ascii=False),
                encoding="utf-8",
            )
            projection_path.write_text(
                json.dumps(projection, ensure_ascii=False),
                encoding="utf-8",
            )
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["required_agent_count"] = len(selected_ids)
            spec["cohort_registry_sha256"] = canonical_sha256(cohort)
            spec["persona_projection_manifest_sha256"] = canonical_sha256(
                projection
            )
            spec["prompt_bundle_sha256"] = integrated_prompt_bundle_sha256(
                config.PROMPT_DIR
            )
            spec["cohort_assertions"] = {
                "depth_counts": dict(
                    Counter(
                        str(row["news_depth"])
                        for row in cohort["agents"]
                    )
                ),
                "initial_cash_counts": dict(
                    Counter(
                        str(row["initial_cash"])
                        for row in cohort["agents"]
                    )
                ),
                "community_post_permission_counts": dict(
                    Counter(
                        str(row["can_post_community"])
                        for row in cohort["agents"]
                    )
                ),
            }
            spec_path.write_text(
                json.dumps(spec, ensure_ascii=False),
                encoding="utf-8",
            )

            profile = self._validate(root)

            self.assertEqual(profile.required_agent_count, 3)
            self.assertEqual(profile.agent_ids, selected_ids)


if __name__ == "__main__":
    unittest.main()
