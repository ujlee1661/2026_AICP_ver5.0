from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.community.agent import CommunityAgent
from twinmarket_kr.community.reading import (
    REACT_OUTPUT_CONTRACT,
    SELECT_OUTPUT_CONTRACT,
    _format_post_list,
    _format_posts_content,
    _validate_selection,
    community_reading_select,
)
from twinmarket_kr.community.thinking import _format_best_posts, _format_posts_read
from twinmarket_kr.llm.belief import load_prompt, render_prompt
from twinmarket_kr.community.validation import (
    CommunityValidationError,
    can_author_community_post,
    expected_selective_read_limit,
    validate_post_body,
    validate_selective_read_limits,
)
from twinmarket_kr.run_logger import (
    SimulationLogger,
    finalize_community_delivery_counts,
)
from twinmarket_kr.run_integrity import validate_community_artifacts
from twinmarket_kr.simulation import (
    post_trade_posting_phase,
    validate_community_runtime_policy,
)


class CommunityPolicyTests(unittest.TestCase):
    def test_community_on_rejects_partial_global_feature_flags(self) -> None:
        validate_community_runtime_policy("on")
        validate_community_runtime_policy("off")

        for posting, reading in ((False, True), (True, False), (False, False)):
            with (
                self.subTest(posting=posting, reading=reading),
                patch("config.ENABLE_COMMUNITY_POSTING", posting),
                patch("config.ENABLE_COMMUNITY_READING", reading),
            ):
                with self.assertRaisesRegex(ValueError, "partial community arms"):
                    validate_community_runtime_policy("on")
                validate_community_runtime_policy("off")

    def test_integrity_contract_accepts_d0_full_best_and_d1_d2_writers(self) -> None:
        body = "Best 게시글 원문"
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        agent_ids = ["D0", "D1", "D2"]
        depth_by_agent = {"D0": 0, "D1": 1, "D2": 2}
        best_row = {
            "date": "2026-02-27",
            "turn": "2",
            "post_id": "1",
            "rank": "1",
            "author_agent_id": "D1",
            "content": body,
            "body_sha256": body_sha256,
            "score": "3",
            "like_count": "3",
            "scheduled_delivery_count": "2",
            "actual_delivery_count": "2",
            "delivery_status": "delivered_am",
            "self_exclusion_policy": "exclude_author_no_backfill",
        }

        def delivery(agent_id: str, author_profile: str, profile_scope: str) -> dict[str, str]:
            return {
                "date": "2026-03-02",
                "turn": "3",
                "source_date": "2026-02-27",
                "source_turn": "2",
                "agent_id": agent_id,
                "post_id": "1",
                "exposure_level": "full_body",
                "content": body,
                "body_sha256": body_sha256,
                "author_profile": author_profile,
                "profile_scope": profile_scope,
                "delivery_status": "delivered_am",
                "is_best": "True",
                "selected": "False",
                "replay": "False",
            }

        errors = validate_community_artifacts(
            community_mode="on",
            agent_ids=agent_ids,
            depth_by_agent=depth_by_agent,
            community_rows=[
                {
                    "agent_id": "D0",
                    "posts_read_json": "[]",
                    "best_posts_json": json.dumps(
                        [{"post_id": 1, "content": body}],
                        ensure_ascii=False,
                    ),
                },
                {"agent_id": "D1"},
                {"agent_id": "D2"},
            ],
            post_rows=[
                {"agent_id": "D1", "content": "D1 글"},
                {"agent_id": "D2", "content": "D2 글"},
            ],
            interaction_rows=[
                delivery("D0", "null", ""),
                delivery("D2", '{"snapshot_turn": 2}', "detailed"),
            ],
            best_rows=[best_row],
            selection_rows=[],
            post_permission_by_agent={"D0": False, "D1": True, "D2": True},
        )

        self.assertEqual(errors, [])

    def test_explicit_post_permission_is_fail_closed_and_independent_of_reading(self) -> None:
        self.assertTrue(
            can_author_community_post(
                {"news_depth": 1, "can_post_community": 1}
            )
        )
        for agent in (
            {"news_depth": 1, "can_post_community": 0},
            {"news_depth": 2},
            {"news_depth": 0, "can_post_community": 1},
        ):
            with self.subTest(agent=agent):
                self.assertFalse(can_author_community_post(agent))

        errors = validate_community_artifacts(
            community_mode="on",
            agent_ids=["D1"],
            depth_by_agent={"D1": 1},
            community_rows=[],
            post_rows=[{"agent_id": "D1", "content": "권한 없는 글"}],
            interaction_rows=[],
            best_rows=[],
            selection_rows=[],
            post_permission_by_agent={"D1": False},
        )
        self.assertIn("ineligible community post author=D1", errors)

    def test_integrity_contract_rejects_d0_post_and_title_only_best(self) -> None:
        errors = validate_community_artifacts(
            community_mode="on",
            agent_ids=["D0"],
            depth_by_agent={"D0": 0},
            community_rows=[],
            post_rows=[{"agent_id": "D0", "content": "금지된 글"}],
            interaction_rows=[
                {
                    "date": "2026-03-02",
                    "turn": "3",
                    "agent_id": "D0",
                    "post_id": "1",
                    "exposure_level": "title_only",
                    "delivery_status": "delivered_am",
                }
            ],
            best_rows=[],
            selection_rows=[],
        )

        self.assertTrue(any("ineligible community post author=D0" in error for error in errors))
        self.assertTrue(any("Depth 0 title-only exposure agent=D0" in error for error in errors))

    def test_approved_depth_limits_are_exact(self) -> None:
        validate_selective_read_limits(depth1=5, depth2=5)
        self.assertEqual(expected_selective_read_limit(0), 0)
        self.assertEqual(expected_selective_read_limit(1), 5)
        self.assertEqual(expected_selective_read_limit(2), 5)

        for depth1, depth2 in ((4, 5), (6, 5), (5, 4), (5, 6)):
            with self.subTest(depth1=depth1, depth2=depth2):
                with self.assertRaisesRegex(
                    CommunityValidationError,
                    "D1=5 and D2=5",
                ):
                    validate_selective_read_limits(
                        depth1=depth1,
                        depth2=depth2,
                    )

    def test_integrity_contract_rejects_legacy_d2_ten_limit(self) -> None:
        errors = validate_community_artifacts(
            community_mode="on",
            agent_ids=["D2"],
            depth_by_agent={"D2": 2},
            community_rows=[],
            post_rows=[],
            interaction_rows=[],
            best_rows=[],
            selection_rows=[
                {
                    "agent_id": "D2",
                    "read_limit": "10",
                }
            ],
        )

        self.assertTrue(
            any(
                "depth=2 limit=10" in error
                for error in errors
            )
        )

    def test_selective_read_cap_boundary_is_not_silently_truncated(self) -> None:
        # AGENTS.md 검증 원칙: a D1 selection of 6 and a D2 selection of 11 must
        # fail, while 5 passes for both depths.  The runtime boundary must reject
        # the whole over-cap selection instead of trimming it to the cap, because
        # a silent trim would change which posts the agent actually read.
        available = set(range(1, 12))
        for depth, over_cap in ((1, 6), (2, 11)):
            limit = expected_selective_read_limit(depth)
            self.assertEqual(limit, 5)

            at_cap, errors = _validate_selection(
                {"selected_post_ids": list(range(1, limit + 1))},
                available=available,
                read_limit=limit,
            )
            self.assertEqual(at_cap, list(range(1, limit + 1)))
            self.assertEqual(errors, [])

            selected, errors = _validate_selection(
                {"selected_post_ids": list(range(1, over_cap + 1))},
                available=available,
                read_limit=limit,
            )
            self.assertIn(f"selected_post_ids:exceeds_limit:{over_cap}>{limit}", errors)
            # No silent truncation: the rejected selection keeps its real size.
            self.assertEqual(len(selected), over_cap)

        # Depth 0 reads nothing selectively, so any selection is over cap.
        self.assertEqual(expected_selective_read_limit(0), 0)
        _, errors = _validate_selection(
            {"selected_post_ids": [1]},
            available=available,
            read_limit=expected_selective_read_limit(0),
        )
        self.assertIn("selected_post_ids:exceeds_limit:1>0", errors)

    def test_post_body_boundary_is_not_silently_truncated(self) -> None:
        body = "가" * 500
        self.assertEqual(validate_post_body(body), body)
        with self.assertRaisesRegex(CommunityValidationError, "500 characters"):
            validate_post_body("가" * 501)


class CommunityPostingPermissionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_posting_llm_is_called_only_for_explicitly_permitted_agents(self) -> None:
        permitted = {
            "agent_id": "A001",
            "news_depth": 1,
            "can_post_community": 1,
        }
        blocked = {
            "agent_id": "A002",
            "news_depth": 2,
            "can_post_community": 0,
        }
        turn_results = [
            {"agent": permitted, "ltb": {"ltb_id": "l1"}},
            {"agent": blocked, "ltb": {"ltb_id": "l2"}},
        ]
        execution_by_agent = {
            "A001": {"fill": {"fill_id": "f1"}},
            "A002": {"fill": {"fill_id": "f2"}},
        }
        decision = AsyncMock(return_value=None)
        with patch(
            "twinmarket_kr.simulation.posting_decision",
            decision,
        ):
            await post_trade_posting_phase(
                turn_results=turn_results,
                community_agent=object(),
                execution_by_agent=execution_by_agent,
                turn=2,
                date="2026-02-27",
                client=object(),
                concurrency=2,
            )

        self.assertEqual(decision.await_count, 1)
        self.assertEqual(decision.await_args.args[0]["agent_id"], "A001")

    def test_legacy_database_write_rechecks_post_body_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            community = CommunityAgent(Path(temporary) / "community.sqlite")
            post_id = community.save_post(
                "A001",
                1,
                "2026-02-27",
                "analysis",
                "500 boundary",
                "가" * 500,
            )
            self.assertGreater(post_id, 0)
            self.assertEqual(len(community.get_post_content(post_id)["content"]), 500)

            with self.assertRaisesRegex(CommunityValidationError, "500 characters"):
                community.save_post(
                    "A001",
                    1,
                    "2026-02-27",
                    "analysis",
                    "501 rejected",
                    "가" * 501,
                )

    def test_neutral_reaction_is_preserved_as_none_for_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            community = CommunityAgent(Path(temporary) / "community.sqlite")
            post_id = community.save_post(
                "AUTHOR",
                2,
                "2026-02-27",
                "analysis",
                "neutral",
                "body",
            )

            recorded = community.record_reaction(
                "READER",
                post_id,
                2,
                "2026-02-27",
                "none",
            )

            with sqlite3.connect(community._db) as connection:
                stored = connection.execute(
                    "SELECT reaction FROM community_interactions"
                ).fetchone()[0]
                score = connection.execute(
                    "SELECT score FROM community_posts WHERE post_id = ?",
                    (post_id,),
                ).fetchone()[0]

        self.assertTrue(recorded)
        self.assertEqual(stored, "none")
        self.assertEqual(score, 0)

    def test_legacy_selector_enforces_depth_boundaries(self) -> None:
        class FakeClient:
            def __init__(self, selected_ids: list[int]) -> None:
                self.selected_ids = selected_ids

            async def chat(self, *_: object, **__: object) -> dict[str, object]:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"selected_post_ids": self.selected_ids}
                                )
                            }
                        }
                    ]
                }

        posts = [
            {
                "post_id": index,
                "post_type": "analysis",
                "title": f"title-{index}",
                "anonymous_code": f"곰-{1000 + index}",
            }
            for index in range(1, 12)
        ]
        for limit in (5,):
            with self.subTest(limit=limit, boundary="accepted"):
                selected = asyncio.run(
                    community_reading_select(
                        {"persona_prompt": "persona"},
                        posts,
                        limit,
                        client=FakeClient(list(range(1, limit + 1))),
                    )
                )
                self.assertEqual(len(selected), limit)
            with self.subTest(limit=limit, boundary="rejected"):
                with self.assertRaises(CommunityValidationError):
                    asyncio.run(
                        community_reading_select(
                            {"persona_prompt": "persona"},
                            posts,
                            limit,
                            client=FakeClient(list(range(1, limit + 2))),
                        )
                    )


class CommunityBestPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temporary.name) / "community.sqlite"
        self.community = CommunityAgent(self.db_path)
        self.memory = MemoryAgent(self.db_path)
        self.memory.init_portfolio_t000(
            [{"agent_id": "A001", "ini_cash": 10_000_000}],
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _save_ranked_posts(self) -> list[int]:
        post_ids = [
            self.community.save_post(
                f"A{index:03d}",
                3,
                "2026-02-27",
                "analysis",
                f"title-{index}",
                f"body-{index}",
            )
            for index in range(1, 7)
        ]
        for score, post_id in zip((6, 5, 4, 3, 2, 1), post_ids, strict=True):
            for _ in range(score):
                self.community.update_post_score_live(post_id, "like")
        return post_ids

    def test_best_is_full_body_depth_projected_and_self_excluded_without_backfill(self) -> None:
        post_ids = self._save_ranked_posts()
        frozen = self.community.freeze_best_posts(
            date="2026-02-27",
            turn=3,
            n=5,
            memory_agent=self.memory,
        )
        self.assertEqual([post["post_id"] for post in frozen], post_ids[:5])
        self.assertEqual(frozen[0]["content"], "body-1")
        self.assertEqual(len(frozen[0]["body_sha256"]), 64)

        for depth in (0, 1):
            with self.subTest(depth=depth):
                projected = self.community.project_best_posts_for_reader(
                    frozen,
                    recipient_agent_id="A001",
                    depth=depth,
                )
                self.assertEqual(
                    [post["post_id"] for post in projected],
                    post_ids[1:5],
                )
                self.assertNotIn(post_ids[5], [post["post_id"] for post in projected])
                self.assertTrue(all(post["content"].startswith("body-") for post in projected))
                self.assertTrue(all(post["author_profile"] is None for post in projected))
                self.assertTrue(all("author_agent_id" not in post for post in projected))

        d2 = self.community.project_best_posts_for_reader(
            frozen,
            recipient_agent_id="A002",
            depth=2,
        )
        self.assertEqual([post["post_id"] for post in d2], [post_ids[0], *post_ids[2:5]])
        self.assertEqual(d2[0]["profile_scope"], "detailed")
        self.assertEqual(d2[0]["author_profile"]["snapshot_turn"], 3)

    def test_d2_profile_is_frozen_at_pm_and_excludes_private_reason_and_future_trade(self) -> None:
        for turn in range(1, 5):
            self.memory.append_trade_log(
                {
                    "agent_id": "A001",
                    "turn": turn,
                    "date": f"2026-02-{24 + turn:02d}",
                    "action": "buy",
                    "stock_code": "005930",
                    "quantity": turn,
                    "executed_price": 70_000 + turn,
                    "trade_value": (70_000 + turn) * turn,
                    "status": "filled",
                    "filled_quantity": turn,
                    "action_reason": f"private-reason-{turn}",
                }
            )
        self.memory.append_trade_log(
            {
                "log_id": "unfilled-at-snapshot",
                "agent_id": "A001",
                "turn": 3,
                "date": "2026-02-27",
                "action": "sell",
                "stock_code": "005930",
                "quantity": 99,
                "status": "unfilled",
                "filled_quantity": 0,
                "action_reason": "private-unfilled-reason",
            }
        )
        self.community.save_post(
            "A001",
            3,
            "2026-02-27",
            "analysis",
            "profile freeze",
            "frozen body",
        )
        frozen = self.community.freeze_best_posts(
            date="2026-02-27",
            turn=3,
            n=5,
            memory_agent=self.memory,
        )
        profile = frozen[0]["author_profile_snapshot"]
        self.assertEqual(
            [trade["turn"] for trade in profile["recent_trades"]],
            [3, 2, 1],
        )
        self.assertTrue(
            all(
                trade["status"] == "filled" and trade["filled_quantity"] > 0
                for trade in profile["recent_trades"]
            )
        )
        self.assertTrue(
            all("action_reason" not in trade for trade in profile["recent_trades"])
        )
        self.assertNotIn("agent_id", profile["portfolio_summary"])

        self.memory.append_trade_log(
            {
                "log_id": "future-overwrite-proof",
                "agent_id": "A001",
                "turn": 5,
                "date": "2026-03-02",
                "action": "sell",
                "stock_code": "005930",
                "quantity": 1,
                "status": "filled",
                "filled_quantity": 1,
            }
        )
        projected = self.community.project_best_posts_for_reader(
            frozen,
            recipient_agent_id="A999",
            depth=2,
        )
        self.assertEqual(
            [trade["turn"] for trade in projected[0]["author_profile"]["recent_trades"]],
            [3, 2, 1],
        )

    def test_full_body_format_does_not_truncate_and_deduplicates_best_overlap(self) -> None:
        long_body = "본문" * 220
        selected = {
            "post_id": 1,
            "post_type": "analysis",
            "title": "same",
            "content": long_body,
            "reaction": "like",
            "anonymous_code": "황소-1001",
        }
        best_text, _best_sources = _format_best_posts(
            [{**selected, "rank": 1, "score": 2}],
            selected_by_id={1: selected},
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
        )
        selected_text, _selected_sources = _format_posts_read(
            [selected],
            excluded_post_ids={1},
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
        )
        self.assertIn(long_body, best_text)
        self.assertIn("내 반응: like", best_text)
        self.assertNotIn(long_body, selected_text)

    def test_select_and_react_prompts_preserve_legacy_visibility_rules(self) -> None:
        profile = {
            "portfolio_summary": {"cash": 10_000_000},
            "recent_trades": [
                {"turn": turn, "action": "buy", "quantity": turn}
                for turn in range(1, 4)
            ],
            "note": "프로필" * 400,
        }
        post = {
            "post_id": 1,
            "post_type": "analysis",
            "title": "title only",
            "content": "본문 전체",
            "anonymous_code": "곰-1001",
            "like_count": 2,
            "unlike_count": 1,
            "score": 1,
            "author_profile": profile,
        }
        candidate_text = _format_post_list([post])
        self.assertNotIn("본문 전체", candidate_text)
        self.assertIn("곰-1001", candidate_text)
        self.assertIn("score 1", candidate_text)

        full_text = _format_posts_content([post])
        self.assertIn("본문 전체", full_text)
        self.assertIn("곰-1001", full_text)
        self.assertIn(profile["note"], full_text)

    def test_no_author_reputation_signal_reaches_any_community_prompt(self) -> None:
        """뱃지·평판 신호는 어느 커뮤니티 단계 프롬프트에도 남지 않는다."""
        post = {
            "post_id": 1,
            "post_type": "analysis",
            "title": "reputation free",
            "content": "본문",
            "anonymous_code": "여우-3021",
            "like_count": 0,
            "unlike_count": 0,
            "score": 0,
            "reaction": "none",
            # 과거 원장/로그에서 흘러들어온 뱃지 키가 있어도 렌더링되지 않아야 한다.
            "author_badges": ["상위 수익자", "자산가", "커뮤니티 인플루언서"],
        }
        best_text, _ = _format_best_posts(
            [{**post, "rank": 1, "score": 0}],
            selected_by_id={},
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
        )
        read_text, _ = _format_posts_read(
            [post],
            excluded_post_ids=set(),
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
        )
        rendered = [
            _format_post_list([post]),
            _format_posts_content([post]),
            best_text,
            read_text,
        ]
        for text in rendered:
            for label in ("뱃지", "배지", "상위 수익자", "자산가", "커뮤니티 인플루언서"):
                self.assertNotIn(label, text)
        selection_prompt = load_prompt("community_reading.txt")
        for label in ("배지", "뱃지", "평판"):
            self.assertNotIn(label, selection_prompt)

    def test_best_and_selected_overlap_keeps_both_exposure_relations(self) -> None:
        """본문은 한 번만 직렬화하되 두 노출 관계 ID를 모두 인용할 수 있어야 한다."""
        body = "겹치는 본문"
        selected = {
            "post_id": 11,
            "post_type": "analysis",
            "title": "overlap",
            "content": body,
            "reaction": "like",
            "anonymous_code": "황소-1001",
        }
        best_text, best_sources = _format_best_posts(
            [{**selected, "rank": 1, "score": 3}],
            selected_by_id={11: selected},
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
        )
        best_id = (
            "community:2026-02-27:t2:post:11:best_full_body:A999:delivered_t3"
        )
        replay_id = (
            "community:2026-02-27:t2:post:11:"
            "selected_full_body_replay:A999:delivered_t3"
        )
        self.assertEqual(set(best_sources), {best_id, replay_id})
        self.assertEqual(best_sources[best_id], best_sources[replay_id])
        # 두 관계가 같은 본문을 가리키더라도 본문은 한 번만 직렬화된다.
        self.assertEqual(best_text.count(body), 1)
        self.assertIn(best_id, best_text)
        self.assertIn(replay_id, best_text)

        # 겹치지 않는 Best는 replay 관계를 만들지 않는다.
        _, only_best_sources = _format_best_posts(
            [{**selected, "rank": 1, "score": 3}],
            selected_by_id={},
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
        )
        self.assertEqual(set(only_best_sources), {best_id})

    def test_one_post_per_agent_per_pm_is_enforced_by_the_database(self) -> None:
        self.community.save_post(
            "A001", 3, "2026-02-27", "analysis", "first", "body",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.community.save_post(
                "A001", 3, "2026-02-27", "impression", "second", "body",
            )
        # 다른 거래일에는 다시 쓸 수 있다.
        self.community.save_post(
            "A001", 5, "2026-03-02", "analysis", "next day", "body",
        )
        self.assertEqual(
            len(self.community.get_today_posts("2026-02-27")),
            1,
        )

    def test_legacy_artifacts_distinguish_title_full_body_and_am_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = SimulationLogger(root_dir=temporary, run_id="community-artifacts")
            candidate = {
                "post_id": 7,
                "anonymous_code": "황소-1007",
                "post_type": "analysis",
                "title": "candidate",
                "like_count": 0,
                "unlike_count": 0,
                "score": 0,
            }
            logger.log_community_selection_input(
                agent_id="D1",
                turn=3,
                date="2026-02-27",
                depth=1,
                read_limit=5,
                visible_posts=[candidate],
                selected_post_ids=[7],
            )
            full_body = {
                **candidate,
                "content": "full body",
                "body_sha256": "a" * 64,
                "reaction": "like",
                "author_profile": None,
                "profile_scope": "minimal",
                "exposure_level": "full_body",
            }
            logger.log_community_reading(
                agent_id="D1",
                turn=3,
                date="2026-02-27",
                selected_post_ids=[7],
                posts_read=[full_body],
            )
            frozen_best = {
                **full_body,
                "rank": 1,
                "is_best": True,
                "author_agent_id": "AUTHOR",
                "scheduled_delivery_count": 1,
                "actual_delivery_count": None,
                "delivery_status": "scheduled_next_am",
            }
            logger.log_community_best_posts(
                turn=3,
                date="2026-02-27",
                best_posts=[frozen_best],
            )
            logger.log_community_delivery(
                agent_id="D1",
                source_turn=3,
                delivery_turn=4,
                source_date="2026-02-27",
                delivery_date="2026-03-02",
                best_posts=[frozen_best],
                posts_read=[full_body],
            )
            logger.log_community_delivery(
                agent_id="D1",
                source_turn=3,
                delivery_turn=4,
                source_date="2026-02-27",
                delivery_date="2026-03-02",
                best_posts=[frozen_best],
                posts_read=[full_body],
            )
            finalize_community_delivery_counts(logger.run_dir)

            with (logger.run_dir / "community_interactions.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                rows = list(csv.DictReader(f))
            with (logger.run_dir / "community_best_posts.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                best_rows = list(csv.DictReader(f))
            trace_rows = [
                json.loads(line)
                for line in (
                    logger.run_dir / "traces" / "community_exposure_trace.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(
            [row["exposure_level"] for row in rows],
            ["title_only", "full_body", "full_body", "full_body"],
        )
        self.assertEqual(rows[0]["content"], "")
        self.assertEqual(rows[1]["content"], "full body")
        self.assertEqual(rows[2]["delivery_status"], "delivered_am")
        self.assertEqual(rows[2]["is_best"], "True")
        self.assertEqual(rows[3]["replay"], "True")
        self.assertEqual(best_rows[0]["actual_delivery_count"], "1")
        self.assertEqual(best_rows[0]["delivery_status"], "delivered_am")
        self.assertEqual(len(trace_rows), 4)
        self.assertTrue(
            all(row["artifact"] == "community_exposure_trace" for row in trace_rows)
        )

    def test_common_logger_connects_delivery_and_right_censors_final_pm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = SimulationLogger(
                root_dir=temporary,
                run_id="event-checkpoint-run",
            )
            previous_pm_best = {
                "post_id": 1,
                "rank": 1,
                "author_agent_id": "AUTHOR",
                "title": "best",
                "post_type": "analysis",
                "content": "body",
                "body_sha256": "b" * 64,
                "scheduled_delivery_count": 1,
                "actual_delivery_count": None,
                "delivery_status": "scheduled_next_am",
            }
            logger.log_community_best_posts(
                turn=2,
                date="2026-02-27",
                best_posts=[previous_pm_best],
            )

            logger.log_community_delivery(
                agent_id="D0",
                source_turn=2,
                delivery_turn=3,
                source_date="2026-02-27",
                delivery_date="2026-03-02",
                best_posts=[previous_pm_best],
                posts_read=[],
            )
            final_pm_best = {
                **previous_pm_best,
                "post_id": 2,
                "rank": 1,
                "title": "final-day-best",
                "content": "final-day-body",
                "body_sha256": "c" * 64,
            }
            logger.log_community_best_posts(
                turn=4,
                date="2026-03-02",
                best_posts=[final_pm_best],
            )

            summary = finalize_community_delivery_counts(logger.run_dir)

            with (logger.run_dir / "community_interactions.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                master_interaction_rows = list(csv.DictReader(f))
            with (logger.run_dir / "community_best_posts.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                master_best_rows = list(csv.DictReader(f))

            self.assertEqual(len(master_interaction_rows), 1)
            self.assertEqual(
                master_interaction_rows[0]["source_date"],
                "2026-02-27",
            )
            self.assertEqual(
                master_interaction_rows[0]["delivery_date"],
                "2026-03-02",
            )
            best_by_post_id = {
                row["post_id"]: row
                for row in master_best_rows
            }
            self.assertEqual(
                best_by_post_id["1"]["actual_delivery_count"],
                "1",
            )
            self.assertEqual(
                best_by_post_id["1"]["delivery_status"],
                "delivered_am",
            )
            self.assertEqual(
                best_by_post_id["2"]["actual_delivery_count"],
                "0",
            )
            self.assertEqual(
                best_by_post_id["2"]["delivery_status"],
                "right_censored",
            )
            self.assertEqual(summary["delivered"], 1)
            self.assertEqual(summary["right_censored"], 1)

    def test_best_with_no_eligible_recipient_is_not_reported_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = SimulationLogger(
                root_dir=temporary,
                run_id="single-agent-community",
            )
            logger.log_community_best_posts(
                turn=2,
                date="2026-02-27",
                best_posts=[
                    {
                        "post_id": 1,
                        "rank": 1,
                        "author_agent_id": "ONLY_AGENT",
                        "title": "self-authored best",
                        "post_type": "analysis",
                        "content": "body",
                        "body_sha256": "d" * 64,
                        "scheduled_delivery_count": 0,
                        "actual_delivery_count": None,
                        "delivery_status": "scheduled_next_am",
                    }
                ],
            )

            summary = finalize_community_delivery_counts(logger.run_dir)

            with (logger.run_dir / "community_best_posts.csv").open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(summary["no_eligible_recipient"], 1)
        self.assertEqual(rows[0]["actual_delivery_count"], "0")
        self.assertEqual(rows[0]["delivery_status"], "no_eligible_recipient")


class CommunityThinkingQuoteContractTests(unittest.TestCase):
    """번호 인용 계약: 모델은 [인용 N]을 고르고 시스템이 원문을 조회한다.

    자유 문자열 인용은 reasoning-off 경량 모델이 구조적으로 실패하는 과제였다
    (공백 통일·조사 변경·짜깁기로 세 번의 유료 실행이 중단). 다른 모든 근거
    (뉴스·검색·outcome)와 같은 ID 인용 + 시스템 조회 패턴으로 통일한다.
    """

    BODY_S1 = "엔비디아 쇼크에 외국인들이 16조나 팔아치우는데 주가는 여전히 꿈틀거려."
    BODY_S2 = "그래도 저평가 구간이라는 생각은 변함없습니다."

    def _community_log(self, *, overlap: bool = False) -> dict:
        best = [
            {
                "post_id": 1,
                "rank": 1,
                "score": 3,
                "post_type": "analysis",
                "title": "외국인 16조 매도세에 개미들만 잔치?",
                "content": f"{self.BODY_S1} {self.BODY_S2}",
                "anonymous_code": "곰-1001",
            }
        ]
        read = [
            {
                "post_id": 2,
                "post_type": "impression",
                "title": "다른 글 제목",
                "content": "다른 글의 본문입니다.",
                "reaction": "like",
                "anonymous_code": "여우-2002",
            }
        ]
        if overlap:
            read.append({**best[0], "reaction": "like"})
        return {
            "turn": 2,
            "date": "2026-02-27",
            "best_posts_seen": best,
            "posts_read": read,
        }

    def _run(self, response: dict, *, overlap: bool = False) -> dict:
        from twinmarket_kr.community.thinking import community_thinking

        class _Client:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            async def chat(self, messages, **_kwargs):
                self.prompts.append(messages[-1]["content"])
                return json.dumps(response, ensure_ascii=False)

        client = _Client()
        result = asyncio.run(
            community_thinking(
                {"agent_id": "A999", "persona_prompt": "페르소나"},
                self._community_log(overlap=overlap),
                delivery_turn=3,
                delivery_date="2026-03-03",
                client=client,
            )
        )
        return {"result": result, "prompt": client.prompts[0]}

    def _payload(self, ref, sources: list[str] | None = None) -> dict:
        return {
            "observed_sentiment": "mixed",
            "claims": [
                {
                    "claim_text": "외국인 매도에도 개인 매수가 이어진다",
                    "claim_stance": "uncertain",
                    "source_exposure_ids": sources
                    or [
                        "community:2026-02-27:t2:post:1:best_full_body:A999:delivered_t3"
                    ],
                    "supporting_quote_ref": ref,
                }
            ],
            "agreement_disagreement": "공감과 반대가 섞여 있다",
            "uncertainty": "외국인 수급 지속 여부",
        }

    def test_prompt_numbers_titles_and_sentences_sequentially(self) -> None:
        run = self._run(self._payload(2))
        prompt = run["prompt"]
        # 글1: 제목=1, 본문 두 문장=2,3 / 글2: 제목=4, 본문=5
        self.assertIn("[인용 1] 외국인 16조 매도세에 개미들만 잔치?", prompt)
        self.assertIn(f"[인용 2] {self.BODY_S1}", prompt)
        self.assertIn(f"[인용 3] {self.BODY_S2}", prompt)
        self.assertIn("[인용 4] 다른 글 제목", prompt)
        self.assertIn("[인용 5] 다른 글의 본문입니다.", prompt)

    def test_ref_is_materialized_into_the_exact_source_sentence(self) -> None:
        run = self._run(self._payload(2))
        claim = run["result"]["claims"][0]
        # 시스템이 조회한 원문이므로 구조상 항상 verbatim이다.
        self.assertEqual(claim["supporting_quote"], self.BODY_S1)
        self.assertEqual(claim["supporting_quote_ref"], 2)

    def test_misattributed_source_is_healed_from_the_ref(self) -> None:
        # 라이브 실패 유형: 진짜 인용을 하고 출처 ID만 다른 글을 지목.
        # 번호가 가리키는 실제 노출 ID를 시스템이 보충한다.
        run = self._run(
            self._payload(
                5,
                sources=[
                    "community:2026-02-27:t2:post:1:best_full_body:A999:delivered_t3"
                ],
            )
        )
        claim = run["result"]["claims"][0]
        self.assertEqual(claim["supporting_quote"], "다른 글의 본문입니다.")
        self.assertIn(
            "community:2026-02-27:t2:post:2:selected_full_body_replay:A999:delivered_t3",
            claim["source_exposure_ids"],
        )

    def test_overlap_post_refs_carry_both_exposure_relations(self) -> None:
        from twinmarket_kr.community.thinking import (
            _format_best_posts,
            _format_posts_read,
        )

        registry: dict[int, dict] = {}
        log = self._community_log(overlap=True)
        _format_best_posts(
            log["best_posts_seen"],
            selected_by_id={
                int(post["post_id"]): post for post in log["posts_read"]
            },
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
            quotable_registry=registry,
        )
        relations = {
            exposure_id.split(":")[5]
            for exposure_id in registry[2]["exposure_ids"]
        }
        self.assertEqual(relations, {"best_full_body", "selected_full_body_replay"})

    def test_unknown_bool_or_textual_ref_fails(self) -> None:
        from twinmarket_kr.community.thinking import _community_thinking_errors

        registry: dict[int, dict] = {}
        log = self._community_log()
        from twinmarket_kr.community.thinking import _format_best_posts

        _, sources = _format_best_posts(
            log["best_posts_seen"],
            agent_id="A999",
            source_turn=2,
            source_date="2026-02-27",
            delivery_turn=3,
            quotable_registry=registry,
        )
        for bad_ref, label in ((99, "unknown"), (True, "bool"), ("2", "text")):
            with self.subTest(ref=label):
                errors = _community_thinking_errors(
                    self._payload(bad_ref),
                    allowed_sources=sources,
                    quotable_registry=registry,
                )
                self.assertTrue(
                    any("supporting_quote_ref" in e for e in errors)
                )
        # 자유 문자열 인용(구 스키마)은 schema 오류로 거부된다.
        legacy = self._payload(2)
        legacy["claims"][0].pop("supporting_quote_ref")
        legacy["claims"][0]["supporting_quote"] = "아무 문장"
        errors = _community_thinking_errors(
            legacy, allowed_sources=sources, quotable_registry=registry
        )
        self.assertTrue(any("invalid_schema" in e for e in errors))

    def test_offline_stub_satisfies_the_ref_contract(self) -> None:
        import os

        with patch.dict(os.environ, {"TWINMARKET_OFFLINE_LLM": "1"}):
            from twinmarket_kr.llm.client import OpenRouterClient

            client = OpenRouterClient()
            result = asyncio.run(
                __import__(
                    "twinmarket_kr.community.thinking", fromlist=["community_thinking"]
                ).community_thinking(
                    {"agent_id": "A999", "persona_prompt": "페르소나"},
                    self._community_log(),
                    delivery_turn=3,
                    delivery_date="2026-03-03",
                    client=client,
                )
            )
        for claim in result["claims"]:
            self.assertIn("supporting_quote", claim)
            self.assertTrue(claim["supporting_quote"])


class ValidationLogSurvivesEventRollbackTests(unittest.TestCase):
    """검증 거부 텔레메트리는 event 롤백에서 살아남아야 한다."""

    def test_validation_log_is_a_control_artifact(self) -> None:
        from twinmarket_kr.experiment_runtime import _CONTROL_ARTIFACTS

        self.assertIn("llm_validation_errors.jsonl", _CONTROL_ARTIFACTS)
        self.assertIn("openrouter_calls.jsonl", _CONTROL_ARTIFACTS)

    def test_rollback_truncates_science_but_keeps_diagnostics(self) -> None:
        from twinmarket_kr.experiment_runtime import (
            capture_artifact_state,
            restore_artifact_state,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            science = root / "agent_turns.csv"
            diagnostics = root / "llm_validation_errors.jsonl"
            science.write_text("header\nrow1\n", encoding="utf-8")
            diagnostics.write_text("", encoding="utf-8")
            state = capture_artifact_state(root)
            # 실패한 event가 과학 원장과 진단 로그에 모두 행을 남긴다.
            science.write_text("header\nrow1\nrow2\n", encoding="utf-8")
            diagnostics.write_text('{"label":"community_thinking"}\n', encoding="utf-8")
            restore_artifact_state(root, state)
            self.assertEqual(
                science.read_text(encoding="utf-8"), "header\nrow1\n"
            )
            self.assertEqual(
                diagnostics.read_text(encoding="utf-8"),
                '{"label":"community_thinking"}\n',
            )


class CommunityReadingOutputContractTests(unittest.TestCase):
    """select/react 출력 계약이 빈 배열 예시로 되돌아가지 않게 고정한다.

    AGENTS.md "community select/react 출력 예시는 비어 있지 않게 유지한다"
    항목의 회귀 테스트다. v2에서 두 예시가 모두 `[]`로 바뀐 드리프트가 있었고,
    그 상태에서는 ON arm이 오류 없이 조용히 약해진다.
    """

    def test_prompt_file_owns_no_mode_specific_schema(self) -> None:
        text = load_prompt("community_reading.txt")
        self.assertIn("{output_contract}", text)
        # 한 파일이 두 모드에 쓰이므로 스키마는 호출 시점에만 주입한다.
        self.assertNotIn("selected_post_ids", text)
        self.assertNotIn("reactions", text)

    def test_contract_examples_are_not_empty_arrays(self) -> None:
        for label, contract in (
            ("select", SELECT_OUTPUT_CONTRACT),
            ("react", REACT_OUTPUT_CONTRACT),
        ):
            with self.subTest(mode=label):
                self.assertNotIn("[]", contract)
        self.assertRegex(SELECT_OUTPUT_CONTRACT, r'"selected_post_ids":\s*\[\s*\d')
        self.assertRegex(REACT_OUTPUT_CONTRACT, r'"post_id":\s*\d+,\s*"reaction"')
        # 빈 선택은 설계상 유효하므로 산문으로는 계속 허용한다.
        self.assertIn("빈 배열도 유효", SELECT_OUTPUT_CONTRACT)
        # 빈 반응은 _validate_reactions가 항상 거부한다.
        self.assertIn("빈 배열은 허용되지 않습니다", REACT_OUTPUT_CONTRACT)

    def test_rendered_prompt_carries_only_the_calling_mode_contract(self) -> None:
        select = render_prompt(
            "community_reading.txt",
            mode="select",
            persona_prompt="페르소나",
            post_list_str="post_id=1 | 제목",
            read_limit=5,
            posts_content_str="",
            output_contract=SELECT_OUTPUT_CONTRACT,
        )
        react = render_prompt(
            "community_reading.txt",
            mode="react",
            persona_prompt="페르소나",
            post_list_str="",
            read_limit=1,
            posts_content_str="post_id=1 | 제목 | 본문",
            output_contract=REACT_OUTPUT_CONTRACT,
        )
        self.assertIn("selected_post_ids", select)
        self.assertNotIn("reactions", select)
        self.assertIn("reactions", react)
        self.assertNotIn("selected_post_ids", react)
        for label, text in (("select", select), ("react", react)):
            with self.subTest(mode=label):
                # 채워지지 않은 슬롯이 그대로 모델에 나가면 안 된다.
                self.assertNotRegex(text, r"\{[a-z_0-9]+\}")


if __name__ == "__main__":
    unittest.main()
