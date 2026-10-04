"""Per-agent advisor bias response and pre-advice return grouping."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs/experiments/derived/advisor_three_bias_20260710/per_agent_bias_metrics.csv"
OUT = ROOT / "outputs/experiments/derived/advisor_three_bias_20260710"
PARENT_DB = ROOT / "outputs/experiments/ADVISOR_MAIN_FROM_SCRATCH_PARENT_20260923/.runtime/committed.db"


def load_metrics():
    with SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 400
    return {(row["run"], row["agent_id"]): row for row in rows}


def load_returns():
    db = sqlite3.connect(f"file:{PARENT_DB}?mode=ro", uri=True)
    try:
        initial = dict(db.execute("SELECT agent_id, total_value FROM portfolio_state WHERE turn=0"))
        end = dict(db.execute("SELECT agent_id, total_value FROM portfolio_state WHERE turn=90"))
        stored = dict(db.execute("SELECT agent_id, total_return_rate FROM portfolio_state WHERE turn=90"))
    finally:
        db.close()
    assert len(initial) == len(end) == len(stored) == 100
    computed = {agent: end[agent] / initial[agent] - 1 for agent in initial}
    assert all(math.isclose(computed[a], stored[a], abs_tol=1e-8) for a in computed)
    return computed


def measure(row, bias):
    if bias == "M":
        return int(row["high_risk_m_events"]) / int(row["m_opportunities"])
    if bias == "D":
        return float(row["d_gap"])
    if bias == "S":
        return int(row["s_events"]) / int(row["s_opportunities"])
    raise AssertionError(bias)


def direction(personal, comparator):
    if personal < comparator - 1e-12:
        return "improved"
    if personal > comparator + 1e-12:
        return "worsened"
    return "unchanged"


def fisher_two_sided(a, b, c, d):
    n1, n0 = a + b, c + d
    total_success = a + c
    total = n1 + n0
    def probability(x):
        return math.comb(total_success, x) * math.comb(total - total_success, n1 - x) / math.comb(total, n1)
    observed = probability(a)
    lo = max(0, n1 - (total - total_success))
    hi = min(n1, total_success)
    return min(1.0, sum(probability(x) for x in range(lo, hi + 1) if probability(x) <= observed + 1e-15))


def group_test(rows, predicate):
    observed = [row for row in rows if predicate(row)]
    high = [row for row in observed if row["above_mean"]]
    low = [row for row in observed if not row["above_mean"]]
    a = sum(row["is_improved"] for row in high)
    c = sum(row["is_improved"] for row in low)
    return {
        "high_return": {"improved": a, "n": len(high), "rate": a / len(high) if high else None},
        "below_mean_return": {"improved": c, "n": len(low), "rate": c / len(low) if low else None},
        "risk_difference_high_minus_low": a / len(high) - c / len(low) if high and low else None,
        "fisher_two_sided_p_exploratory": fisher_two_sided(a, len(high) - a, c, len(low) - c) if high and low else None,
    }


def main():
    metrics = load_metrics()
    returns = load_returns()
    mean_return = sum(returns.values()) / 100
    detailed = []
    for agent in sorted(returns):
        prior = metrics["parent", agent]
        post = {arm: metrics[arm, agent] for arm in ("advisor_off", "general", "personalized")}
        diagnosed = {
            "M": int(prior["high_risk_m_events"]) > 0,
            "D": int(prior["d"]) == 1,
            "S": int(prior["s_events"]) > 0,
        }
        row = {
            "agent_id": agent,
            "return_to_20260504": returns[agent],
            "above_mean": returns[agent] >= mean_return,
            "prior_diagnoses": "".join(b for b in "MDS" if diagnosed[b]) or "none",
            "prior_high_risk_m_events": int(prior["high_risk_m_events"]),
            "prior_d": int(prior["d"]),
            "prior_d_gap": float(prior["d_gap"]),
            "prior_s_events": int(prior["s_events"]),
        }
        for bias in "MDS":
            row[f"{bias}_diagnosed"] = diagnosed[bias]
            row[f"{bias}_pre_measure"] = measure(prior, bias)
            for arm in ("advisor_off", "general", "personalized"):
                arm_row = post[arm]
                row[f"{bias}_{arm}_measure"] = measure(arm_row, bias)
                row[f"{bias}_{arm}_event_count"] = (
                    int(arm_row["high_risk_m_events"]) if bias == "M"
                    else int(arm_row["d"]) if bias == "D"
                    else int(arm_row["s_events"])
                )
                row[f"{bias}_{arm}_opportunities"] = (
                    int(arm_row["m_opportunities"]) if bias == "M"
                    else int(arm_row["gain_opportunities"]) + int(arm_row["loss_opportunities"]) if bias == "D"
                    else int(arm_row["s_opportunities"])
                )
            row[f"{bias}_personal_vs_general"] = (
                direction(row[f"{bias}_personalized_measure"], row[f"{bias}_general_measure"])
                if diagnosed[bias] else "not_diagnosed"
            )
            row[f"{bias}_personal_vs_pre"] = (
                direction(row[f"{bias}_personalized_measure"], row[f"{bias}_pre_measure"])
                if diagnosed[bias] else "not_diagnosed"
            )
        statuses = [row[f"{b}_personal_vs_general"] for b in "MDS" if diagnosed[b]]
        if not statuses:
            category = "no_prior_diagnosis"
        elif "improved" in statuses and "worsened" not in statuses:
            category = "clear_improvement"
        elif "worsened" in statuses and "improved" not in statuses:
            category = "clear_worsening"
        elif "improved" in statuses and "worsened" in statuses:
            category = "mixed"
        else:
            category = "unchanged"
        row["overall_personal_vs_general"] = category
        row["diagnosis_improved_count"] = statuses.count("improved")
        row["diagnosis_worsened_count"] = statuses.count("worsened")
        pre_statuses = [row[f"{b}_personal_vs_pre"] for b in "MDS" if diagnosed[b]]
        if not pre_statuses:
            pre_category = "no_prior_diagnosis"
        elif "improved" in pre_statuses and "worsened" not in pre_statuses:
            pre_category = "clear_improvement"
        elif "worsened" in pre_statuses and "improved" not in pre_statuses:
            pre_category = "clear_worsening"
        elif "improved" in pre_statuses and "worsened" in pre_statuses:
            pre_category = "mixed"
        else:
            pre_category = "unchanged"
        row["overall_personal_vs_pre"] = pre_category
        row["improved_vs_pre_and_general"] = (
            pre_category == "clear_improvement" and category == "clear_improvement"
        )
        detailed.append(row)

    assert len(detailed) == 100
    assert sum(row["above_mean"] for row in detailed) == 47
    assert {b: sum(row[f"{b}_diagnosed"] for row in detailed) for b in "MDS"} == {"M": 43, "D": 50, "S": 44}
    status_counts = Counter(row["overall_personal_vs_general"] for row in detailed)
    response = {}
    for bias in "MDS":
        cases = [row for row in detailed if row[f"{bias}_diagnosed"]]
        counts = Counter(row[f"{bias}_personal_vs_general"] for row in cases)
        groups = []
        for row in cases:
            groups.append({"above_mean": row["above_mean"], "is_improved": row[f"{bias}_personal_vs_general"] == "improved"})
        response[bias] = {
            "diagnosed": len(cases),
            "personal_vs_general": dict(counts),
            "personal_vs_pre": dict(Counter(row[f"{bias}_personal_vs_pre"] for row in cases)),
            "return_group_test": group_test(groups, lambda _: True),
            "improved_agent_ids": [row["agent_id"] for row in cases if row[f"{bias}_personal_vs_general"] == "improved"],
        }
    group_rows = [
        {"above_mean": row["above_mean"], "is_improved": row["overall_personal_vs_general"] == "clear_improvement"}
        for row in detailed if row["prior_diagnoses"] != "none"
    ]
    robust_group_rows = [
        {"above_mean": row["above_mean"], "is_improved": row["improved_vs_pre_and_general"]}
        for row in detailed if row["prior_diagnoses"] != "none"
    ]
    payload = {
        "mean_parent_return": mean_return,
        "return_group_sizes": {"at_or_above_mean": 47, "below_mean": 53},
        "response_definition": "Among biases diagnosed by 2026-05-04, the personalized post-period opportunity-adjusted measure is lower than the general-advice measure; D uses the profit-loss sell-rate gap. This measures behavior, not whether the agent read or obeyed advice.",
        "overall_definition": "clear_improvement: at least one diagnosed bias improved and none worsened; mixed: both improved and worsened; clear_worsening: at least one worsened and none improved",
        "overall_counts": dict(status_counts),
        "overall_clear_improvement_by_return": group_test(group_rows, lambda _: True),
        "improved_vs_pre_and_general_by_return": group_test(robust_group_rows, lambda _: True),
        "bias_response": response,
        "clear_improvement_agent_ids": [row["agent_id"] for row in detailed if row["overall_personal_vs_general"] == "clear_improvement"],
        "improved_vs_pre_and_general_agent_ids": [row["agent_id"] for row in detailed if row["improved_vs_pre_and_general"]],
        "status": "exploratory observational proxy within simulated agents; advisor exposure is not direct evidence of compliance",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "per_agent_response_by_return.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(detailed[0]))
        writer.writeheader()
        writer.writerows(detailed)
    (OUT / "response_by_return_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Advisor 편향 진단 변화: 100명 전원",
        "",
        "사전 수익률은 2026-05-04 PM 총자산 / 최초 총자산 - 1이다. 평균은 9.252696%이다.",
        "M은 고위험 물타기 비율, D는 이익-손실 매도율 격차, S는 급등 조건 기회당 추가매수 비율이다.",
        "각 지표의 숫자는 사전 / 사후 일반 조언 / 사후 개인화 조언 순서이며 단위는 %다.",
        "'일반 대비'는 사전 진단 항목에서 개인화의 비율이 하나 이상 낮고 다른 진단 항목은 높지 않을 때 개선으로 표시한다.",
        "'동시 개선'은 일반 대비 개선이면서 사전 대비로도 같은 기준을 충족한 경우다.",
        "14명의 진단 없음은 일반 물타기 M 자체가 없다는 의미가 아니다. 사전 고위험 M, D, S 진단이 없다는 의미다.",
        "",
        "| Agent | 5/4 수익률 | 평균 이상 | 사전 진단 | M: 사전/일반/개인 | D: 사전/일반/개인 | S: 사전/일반/개인 | 사전 대비 | 일반 대비 | 동시 개선 |",
        "| --- | ---: | :---: | :---: | ---: | ---: | ---: | :---: | :---: | :---: |",
    ]
    labels = {
        "clear_improvement": "개선", "clear_worsening": "악화",
        "mixed": "혼합", "unchanged": "동일", "no_prior_diagnosis": "평가대상 아님",
    }
    for row in detailed:
        triples = []
        for bias in "MDS":
            triples.append("/".join(
                f"{100 * row[f'{bias}_{part}_measure']:.1f}"
                for part in ("pre", "general", "personalized")
            ))
        lines.append(
            f"| {row['agent_id']} | {100 * row['return_to_20260504']:.1f}% | "
            f"{'예' if row['above_mean'] else '아니오'} | {row['prior_diagnoses']} | "
            f"{' | '.join(triples)} | {labels[row['overall_personal_vs_pre']]} | "
            f"{labels[row['overall_personal_vs_general']]} | "
            f"{'예' if row['improved_vs_pre_and_general'] else '아니오'} |"
        )
    (OUT / "per_agent_response_by_return.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("mean_parent_return", "return_group_sizes", "overall_counts", "overall_clear_improvement_by_return")}, ensure_ascii=False, indent=2))
    for bias in "MDS":
        print(bias, json.dumps(response[bias], ensure_ascii=False))


if __name__ == "__main__":
    main()
