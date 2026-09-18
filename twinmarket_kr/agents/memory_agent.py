from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import config
from twinmarket_kr.db.connection import connect, init_sim_db
from twinmarket_kr.outcome_schedule import (
    OUTCOME_HORIZONS,
    FrozenEventSchedule,
    OutcomeScheduleError,
    outcome_evidence_relation,
)
from twinmarket_kr.persona.select import generate_persona_prompt


HIERARCHICAL_DIMENSION_KEYS = (
    "dim_1",
    "dim_2",
    "dim_3",
    "dim_4",
    "dim_5",
    "dim_6",
)
TRADE_LINEAGE_COLUMNS = (
    "analysis_id",
    "decision_id",
    "source_ltb_id",
    "source_stb_id",
    "fill_id",
    "post_fill_ltb_id",
)


def _canonical_json(value: Any, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc


def _scientific_sha256(value: Any, *, label: str) -> str:
    return hashlib.sha256(
        _canonical_json(value, label=label).encode("utf-8")
    ).hexdigest()


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if not numeric.is_integer() or numeric < 1:
        raise ValueError(f"{label} must be a positive integer")
    return int(numeric)


def _positive_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive number")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a positive number") from exc
    if numeric <= 0 or numeric == float("inf") or numeric != numeric:
        raise ValueError(f"{label} must be a positive finite number")
    return numeric


def _subturn(value: Any, *, allow_initial: bool = False) -> str:
    normalized = _required_text(value, label="subturn").lower()
    allowed = {"am", "pm"} | ({"initial"} if allow_initial else set())
    if normalized not in allowed:
        raise ValueError(f"subturn must be one of {sorted(allowed)}")
    return normalized


def _dimensions(value: Mapping[str, Any], *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(HIERARCHICAL_DIMENSION_KEYS):
        raise ValueError(
            f"{label} must contain exactly {list(HIERARCHICAL_DIMENSION_KEYS)}"
        )
    # 생성 경계(llm/belief.py belief_dimensions)와 같은 정책이다: 글자수
    # 한도는 프롬프트의 목표치일 뿐 저장을 거부하는 근거로 쓰지 않는다.
    normalized: dict[str, str] = {}
    for key in HIERARCHICAL_DIMENSION_KEYS:
        normalized[key] = _required_text(value[key], label=f"{label}.{key}")
    return normalized


def _row_dimensions(row: Mapping[str, Any]) -> dict[str, str]:
    return {key: str(row[key]) for key in HIERARCHICAL_DIMENSION_KEYS}


def _empty_dimension_evidence() -> dict[str, dict[str, list[str]]]:
    return {
        key: {"support": [], "contradict": []}
        for key in HIERARCHICAL_DIMENSION_KEYS
    }


def _dimension_evidence(
    value: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, dict[str, list[str]]]:
    if not isinstance(value, Mapping) or set(value) != set(HIERARCHICAL_DIMENSION_KEYS):
        raise ValueError(
            f"{label} must contain exactly {list(HIERARCHICAL_DIMENSION_KEYS)}"
        )
    normalized: dict[str, dict[str, list[str]]] = {}
    for dimension in HIERARCHICAL_DIMENSION_KEYS:
        relations = value[dimension]
        if not isinstance(relations, Mapping) or set(relations) != {
            "support",
            "contradict",
        }:
            raise ValueError(
                f"{label}.{dimension} must contain exactly support and contradict"
            )
        normalized_relations: dict[str, list[str]] = {}
        for relation in ("support", "contradict"):
            raw_ids = relations[relation]
            if not isinstance(raw_ids, list):
                raise ValueError(
                    f"{label}.{dimension}.{relation} must be a list"
                )
            ids = [
                _required_text(
                    evidence_id,
                    label=f"{label}.{dimension}.{relation} evidence ID",
                )
                for evidence_id in raw_ids
            ]
            if len(ids) != len(set(ids)):
                raise ValueError(
                    f"{label}.{dimension}.{relation} contains duplicate IDs"
                )
            normalized_relations[relation] = ids
        overlap = set(normalized_relations["support"]) & set(
            normalized_relations["contradict"]
        )
        if overlap:
            raise ValueError(
                f"{label}.{dimension} reuses IDs across support and contradict: "
                + ",".join(sorted(overlap))
            )
        normalized[dimension] = normalized_relations
    return normalized


def _insert_or_verify(
    connection: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    key_value: str,
    values: Mapping[str, Any],
    compare_columns: tuple[str, ...],
) -> None:
    existing = connection.execute(
        f"SELECT * FROM {table} WHERE {key_column} = ?",
        (key_value,),
    ).fetchone()
    if existing is not None:
        mismatched = [
            column
            for column in compare_columns
            if existing[column] != values[column]
        ]
        if mismatched:
            raise ValueError(
                f"{table} idempotency mismatch for {key_value}: {mismatched}"
            )
        return
    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    try:
        connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            tuple(values[column] for column in columns),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"invalid {table} lineage for {key_value}: {exc}") from exc


class MemoryAgent:
    def __init__(
        self,
        db_path: Path | str = config.SIM_DB,
        *,
        event_schedule: FrozenEventSchedule | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        if event_schedule is not None and not isinstance(
            event_schedule,
            FrozenEventSchedule,
        ):
            raise TypeError("event_schedule must be a FrozenEventSchedule")
        self.event_schedule = event_schedule
        init_sim_db(self.db_path)
        self._ensure_trade_log_columns()

    def save_belief(self, belief: dict[str, Any]) -> None:
        required = {"agent_id", "turn", "date", "belief_summary"}
        missing = required - belief.keys()
        if missing:
            raise ValueError(f"belief missing required keys: {sorted(missing)}")
        belief_id = belief.get("belief_id") or f"belief_{belief['agent_id']}_t{belief['turn']:03d}"
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO belief_history (
                    belief_id, agent_id, turn, date, dim_1, dim_2, dim_3,
                    dim_4, dim_5, dim_6, belief_summary, view_change
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    belief_id,
                    belief["agent_id"],
                    int(belief["turn"]),
                    belief["date"],
                    belief.get("dim_1"),
                    belief.get("dim_2"),
                    belief.get("dim_3"),
                    belief.get("dim_4"),
                    belief.get("dim_5"),
                    belief.get("dim_6"),
                    belief["belief_summary"],
                    belief.get("view_change"),
                ),
            )
            conn.commit()

    def save_stb(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        subturn: str,
        dimensions: Mapping[str, Any],
        evidence: Any | None = None,
        dimension_evidence: Any | None = None,
        stb_id: str | None = None,
    ) -> str:
        """Persist one current-evidence-only six-dimensional STB.

        The STB table intentionally has no ``belief_summary`` or ``view_change``
        column.  Those human-readable compatibility fields are not scientific
        decision inputs in the hierarchical path.
        """

        agent = _required_text(agent_id, label="STB agent_id")
        decision_turn = _positive_int(turn, label="STB turn")
        event_date = _required_text(date, label="STB date")
        event_subturn = _subturn(subturn)
        belief = _dimensions(dimensions, label="STB dimensions")
        evidence_value = [] if evidence is None else evidence
        dimension_evidence_value = (
            {
                key: {"support": [], "contradict": []}
                for key in HIERARCHICAL_DIMENSION_KEYS
            }
            if dimension_evidence is None
            else dimension_evidence
        )
        evidence_json = _canonical_json(evidence_value, label="STB evidence")
        dimension_evidence_json = _canonical_json(
            dimension_evidence_value,
            label="STB dimension evidence",
        )
        actual_id = stb_id or f"stb_{agent}_t{decision_turn:03d}"
        scientific = _scientific_sha256(
            {
                "artifact_type": "simulation_stb",
                "agent_id": agent,
                "turn": decision_turn,
                "date": event_date,
                "subturn": event_subturn,
                "dimensions": belief,
                "evidence": evidence_value,
                "dimension_evidence": dimension_evidence_value,
            },
            label="STB scientific state",
        )
        values = {
            "stb_id": actual_id,
            "agent_id": agent,
            "turn": decision_turn,
            "date": event_date,
            "subturn": event_subturn,
            **belief,
            "evidence_json": evidence_json,
            "dimension_evidence_json": dimension_evidence_json,
            "scientific_sha256": scientific,
        }
        with connect(self.db_path) as conn:
            _insert_or_verify(
                conn,
                table="simulation_stb_states",
                key_column="stb_id",
                key_value=actual_id,
                values=values,
                compare_columns=("scientific_sha256",),
            )
            conn.commit()
        return actual_id

    def get_stb(self, stb_id: str) -> dict[str, Any]:
        row = self._hierarchical_row(
            "simulation_stb_states",
            "stb_id",
            _required_text(stb_id, label="stb_id"),
        )
        return {
            "stb_id": str(row["stb_id"]),
            "agent_id": str(row["agent_id"]),
            "turn": int(row["turn"]),
            "date": str(row["date"]),
            "subturn": str(row["subturn"]),
            "dimensions": _row_dimensions(row),
            "evidence": json.loads(str(row["evidence_json"])),
            "dimension_evidence": json.loads(
                str(row["dimension_evidence_json"])
            ),
            "scientific_sha256": str(row["scientific_sha256"]),
        }

    def record_analysis_lineage(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        subturn: str,
        source_ltb_id: str,
        source_stb_id: str,
        analysis: Mapping[str, Any],
        analysis_id: str | None = None,
    ) -> str:
        """Bind one validated decision-making analysis to its two beliefs."""

        if not isinstance(analysis, Mapping) or not analysis:
            raise ValueError("analysis must be a non-empty object")
        forbidden = {"belief_summary", "view_change"} & set(analysis)
        if forbidden:
            raise ValueError(
                "analysis lineage may not use human belief log fields: "
                + ",".join(sorted(forbidden))
            )
        agent = _required_text(agent_id, label="analysis agent_id")
        analysis_turn = _positive_int(turn, label="analysis turn")
        event_date = _required_text(date, label="analysis date")
        event_subturn = _subturn(subturn)
        parent = self.previous_ltb(
            agent_id=agent,
            decision_turn=analysis_turn,
        )
        if parent["ltb_id"] != source_ltb_id:
            raise ValueError("analysis source_ltb_id is not the latest visible LTB")
        stb = self.get_stb(source_stb_id)
        if (
            stb["agent_id"] != agent
            or stb["turn"] != analysis_turn
            or stb["date"] != event_date
            or stb["subturn"] != event_subturn
        ):
            raise ValueError("analysis source STB differs from its agent/event")
        analysis_json = _canonical_json(analysis, label="analysis")
        actual_id = analysis_id or f"analysis_{agent}_t{analysis_turn:03d}"
        scientific = _scientific_sha256(
            {
                "artifact_type": "simulation_analysis",
                "agent_id": agent,
                "turn": analysis_turn,
                "date": event_date,
                "subturn": event_subturn,
                "source_ltb_id": source_ltb_id,
                "source_ltb_sha256": parent["scientific_sha256"],
                "source_stb_id": source_stb_id,
                "source_stb_sha256": stb["scientific_sha256"],
                "analysis": dict(analysis),
            },
            label="analysis scientific state",
        )
        values = {
            "analysis_id": actual_id,
            "agent_id": agent,
            "turn": analysis_turn,
            "date": event_date,
            "subturn": event_subturn,
            "source_ltb_id": source_ltb_id,
            "source_stb_id": source_stb_id,
            "analysis_json": analysis_json,
            "scientific_sha256": scientific,
        }
        with connect(self.db_path) as conn:
            _insert_or_verify(
                conn,
                table="simulation_analyses",
                key_column="analysis_id",
                key_value=actual_id,
                values=values,
                compare_columns=("scientific_sha256",),
            )
            self._attach_trade_lineage(
                conn,
                agent_id=agent,
                turn=analysis_turn,
                lineage={"analysis_id": actual_id},
            )
            conn.commit()
        return actual_id

    def get_analysis_lineage(self, analysis_id: str) -> dict[str, Any]:
        row = self._hierarchical_row(
            "simulation_analyses",
            "analysis_id",
            _required_text(analysis_id, label="analysis_id"),
        )
        return {
            "analysis_id": str(row["analysis_id"]),
            "agent_id": str(row["agent_id"]),
            "turn": int(row["turn"]),
            "date": str(row["date"]),
            "subturn": str(row["subturn"]),
            "source_ltb_id": str(row["source_ltb_id"]),
            "source_stb_id": str(row["source_stb_id"]),
            "analysis": json.loads(str(row["analysis_json"])),
            "scientific_sha256": str(row["scientific_sha256"]),
        }

    def bootstrap_ltb(
        self,
        *,
        agent_id: str,
        date: str,
        dimensions: Mapping[str, Any],
        belief_summary: str,
        view_change: Any = "initial",
        ltb_id: str | None = None,
    ) -> str:
        """Create LTB_0, visible to the first AM decision.

        ``belief_summary`` and ``view_change`` are stored and hashed only as a
        human log.  They are excluded from the scientific LTB digest and from
        :meth:`previous_ltb`.
        """

        agent = _required_text(agent_id, label="LTB agent_id")
        event_date = _required_text(date, label="LTB date")
        belief = _dimensions(dimensions, label="initial LTB dimensions")
        summary = _required_text(
            belief_summary,
            label="initial LTB belief_summary",
        )
        view_change_json = self._view_change_json(view_change)
        integration_evidence = _empty_dimension_evidence()
        integration_evidence_json = _canonical_json(
            integration_evidence,
            label="initial LTB integration_evidence",
        )
        actual_id = ltb_id or f"ltb_{agent}_t000"
        scientific = _scientific_sha256(
            {
                "artifact_type": "simulation_ltb",
                "agent_id": agent,
                "turn": 0,
                "visible_from_turn": 1,
                "date": event_date,
                "subturn": "initial",
                "parent_ltb_id": None,
                "source_stb_id": None,
                "source_decision_id": None,
                "source_fill_id": None,
                "dimensions": belief,
                "integration_evidence": integration_evidence,
            },
            label="initial LTB scientific state",
        )
        human_log_sha256 = _scientific_sha256(
            {
                "belief_summary": summary,
                "view_change": json.loads(view_change_json),
            },
            label="initial LTB human log",
        )
        values = {
            "ltb_id": actual_id,
            "agent_id": agent,
            "turn": 0,
            "visible_from_turn": 1,
            "date": event_date,
            "subturn": "initial",
            "parent_ltb_id": None,
            "source_stb_id": None,
            "source_decision_id": None,
            "source_fill_id": None,
            **belief,
            "integration_evidence_json": integration_evidence_json,
            "scientific_sha256": scientific,
            "belief_summary": summary,
            "view_change_json": view_change_json,
            "human_log_sha256": human_log_sha256,
        }
        with connect(self.db_path) as conn:
            _insert_or_verify(
                conn,
                table="simulation_ltb_states",
                key_column="ltb_id",
                key_value=actual_id,
                values=values,
                compare_columns=("scientific_sha256", "human_log_sha256"),
            )
            conn.commit()
        return actual_id

    def ensure_initial_ltb(self, agent_id: str) -> str:
        """Materialize LTB_0 from the numbered pipeline's turn-zero belief."""

        agent = _required_text(agent_id, label="initial LTB agent_id")
        try:
            return str(
                self.previous_ltb(
                    agent_id=agent,
                    decision_turn=1,
                )["ltb_id"]
            )
        except ValueError:
            pass
        with connect(self.db_path, read_only=True) as conn:
            row = conn.execute(
                """
                SELECT date, dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                       belief_summary, view_change
                FROM belief_history
                WHERE agent_id = ? AND turn = 0
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (agent,),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"initial belief is missing for agent={agent}; "
                "run scripts/04_build_experiment_base.py first"
            )
        dimensions = {
            key: row[key]
            for key in HIERARCHICAL_DIMENSION_KEYS
        }
        return self.bootstrap_ltb(
            agent_id=agent,
            date=str(row["date"]),
            dimensions=dimensions,
            belief_summary=str(row["belief_summary"]),
            view_change=row["view_change"] or "initial",
        )

    def previous_ltb(self, *, agent_id: str, decision_turn: int) -> dict[str, Any]:
        """Return the latest six-dimensional LTB visible to a decision.

        The human ``belief_summary`` and ``view_change`` fields are deliberately
        omitted so callers cannot accidentally use them instead of dim_1..dim_6.
        A turn-t post-fill LTB has ``visible_from_turn=t+1``.
        """

        agent = _required_text(agent_id, label="LTB agent_id")
        turn = _positive_int(decision_turn, label="decision_turn")
        with connect(self.db_path, read_only=True) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM simulation_ltb_states
                WHERE agent_id = ? AND visible_from_turn <= ?
                ORDER BY turn DESC, created_at DESC
                LIMIT 1
                """,
                (agent, turn),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"no previous visible LTB for agent={agent} turn={turn}"
            )
        return self._ltb_state(row)

    def get_ltb(self, ltb_id: str) -> dict[str, Any]:
        """Read one scientific LTB projection without its human log fields."""

        row = self._hierarchical_row(
            "simulation_ltb_states",
            "ltb_id",
            _required_text(ltb_id, label="ltb_id"),
        )
        return self._ltb_state(row)

    def get_ltb_human_log(self, ltb_id: str) -> dict[str, Any]:
        """Read the non-causal compatibility log for one LTB."""

        row = self._hierarchical_row(
            "simulation_ltb_states",
            "ltb_id",
            _required_text(ltb_id, label="ltb_id"),
        )
        return {
            "ltb_id": str(row["ltb_id"]),
            "belief_summary": str(row["belief_summary"]),
            "view_change": json.loads(str(row["view_change_json"])),
            "human_log_sha256": str(row["human_log_sha256"]),
        }

    def record_decision_lineage(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        subturn: str,
        source_ltb_id: str,
        source_stb_id: str,
        analysis_id: str,
        decision: Mapping[str, Any],
        decision_id: str | None = None,
    ) -> str:
        """Bind a buy/sell decision to the exact previous LTB and current STB."""

        if not isinstance(decision, Mapping):
            raise ValueError("decision must be an object")
        forbidden = {"belief_summary", "view_change"} & set(decision)
        if forbidden:
            raise ValueError(
                "decision lineage may not use human belief log fields: "
                + ",".join(sorted(forbidden))
            )
        agent = _required_text(agent_id, label="decision agent_id")
        decision_turn = _positive_int(turn, label="decision turn")
        event_date = _required_text(date, label="decision date")
        event_subturn = _subturn(subturn)
        action = _required_text(
            decision.get("action"),
            label="decision action",
        ).lower()
        if action not in {"buy", "sell"}:
            raise ValueError("decision action must be buy or sell")
        quantity = _positive_int(
            decision.get("quantity", decision.get("requested_quantity")),
            label="decision quantity",
        )
        parent = self.previous_ltb(
            agent_id=agent,
            decision_turn=decision_turn,
        )
        if parent["ltb_id"] != source_ltb_id:
            raise ValueError("decision source_ltb_id is not the latest visible LTB")
        stb = self.get_stb(source_stb_id)
        if (
            stb["agent_id"] != agent
            or stb["turn"] != decision_turn
            or stb["date"] != event_date
            or stb["subturn"] != event_subturn
        ):
            raise ValueError("decision source STB differs from its agent/event")
        analysis = self.get_analysis_lineage(analysis_id)
        if (
            analysis["agent_id"] != agent
            or analysis["turn"] != decision_turn
            or analysis["date"] != event_date
            or analysis["subturn"] != event_subturn
            or analysis["source_ltb_id"] != source_ltb_id
            or analysis["source_stb_id"] != source_stb_id
        ):
            raise ValueError("decision source analysis has inconsistent lineage")
        decision_json = _canonical_json(decision, label="decision")
        actual_id = decision_id or f"decision_{agent}_t{decision_turn:03d}"
        scientific = _scientific_sha256(
            {
                "artifact_type": "simulation_decision",
                "agent_id": agent,
                "turn": decision_turn,
                "date": event_date,
                "subturn": event_subturn,
                "action": action,
                "requested_quantity": quantity,
                "source_ltb_id": source_ltb_id,
                "source_ltb_sha256": parent["scientific_sha256"],
                "source_stb_id": source_stb_id,
                "source_stb_sha256": stb["scientific_sha256"],
                "analysis_id": analysis_id,
                "analysis_sha256": analysis["scientific_sha256"],
                "decision": dict(decision),
            },
            label="decision scientific state",
        )
        values = {
            "decision_id": actual_id,
            "agent_id": agent,
            "turn": decision_turn,
            "date": event_date,
            "subturn": event_subturn,
            "action": action,
            "requested_quantity": quantity,
            "source_ltb_id": source_ltb_id,
            "source_stb_id": source_stb_id,
            "analysis_id": analysis_id,
            "decision_json": decision_json,
            "scientific_sha256": scientific,
        }
        with connect(self.db_path) as conn:
            _insert_or_verify(
                conn,
                table="simulation_decisions",
                key_column="decision_id",
                key_value=actual_id,
                values=values,
                compare_columns=("scientific_sha256",),
            )
            self._attach_trade_lineage(
                conn,
                agent_id=agent,
                turn=decision_turn,
                lineage={
                    "analysis_id": analysis_id,
                    "decision_id": actual_id,
                    "source_ltb_id": source_ltb_id,
                    "source_stb_id": source_stb_id,
                },
            )
            conn.commit()
        return actual_id

    def get_decision_lineage(self, decision_id: str) -> dict[str, Any]:
        row = self._hierarchical_row(
            "simulation_decisions",
            "decision_id",
            _required_text(decision_id, label="decision_id"),
        )
        return {
            "decision_id": str(row["decision_id"]),
            "agent_id": str(row["agent_id"]),
            "turn": int(row["turn"]),
            "date": str(row["date"]),
            "subturn": str(row["subturn"]),
            "action": str(row["action"]),
            "requested_quantity": int(row["requested_quantity"]),
            "source_ltb_id": str(row["source_ltb_id"]),
            "source_stb_id": str(row["source_stb_id"]),
            "analysis_id": str(row["analysis_id"]),
            "decision": json.loads(str(row["decision_json"])),
            "scientific_sha256": str(row["scientific_sha256"]),
        }

    def record_fill_lineage(
        self,
        *,
        decision_id: str,
        filled_quantity: int,
        executed_price: float,
        pre_portfolio: Mapping[str, Any],
        post_portfolio: Mapping[str, Any],
        stock_code: str = config.STOCK_CODE,
        fee: float = 0.0,
        fill_id: str | None = None,
    ) -> str:
        """Persist the deterministic full fill derived from a committed decision."""

        decision = self.get_decision_lineage(decision_id)
        quantity = _positive_int(filled_quantity, label="filled_quantity")
        if quantity != int(decision["requested_quantity"]):
            raise ValueError("hierarchical fill must fully match requested_quantity")
        price = _positive_number(executed_price, label="executed_price")
        if isinstance(fee, bool):
            raise ValueError("hierarchical fill fee must be zero")
        try:
            normalized_fee = float(fee)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("hierarchical fill fee must be zero") from exc
        if normalized_fee != 0.0:
            raise ValueError("hierarchical fill fee must be zero")
        if not isinstance(pre_portfolio, Mapping) or not isinstance(
            post_portfolio,
            Mapping,
        ):
            raise ValueError("fill pre/post portfolio must be objects")
        pre_json = _canonical_json(pre_portfolio, label="pre_portfolio")
        post_json = _canonical_json(post_portfolio, label="post_portfolio")
        code = _required_text(stock_code, label="stock_code")
        if self.event_schedule is not None:
            try:
                scheduled = self.event_schedule.event_for_identity(
                    turn=int(decision["turn"]),
                    event_date=str(decision["date"]),
                    subturn=str(decision["subturn"]),
                )
            except OutcomeScheduleError as exc:
                raise ValueError(
                    "fill event differs from the frozen event schedule"
                ) from exc
            if code != self.event_schedule.stock_code:
                raise ValueError(
                    "fill stock_code differs from the frozen price registry"
                )
            if abs(price - float(scheduled["execution_price"])) > 1e-6:
                raise ValueError(
                    "fill executed_price differs from the frozen event price"
                )
        actual_id = fill_id or (
            f"fill_{decision['agent_id']}_t{int(decision['turn']):03d}"
        )
        scientific = _scientific_sha256(
            {
                "artifact_type": "simulation_fill",
                "agent_id": decision["agent_id"],
                "turn": decision["turn"],
                "date": decision["date"],
                "subturn": decision["subturn"],
                "stock_code": code,
                "action": decision["action"],
                "requested_quantity": decision["requested_quantity"],
                "filled_quantity": quantity,
                "executed_price": price,
                "fee": normalized_fee,
                "source_ltb_id": decision["source_ltb_id"],
                "source_stb_id": decision["source_stb_id"],
                "decision_id": decision_id,
                "decision_sha256": decision["scientific_sha256"],
                "pre_portfolio": dict(pre_portfolio),
                "post_portfolio": dict(post_portfolio),
            },
            label="fill scientific state",
        )
        values = {
            "fill_id": actual_id,
            "agent_id": decision["agent_id"],
            "turn": decision["turn"],
            "date": decision["date"],
            "subturn": decision["subturn"],
            "stock_code": code,
            "action": decision["action"],
            "requested_quantity": decision["requested_quantity"],
            "filled_quantity": quantity,
            "executed_price": price,
            "fee": normalized_fee,
            "source_ltb_id": decision["source_ltb_id"],
            "source_stb_id": decision["source_stb_id"],
            "decision_id": decision_id,
            "pre_portfolio_json": pre_json,
            "post_portfolio_json": post_json,
            "scientific_sha256": scientific,
        }
        with connect(self.db_path) as conn:
            _insert_or_verify(
                conn,
                table="simulation_fills",
                key_column="fill_id",
                key_value=actual_id,
                values=values,
                compare_columns=("scientific_sha256",),
            )
            self._attach_trade_lineage(
                conn,
                agent_id=str(decision["agent_id"]),
                turn=int(decision["turn"]),
                lineage={
                    "decision_id": decision_id,
                    "source_ltb_id": decision["source_ltb_id"],
                    "source_stb_id": decision["source_stb_id"],
                    "fill_id": actual_id,
                },
            )
            conn.commit()
        return actual_id

    def get_fill_lineage(self, fill_id: str) -> dict[str, Any]:
        row = self._hierarchical_row(
            "simulation_fills",
            "fill_id",
            _required_text(fill_id, label="fill_id"),
        )
        return {
            "fill_id": str(row["fill_id"]),
            "agent_id": str(row["agent_id"]),
            "turn": int(row["turn"]),
            "date": str(row["date"]),
            "subturn": str(row["subturn"]),
            "stock_code": str(row["stock_code"]),
            "action": str(row["action"]),
            "requested_quantity": int(row["requested_quantity"]),
            "filled_quantity": int(row["filled_quantity"]),
            "executed_price": float(row["executed_price"]),
            "fee": float(row["fee"]),
            "source_ltb_id": str(row["source_ltb_id"]),
            "source_stb_id": str(row["source_stb_id"]),
            "decision_id": str(row["decision_id"]),
            "pre_portfolio": json.loads(str(row["pre_portfolio_json"])),
            "post_portfolio": json.loads(str(row["post_portfolio_json"])),
            "scientific_sha256": str(row["scientific_sha256"]),
        }

    def mature_outcomes_for_event(self, event_id: str) -> tuple[str, ...]:
        """Materialize every price outcome that becomes knowable at ``event_id``.

        The caller supplies no mark price.  The price is read only from the
        validated frozen schedule, and the method rejects a jump to a future
        event relative to the integrated fill ledger.
        """

        schedule = self._require_event_schedule()
        scheduled = schedule.event(
            _required_text(event_id, label="outcome event_id")
        )
        outcome_ids: list[str] = []
        with connect(self.db_path) as conn:
            self._assert_outcome_event_order(
                conn,
                event_id=str(scheduled["event_id"]),
            )
            fills = conn.execute(
                """
                SELECT *
                FROM simulation_fills
                WHERE turn < ?
                ORDER BY turn, agent_id
                """,
                (int(scheduled["turn"]),),
            ).fetchall()
            for fill in fills:
                fill_event = self._scheduled_event_for_fill(fill)
                for horizon in OUTCOME_HORIZONS:
                    due_event_id = schedule.due_event_id(
                        fill_event_id=str(fill_event["event_id"]),
                        horizon=horizon,
                    )
                    if due_event_id != scheduled["event_id"]:
                        continue
                    outcome_ids.append(
                        self._record_terminal_outcome_in_transaction(
                            conn,
                            fill=fill,
                            horizon=horizon,
                            due_event_id=str(scheduled["event_id"]),
                            mark_price=float(scheduled["execution_price"]),
                            status="matured",
                        )
                    )
            conn.commit()
        return tuple(outcome_ids)

    def eligible_outcomes(
        self,
        agent_id: str,
        event_id: str,
    ) -> list[dict[str, Any]]:
        """Return this agent's unconsumed outcomes available at this event.

        The result is safe to place only in the dim_6 LTB-update context.
        ``save_post_fill_ltb`` independently verifies exact consumption, so a
        caller cannot omit, reuse, or move an outcome to another dimension.
        """

        agent = _required_text(agent_id, label="outcome agent_id")
        schedule = self._require_event_schedule()
        scheduled = schedule.event(
            _required_text(event_id, label="outcome event_id")
        )
        with connect(self.db_path, read_only=True) as conn:
            self._assert_outcome_event_order(
                conn,
                event_id=str(scheduled["event_id"]),
            )
            rows = self._due_outcome_rows_for_agent_event(
                conn,
                agent_id=agent,
                event_id=str(scheduled["event_id"]),
                require_complete=True,
            )
            consumed = {
                str(row["outcome_id"])
                for row in conn.execute(
                    """
                    SELECT outcome_id
                    FROM simulation_outcome_consumptions
                    WHERE consumed_at_event_id = ?
                    """,
                    (str(scheduled["event_id"]),),
                ).fetchall()
            }
        eligible: list[dict[str, Any]] = []
        for row in rows:
            outcome_id = str(row["outcome_id"])
            if outcome_id in consumed:
                continue
            entry_action = str(row["entry_action"])
            entry_price = float(row["entry_price"])
            mark_price = float(row["mark_price"])
            direction = 1.0 if entry_action == "buy" else -1.0
            price_change_pct = (mark_price - entry_price) / entry_price * 100.0
            # 부분 매도인지 전량 청산인지, 매수 뒤 잔여 현금이 있었는지는
            # 이 결과를 어떻게 읽어야 하는지에 결정적이다. 예전에는 체결
            # 수량과 잔여 포지션이 payload에 없어서, 모델이 264주 중 100주를
            # 판 것과 전량 청산을 구분할 수 없었다.
            entry_quantity = int(row["entry_quantity"])
            try:
                pre_portfolio = json.loads(
                    str(row["entry_pre_portfolio_json"]) or "{}"
                )
                post_portfolio = json.loads(
                    str(row["entry_post_portfolio_json"]) or "{}"
                )
            except (TypeError, ValueError):
                pre_portfolio, post_portfolio = {}, {}
            position_before = pre_portfolio.get("quantity")
            position_after = post_portfolio.get("quantity")
            eligible.append(
                {
                    "outcome_id": outcome_id,
                    "fill_id": str(row["fill_id"]),
                    "horizon": str(row["horizon"]),
                    "mark_price": mark_price,
                    "observed_event_id": str(row["observed_event_id"]),
                    "entry_action": entry_action,
                    "entry_price": entry_price,
                    "entry_quantity": entry_quantity,
                    "position_before_trade": position_before,
                    "position_after_trade": position_after,
                    "full_exit": (
                        entry_action == "sell" and position_after == 0
                    ),
                    "price_change_pct_since_trade": price_change_pct,
                    "action_aligned_markout": (
                        direction * (mark_price - entry_price) / entry_price
                    ),
                    "source_ltb_id": str(row["source_ltb_id"]),
                    "source_stb_id": str(row["source_stb_id"]),
                }
            )
        return eligible

    def right_censor_unavailable_outcomes(self) -> tuple[str, ...]:
        """Close only horizons with no future event in the frozen schedule."""

        schedule = self._require_event_schedule()
        outcome_ids: list[str] = []
        with connect(self.db_path) as conn:
            max_turn_row = conn.execute(
                "SELECT COALESCE(MAX(turn), 0) AS max_turn FROM simulation_fills"
            ).fetchone()
            max_turn = 0 if max_turn_row is None else int(max_turn_row["max_turn"])
            if max_turn != len(schedule.events):
                raise ValueError(
                    "outcomes may be right-censored only after the final "
                    "scheduled event has produced a fill"
                )
            fills = conn.execute(
                """
                SELECT *
                FROM simulation_fills
                ORDER BY turn, agent_id
                """
            ).fetchall()
            for fill in fills:
                fill_event = self._scheduled_event_for_fill(fill)
                for horizon in OUTCOME_HORIZONS:
                    if (
                        schedule.due_event_id(
                            fill_event_id=str(fill_event["event_id"]),
                            horizon=horizon,
                        )
                        is not None
                    ):
                        continue
                    outcome_ids.append(
                        self._record_terminal_outcome_in_transaction(
                            conn,
                            fill=fill,
                            horizon=horizon,
                            due_event_id=None,
                            mark_price=None,
                            status="right_censored",
                        )
                    )
            self._assert_outcome_ledger_finalized(conn)
            conn.commit()
        return tuple(outcome_ids)

    def save_post_fill_ltb(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        subturn: str,
        parent_ltb_id: str,
        stb_id: str,
        decision_id: str,
        fill_id: str,
        dimensions: Mapping[str, Any],
        integration_evidence: Mapping[str, Any],
        belief_summary: str,
        view_change: Any,
        ignored_outcome_ids: Iterable[str] = (),
        ltb_id: str | None = None,
    ) -> str:
        """Persist LTB_t only after the same-turn decision and full fill exist."""

        agent = _required_text(agent_id, label="LTB agent_id")
        event_turn = _positive_int(turn, label="LTB turn")
        event_date = _required_text(date, label="LTB date")
        event_subturn = _subturn(subturn)
        parent = self.previous_ltb(
            agent_id=agent,
            decision_turn=event_turn,
        )
        if parent["ltb_id"] != parent_ltb_id:
            raise ValueError("LTB parent is not the latest visible LTB")
        stb = self.get_stb(stb_id)
        decision = self.get_decision_lineage(decision_id)
        fill = self.get_fill_lineage(fill_id)
        event_identity = (agent, event_turn, event_date, event_subturn)
        if (
            (stb["agent_id"], stb["turn"], stb["date"], stb["subturn"])
            != event_identity
        ):
            raise ValueError("LTB source STB differs from its agent/event")
        if (
            (
                decision["agent_id"],
                decision["turn"],
                decision["date"],
                decision["subturn"],
            )
            != event_identity
            or decision["source_ltb_id"] != parent_ltb_id
            or decision["source_stb_id"] != stb_id
        ):
            raise ValueError("LTB source decision has inconsistent lineage")
        if (
            (fill["agent_id"], fill["turn"], fill["date"], fill["subturn"])
            != event_identity
            or fill["decision_id"] != decision_id
            or fill["source_ltb_id"] != parent_ltb_id
            or fill["source_stb_id"] != stb_id
        ):
            raise ValueError("LTB source fill has inconsistent lineage")
        belief = _dimensions(dimensions, label="post-fill LTB dimensions")
        normalized_integration_evidence = _dimension_evidence(
            integration_evidence,
            label="post-fill LTB integration_evidence",
        )
        integration_evidence_json = _canonical_json(
            normalized_integration_evidence,
            label="post-fill LTB integration_evidence",
        )
        normalized_ignored_outcome_ids = tuple(
            sorted({str(item) for item in ignored_outcome_ids})
        )
        # 차원별 문장 유지는 정당하다. 관점이 안 변한 차원에 새 표현을 강제하면
        # 임베딩 기반 deviation 측정에 억지 패러프레이즈 노이즈가 깔린다.
        # 퇴행적 전체 복사만 막는다. 같은 정책이 생성 경계(llm/belief.py)와
        # 이 저장 경계 두 곳에 있으므로 함께 유지해야 한다.
        if all(
            belief[key] == parent["dimensions"][key]
            for key in HIERARCHICAL_DIMENSION_KEYS
        ):
            raise ValueError(
                "post-fill LTB must not copy all six dimensions verbatim"
            )
        summary = _required_text(
            belief_summary,
            label="post-fill LTB belief_summary",
        )
        view_change_json = self._view_change_json(view_change)
        actual_id = ltb_id or f"ltb_{agent}_t{event_turn:03d}"
        scientific = _scientific_sha256(
            {
                "artifact_type": "simulation_ltb",
                "agent_id": agent,
                "turn": event_turn,
                "visible_from_turn": event_turn + 1,
                "date": event_date,
                "subturn": event_subturn,
                "parent_ltb_id": parent_ltb_id,
                "parent_ltb_sha256": parent["scientific_sha256"],
                "source_stb_id": stb_id,
                "source_stb_sha256": stb["scientific_sha256"],
                "source_decision_id": decision_id,
                "source_decision_sha256": decision["scientific_sha256"],
                "source_fill_id": fill_id,
                "source_fill_sha256": fill["scientific_sha256"],
                "dimensions": belief,
                "integration_evidence": normalized_integration_evidence,
                "ignored_outcome_ids": list(normalized_ignored_outcome_ids),
            },
            label="post-fill LTB scientific state",
        )
        human_log_sha256 = _scientific_sha256(
            {
                "belief_summary": summary,
                "view_change": json.loads(view_change_json),
            },
            label="post-fill LTB human log",
        )
        values = {
            "ltb_id": actual_id,
            "agent_id": agent,
            "turn": event_turn,
            "visible_from_turn": event_turn + 1,
            "date": event_date,
            "subturn": event_subturn,
            "parent_ltb_id": parent_ltb_id,
            "source_stb_id": stb_id,
            "source_decision_id": decision_id,
            "source_fill_id": fill_id,
            **belief,
            "integration_evidence_json": integration_evidence_json,
            "scientific_sha256": scientific,
            "belief_summary": summary,
            "view_change_json": view_change_json,
            "human_log_sha256": human_log_sha256,
        }
        with connect(self.db_path) as conn:
            outcome_ids: tuple[str, ...] = ()
            scheduled_event_id: str | None = None
            if self.event_schedule is not None:
                try:
                    scheduled = self.event_schedule.event_for_identity(
                        turn=event_turn,
                        event_date=event_date,
                        subturn=event_subturn,
                    )
                except OutcomeScheduleError as exc:
                    raise ValueError(
                        "post-fill LTB event differs from the frozen schedule"
                    ) from exc
                scheduled_event_id = str(scheduled["event_id"])
                self._assert_outcome_event_order(
                    conn,
                    event_id=scheduled_event_id,
                )
                outcome_ids = self._validate_ltb_outcome_evidence(
                    conn,
                    agent_id=agent,
                    event_id=scheduled_event_id,
                    stb=stb,
                    integration_evidence=normalized_integration_evidence,
                    ignored_outcome_ids=normalized_ignored_outcome_ids,
                )
            else:
                self._validate_ltb_integration_evidence(
                    stb=stb,
                    integration_evidence=normalized_integration_evidence,
                )
            _insert_or_verify(
                conn,
                table="simulation_ltb_states",
                key_column="ltb_id",
                key_value=actual_id,
                values=values,
                compare_columns=("scientific_sha256", "human_log_sha256"),
            )
            for outcome_id in outcome_ids:
                self._consume_outcome_in_transaction(
                    conn,
                    outcome_id=outcome_id,
                    ltb_id=actual_id,
                    agent_id=agent,
                    event_id=str(scheduled_event_id),
                )
            self._attach_trade_lineage(
                conn,
                agent_id=agent,
                turn=event_turn,
                lineage={
                    "decision_id": decision_id,
                    "source_ltb_id": parent_ltb_id,
                    "source_stb_id": stb_id,
                    "fill_id": fill_id,
                    "post_fill_ltb_id": actual_id,
                },
            )
            conn.commit()
        return actual_id

    def get_previous_belief(self, agent_id: str, turn: int) -> str | None:
        previous_turn = 0 if turn <= 1 else turn - 1
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT belief_summary
                FROM belief_history
                WHERE agent_id = ? AND turn = ?
                """,
                (agent_id, previous_turn),
            ).fetchone()
        return None if row is None else str(row["belief_summary"])

    def init_portfolio_t000(self, agents: list[dict[str, Any]], date: str = "t000") -> None:
        with connect(self.db_path) as conn:
            for agent in agents:
                ini_cash = float(agent["ini_cash"])
                conn.execute(
                    """
                    INSERT OR REPLACE INTO portfolio_state (
                        state_id, agent_id, turn, date, cash, positions,
                        total_value, realized_pnl, total_return_rate
                    ) VALUES (?, ?, 0, ?, ?, ?, ?, 0, 0)
                    """,
                    (
                        f"ps_{agent['agent_id']}_t000",
                        agent["agent_id"],
                        date,
                        ini_cash,
                        "[]",
                        ini_cash,
                    ),
                )
            conn.commit()

    def update_portfolio(
        self,
        agent_id: str,
        turn: int,
        date: str,
        fills: list[dict[str, Any]],
        *,
        current_prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        previous = self._latest_portfolio(agent_id, before_or_at_turn=turn - 1)
        if previous is None:
            raise ValueError(f"previous portfolio not found for {agent_id} before turn {turn}")

        cash = float(previous["cash"])
        realized_pnl = float(previous["realized_pnl"])
        initial_value = self._initial_value(agent_id)
        positions = {
            pos["stock_code"]: {
                "stock_code": pos["stock_code"],
                "quantity": int(pos["quantity"]),
                "avg_cost": float(pos["avg_cost"]),
                "current_price": float(pos.get("current_price", pos["avg_cost"])),
                "unrealized_pnl": float(pos.get("unrealized_pnl", 0)),
                "unrealized_pnl_rate": float(pos.get("unrealized_pnl_rate", 0)),
            }
            for pos in json.loads(previous["positions"])
        }

        for fill in fills:
            if fill.get("user_id") not in {None, agent_id}:
                continue
            stock_code = fill.get("stock_code", config.STOCK_CODE)
            direction = fill["direction"]
            quantity = int(fill["quantity"])
            price = float(fill["price"])
            pos = positions.setdefault(
                stock_code,
                {
                    "stock_code": stock_code,
                    "quantity": 0,
                    "avg_cost": 0.0,
                    "current_price": price,
                    "unrealized_pnl": 0.0,
                    "unrealized_pnl_rate": 0.0,
                },
            )
            if direction == "buy":
                total_cost = pos["avg_cost"] * pos["quantity"] + price * quantity
                new_qty = pos["quantity"] + quantity
                pos["quantity"] = new_qty
                pos["avg_cost"] = total_cost / new_qty if new_qty else 0.0
                cash -= price * quantity
            elif direction == "sell":
                sell_qty = min(quantity, pos["quantity"])
                realized_pnl += (price - pos["avg_cost"]) * sell_qty
                pos["quantity"] -= sell_qty
                cash += price * sell_qty
                if pos["quantity"] <= 0:
                    positions.pop(stock_code, None)
            else:
                raise ValueError(f"unknown fill direction: {direction}")

        prices = current_prices or {}
        normalized_positions = []
        stock_value = 0.0
        for stock_code, pos in sorted(positions.items()):
            current_price = float(prices.get(stock_code, pos["current_price"]))
            market_value = pos["quantity"] * current_price
            unrealized = (current_price - pos["avg_cost"]) * pos["quantity"]
            rate = unrealized / (pos["avg_cost"] * pos["quantity"]) if pos["avg_cost"] and pos["quantity"] else 0.0
            pos.update(
                {
                    "current_price": current_price,
                    "unrealized_pnl": unrealized,
                    "unrealized_pnl_rate": rate,
                }
            )
            normalized_positions.append(pos)
            stock_value += market_value

        total_value = cash + stock_value
        total_return_rate = (total_value - initial_value) / initial_value if initial_value else 0.0
        state = {
            "state_id": f"ps_{agent_id}_t{turn:03d}",
            "agent_id": agent_id,
            "turn": turn,
            "date": date,
            "cash": cash,
            "positions": normalized_positions,
            "total_value": total_value,
            "realized_pnl": realized_pnl,
            "total_return_rate": total_return_rate,
        }
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO portfolio_state (
                    state_id, agent_id, turn, date, cash, positions,
                    total_value, realized_pnl, total_return_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state["state_id"],
                    agent_id,
                    turn,
                    date,
                    cash,
                    json.dumps(normalized_positions, ensure_ascii=False),
                    total_value,
                    realized_pnl,
                    total_return_rate,
                ),
            )
            conn.commit()
        return state

    @staticmethod
    def _event_trade_lineage(
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        turn: int,
    ) -> dict[str, str]:
        lineage: dict[str, str] = {}
        decision = connection.execute(
            """
            SELECT analysis_id, decision_id, source_ltb_id, source_stb_id
            FROM simulation_decisions
            WHERE agent_id = ? AND turn = ?
            """,
            (agent_id, int(turn)),
        ).fetchone()
        if decision is not None:
            lineage.update(
                {
                    "analysis_id": str(decision["analysis_id"]),
                    "decision_id": str(decision["decision_id"]),
                    "source_ltb_id": str(decision["source_ltb_id"]),
                    "source_stb_id": str(decision["source_stb_id"]),
                }
            )
        fill = connection.execute(
            """
            SELECT fill_id, decision_id, source_ltb_id, source_stb_id
            FROM simulation_fills
            WHERE agent_id = ? AND turn = ?
            """,
            (agent_id, int(turn)),
        ).fetchone()
        if fill is not None:
            for column in (
                "fill_id",
                "decision_id",
                "source_ltb_id",
                "source_stb_id",
            ):
                value = str(fill[column])
                if column in lineage and lineage[column] != value:
                    raise ValueError(
                        f"simulation fill and decision disagree on {column}"
                    )
                lineage[column] = value
        post_fill_ltb = connection.execute(
            """
            SELECT ltb_id, source_decision_id, source_fill_id,
                   parent_ltb_id, source_stb_id
            FROM simulation_ltb_states
            WHERE agent_id = ? AND turn = ? AND turn > 0
            """,
            (agent_id, int(turn)),
        ).fetchone()
        if post_fill_ltb is not None:
            ltb_lineage = {
                "post_fill_ltb_id": str(post_fill_ltb["ltb_id"]),
                "decision_id": str(post_fill_ltb["source_decision_id"]),
                "source_ltb_id": str(post_fill_ltb["parent_ltb_id"]),
                "source_stb_id": str(post_fill_ltb["source_stb_id"]),
                "fill_id": str(post_fill_ltb["source_fill_id"]),
            }
            for column, value in ltb_lineage.items():
                if column in lineage and lineage[column] != value:
                    raise ValueError(
                        f"simulation LTB and prior lineage disagree on {column}"
                    )
                lineage[column] = value
        return lineage

    @classmethod
    def _resolved_trade_lineage(
        cls,
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        turn: int,
        explicit: Mapping[str, Any],
        existing: Mapping[str, Any] | None = None,
    ) -> dict[str, str | None]:
        resolved: dict[str, str | None] = {
            column: None for column in TRADE_LINEAGE_COLUMNS
        }
        sources: tuple[tuple[str, Mapping[str, Any]], ...] = (
            (
                "persisted trade_log",
                {} if existing is None else dict(existing),
            ),
            (
                "hierarchical event",
                cls._event_trade_lineage(
                    connection,
                    agent_id=agent_id,
                    turn=turn,
                ),
            ),
            ("caller", explicit),
        )
        for source, values in sources:
            for column in TRADE_LINEAGE_COLUMNS:
                raw = values.get(column)
                if raw is None or raw == "":
                    continue
                value = _required_text(raw, label=f"{source} {column}")
                prior = resolved[column]
                if prior is not None and prior != value:
                    raise ValueError(
                        f"trade lineage conflict for {column}: "
                        f"{prior!r} != {value!r}"
                    )
                resolved[column] = value
        return resolved

    @classmethod
    def _attach_trade_lineage(
        cls,
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        turn: int,
        lineage: Mapping[str, Any],
    ) -> None:
        rows = connection.execute(
            """
            SELECT log_id, decision_id, source_ltb_id, source_stb_id,
                   analysis_id, fill_id, post_fill_ltb_id
            FROM trade_log
            WHERE agent_id = ? AND turn = ?
            """,
            (agent_id, int(turn)),
        ).fetchall()
        for row in rows:
            resolved = cls._resolved_trade_lineage(
                connection,
                agent_id=agent_id,
                turn=turn,
                explicit=lineage,
                existing=row,
            )
            connection.execute(
                """
                UPDATE trade_log
                SET analysis_id = ?,
                    decision_id = ?,
                    source_ltb_id = ?,
                    source_stb_id = ?,
                    fill_id = ?,
                    post_fill_ltb_id = ?
                WHERE log_id = ?
                """,
                (
                    resolved["analysis_id"],
                    resolved["decision_id"],
                    resolved["source_ltb_id"],
                    resolved["source_stb_id"],
                    resolved["fill_id"],
                    resolved["post_fill_ltb_id"],
                    row["log_id"],
                ),
            )

    def append_trade_log(self, record: dict[str, Any]) -> None:
        required = {"agent_id", "turn", "date", "action", "stock_code", "quantity"}
        missing = required - record.keys()
        if missing:
            raise ValueError(f"trade log missing required keys: {sorted(missing)}")
        log_id = record.get("log_id") or f"tl_{record['agent_id']}_t{record['turn']:03d}"
        with connect(self.db_path) as conn:
            existing = conn.execute(
                """
                SELECT analysis_id, decision_id, source_ltb_id, source_stb_id,
                       fill_id, post_fill_ltb_id
                FROM trade_log
                WHERE log_id = ?
                """,
                (log_id,),
            ).fetchone()
            lineage = self._resolved_trade_lineage(
                conn,
                agent_id=str(record["agent_id"]),
                turn=int(record["turn"]),
                explicit=record,
                existing=existing,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO trade_log (
                    log_id, agent_id, turn, date, action, stock_code, quantity,
                    executed_price, trade_value, fee, action_reason, risk_control,
                    order_type, submitted_price, status, filled_quantity,
                    analysis_id, decision_id, source_ltb_id, source_stb_id,
                    fill_id, post_fill_ltb_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    record["agent_id"],
                    int(record["turn"]),
                    record["date"],
                    record["action"],
                    record["stock_code"],
                    int(record["quantity"]),
                    record.get("executed_price"),
                    record.get("trade_value"),
                    float(record.get("fee", 0)),
                    record.get("action_reason"),
                    record.get("risk_control"),
                    record.get("order_type"),
                    record.get("submitted_price"),
                    record.get("status", "pending"),
                    int(record.get("filled_quantity", 0)),
                    lineage["analysis_id"],
                    lineage["decision_id"],
                    lineage["source_ltb_id"],
                    lineage["source_stb_id"],
                    lineage["fill_id"],
                    lineage["post_fill_ltb_id"],
                ),
            )
            conn.commit()

    def save_system_message(
        self,
        agent_id: str,
        turn: int,
        date: str,
        *,
        message_type: str,
        message: str,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_system_messages (
                    agent_id, turn, date, message_type, message
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (agent_id, int(turn), date, message_type, message),
            )
            conn.commit()

    def update_trade_execution(
        self,
        agent_id: str,
        turn: int,
        *,
        filled_quantity: int,
        executed_price: float | None,
        fee: float = 0.0,
        decision_id: str | None = None,
        source_ltb_id: str | None = None,
        source_stb_id: str | None = None,
        fill_id: str | None = None,
        post_fill_ltb_id: str | None = None,
    ) -> None:
        log_id = f"tl_{agent_id}_t{turn:03d}"
        status = "filled" if filled_quantity > 0 else "unfilled"
        trade_value = None if executed_price is None else executed_price * filled_quantity
        with connect(self.db_path) as conn:
            existing = conn.execute(
                """
                SELECT decision_id, source_ltb_id, source_stb_id,
                       fill_id, post_fill_ltb_id
                FROM trade_log
                WHERE log_id = ?
                """,
                (log_id,),
            ).fetchone()
            lineage = self._resolved_trade_lineage(
                conn,
                agent_id=agent_id,
                turn=int(turn),
                explicit={
                    "decision_id": decision_id,
                    "source_ltb_id": source_ltb_id,
                    "source_stb_id": source_stb_id,
                    "fill_id": fill_id,
                    "post_fill_ltb_id": post_fill_ltb_id,
                },
                existing=existing,
            )
            conn.execute(
                """
                UPDATE trade_log
                SET status = ?,
                    filled_quantity = ?,
                    executed_price = ?,
                    trade_value = ?,
                    fee = ?,
                    decision_id = ?,
                    source_ltb_id = ?,
                    source_stb_id = ?,
                    fill_id = ?,
                    post_fill_ltb_id = ?
                WHERE log_id = ?
                """,
                (
                    status,
                    filled_quantity,
                    executed_price,
                    trade_value,
                    fee,
                    lineage["decision_id"],
                    lineage["source_ltb_id"],
                    lineage["source_stb_id"],
                    lineage["fill_id"],
                    lineage["post_fill_ltb_id"],
                    log_id,
                ),
            )
            conn.commit()

    def get_recent_order_history(
        self,
        agent_id: str,
        last_n: int = 5,
        *,
        current_date: str | None = None,
    ) -> list[dict[str, Any]]:
        # ``current_date`` remains in the public signature for callers that
        # already pass it, but raw StockData marks are deliberately excluded.
        # Price reflection must enter memory only through the horizon-gated
        # outcome ledger; joining a trade row to an eventual daily close here
        # would create an alternate, easy-to-misuse leakage path.
        _ = current_date
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    tl.date,
                    tl.turn,
                    tl.action,
                    tl.status,
                    tl.executed_price,
                    tl.filled_quantity
                FROM trade_log tl
                WHERE tl.agent_id = ?
                  AND tl.action IN ('buy', 'sell')
                ORDER BY tl.turn DESC
                LIMIT ?
                """,
                (agent_id, int(last_n)),
            ).fetchall()
        history = []
        for row in rows:
            filled_qty = int(row["filled_quantity"] or 0)
            filled = str(row["status"]) == "filled" and filled_qty > 0
            history.append(
                {
                    "date": row["date"],
                    "turn": row["turn"],
                    "action": row["action"],
                    "filled": filled,
                    "status": row["status"],
                    "executed_price": row["executed_price"] if filled else None,
                    "filled_quantity": filled_qty,
                }
            )
        return history

    def get_last_action_reason(self, agent_id: str) -> str | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT action, quantity, status, filled_quantity,
                       executed_price, action_reason
                FROM trade_log
                WHERE agent_id = ? AND action IN ('buy', 'sell')
                  AND action_reason IS NOT NULL
                ORDER BY turn DESC
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        if row["status"] == "filled":
            result = f"체결 {int(row['filled_quantity']):,}주"
            if row["executed_price"] is not None:
                result += f"@{float(row['executed_price']):,.0f}원"
        elif row["status"] == "unfilled":
            result = "미체결"
        else:
            result = "체결 결과 대기"
        return (
            f"이전 주문: {row['action']} {int(row['quantity']):,}주. "
            f"체결 결과: {result}. 사유: {row['action_reason']}"
        )

    def get_recent_system_message(self, agent_id: str, *, current_turn: int | None = None) -> str | None:
        query = """
            SELECT turn, date, message_type, message
            FROM agent_system_messages
            WHERE agent_id = ?
        """
        params: list[Any] = [agent_id]
        if current_turn is not None:
            query += " AND turn < ?"
            params.append(int(current_turn))
        query += " ORDER BY turn DESC, message_id DESC LIMIT 1"
        with connect(self.db_path) as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        return f"시스템 알림: {row['date']} turn {row['turn']} {row['message_type']}. {row['message']}"

    def get_portfolio_summary(self, agent_id: str, turn: int) -> str:
        row = self._latest_portfolio(agent_id, before_or_at_turn=turn)
        if row is None:
            raise ValueError(f"portfolio not found for {agent_id} at turn {turn}")
        positions = json.loads(row["positions"])
        cash = float(row["cash"])
        total_value = float(row["total_value"])
        total_return_rate = float(row["total_return_rate"])
        if not positions:
            return (
                f"보유 현금 {cash:,.0f}원, 현재 보유 종목 없음. "
                f"총 자산 {total_value:,.0f}원, 누적 수익률 {total_return_rate * 100:.2f}%."
            )
        parts = []
        for pos in positions:
            parts.append(
                f"{pos['stock_code']} {int(pos['quantity']):,}주 보유, "
                f"평균 매수 단가 {float(pos['avg_cost']):,.0f}원, "
                f"현재가 {float(pos['current_price']):,.0f}원, "
                f"미실현손익 {float(pos['unrealized_pnl']):,.0f}원"
            )
        return (
            f"보유 현금 {cash:,.0f}원, " + "; ".join(parts) + ". "
            f"총 자산 {total_value:,.0f}원, 누적 수익률 {total_return_rate * 100:.2f}%."
        )

    def _require_event_schedule(self) -> FrozenEventSchedule:
        if self.event_schedule is None:
            raise ValueError(
                "a FrozenEventSchedule is required for outcome reflection"
            )
        return self.event_schedule

    def _assert_outcome_event_order(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
    ) -> None:
        """Reject use of a sealed future price before its runtime event."""

        schedule = self._require_event_schedule()
        event = schedule.event(event_id)
        row = connection.execute(
            "SELECT COALESCE(MAX(turn), 0) AS max_turn FROM simulation_fills"
        ).fetchone()
        max_fill_turn = 0 if row is None else int(row["max_turn"])
        event_turn = int(event["turn"])
        if event_turn not in {max_fill_turn, max_fill_turn + 1}:
            raise ValueError(
                "outcome event is not the current or immediately next "
                f"runtime event: event_turn={event_turn}, "
                f"max_fill_turn={max_fill_turn}"
            )

    def _scheduled_event_for_fill(
        self,
        fill: Mapping[str, Any],
    ) -> dict[str, Any]:
        schedule = self._require_event_schedule()
        try:
            event = schedule.event_for_identity(
                turn=int(fill["turn"]),
                event_date=str(fill["date"]),
                subturn=str(fill["subturn"]),
            )
        except OutcomeScheduleError as exc:
            raise ValueError(
                f"fill {fill['fill_id']} differs from the frozen schedule"
            ) from exc
        if str(fill["stock_code"]) != schedule.stock_code:
            raise ValueError(
                f"fill {fill['fill_id']} has a stock outside the price registry"
            )
        if (
            abs(
                float(fill["executed_price"])
                - float(event["execution_price"])
            )
            > 1e-6
        ):
            raise ValueError(
                f"fill {fill['fill_id']} price differs from its frozen event"
            )
        return event

    def _record_terminal_outcome_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        fill: Mapping[str, Any],
        horizon: str,
        due_event_id: str | None,
        mark_price: float | None,
        status: str,
    ) -> str:
        schedule = self._require_event_schedule()
        if horizon not in OUTCOME_HORIZONS:
            raise ValueError(f"unknown outcome horizon: {horizon}")
        fill_event = self._scheduled_event_for_fill(fill)
        expected_due = schedule.due_event_id(
            fill_event_id=str(fill_event["event_id"]),
            horizon=horizon,
        )
        if due_event_id != expected_due:
            raise ValueError(
                "outcome due event differs from the frozen horizon schedule"
            )
        if status == "matured":
            if due_event_id is None or mark_price is None:
                raise ValueError("matured outcome requires its due event and mark")
            due_event = schedule.event(due_event_id)
            normalized_mark = _positive_number(
                mark_price,
                label="outcome mark_price",
            )
            if (
                abs(
                    normalized_mark
                    - float(due_event["execution_price"])
                )
                > 1e-6
            ):
                raise ValueError(
                    "outcome mark price differs from the frozen due-event price"
                )
            available_from_event_id = due_event_id
            observed_event_id = due_event_id
        elif status == "right_censored":
            if due_event_id is not None or mark_price is not None:
                raise ValueError(
                    "right-censored outcome may not contain a future event or mark"
                )
            normalized_mark = None
            available_from_event_id = None
            observed_event_id = None
        else:
            raise ValueError(
                "outcome status must be matured or right_censored"
            )
        fill_id = str(fill["fill_id"])
        outcome_id = f"outcome:{fill_id}:{horizon}"
        scientific = _scientific_sha256(
            {
                "artifact_type": "simulation_trade_outcome",
                "fill_id": fill_id,
                "fill_sha256": str(fill["scientific_sha256"]),
                "horizon": horizon,
                "due_event_id": due_event_id,
                "available_from_event_id": available_from_event_id,
                "observed_event_id": observed_event_id,
                "mark_price": normalized_mark,
                "status": status,
                "calendar_sha256": schedule.calendar_sha256,
                "prices_sha256": schedule.prices_sha256,
            },
            label="simulation trade outcome",
        )
        values = {
            "outcome_id": outcome_id,
            "fill_id": fill_id,
            "horizon": horizon,
            "due_event_id": due_event_id,
            "available_from_event_id": available_from_event_id,
            "observed_event_id": observed_event_id,
            "mark_price": normalized_mark,
            "status": status,
            "scientific_sha256": scientific,
        }
        _insert_or_verify(
            connection,
            table="simulation_trade_outcomes",
            key_column="outcome_id",
            key_value=outcome_id,
            values=values,
            compare_columns=("scientific_sha256",),
        )
        return outcome_id

    def _due_outcome_rows_for_agent_event(
        self,
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        event_id: str,
        require_complete: bool,
    ) -> list[sqlite3.Row]:
        schedule = self._require_event_schedule()
        current = schedule.event(event_id)
        fills = connection.execute(
            """
            SELECT *
            FROM simulation_fills
            WHERE agent_id = ? AND turn < ?
            ORDER BY turn
            """,
            (agent_id, int(current["turn"])),
        ).fetchall()
        expected: set[tuple[str, str]] = set()
        for fill in fills:
            fill_event = self._scheduled_event_for_fill(fill)
            for horizon in OUTCOME_HORIZONS:
                if (
                    schedule.due_event_id(
                        fill_event_id=str(fill_event["event_id"]),
                        horizon=horizon,
                    )
                    == event_id
                ):
                    expected.add((str(fill["fill_id"]), horizon))
        rows = connection.execute(
            """
            SELECT outcome.*, fill.agent_id, fill.action AS entry_action,
                   fill.executed_price AS entry_price,
                   fill.filled_quantity AS entry_quantity,
                   fill.pre_portfolio_json AS entry_pre_portfolio_json,
                   fill.post_portfolio_json AS entry_post_portfolio_json,
                   fill.source_ltb_id, fill.source_stb_id
            FROM simulation_trade_outcomes AS outcome
            JOIN simulation_fills AS fill ON fill.fill_id = outcome.fill_id
            WHERE fill.agent_id = ?
              AND outcome.available_from_event_id = ?
            ORDER BY fill.turn,
                     CASE outcome.horizon
                         WHEN 'next_turn' THEN 1
                         WHEN 'h1' THEN 2
                         WHEN 'h5' THEN 3
                     END
            """,
            (agent_id, event_id),
        ).fetchall()
        actual: set[tuple[str, str]] = set()
        due_price = float(current["execution_price"])
        for row in rows:
            key = (str(row["fill_id"]), str(row["horizon"]))
            if (
                str(row["status"]) != "matured"
                or str(row["due_event_id"]) != event_id
                or str(row["observed_event_id"]) != event_id
                or abs(float(row["mark_price"]) - due_price) > 1e-6
                or str(row["outcome_id"])
                != f"outcome:{row['fill_id']}:{row['horizon']}"
            ):
                raise ValueError(
                    "stored outcome differs from its frozen due-event fact"
                )
            actual.add(key)
        if require_complete and actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                "mature_outcomes_for_event must record every and only due "
                f"outcome before STB/LTB use: missing={missing}, extra={extra}"
            )
        return list(rows)

    def _validate_ltb_outcome_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        event_id: str,
        stb: Mapping[str, Any],
        integration_evidence: Mapping[str, Any],
        ignored_outcome_ids: Iterable[str] = (),
    ) -> tuple[str, ...]:
        due_rows = self._due_outcome_rows_for_agent_event(
            connection,
            agent_id=agent_id,
            event_id=event_id,
            require_complete=True,
        )
        due_ids = {str(row["outcome_id"]) for row in due_rows}
        supplied_outcomes = self._validate_ltb_integration_evidence(
            stb=stb,
            integration_evidence=integration_evidence,
            due_rows=due_rows,
        )
        ignored = {str(item) for item in ignored_outcome_ids}
        if supplied_outcomes & ignored:
            raise ValueError(
                "ignored outcome IDs must not also appear in LTB evidence"
            )
        accounted_outcomes = supplied_outcomes | ignored
        if accounted_outcomes != due_ids:
            raise ValueError(
                "post-fill LTB must consume every and only matured outcome "
                f"due at this event: missing={sorted(due_ids - accounted_outcomes)}, "
                f"extra={sorted(accounted_outcomes - due_ids)}"
            )
        return tuple(sorted(due_ids))

    def _validate_ltb_integration_evidence(
        self,
        *,
        stb: Mapping[str, Any],
        integration_evidence: Mapping[str, Any],
        due_rows: list[sqlite3.Row] | tuple[sqlite3.Row, ...] = (),
    ) -> set[str]:
        """Keep STB polarity exact; let the agent judge its own outcomes.

        The current STB remains the only ordinary evidence source for LTB_t.
        Citing an STB evidence ID is optional, but moving a cited ID to a
        different dimension or from support to contradict (or vice versa) is
        not.  Matured outcome IDs are an explicit exception: they belong only
        to dim_6, and the agent decides their relation.  dim_6 is the agent's
        cumulative self-assessment, so how it reads its own results is an
        observation target rather than something to overwrite.
        """

        stored_stb_evidence = _dimension_evidence(
            stb["dimension_evidence"],
            label="stored STB dimension_evidence",
        )
        due_by_id = {
            str(row["outcome_id"]): row
            for row in due_rows
        }

        supplied_outcomes: set[str] = set()
        for dimension in HIERARCHICAL_DIMENSION_KEYS:
            for relation in ("support", "contradict"):
                for evidence_id in integration_evidence[dimension][relation]:
                    due_row = due_by_id.get(evidence_id)
                    if due_row is not None:
                        if dimension != "dim_6":
                            raise ValueError(
                                "matured price outcomes may affect only LTB dim_6"
                            )
                        # 생성 경계(llm/belief.py)와 같은 정책이다: 가격 결과의
                        # support/contradict는 에이전트의 자기평가이므로
                        # markout 부호로 강제하지 않는다. 객관적 부호는
                        # simulation_trade_outcomes에 그대로 남아 사후 대조가
                        # 가능하다. dim_6 전용이라는 제약만 유지한다.
                        supplied_outcomes.add(evidence_id)
                        continue
                    if evidence_id in stored_stb_evidence[dimension][relation]:
                        continue
                    opposite = (
                        "contradict" if relation == "support" else "support"
                    )
                    if evidence_id in stored_stb_evidence[dimension][opposite]:
                        raise ValueError(
                            "LTB integration evidence must preserve the current "
                            "STB support/contradict relation: "
                            f"{dimension}.{opposite} -> {dimension}.{relation} "
                            f"for {evidence_id}"
                        )
                    else:
                        raise ValueError(
                            "LTB integration evidence must come from the "
                            f"current STB at the same dimension and relation "
                            f"for {dimension}, or from a due "
                            f"dim_6 outcome: {evidence_id}"
                        )
        return supplied_outcomes

    def _assert_outcome_ledger_finalized(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        schedule = self._require_event_schedule()
        fills = connection.execute(
            "SELECT * FROM simulation_fills ORDER BY turn, agent_id"
        ).fetchall()
        outcomes = connection.execute(
            "SELECT * FROM simulation_trade_outcomes"
        ).fetchall()
        outcome_by_key = {
            (str(row["fill_id"]), str(row["horizon"])): row
            for row in outcomes
        }
        if len(outcome_by_key) != len(outcomes):
            raise ValueError("outcome ledger contains duplicate fill/horizon keys")
        consumptions = connection.execute(
            """
            SELECT consumption.*, ltb.agent_id AS ltb_agent_id,
                   ltb.date AS ltb_date, ltb.subturn AS ltb_subturn
            FROM simulation_outcome_consumptions AS consumption
            JOIN simulation_ltb_states AS ltb ON ltb.ltb_id = consumption.ltb_id
            """
        ).fetchall()
        consumption_by_outcome = {
            str(row["outcome_id"]): row
            for row in consumptions
        }
        if len(consumption_by_outcome) != len(consumptions):
            raise ValueError("outcome ledger contains duplicate consumption edges")

        expected_outcome_ids: set[str] = set()
        expected_consumed_ids: set[str] = set()
        for fill in fills:
            fill_event = self._scheduled_event_for_fill(fill)
            fill_id = str(fill["fill_id"])
            for horizon in OUTCOME_HORIZONS:
                key = (fill_id, horizon)
                outcome = outcome_by_key.get(key)
                if outcome is None:
                    raise ValueError(
                        f"final outcome is missing for {fill_id}/{horizon}"
                    )
                expected_id = f"outcome:{fill_id}:{horizon}"
                expected_outcome_ids.add(expected_id)
                if str(outcome["outcome_id"]) != expected_id:
                    raise ValueError("outcome ID differs from its fill/horizon")
                due_event_id = schedule.due_event_id(
                    fill_event_id=str(fill_event["event_id"]),
                    horizon=horizon,
                )
                consumption = consumption_by_outcome.get(expected_id)
                if due_event_id is None:
                    if (
                        str(outcome["status"]) != "right_censored"
                        or outcome["due_event_id"] is not None
                        or outcome["available_from_event_id"] is not None
                        or outcome["observed_event_id"] is not None
                        or outcome["mark_price"] is not None
                        or consumption is not None
                    ):
                        raise ValueError(
                            "unavailable horizon must be right-censored "
                            "without a mark or consumption"
                        )
                    continue
                expected_consumed_ids.add(expected_id)
                due = schedule.event(due_event_id)
                if (
                    str(outcome["status"]) != "matured"
                    or str(outcome["due_event_id"]) != due_event_id
                    or str(outcome["available_from_event_id"]) != due_event_id
                    or str(outcome["observed_event_id"]) != due_event_id
                    or abs(
                        float(outcome["mark_price"])
                        - float(due["execution_price"])
                    )
                    > 1e-6
                ):
                    raise ValueError(
                        "scheduled horizon is not a valid matured outcome"
                    )
                if (
                    consumption is None
                    or str(consumption["fill_id"]) != fill_id
                    or str(consumption["horizon"]) != horizon
                    or str(consumption["consumed_at_event_id"]) != due_event_id
                    or str(consumption["ltb_agent_id"])
                    != str(fill["agent_id"])
                    or (
                        f"{consumption['ltb_date']}/"
                        f"{str(consumption['ltb_subturn']).upper()}"
                    )
                    != due_event_id
                ):
                    raise ValueError(
                        "matured outcome lacks its owner/due-event LTB "
                        "consumption edge"
                    )
        if set(row["outcome_id"] for row in outcomes) != expected_outcome_ids:
            raise ValueError("outcome ledger contains an orphan outcome")
        if set(consumption_by_outcome) != expected_consumed_ids:
            raise ValueError("outcome ledger contains an orphan consumption")

    @staticmethod
    def _consume_outcome_in_transaction(
        connection: sqlite3.Connection,
        *,
        outcome_id: str,
        ltb_id: str,
        agent_id: str,
        event_id: str,
    ) -> str:
        row = connection.execute(
            """
            SELECT outcome.outcome_id, outcome.fill_id, outcome.horizon,
                   outcome.status, outcome.available_from_event_id,
                   fill.agent_id
            FROM simulation_trade_outcomes AS outcome
            JOIN simulation_fills AS fill ON fill.fill_id = outcome.fill_id
            WHERE outcome.outcome_id = ?
            """,
            (outcome_id,),
        ).fetchone()
        if row is None or str(row["status"]) != "matured":
            raise ValueError("only a persisted matured outcome may be consumed")
        if str(row["agent_id"]) != agent_id:
            raise ValueError("outcome may be consumed only by its fill owner")
        if str(row["available_from_event_id"]) != event_id:
            raise ValueError("outcome may be consumed only at its due event")
        ltb = connection.execute(
            """
            SELECT agent_id, date, subturn
            FROM simulation_ltb_states
            WHERE ltb_id = ?
            """,
            (ltb_id,),
        ).fetchone()
        if ltb is None or str(ltb["agent_id"]) != agent_id:
            raise ValueError("outcome consumption requires the owner's LTB")
        if f"{ltb['date']}/{str(ltb['subturn']).upper()}" != event_id:
            raise ValueError("outcome consumption LTB is not the due event LTB")
        fill_id = str(row["fill_id"])
        horizon = str(row["horizon"])
        consumption_id = f"outcome-consumption:{fill_id}:{horizon}"
        values = {
            "consumption_id": consumption_id,
            "outcome_id": outcome_id,
            "fill_id": fill_id,
            "horizon": horizon,
            "ltb_id": ltb_id,
            "consumed_at_event_id": event_id,
        }
        _insert_or_verify(
            connection,
            table="simulation_outcome_consumptions",
            key_column="consumption_id",
            key_value=consumption_id,
            values=values,
            compare_columns=(
                "outcome_id",
                "ltb_id",
                "consumed_at_event_id",
            ),
        )
        return consumption_id

    def _hierarchical_row(
        self,
        table: str,
        key_column: str,
        key_value: str,
    ) -> sqlite3.Row:
        allowed = {
            ("simulation_stb_states", "stb_id"),
            ("simulation_ltb_states", "ltb_id"),
            ("simulation_analyses", "analysis_id"),
            ("simulation_decisions", "decision_id"),
            ("simulation_fills", "fill_id"),
        }
        if (table, key_column) not in allowed:
            raise ValueError("unsupported hierarchical memory lookup")
        with connect(self.db_path, read_only=True) as conn:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE {key_column} = ?",
                (key_value,),
            ).fetchone()
        if row is None:
            raise ValueError(f"{table} row not found: {key_value}")
        return row

    @staticmethod
    def _ltb_state(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ltb_id": str(row["ltb_id"]),
            "agent_id": str(row["agent_id"]),
            "turn": int(row["turn"]),
            "visible_from_turn": int(row["visible_from_turn"]),
            "date": str(row["date"]),
            "subturn": str(row["subturn"]),
            "parent_ltb_id": (
                None
                if row["parent_ltb_id"] is None
                else str(row["parent_ltb_id"])
            ),
            "source_stb_id": (
                None
                if row["source_stb_id"] is None
                else str(row["source_stb_id"])
            ),
            "source_decision_id": (
                None
                if row["source_decision_id"] is None
                else str(row["source_decision_id"])
            ),
            "source_fill_id": (
                None
                if row["source_fill_id"] is None
                else str(row["source_fill_id"])
            ),
            "dimensions": _row_dimensions(row),
            "integration_evidence": json.loads(
                str(row["integration_evidence_json"])
            ),
            "scientific_sha256": str(row["scientific_sha256"]),
        }

    @staticmethod
    def _view_change_json(view_change: Any) -> str:
        if isinstance(view_change, str):
            normalized: Any = _required_text(
                view_change,
                label="LTB view_change",
            )
        elif isinstance(view_change, Mapping):
            if not view_change:
                raise ValueError("LTB view_change object must not be empty")
            normalized = dict(view_change)
        elif isinstance(view_change, list):
            if not view_change:
                raise ValueError("LTB view_change list must not be empty")
            normalized = list(view_change)
        else:
            raise ValueError(
                "LTB view_change must be a non-empty string, object, or list"
            )
        return _canonical_json(normalized, label="LTB view_change")

    def _latest_portfolio(self, agent_id: str, before_or_at_turn: int) -> sqlite3.Row | None:
        with connect(self.db_path) as conn:
            return conn.execute(
                """
                SELECT *
                FROM portfolio_state
                WHERE agent_id = ? AND turn <= ?
                ORDER BY turn DESC
                LIMIT 1
                """,
                (agent_id, before_or_at_turn),
            ).fetchone()

    def _initial_value(self, agent_id: str) -> float:
        row = self._latest_portfolio(agent_id, before_or_at_turn=0)
        return 0.0 if row is None else float(row["total_value"])

    def _ensure_trade_log_columns(self) -> None:
        expected = {
            "order_type": "TEXT",
            "submitted_price": "REAL",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "filled_quantity": "INTEGER NOT NULL DEFAULT 0",
            "analysis_id": "TEXT REFERENCES simulation_analyses(analysis_id)",
            "decision_id": "TEXT REFERENCES simulation_decisions(decision_id)",
            "source_ltb_id": "TEXT REFERENCES simulation_ltb_states(ltb_id)",
            "source_stb_id": "TEXT REFERENCES simulation_stb_states(stb_id)",
            "fill_id": "TEXT REFERENCES simulation_fills(fill_id)",
            "post_fill_ltb_id": "TEXT REFERENCES simulation_ltb_states(ltb_id)",
        }
        with connect(self.db_path) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(trade_log)").fetchall()}
            for name, ddl in expected.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE trade_log ADD COLUMN {name} {ddl}")
            conn.commit()


def load_agents_from_sys100(
    sys100_db: Path | str = config.SYS_100_DB,
    *,
    instrument_name: str = "삼성전자",
) -> list[dict[str, Any]]:
    with connect(sys100_db, read_only=True) as conn:
        agents = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM agents ORDER BY agent_id"
            ).fetchall()
        ]
    for agent in agents:
        source_prompt = str(agent.get("persona_prompt") or "")
        rendered_prompt = generate_persona_prompt(
            agent,
            instrument_name=instrument_name,
        )
        agent["source_persona_prompt_sha256"] = hashlib.sha256(
            source_prompt.encode("utf-8")
        ).hexdigest()
        agent["persona_prompt_sha256"] = hashlib.sha256(
            rendered_prompt.encode("utf-8")
        ).hexdigest()
        agent["persona_prompt_regenerated"] = source_prompt != rendered_prompt
        # The structured row, especially news_depth, is the single source of
        # truth. A stale prompt blob must never reach an experiment request.
        agent["persona_prompt"] = rendered_prompt
    return agents
