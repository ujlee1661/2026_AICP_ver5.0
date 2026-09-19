from __future__ import annotations

import json
import math
import sqlite3
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from twinmarket_kr.advisor.artifact import canonical_sha256


def _json(value: Any) -> Any:
    return json.loads(str(value or "null"))


def _portfolio_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    positions = _json(row["positions"]) or []
    return {
        "turn": int(row["turn"]),
        "date": str(row["date"]),
        "cash": float(row["cash"]),
        "positions": positions,
        "total_value": float(row["total_value"]),
        "realized_pnl": float(row["realized_pnl"]),
        "total_return_rate": float(row["total_return_rate"]),
    }


def build_advisor_case(
    db_path: Path | str,
    *,
    agent: Mapping[str, Any],
    cutoff_turn: int = 90,
) -> dict[str, Any]:
    agent_id = str(agent["agent_id"])
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        portfolios = connection.execute(
            """
            SELECT turn, date, cash, positions, total_value, realized_pnl, total_return_rate
            FROM portfolio_state WHERE agent_id=? AND turn BETWEEN 0 AND ? ORDER BY turn
            """,
            (agent_id, cutoff_turn),
        ).fetchall()
        fills = connection.execute(
            """
            SELECT f.fill_id, f.turn, f.date, f.subturn, f.action,
                   f.filled_quantity, f.executed_price, f.pre_portfolio_json,
                   f.post_portfolio_json, d.decision_json
            FROM simulation_fills AS f
            JOIN simulation_decisions AS d ON d.decision_id=f.decision_id
            WHERE f.agent_id=? AND f.turn BETWEEN 1 AND ? ORDER BY f.turn
            """,
            (agent_id, cutoff_turn),
        ).fetchall()
        ltb = connection.execute(
            """
            SELECT ltb_id, turn, date, dim_1, dim_2, dim_3, dim_4, dim_5, dim_6
            FROM simulation_ltb_states WHERE agent_id=? AND turn<=?
            ORDER BY turn DESC LIMIT 1
            """,
            (agent_id, cutoff_turn),
        ).fetchone()
    if len(portfolios) != cutoff_turn + 1 or ltb is None or int(ltb["turn"]) != cutoff_turn:
        raise ValueError(f"advisor case boundary is incomplete for {agent_id}")

    portfolio_rows = [_portfolio_projection(row) for row in portfolios]
    initial_value = portfolio_rows[0]["total_value"]
    final = portfolio_rows[-1]
    total_values = [row["total_value"] for row in portfolio_rows]
    returns = [
        total_values[index] / total_values[index - 1] - 1.0
        for index in range(1, len(total_values))
        if total_values[index - 1]
    ]
    peak = total_values[0]
    peak_index = 0
    mdd = 0.0
    mdd_peak_index = 0
    mdd_trough_index = 0
    for index, value in enumerate(total_values):
        if value > peak:
            peak = value
            peak_index = index
        if peak:
            drawdown = value / peak - 1.0
            if drawdown < mdd:
                mdd = drawdown
                mdd_peak_index = peak_index
                mdd_trough_index = index
    recovery_index = next(
        (
            index
            for index in range(mdd_trough_index + 1, len(total_values))
            if total_values[index] >= total_values[mdd_peak_index]
        ),
        None,
    )

    episodes: list[dict[str, Any]] = []
    ratios: list[float] = []
    actions: list[str] = []
    for row in fills:
        pre = _json(row["pre_portfolio_json"]) or {}
        post = _json(row["post_portfolio_json"]) or {}
        decision = _json(row["decision_json"]) or {}
        trade_value = float(row["filled_quantity"]) * float(row["executed_price"])
        pre_total = float(pre.get("total_value") or 0.0)
        ratio = trade_value / pre_total if pre_total else None
        if ratio is not None:
            ratios.append(ratio)
        actions.append(str(row["action"]))
        episodes.append(
            {
                "fill_id": str(row["fill_id"]),
                "turn": int(row["turn"]),
                "event_id": f"{row['date']}/{str(row['subturn']).upper()}",
                "action": str(row["action"]),
                "filled_quantity": int(row["filled_quantity"]),
                "executed_price": float(row["executed_price"]),
                "filled_trade_value": trade_value,
                "pre_total_value": pre_total,
                "trade_to_assets": ratio,
                "reason": str(decision.get("reason") or ""),
                "risk_control": str(decision.get("risk_control") or ""),
                "pre_portfolio": pre,
                "post_portfolio": post,
            }
        )
    direction_switches = sum(a != b for a, b in zip(actions, actions[1:]))
    average_total = statistics.fmean(total_values) if total_values else 0.0
    turnover = sum(row["filled_trade_value"] for row in episodes) / average_total if average_total else None

    cash_ratios: list[float] = []
    concentrations: list[float] = []
    for row in portfolio_rows:
        total = row["total_value"]
        if not total:
            continue
        cash_ratios.append(row["cash"] / total)
        stock_value = sum(
            float(position.get("quantity") or 0) * float(position.get("current_price") or 0)
            for position in row["positions"]
        )
        concentrations.append(stock_value / total)
    unrealized = 0.0
    for position in final["positions"]:
        quantity = int(position.get("quantity") or 0)
        recalculated = (
            float(position.get("current_price") or 0.0)
            - float(position.get("avg_cost") or 0.0)
        ) * quantity
        stored = float(position.get("unrealized_pnl") or 0.0)
        if not math.isclose(recalculated, stored, rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError(
                f"stored unrealized PnL differs from avg_cost ledger for {agent_id}"
            )
        unrealized += stored

    reason_by_fill: dict[str, set[str]] = {}
    for row in sorted(episodes, key=lambda item: (-item["filled_trade_value"], item["turn"]))[:5]:
        reason_by_fill.setdefault(row["fill_id"], set()).add("largest_trade_value")
    for previous, current in zip(episodes, episodes[1:]):
        if previous["action"] != current["action"]:
            reason_by_fill.setdefault(current["fill_id"], set()).add("direction_switch")
            if sum("direction_switch" in values for values in reason_by_fill.values()) >= 5:
                break
    for row in episodes[-10:]:
        reason_by_fill.setdefault(row["fill_id"], set()).add("recent_fill")
    representative = []
    for row in episodes:
        reasons = reason_by_fill.get(row["fill_id"])
        if reasons:
            representative.append({**row, "selection_reasons": sorted(reasons)})

    persona_fields = {
        key: agent.get(key)
        for key in (
            "user_type", "bh_disposition_effect_category",
            "bh_lottery_preference_category", "bh_total_return_category",
            "bh_underdiversification_category", "strategy",
            "momentum_contrarian", "ini_cash", "news_depth",
        )
    }
    case = {
        "agent_id": agent_id,
        "cutoff_event_id": "2026-05-04/PM",
        "persona": {"persona_prompt": str(agent["persona_prompt"]), "structured": persona_fields},
        "latest_ltb": {key: str(ltb[key]) for key in ("ltb_id", "dim_1", "dim_2", "dim_3", "dim_4", "dim_5", "dim_6")},
        "behavior_summary": {
            "filled_trade_count": len(episodes),
            "buy_count": actions.count("buy"),
            "sell_count": actions.count("sell"),
            "turnover": turnover,
            "average_trade_to_assets": statistics.fmean(ratios) if ratios else None,
            "maximum_trade_to_assets": max(ratios) if ratios else None,
            "direction_switch_count": direction_switches,
            "average_cash_ratio": statistics.fmean(cash_ratios) if cash_ratios else None,
            "ending_cash_ratio": cash_ratios[-1] if cash_ratios else None,
            "average_concentration": statistics.fmean(concentrations) if concentrations else None,
            "maximum_concentration": max(concentrations) if concentrations else None,
        },
        "performance_summary": {
            "initial_total_value": initial_value,
            "ending_total_value": final["total_value"],
            "cumulative_return": (final["total_value"] - initial_value) / initial_value,
            "realized_pnl": final["realized_pnl"],
            "unrealized_pnl": unrealized,
            "maximum_drawdown": mdd,
            "maximum_drawdown_episode": {
                "peak": portfolio_rows[mdd_peak_index],
                "trough": portfolio_rows[mdd_trough_index],
                "recovery": (
                    portfolio_rows[recovery_index]
                    if recovery_index is not None
                    else None
                ),
            },
            "event_return_volatility": statistics.pstdev(returns) if len(returns) > 1 else 0.0,
        },
        "ending_portfolio": final,
        "representative_episodes": representative,
    }
    case["source_state_sha256"] = canonical_sha256(case)
    return case


def build_advisor_cases(
    db_path: Path | str,
    *,
    agents: Sequence[Mapping[str, Any]],
    selected_agent_ids: Sequence[str],
) -> list[dict[str, Any]]:
    by_id = {str(agent["agent_id"]): agent for agent in agents}
    return [
        build_advisor_case(db_path, agent=by_id[agent_id])
        for agent_id in selected_agent_ids
    ]
