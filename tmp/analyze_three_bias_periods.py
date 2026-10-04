"""Read-only three-bias comparison for the advisor parent and continuations."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT / "outputs/experiments"
REPORT = ROOT / "outputs/advisor/advisor_comm_on_20260919/final_three_bias_100_agents_report.md"
OUT = EXPERIMENTS / "derived/advisor_three_bias_20260710"
RUNS = {
    "parent": "ADVISOR_MAIN_FROM_SCRATCH_PARENT_20260923",
    "advisor_off": "ADVISOR_MAIN_CONT_OFF_20260925",
    "general": "ADVISOR_GENERAL_20261001_TO_20260710",
    "personalized": "ADVISOR_PERSONAL_20261001_TO_20260710",
}
LOSS_EPSILON = 0.001


def db_rows(run: str):
    path = EXPERIMENTS / RUNS[run] / ".runtime/committed.db"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        states = {}
        for agent, turn, positions, cash, total in connection.execute(
            "SELECT agent_id, turn, positions, cash, total_value FROM portfolio_state"
        ):
            positions = json.loads(positions)
            position = next((p for p in positions if p["stock_code"] == "005930"), None)
            states[(agent, turn)] = {
                "quantity": int(position["quantity"]) if position else 0,
                "avg_cost": float(position["avg_cost"]) if position else 0.0,
                "cash": float(cash),
                "total": float(total),
            }
        fills = {}
        for agent, turn, date, action, quantity, price in connection.execute(
            "SELECT agent_id, turn, date, action, filled_quantity, executed_price "
            "FROM simulation_fills WHERE stock_code='005930'"
        ):
            fills[(agent, turn)] = {
                "date": date,
                "action": action,
                "quantity": int(quantity),
                "price": float(price),
            }
        return states, fills
    finally:
        connection.close()


def report_labels():
    labels = {}
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        match = re.match(
            r"^\| (A\d{3}) \| ([MDS+]+) \| (\d+)건·([^|]+) \|", line
        )
        if match:
            cells = [cell.strip() for cell in line.split("|")]
            high_risk = re.search(r"\((\d+)건\)", match.group(4))
            s_count = re.match(r"(\d+)건", cells[5])
            labels[match.group(1)] = {
                "biases": set(match.group(2).split("+")),
                "m_count": int(match.group(3)),
                "m_severity": match.group(4).strip(),
                "high_risk_m_count": int(high_risk.group(1)) if high_risk else 0,
                "s_count": int(s_count.group(1)) if s_count else 0,
            }
    assert len(labels) == 100
    return labels


def analyze(run: str, start_turn: int, end_turn: int):
    states, fills = db_rows(run)
    agents = sorted({agent for agent, _ in fills})
    assert len(agents) == 100
    assert all((agent, turn) in fills and (agent, turn - 1) in states and (agent, turn) in states
               for agent in agents for turn in range(start_turn, end_turn + 1))
    assert len({fills[(agent, turn)]["date"] for agent in agents for turn in range(start_turn, end_turn + 1)}) == (end_turn - start_turn + 1) // 2

    prices = {}
    for turn in range(1, end_turn + 1):
        values = {fills[(agent, turn)]["price"] for agent in agents}
        assert len(values) == 1, (run, turn, values)
        prices[turn] = values.pop()

    by_agent = {agent: Counter() for agent in agents}
    events = []
    for agent in agents:
        for turn in range(start_turn, end_turn + 1):
            fill = fills[(agent, turn)]
            pre = states[(agent, turn - 1)]
            post = states[(agent, turn)]
            price = fill["price"]
            both_possible = pre["quantity"] > 0 and pre["cash"] >= price
            if not both_possible:
                continue

            c = by_agent[agent]
            c["eligible_events"] += 1
            if fill["action"] == "buy":
                c["eligible_buys"] += 1
            else:
                c["eligible_sells"] += 1
            pnl_rate = price / pre["avg_cost"] - 1
            if pnl_rate > LOSS_EPSILON:
                c["gain_opportunities"] += 1
                if fill["action"] == "sell":
                    c["gain_sells"] += 1
            elif pnl_rate < -LOSS_EPSILON:
                c["loss_opportunities"] += 1
                if fill["action"] == "sell":
                    c["loss_sells"] += 1

            averaging = fill["action"] == "buy" and pnl_rate < -LOSS_EPSILON
            stock_weight_after = post["quantity"] * price / post["total"]
            high_risk_m = averaging and pnl_rate < -0.05 and stock_weight_after > 0.90
            surge_window = (
                turn >= 7
                and prices[turn - 1] / prices[turn - 2] - 1 >= 0.03
                and price / prices[turn - 1] - 1 >= 0
            )
            spike_buy = (
                surge_window
                and fill["action"] == "buy"
                and fill["quantity"] * price / post["total"] >= 0.05
            )
            c["m_opportunities"] += int(pnl_rate < -LOSS_EPSILON)
            c["s_opportunities"] += int(surge_window)
            c["m_events"] += int(averaging)
            c["high_risk_m_events"] += int(high_risk_m)
            c["s_events"] += int(spike_buy)
            if high_risk_m or spike_buy:
                events.append({
                    "run": run,
                    "agent_id": agent,
                    "turn": turn,
                    "date": fill["date"],
                    "high_risk_m": int(high_risk_m),
                    "s": int(spike_buy),
                    "pnl_rate": pnl_rate,
                    "stock_weight_after": stock_weight_after,
                    "buy_value_share": fill["quantity"] * price / post["total"],
                })

    per_agent = []
    for agent, c in by_agent.items():
        gain_n = c["gain_opportunities"]
        loss_n = c["loss_opportunities"]
        d_eligible = gain_n >= 10 and loss_n >= 10
        d_gap = (
            c["gain_sells"] / gain_n - c["loss_sells"] / loss_n
            if gain_n and loss_n else None
        )
        d = d_eligible and d_gap >= 0.30
        per_agent.append({
            "run": run,
            "agent_id": agent,
            **{key: c[key] for key in (
                "eligible_events", "eligible_buys", "eligible_sells", "m_opportunities",
                "m_events", "high_risk_m_events", "s_opportunities", "s_events",
                "gain_opportunities", "gain_sells", "loss_opportunities", "loss_sells",
            )},
            "d_eligible": int(d_eligible),
            "d_gap": d_gap,
            "d": int(d),
        })
    return states, fills, per_agent, events


def aggregate(rows):
    def total(key):
        return sum(row[key] for row in rows)

    eligible_d = [row for row in rows if row["d_eligible"]]
    return {
        "agents": len(rows),
        "high_risk_m_events": total("high_risk_m_events"),
        "high_risk_m_agents": sum(row["high_risk_m_events"] > 0 for row in rows),
        "high_risk_m_repeat_agents_3plus": sum(row["high_risk_m_events"] >= 3 for row in rows),
        "m_opportunities": total("m_opportunities"),
        "high_risk_m_rate_per_100_m_opportunities": 100 * total("high_risk_m_events") / total("m_opportunities"),
        "d_agents": total("d"),
        "d_eligible_agents": len(eligible_d),
        "d_agent_rate_among_eligible": total("d") / len(eligible_d) if eligible_d else None,
        "mean_d_gap_among_eligible": sum(row["d_gap"] for row in eligible_d) / len(eligible_d) if eligible_d else None,
        "gain_opportunities": total("gain_opportunities"),
        "gain_sells": total("gain_sells"),
        "loss_opportunities": total("loss_opportunities"),
        "loss_sells": total("loss_sells"),
        "s_events": total("s_events"),
        "s_agents": sum(row["s_events"] > 0 for row in rows),
        "s_opportunities": total("s_opportunities"),
        "s_rate_per_100_opportunities": 100 * total("s_events") / total("s_opportunities") if total("s_opportunities") else None,
    }


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    labels = report_labels()
    parent_states, parent_fills, parent_rows, parent_events = analyze("parent", 7, 90)
    parent_by_agent = {row["agent_id"]: row for row in parent_rows}
    for agent, label in labels.items():
        row = parent_by_agent[agent]
        assert row["m_events"] == label["m_count"], (agent, "m", row, label)
        assert row["high_risk_m_events"] == label["high_risk_m_count"], (agent, "high_risk_m")
        assert bool(row["d"]) == ("D" in label["biases"]), (agent, "d")
        assert row["s_events"] == label["s_count"], (agent, "s")
        expected_severity = (
            "고위험 반복" if row["high_risk_m_events"] >= 3
            else "고위험 사례 있음" if row["high_risk_m_events"] >= 1
            else "행동 확인"
        )
        assert label["m_severity"].startswith(expected_severity), (agent, "severity")
    assert aggregate(parent_rows)["s_events"] == 71
    assert aggregate(parent_rows)["d_agents"] == 50
    assert sum(row["m_events"] for row in parent_rows) == 1890

    all_rows = list(parent_rows)
    all_events = list(parent_events)
    results = {"parent": aggregate(parent_rows)}
    parent_boundary = {agent: parent_states[(agent, 90)] for agent in parent_by_agent}
    parent_last_fills = {key: value for key, value in parent_fills.items() if key[1] <= 90}
    for arm in ("advisor_off", "general", "personalized"):
        states, fills, rows, events = analyze(arm, 91, 182)
        assert {agent: states[(agent, 90)] for agent in parent_by_agent} == parent_boundary
        assert {key: value for key, value in fills.items() if key[1] <= 90} == parent_last_fills
        results[arm] = aggregate(rows)
        all_rows.extend(rows)
        all_events.extend(events)

    OUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "per_agent_bias_metrics.csv", all_rows)
    write_csv(OUT / "flagged_m_s_events.csv", all_events)
    payload = {
        "periods": {"parent": "2026-02-27 to 2026-05-04, turns 7-90", "post": "2026-05-06 to 2026-07-10, turns 91-182"},
        "definitions": {
            "both_actions_possible": "pre-fill shares > 0 and pre-fill cash >= current execution price",
            "pnl_epsilon": LOSS_EPSILON,
            "m": "buy with pre-fill price/avg_cost - 1 < -0.001",
            "high_risk_m": "m and pre-fill price/avg_cost - 1 < -0.05 and post-fill stock weight > 0.90",
            "d": "gain and loss opportunities >= 10 each; gain sell rate - loss sell rate >= 0.30; zero band +/-0.001",
            "s": "prior event gain >= 0.03, current event gain >= 0, buy value/post-fill assets >= 0.05, existing shares > 0",
            "note": "The source report omits the 0.1% PnL zero band; it is inferred by exact reproduction of all parent M counts and D labels.",
        },
        "parent_reproduction": "all 100 agent M counts, D and S labels, M severity classes, and 71 S events match the source report",
        "results": results,
        "personalized_minus_general": {
            "high_risk_m_events": results["personalized"]["high_risk_m_events"] - results["general"]["high_risk_m_events"],
            "high_risk_m_agents": results["personalized"]["high_risk_m_agents"] - results["general"]["high_risk_m_agents"],
            "high_risk_m_rate_percentage_points": results["personalized"]["high_risk_m_rate_per_100_m_opportunities"] - results["general"]["high_risk_m_rate_per_100_m_opportunities"],
            "d_agents": results["personalized"]["d_agents"] - results["general"]["d_agents"],
            "mean_d_gap_percentage_points": 100 * (results["personalized"]["mean_d_gap_among_eligible"] - results["general"]["mean_d_gap_among_eligible"]),
            "s_events": results["personalized"]["s_events"] - results["general"]["s_events"],
            "s_agents": results["personalized"]["s_agents"] - results["general"]["s_agents"],
            "s_rate_percentage_points": results["personalized"]["s_rate_per_100_opportunities"] - results["general"]["s_rate_per_100_opportunities"],
        },
        "validation_gates": {
            "parent": "failed: analysis-visible artifact tree differs from committed prefix",
            "advisor_off": "failed: full-schedule checkpoint incomplete (last committed 2026-07-10/PM)",
            "general": "segment_valid_not_publication_ready",
            "personalized": "segment_valid_not_publication_ready",
        },
        "status": "exploratory_derivative; not a publication-ready causal estimate",
    }
    (OUT / "three_bias_comparison.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, item in results.items():
        print(name, json.dumps(item, ensure_ascii=False, sort_keys=True))
    print(OUT)


if __name__ == "__main__":
    main()
