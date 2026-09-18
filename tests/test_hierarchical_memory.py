from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.db.connection import connect
from twinmarket_kr.llm.belief import update_long_term_belief
from twinmarket_kr.llm.client import OpenRouterClient


def _dimensions(label: str) -> dict[str, str]:
    return {
        f"dim_{index}": f"{label} dimension {index}"
        for index in range(1, 7)
    }


def _evidence(*ids: str) -> dict[str, dict[str, list[str]]]:
    return {
        f"dim_{index}": {
            "support": list(ids) if index == 1 else [],
            "contradict": [],
        }
        for index in range(1, 7)
    }


class _SequenceClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [
            json.dumps(response, ensure_ascii=False)
            for response in responses
        ]
        self.calls = 0

    async def chat(self, *args, **kwargs) -> str:
        self.calls += 1
        return self.responses.pop(0)


class HierarchicalMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "simulation.sqlite"
        self.memory = MemoryAgent(self.db_path)

    def _bootstrap(self, *, summary: str = "initial human log") -> str:
        return self.memory.bootstrap_ltb(
            agent_id="agent-1",
            date="2026-02-27",
            dimensions=_dimensions("initial"),
            belief_summary=summary,
            view_change={"status": "initial"},
        )

    def _record_event(
        self,
        *,
        turn: int,
        date: str,
        subturn: str,
        parent_ltb_id: str,
        action: str,
        quantity: int,
        executed_price: float,
    ) -> tuple[str, str, str, str]:
        stb_id = self.memory.save_stb(
            agent_id="agent-1",
            turn=turn,
            date=date,
            subturn=subturn,
            dimensions=_dimensions(f"{subturn}-{turn}-short"),
            evidence=[{"source": "sealed_event", "turn": turn}],
            dimension_evidence=_evidence(f"sealed-event:{turn}"),
        )
        decision_payload = {
            "action": action,
            "quantity": quantity,
            "reason": f"{subturn} decision from six-dimensional LTB and STB",
            "risk_control": "respect the available cash and inventory",
        }
        analysis_id = self.memory.record_analysis_lineage(
            agent_id="agent-1",
            turn=turn,
            date=date,
            subturn=subturn,
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            analysis={"market_view": f"{subturn}-{turn} validated analysis"},
        )
        decision_id = self.memory.record_decision_lineage(
            agent_id="agent-1",
            turn=turn,
            date=date,
            subturn=subturn,
            source_ltb_id=parent_ltb_id,
            source_stb_id=stb_id,
            analysis_id=analysis_id,
            decision=decision_payload,
        )
        self.memory.append_trade_log(
            {
                "agent_id": "agent-1",
                "turn": turn,
                "date": date,
                "action": action,
                "stock_code": "005930",
                "quantity": quantity,
                "fee": 0,
                "decision_id": decision_id,
                "source_ltb_id": parent_ltb_id,
                "source_stb_id": stb_id,
            }
        )
        pre_portfolio = {
            "cash": 1_000 if action == "buy" else 800,
            "quantity": 0 if action == "buy" else 2,
        }
        post_portfolio = {
            "cash": 1_000 - executed_price * quantity
            if action == "buy"
            else 800 + executed_price * quantity,
            "quantity": quantity if action == "buy" else 2 - quantity,
        }
        fill_id = self.memory.record_fill_lineage(
            decision_id=decision_id,
            filled_quantity=quantity,
            executed_price=executed_price,
            pre_portfolio=pre_portfolio,
            post_portfolio=post_portfolio,
            stock_code="005930",
            fee=0,
        )
        self.memory.update_trade_execution(
            "agent-1",
            turn,
            filled_quantity=quantity,
            executed_price=executed_price,
            fee=0,
        )
        ltb_id = self.memory.save_post_fill_ltb(
            agent_id="agent-1",
            turn=turn,
            date=date,
            subturn=subturn,
            parent_ltb_id=parent_ltb_id,
            stb_id=stb_id,
            decision_id=decision_id,
            fill_id=fill_id,
            dimensions=_dimensions(f"{subturn}-{turn}-long"),
            integration_evidence=_evidence(f"sealed-event:{turn}"),
            belief_summary=f"{subturn} human-readable compatibility summary",
            view_change={
                "turn": turn,
                "changed": True,
                "source_fill_id": fill_id,
            },
        )
        return stb_id, decision_id, fill_id, ltb_id

    def test_old_trade_and_community_tables_receive_nullable_lineage_columns(
        self,
    ) -> None:
        old_db = Path(self.tempdir.name) / "old.sqlite"
        with sqlite3.connect(old_db) as connection:
            connection.execute(
                """
                CREATE TABLE trade_log (
                    log_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    action TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    executed_price REAL,
                    trade_value REAL,
                    fee REAL NOT NULL,
                    action_reason TEXT,
                    risk_control TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO trade_log (
                    log_id, agent_id, turn, date, action, stock_code,
                    quantity, fee
                ) VALUES ('legacy', 'agent-1', 1, '2026-02-27',
                          'buy', '005930', 1, 0)
                """
            )
            connection.execute(
                """
                CREATE TABLE community_posts (
                    post_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    anonymous_code TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    post_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    like_count INTEGER NOT NULL DEFAULT 0,
                    unlike_count INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL DEFAULT 0,
                    is_best INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                INSERT INTO community_posts (
                    agent_id, anonymous_code, turn, date, post_type,
                    title, content
                ) VALUES ('agent-1', '황소-1000', 1, '2026-02-27',
                          'analysis', 'legacy title', 'legacy body')
                """
            )
            connection.commit()

        MemoryAgent(old_db)

        with connect(old_db, read_only=True) as connection:
            trade_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(trade_log)")
            }
            post_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(community_posts)"
                )
            }
            trade = connection.execute(
                """
                SELECT action, decision_id, source_ltb_id, source_stb_id,
                       analysis_id, fill_id, post_fill_ltb_id
                FROM trade_log WHERE log_id = 'legacy'
                """
            ).fetchone()
            post = connection.execute(
                """
                SELECT title, source_ltb_id, source_fill_id,
                       source_decision_id
                FROM community_posts WHERE post_id = 1
                """
            ).fetchone()

        self.assertTrue(set((
            "analysis_id",
            "decision_id",
            "source_ltb_id",
            "source_stb_id",
            "fill_id",
            "post_fill_ltb_id",
        )).issubset(trade_columns))
        self.assertTrue(set((
            "source_ltb_id",
            "source_fill_id",
            "source_decision_id",
        )).issubset(post_columns))
        self.assertEqual(trade["action"], "buy")
        self.assertIsNone(trade["decision_id"])
        self.assertEqual(post["title"], "legacy title")
        self.assertIsNone(post["source_ltb_id"])

    def test_old_ltb_rows_gain_empty_evidence_without_hash_rewrite(self) -> None:
        old_ltb_db = Path(self.tempdir.name) / "old-ltb.sqlite"
        MemoryAgent(old_ltb_db)
        with connect(old_ltb_db) as connection:
            connection.execute(
                """
                ALTER TABLE simulation_ltb_states
                DROP COLUMN integration_evidence_json
                """
            )
            connection.execute(
                """
                INSERT INTO simulation_ltb_states (
                    ltb_id, agent_id, turn, visible_from_turn, date, subturn,
                    parent_ltb_id, source_stb_id, source_decision_id,
                    source_fill_id, dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                    scientific_sha256, belief_summary, view_change_json,
                    human_log_sha256
                ) VALUES (
                    'legacy-ltb', 'agent-1', 0, 1, '2026-02-27', 'initial',
                    NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?,
                    'legacy-scientific-hash', 'legacy summary', '"initial"',
                    'legacy-human-hash'
                )
                """,
                tuple(_dimensions("legacy").values()),
            )
            connection.commit()

        migrated = MemoryAgent(old_ltb_db)
        state = migrated.get_ltb("legacy-ltb")

        self.assertEqual(state["integration_evidence"], {})
        self.assertEqual(
            state["scientific_sha256"],
            "legacy-scientific-hash",
        )

    def test_schema_is_additive_and_preserves_legacy_rows(self) -> None:
        self.memory.save_belief(
            {
                "belief_id": "legacy-belief",
                "agent_id": "agent-1",
                "turn": 0,
                "date": "2026-02-27",
                **_dimensions("legacy"),
                "belief_summary": "legacy summary remains readable",
                "view_change": "legacy view change",
            }
        )
        self.memory.append_trade_log(
            {
                "log_id": "legacy-trade",
                "agent_id": "agent-1",
                "turn": 1,
                "date": "2026-02-27",
                "action": "buy",
                "stock_code": "005930",
                "quantity": 1,
                "fee": 0,
            }
        )

        MemoryAgent(self.db_path)

        with connect(self.db_path, read_only=True) as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            belief = connection.execute(
                "SELECT belief_summary FROM belief_history WHERE belief_id = ?",
                ("legacy-belief",),
            ).fetchone()
            trade = connection.execute(
                "SELECT action, quantity FROM trade_log WHERE log_id = ?",
                ("legacy-trade",),
            ).fetchone()

        self.assertTrue(
            {
                "simulation_stb_states",
                "simulation_ltb_states",
                "simulation_analyses",
                "simulation_decisions",
                "simulation_fills",
            }.issubset(tables)
        )
        self.assertTrue(
            {
                "rn_schema_meta",
                "paper_initial_portfolios",
                "short_term_belief_history",
                "paper_ltb_states",
                "ltb_dimension_transitions",
                "paper_fill_ledger",
                "paper_analyses",
                "paper_decisions",
                "trade_outcomes",
                "ltb_outcome_consumptions",
                "observation_events",
                "agent_exposures",
                "memory_evidence_edges",
                "turn_belief_trace",
                "community_post_trace",
                "phase_consumptions",
            }.isdisjoint(tables)
        )
        self.assertEqual(belief["belief_summary"], "legacy summary remains readable")
        self.assertEqual((trade["action"], trade["quantity"]), ("buy", 1))

    def test_am_and_pm_chain_uses_post_fill_ltb_only_from_next_turn(self) -> None:
        ltb_0 = self._bootstrap()
        first_input = self.memory.previous_ltb(
            agent_id="agent-1",
            decision_turn=1,
        )
        self.assertEqual(first_input["ltb_id"], ltb_0)
        self.assertNotIn("belief_summary", first_input)
        self.assertNotIn("view_change", first_input)

        stb_1, decision_1, fill_1, ltb_1 = self._record_event(
            turn=1,
            date="2026-02-27",
            subturn="am",
            parent_ltb_id=ltb_0,
            action="buy",
            quantity=2,
            executed_price=100,
        )

        self.assertEqual(
            self.memory.previous_ltb(
                agent_id="agent-1",
                decision_turn=1,
            )["ltb_id"],
            ltb_0,
        )
        self.assertEqual(
            self.memory.previous_ltb(
                agent_id="agent-1",
                decision_turn=2,
            )["ltb_id"],
            ltb_1,
        )

        stb_2, decision_2, fill_2, ltb_2 = self._record_event(
            turn=2,
            date="2026-02-27",
            subturn="pm",
            parent_ltb_id=ltb_1,
            action="sell",
            quantity=1,
            executed_price=105,
        )
        self.assertEqual(
            self.memory.previous_ltb(
                agent_id="agent-1",
                decision_turn=2,
            )["ltb_id"],
            ltb_1,
        )
        self.assertEqual(
            self.memory.previous_ltb(
                agent_id="agent-1",
                decision_turn=3,
            )["ltb_id"],
            ltb_2,
        )

        first_decision = self.memory.get_decision_lineage(decision_1)
        first_fill = self.memory.get_fill_lineage(fill_1)
        first_post_fill_ltb = self.memory.get_ltb(ltb_1)
        second_post_fill_ltb = self.memory.get_ltb(ltb_2)
        self.assertEqual(first_decision["source_ltb_id"], ltb_0)
        self.assertEqual(first_decision["source_stb_id"], stb_1)
        self.assertEqual(first_fill["decision_id"], decision_1)
        self.assertEqual(first_fill["source_ltb_id"], ltb_0)
        self.assertEqual(first_post_fill_ltb["source_fill_id"], fill_1)
        self.assertEqual(
            first_post_fill_ltb["integration_evidence"]["dim_1"]["support"],
            ["sealed-event:1"],
        )
        self.assertEqual(second_post_fill_ltb["parent_ltb_id"], ltb_1)
        self.assertEqual(second_post_fill_ltb["source_stb_id"], stb_2)
        self.assertEqual(second_post_fill_ltb["source_decision_id"], decision_2)
        self.assertEqual(second_post_fill_ltb["source_fill_id"], fill_2)

        with connect(self.db_path, read_only=True) as connection:
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "simulation_stb_states",
                    "simulation_ltb_states",
                    "simulation_analyses",
                    "simulation_decisions",
                    "simulation_fills",
                )
            }
        self.assertEqual(
            counts,
            {
                "simulation_stb_states": 2,
                "simulation_ltb_states": 3,
                "simulation_analyses": 2,
                "simulation_decisions": 2,
                "simulation_fills": 2,
            },
        )
        with connect(self.db_path, read_only=True) as connection:
            trade_lineage = connection.execute(
                """
                SELECT analysis_id, decision_id, source_ltb_id, source_stb_id,
                       fill_id, post_fill_ltb_id
                FROM trade_log
                WHERE agent_id = 'agent-1' AND turn = 1
                """
            ).fetchone()
            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        self.assertEqual(
            dict(trade_lineage),
            {
                "analysis_id": first_decision["analysis_id"],
                "decision_id": decision_1,
                "source_ltb_id": ltb_0,
                "source_stb_id": stb_1,
                "fill_id": fill_1,
                "post_fill_ltb_id": ltb_1,
            },
        )
        self.assertEqual(foreign_key_violations, [])

    def test_exact_retries_are_idempotent(self) -> None:
        ltb_0 = self._bootstrap()
        first_ids = self._record_event(
            turn=1,
            date="2026-02-27",
            subturn="am",
            parent_ltb_id=ltb_0,
            action="buy",
            quantity=2,
            executed_price=100,
        )
        retry_ids = self._record_event(
            turn=1,
            date="2026-02-27",
            subturn="am",
            parent_ltb_id=ltb_0,
            action="buy",
            quantity=2,
            executed_price=100,
        )
        self.assertEqual(first_ids, retry_ids)
        stb_id, decision_id, fill_id, _ltb_id = first_ids
        with self.assertRaisesRegex(ValueError, "idempotency mismatch"):
            self.memory.save_post_fill_ltb(
                agent_id="agent-1",
                turn=1,
                date="2026-02-27",
                subturn="am",
                parent_ltb_id=ltb_0,
                stb_id=stb_id,
                decision_id=decision_id,
                fill_id=fill_id,
                dimensions=_dimensions("am-1-long-retry"),
                integration_evidence=_evidence("sealed-event:1"),
                belief_summary="am human-readable compatibility summary",
                view_change={
                    "turn": 1,
                    "changed": True,
                    "source_fill_id": fill_id,
                },
            )

    def test_post_fill_ltb_cannot_flip_current_stb_evidence_relation(
        self,
    ) -> None:
        ltb_0 = self._bootstrap()
        stb_id, decision_id, fill_id, _ltb_id = self._record_event(
            turn=1,
            date="2026-02-27",
            subturn="am",
            parent_ltb_id=ltb_0,
            action="buy",
            quantity=2,
            executed_price=100,
        )
        swapped = _evidence()
        swapped["dim_1"]["contradict"] = ["sealed-event:1"]

        with self.assertRaisesRegex(
            ValueError,
            "preserve the current STB support/contradict relation",
        ):
            self.memory.save_post_fill_ltb(
                agent_id="agent-1",
                turn=1,
                date="2026-02-27",
                subturn="am",
                parent_ltb_id=ltb_0,
                stb_id=stb_id,
                decision_id=decision_id,
                fill_id=fill_id,
                dimensions=_dimensions("am-1-long-retry"),
                integration_evidence=swapped,
                belief_summary="relation flip must be rejected",
                view_change={"turn": 1},
            )

    def test_lineage_rejects_human_log_inputs_partial_fill_and_stale_ltb(
        self,
    ) -> None:
        ltb_0 = self._bootstrap()
        stb_id = self.memory.save_stb(
            agent_id="agent-1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            dimensions=_dimensions("short"),
        )
        with self.assertRaisesRegex(ValueError, "human belief log fields"):
            analysis_id = self.memory.record_analysis_lineage(
                agent_id="agent-1",
                turn=1,
                date="2026-02-27",
                subturn="am",
                source_ltb_id=ltb_0,
                source_stb_id=stb_id,
                analysis={"market_view": "validated"},
            )
            self.memory.record_decision_lineage(
                agent_id="agent-1",
                turn=1,
                date="2026-02-27",
                subturn="am",
                source_ltb_id=ltb_0,
                source_stb_id=stb_id,
                analysis_id=analysis_id,
                decision={
                    "action": "buy",
                    "quantity": 2,
                    "belief_summary": "must not become a decision input",
                },
            )

        decision_id = self.memory.record_decision_lineage(
            agent_id="agent-1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            source_ltb_id=ltb_0,
            source_stb_id=stb_id,
            analysis_id=analysis_id,
            decision={"action": "buy", "quantity": 2},
        )
        with self.assertRaisesRegex(ValueError, "fully match"):
            self.memory.record_fill_lineage(
                decision_id=decision_id,
                filled_quantity=1,
                executed_price=100,
                pre_portfolio={"cash": 1_000, "quantity": 0},
                post_portfolio={"cash": 900, "quantity": 1},
            )
        with self.assertRaisesRegex(ValueError, "fee must be zero"):
            self.memory.record_fill_lineage(
                decision_id=decision_id,
                filled_quantity=2,
                executed_price=100,
                pre_portfolio={"cash": 1_000, "quantity": 0},
                post_portfolio={"cash": 800, "quantity": 2},
                fee=1,
            )
        fill_id = self.memory.record_fill_lineage(
            decision_id=decision_id,
            filled_quantity=2,
            executed_price=100,
            pre_portfolio={"cash": 1_000, "quantity": 0},
            post_portfolio={"cash": 800, "quantity": 2},
        )
        # 차원별 문장 유지는 허용된다. 관점이 안 변한 차원에 새 표현을
        # 강제하면 임베딩 deviation 측정에 억지 패러프레이즈가 섞인다.
        carried = _dimensions("long")
        carried["dim_4"] = _dimensions("initial")["dim_4"]
        carried_ltb = self.memory.save_post_fill_ltb(
            agent_id="agent-1",
            turn=1,
            date="2026-02-27",
            subturn="am",
            parent_ltb_id=ltb_0,
            stb_id=stb_id,
            decision_id=decision_id,
            fill_id=fill_id,
            dimensions=carried,
            integration_evidence=_evidence(),
            belief_summary="human log",
            view_change="changed",
        )
        self.assertTrue(carried_ltb)
        self.assertEqual(
            self.memory.get_ltb(carried_ltb)["dimensions"]["dim_4"],
            _dimensions("initial")["dim_4"],
        )

        # 퇴행적 전체 복사만 거부한다.
        with self.assertRaisesRegex(ValueError, "copy all six dimensions verbatim"):
            self.memory.save_post_fill_ltb(
                agent_id="agent-1",
                turn=1,
                date="2026-02-27",
                subturn="am",
                parent_ltb_id=ltb_0,
                stb_id=stb_id,
                decision_id=decision_id,
                fill_id=fill_id,
                dimensions=_dimensions("initial"),
                integration_evidence=_evidence(),
                belief_summary="human log",
                view_change="changed",
            )

        with self.assertRaisesRegex(ValueError, "exactly support and contradict"):
            self.memory.save_post_fill_ltb(
                agent_id="agent-1",
                turn=1,
                date="2026-02-27",
                subturn="am",
                parent_ltb_id=ltb_0,
                stb_id=stb_id,
                decision_id=decision_id,
                fill_id=fill_id,
                dimensions=_dimensions("valid long"),
                integration_evidence={
                    **_evidence(),
                    "dim_1": {"support": []},
                },
                belief_summary="human log",
                view_change="changed",
            )

    def test_human_log_changes_do_not_change_scientific_ltb_hash(self) -> None:
        other_db = Path(self.tempdir.name) / "other.sqlite"
        other_memory = MemoryAgent(other_db)

        first_id = self.memory.bootstrap_ltb(
            agent_id="agent-1",
            date="2026-02-27",
            dimensions=_dimensions("same"),
            belief_summary="first human wording",
            view_change={"wording": "first"},
        )
        second_id = other_memory.bootstrap_ltb(
            agent_id="agent-1",
            date="2026-02-27",
            dimensions=_dimensions("same"),
            belief_summary="different human wording",
            view_change={"wording": "different"},
        )

        first_state = self.memory.get_ltb(first_id)
        second_state = other_memory.get_ltb(second_id)
        first_log = self.memory.get_ltb_human_log(first_id)
        second_log = other_memory.get_ltb_human_log(second_id)
        self.assertEqual(
            first_state["scientific_sha256"],
            second_state["scientific_sha256"],
        )
        self.assertNotEqual(
            first_log["human_log_sha256"],
            second_log["human_log_sha256"],
        )
        self.assertNotIn("belief_summary", first_state)
        self.assertNotIn("view_change", first_state)
        self.assertEqual(first_state["integration_evidence"], _evidence())


class HierarchicalBeliefValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_ltb_stub_preserves_negative_outcome_relation(
        self,
    ) -> None:
        outcome_id = "outcome:fill-1:h1"
        with patch.dict(
            os.environ,
            {"TWINMARKET_OFFLINE_LLM": "1"},
        ):
            result = await update_long_term_belief(
                {
                    "agent_id": "agent-1",
                    "news_depth": 1,
                    "persona_prompt": "test persona",
                },
                event={
                    "event_id": "2026-03-03/AM",
                    "turn": 3,
                    "date": "2026-03-03",
                    "subturn": "am",
                },
                previous_ltb={"dimensions": _dimensions("previous")},
                current_stb={
                    "dimensions": _dimensions("current"),
                    "dimension_evidence": _evidence("news-1"),
                },
                transaction_episode={
                    "fill_id": "fill-3",
                    "action": "buy",
                    "filled_quantity": 1,
                    "executed_price": 90.0,
                },
                eligible_price_outcomes_dim_6_only=[
                    {
                        "outcome_id": outcome_id,
                        "action_aligned_markout": -0.1,
                    }
                ],
                client=OpenRouterClient(),
                seed=2,
                validation_attempts=1,
            )

        self.assertEqual(
            result["integration_evidence"]["dim_6"]["contradict"],
            [outcome_id],
        )

    async def test_ltb_ignores_neutral_verdict_for_flat_outcome(self) -> None:
        outcome_id = "outcome:fill-flat:h1"
        client = _SequenceClient(
            [
                {
                    **_dimensions("flat outcome"),
                    "integration_evidence": _evidence(),
                    "outcome_verdicts": ["neutral"],
                }
            ]
        )
        result = await update_long_term_belief(
            {"agent_id": "agent-1", "news_depth": 1, "persona_prompt": "persona"},
            event={
                "event_id": "2026-03-03/AM",
                "turn": 3,
                "date": "2026-03-03",
                "subturn": "am",
            },
            previous_ltb={"dimensions": _dimensions("previous")},
            current_stb={
                "dimensions": _dimensions("current"),
                "dimension_evidence": _evidence(),
            },
            transaction_episode={
                "fill_id": "fill-3",
                "action": "buy",
                "filled_quantity": 1,
                "executed_price": 100.0,
            },
            eligible_price_outcomes_dim_6_only=[
                {
                    "outcome_id": outcome_id,
                    "action_aligned_markout": 0.0,
                }
            ],
            client=client,
            seed=2,
            validation_attempts=1,
        )
        self.assertEqual(result["integration_evidence"]["dim_6"]["support"], [])
        self.assertEqual(result["integration_evidence"]["dim_6"]["contradict"], [])
        self.assertEqual(result["ignored_outcome_ids"], [outcome_id])

    async def test_ltb_ignores_neutral_verdict_for_nonflat_outcome(self) -> None:
        outcome_id = "outcome:fill-nonflat:h1"
        def response(verdict: str) -> dict[str, object]:
            return {
                **_dimensions(verdict),
                "integration_evidence": _evidence(),
                "outcome_verdicts": [verdict],
            }
        client = _SequenceClient([response("neutral")])
        result = await update_long_term_belief(
            {"agent_id": "agent-1", "news_depth": 1, "persona_prompt": "persona"},
            event={
                "event_id": "2026-03-03/AM", "turn": 3,
                "date": "2026-03-03", "subturn": "am",
            },
            previous_ltb={"dimensions": _dimensions("previous")},
            current_stb={
                "dimensions": _dimensions("current"),
                "dimension_evidence": _evidence(),
            },
            transaction_episode={
                "fill_id": "fill-3", "action": "buy",
                "filled_quantity": 1, "executed_price": 100.0,
            },
            eligible_price_outcomes_dim_6_only=[{
                "outcome_id": outcome_id, "action_aligned_markout": -0.1,
            }],
            client=client,
            seed=2,
            validation_attempts=1,
        )
        self.assertEqual(result["generation_attempts"], 1)
        self.assertEqual(result["integration_evidence"]["dim_6"]["support"], [])
        self.assertEqual(result["integration_evidence"]["dim_6"]["contradict"], [])
        self.assertEqual(result["ignored_outcome_ids"], [outcome_id])

    async def test_ltb_generator_retries_relation_flips_and_accepts_exact_polarity(
        self,
    ) -> None:
        previous = _dimensions("previous")
        current = _dimensions("current")
        outcome_id = "outcome:fill-1:next_turn"

        def response(
            label: str,
            *,
            news_relation: str,
            outcome_relation: str,
        ) -> dict[str, object]:
            integration = _evidence()
            integration["dim_1"][news_relation] = ["news-1"]
            # 가격 결과는 인용이 아니라 순서대로 판정만 낸다.
            return {
                **_dimensions(label),
                "integration_evidence": integration,
                "outcome_verdicts": [outcome_relation],
            }

        client = _SequenceClient(
            [
                response(
                    "invalid-flip",
                    news_relation="contradict",
                    outcome_relation="support",
                ),
                response(
                    "valid",
                    news_relation="support",
                    outcome_relation="contradict",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            config,
            "OPENROUTER_AUDIT_LOG",
            Path(directory) / "openrouter_calls.jsonl",
        ):
            result = await update_long_term_belief(
                {
                    "agent_id": "agent-1",
                    "news_depth": 1,
                    "persona_prompt": "test persona",
                },
                event={
                    "event_id": "2026-02-27/PM",
                    "date": "2026-02-27",
                    "subturn": "pm",
                },
                previous_ltb={"dimensions": previous},
                current_stb={
                    "dimensions": current,
                    "dimension_evidence": _evidence("news-1"),
                },
                transaction_episode={
                    "fill_id": "fill-1",
                    "action": "buy",
                    "filled_quantity": 1,
                    "executed_price": 100.0,
                },
                eligible_price_outcomes_dim_6_only=[
                    {
                        "outcome_id": outcome_id,
                        "action_aligned_markout": -0.1,
                    }
                ],
                client=client,
                seed=2,
                validation_attempts=2,
            )

        self.assertEqual(client.calls, 2)
        self.assertEqual(
            result["integration_evidence"]["dim_1"]["support"],
            ["news-1"],
        )
        self.assertEqual(
            result["integration_evidence"]["dim_6"]["contradict"],
            [outcome_id],
        )


if __name__ == "__main__":
    unittest.main()
