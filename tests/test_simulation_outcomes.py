from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.db.connection import connect
from twinmarket_kr.outcome_schedule import (
    FrozenEventSchedule,
    OutcomeScheduleError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEALED_ROOT = PROJECT_ROOT / "preparation" / "rn_ab_sealed_v1"


def _dimensions(label: str) -> dict[str, str]:
    return {
        f"dim_{index}": f"{label} dimension {index}"
        for index in range(1, 7)
    }


def _empty_evidence() -> dict[str, dict[str, list[str]]]:
    return {
        f"dim_{index}": {"support": [], "contradict": []}
        for index in range(1, 7)
    }


def _integration_evidence(
    *,
    stb_evidence_id: str,
    outcome_ids: list[str],
    outcome_relation: str = "support",
) -> dict[str, dict[str, list[str]]]:
    result = _empty_evidence()
    result["dim_1"]["support"] = [stb_evidence_id]
    result["dim_6"][outcome_relation] = list(outcome_ids)
    return result


def _two_event_schedule(*, pm_price: float = 110.0) -> FrozenEventSchedule:
    return FrozenEventSchedule.from_rows(
        [
            {
                "event_id": "2026-02-27/AM",
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
                "execution_price": 100.0,
                "execution_price_field": "actual_open",
            },
            {
                "event_id": "2026-02-27/PM",
                "turn": 2,
                "date": "2026-02-27",
                "subturn": "pm",
                "execution_price": pm_price,
                "execution_price_field": "actual_close",
            },
        ],
        stock_code="005930",
    )


class FrozenEventScheduleTests(unittest.TestCase):
    def test_sealed_schedule_and_100_agent_terminal_counts(self) -> None:
        schedule = FrozenEventSchedule.from_sealed_files(
            SEALED_ROOT / "calendar.json",
            SEALED_ROOT / "prices.json",
            expected_stock_code="005930",
        )
        self.assertEqual(len(schedule.events), 90)
        self.assertEqual(schedule.first_event_id, "2026-02-27/AM")
        self.assertEqual(schedule.last_event_id, "2026-05-04/PM")

        due_counts = {"next_turn": 0, "h1": 0, "h5": 0}
        censored_counts = {"next_turn": 0, "h1": 0, "h5": 0}
        for event in schedule.events:
            for horizon in ("next_turn", "h1", "h5"):
                due = schedule.due_event_id(
                    fill_event_id=str(event["event_id"]),
                    horizon=horizon,
                )
                target = due_counts if due is not None else censored_counts
                target[horizon] += 100
        self.assertEqual(
            due_counts,
            {"next_turn": 8_900, "h1": 8_800, "h5": 8_000},
        )
        self.assertEqual(
            censored_counts,
            {"next_turn": 100, "h1": 200, "h5": 1_000},
        )
        self.assertEqual(sum(due_counts.values()), 25_700)
        self.assertEqual(sum(censored_counts.values()), 1_300)

    def test_loader_rejects_tampered_price_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = json.loads(
                (SEALED_ROOT / "calendar.json").read_text(encoding="utf-8")
            )
            prices = json.loads(
                (SEALED_ROOT / "prices.json").read_text(encoding="utf-8")
            )
            prices["events"][0]["execution_price"] = -1
            calendar_path = root / "calendar.json"
            prices_path = root / "prices.json"
            calendar_path.write_text(
                json.dumps(
                    calendar,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            prices_path.write_text(
                json.dumps(
                    prices,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                OutcomeScheduleError,
                "positive finite",
            ):
                FrozenEventSchedule.from_sealed_files(
                    calendar_path,
                    prices_path,
                    expected_stock_code="005930",
                )


class SimulationOutcomeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sim.sqlite"
        self.schedule = _two_event_schedule()
        self.memory = MemoryAgent(
            self.db_path,
            event_schedule=self.schedule,
        )
        self.parent_ltb_id = self.memory.bootstrap_ltb(
            agent_id="agent-1",
            date="2026-02-27",
            dimensions=_dimensions("initial"),
            belief_summary="initial",
            view_change={"status": "initial"},
        )

    def _record_event(
        self,
        *,
        turn: int,
        outcome_ids: list[str],
        outcome_relation: str = "support",
        ignored_outcome_ids: list[str] | None = None,
    ) -> tuple[str, str, str, str]:
        event = self.schedule.events[turn - 1]
        evidence_id = f"sealed-current:{event['event_id']}"
        stb_evidence = _empty_evidence()
        stb_evidence["dim_1"]["support"] = [evidence_id]
        stb_id = self.memory.save_stb(
            agent_id="agent-1",
            turn=turn,
            date=str(event["date"]),
            subturn=str(event["subturn"]),
            dimensions=_dimensions(f"stb-{turn}"),
            evidence=[{"evidence_id": evidence_id}],
            dimension_evidence=stb_evidence,
        )
        decision_id = self.memory.record_decision_lineage(
            agent_id="agent-1",
            turn=turn,
            date=str(event["date"]),
            subturn=str(event["subturn"]),
            source_ltb_id=self.parent_ltb_id,
            source_stb_id=stb_id,
            analysis_id=self.memory.record_analysis_lineage(
                agent_id="agent-1",
                turn=turn,
                date=str(event["date"]),
                subturn=str(event["subturn"]),
                source_ltb_id=self.parent_ltb_id,
                source_stb_id=stb_id,
                analysis={"market_view": f"validated analysis {turn}"},
            ),
            decision={
                "action": "buy",
                "quantity": 1,
                "reason": "six-dimensional decision",
            },
        )
        fill_id = self.memory.record_fill_lineage(
            decision_id=decision_id,
            filled_quantity=1,
            executed_price=float(event["execution_price"]),
            pre_portfolio={"cash": 1_000, "quantity": turn - 1},
            post_portfolio={
                "cash": 1_000 - float(event["execution_price"]),
                "quantity": turn,
            },
            stock_code="005930",
            fee=0,
        )
        ltb_id = self.memory.save_post_fill_ltb(
            agent_id="agent-1",
            turn=turn,
            date=str(event["date"]),
            subturn=str(event["subturn"]),
            parent_ltb_id=self.parent_ltb_id,
            stb_id=stb_id,
            decision_id=decision_id,
            fill_id=fill_id,
            dimensions=_dimensions(f"ltb-{turn}"),
            integration_evidence=_integration_evidence(
                stb_evidence_id=evidence_id,
                outcome_ids=outcome_ids,
                outcome_relation=outcome_relation,
            ),
            ignored_outcome_ids=ignored_outcome_ids or [],
            belief_summary=f"human log {turn}",
            view_change={"turn": turn},
        )
        self.parent_ltb_id = ltb_id
        return stb_id, decision_id, fill_id, ltb_id

    def test_maturity_dim6_consumption_and_right_censoring(self) -> None:
        self.assertEqual(
            self.memory.mature_outcomes_for_event("2026-02-27/AM"),
            (),
        )
        self._record_event(turn=1, outcome_ids=[])

        matured = self.memory.mature_outcomes_for_event("2026-02-27/PM")
        self.assertEqual(len(matured), 1)
        self.assertEqual(
            matured,
            self.memory.mature_outcomes_for_event("2026-02-27/PM"),
        )
        eligible = self.memory.eligible_outcomes(
            "agent-1",
            "2026-02-27/PM",
        )
        self.assertEqual(
            [row["outcome_id"] for row in eligible],
            list(matured),
        )
        self.assertAlmostEqual(eligible[0]["action_aligned_markout"], 0.1)
        self._record_event(turn=2, outcome_ids=list(matured))
        self.assertEqual(
            self.memory.eligible_outcomes("agent-1", "2026-02-27/PM"),
            [],
        )

        censored = self.memory.right_censor_unavailable_outcomes()
        self.assertEqual(len(censored), 5)
        with connect(self.db_path, read_only=True) as connection:
            statuses = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM simulation_trade_outcomes
                    GROUP BY status
                    """
                ).fetchall()
            }
            consumption_count = connection.execute(
                "SELECT COUNT(*) AS count "
                "FROM simulation_outcome_consumptions"
            ).fetchone()["count"]
        self.assertEqual(statuses, {"matured": 1, "right_censored": 5})
        self.assertEqual(consumption_count, 1)

    def test_ignored_outcome_is_consumed_without_becoming_evidence(self) -> None:
        self.memory.mature_outcomes_for_event("2026-02-27/AM")
        self._record_event(turn=1, outcome_ids=[])
        matured = self.memory.mature_outcomes_for_event("2026-02-27/PM")

        self._record_event(
            turn=2,
            outcome_ids=[],
            ignored_outcome_ids=list(matured),
        )

        self.assertEqual(
            self.memory.eligible_outcomes("agent-1", "2026-02-27/PM"),
            [],
        )
        with connect(self.db_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT integration_evidence_json FROM simulation_ltb_states WHERE turn = 2"
            ).fetchone()
            consumption_count = connection.execute(
                "SELECT COUNT(*) AS count FROM simulation_outcome_consumptions"
            ).fetchone()["count"]
        evidence = json.loads(row["integration_evidence_json"])
        self.assertEqual(evidence["dim_6"]["support"], [])
        self.assertEqual(evidence["dim_6"]["contradict"], [])
        self.assertEqual(consumption_count, 1)

    def test_agent_may_choose_either_relation_for_a_negative_markout(
        self,
    ) -> None:
        """dim_6은 에이전트의 자기평가이므로 관계를 강제하지 않는다.

        예전에는 markout 부호로 support/contradict를 확정하고 어긋나면
        저장을 거부했다. 라이브에서 이 규칙이 실행을 죽인 지배적 원인이었고
        (v4 소진 24건 중 22건), 강제 라벨 자체도 부분 매도·horizon 불일치·
        노이즈 수준 변화율에서 항상 옳지는 않았다. 객관적 부호는
        simulation_trade_outcomes에 그대로 남아 사후 대조가 가능하다.
        """
        self.schedule = _two_event_schedule(pm_price=90.0)
        self.db_path = Path(self.tempdir.name) / "negative-markout.sqlite"
        self.memory = MemoryAgent(
            self.db_path,
            event_schedule=self.schedule,
        )
        self.parent_ltb_id = self.memory.bootstrap_ltb(
            agent_id="agent-1",
            date="2026-02-27",
            dimensions=_dimensions("initial"),
            belief_summary="initial",
            view_change={"status": "initial"},
        )

        self.memory.mature_outcomes_for_event("2026-02-27/AM")
        self._record_event(turn=1, outcome_ids=[])
        matured = self.memory.mature_outcomes_for_event("2026-02-27/PM")
        eligible = self.memory.eligible_outcomes(
            "agent-1",
            "2026-02-27/PM",
        )
        self.assertAlmostEqual(eligible[0]["action_aligned_markout"], -0.1)

        # markout이 음수여도 에이전트가 support로 해석하면 그대로 저장된다.
        self._record_event(
            turn=2,
            outcome_ids=list(matured),
            outcome_relation="support",
        )
        self.assertEqual(
            self.memory.eligible_outcomes(
                "agent-1",
                "2026-02-27/PM",
            ),
            [],
        )

    def test_missing_or_misplaced_outcome_rolls_back_ltb_and_consumption(
        self,
    ) -> None:
        self.memory.mature_outcomes_for_event("2026-02-27/AM")
        self._record_event(turn=1, outcome_ids=[])
        matured = self.memory.mature_outcomes_for_event("2026-02-27/PM")

        event = self.schedule.events[1]
        evidence_id = "sealed-current:2026-02-27/PM"
        stb_evidence = _empty_evidence()
        stb_evidence["dim_1"]["support"] = [evidence_id]
        stb_id = self.memory.save_stb(
            agent_id="agent-1",
            turn=2,
            date=str(event["date"]),
            subturn=str(event["subturn"]),
            dimensions=_dimensions("stb-2"),
            evidence=[{"evidence_id": evidence_id}],
            dimension_evidence=stb_evidence,
        )
        decision_id = self.memory.record_decision_lineage(
            agent_id="agent-1",
            turn=2,
            date=str(event["date"]),
            subturn=str(event["subturn"]),
            source_ltb_id=self.parent_ltb_id,
            source_stb_id=stb_id,
            analysis_id=self.memory.record_analysis_lineage(
                agent_id="agent-1",
                turn=2,
                date=str(event["date"]),
                subturn=str(event["subturn"]),
                source_ltb_id=self.parent_ltb_id,
                source_stb_id=stb_id,
                analysis={"market_view": "validated analysis 2"},
            ),
            decision={"action": "buy", "quantity": 1},
        )
        fill_id = self.memory.record_fill_lineage(
            decision_id=decision_id,
            filled_quantity=1,
            executed_price=float(event["execution_price"]),
            pre_portfolio={"cash": 1_000, "quantity": 1},
            post_portfolio={"cash": 890, "quantity": 2},
            fee=0,
        )
        misplaced = _integration_evidence(
            stb_evidence_id=evidence_id,
            outcome_ids=[],
        )
        misplaced["dim_1"]["support"].append(matured[0])
        with self.assertRaisesRegex(ValueError, "only LTB dim_6"):
            self.memory.save_post_fill_ltb(
                agent_id="agent-1",
                turn=2,
                date=str(event["date"]),
                subturn=str(event["subturn"]),
                parent_ltb_id=self.parent_ltb_id,
                stb_id=stb_id,
                decision_id=decision_id,
                fill_id=fill_id,
                dimensions=_dimensions("ltb-2"),
                integration_evidence=misplaced,
                belief_summary="must roll back",
                view_change={"turn": 2},
            )
        with connect(self.db_path, read_only=True) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT ltb_id FROM simulation_ltb_states WHERE turn = 2"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS count "
                    "FROM simulation_outcome_consumptions"
                ).fetchone()["count"],
                0,
            )

    def test_future_event_maturity_is_rejected(self) -> None:
        schedule = FrozenEventSchedule.from_sealed_files(
            SEALED_ROOT / "calendar.json",
            SEALED_ROOT / "prices.json",
            expected_stock_code="005930",
        )
        other_db = Path(self.tempdir.name) / "future.sqlite"
        memory = MemoryAgent(other_db, event_schedule=schedule)
        memory.bootstrap_ltb(
            agent_id="agent-1",
            date="2026-02-27",
            dimensions=_dimensions("initial"),
            belief_summary="initial",
            view_change="initial",
        )
        with self.assertRaisesRegex(ValueError, "not the current"):
            memory.mature_outcomes_for_event("2026-03-03/AM")


if __name__ == "__main__":
    unittest.main()
