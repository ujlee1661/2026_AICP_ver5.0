#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.advisor.artifact import (
    ADVISOR_ASSIGNMENT_NAMESPACE,
    ADVISOR_ASSIGNMENT_SEED,
    canonical_sha256,
    deterministic_advisor_agents,
)
from twinmarket_kr.advisor.case_builder import build_advisor_cases
from twinmarket_kr.experiment_runtime import atomic_write_json, file_sha256
from twinmarket_kr.simulation import select_simulation_agents


def main() -> None:
    parser = argparse.ArgumentParser(description="Build cutoff-safe Advisor cases without an API call.")
    parser.add_argument("--parent-run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError(f"Advisor input output already exists: {args.output}")
    terminal = json.loads((args.parent_run_dir / "segment_complete.json").read_text(encoding="utf-8"))
    if terminal.get("status") != "segment_complete" or int(terminal.get("completed_event_count") or 0) != 90:
        raise ValueError("Advisor parent must be a completed 90-event segment")
    db_path = Path(str(terminal["runtime_db"]))
    agents = select_simulation_agents(None)
    selected = deterministic_advisor_agents(agent["agent_id"] for agent in agents)
    cases = build_advisor_cases(db_path, agents=agents, selected_agent_ids=selected)
    payload = {
        "artifact_type": "integrated_advisor_cases_v1",
        "assignment_namespace": ADVISOR_ASSIGNMENT_NAMESPACE,
        "assignment_seed": ADVISOR_ASSIGNMENT_SEED,
        "source_run_id": args.parent_run_dir.name,
        "source_runtime_db_sha256": file_sha256(db_path),
        "source_cutoff_event_id": "2026-05-04/PM",
        "selected_agent_ids": list(selected),
        "cases": cases,
    }
    payload["artifact_sha256"] = canonical_sha256(payload)
    atomic_write_json(args.output, payload)
    print(f"advisor_cases={args.output}")


if __name__ == "__main__":
    main()
