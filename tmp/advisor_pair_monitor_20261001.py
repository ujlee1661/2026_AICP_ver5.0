"""Monitor the two authorized advisor runs and resume interrupted checkpoints."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/Users/leeyujeong/2026_AICP_ver5.0")
PYTHON = "/Users/leeyujeong/2026_AICP_ver3.0/2026_AICP_ver3.0/.venv/bin/python"
MONITOR_DIR = ROOT / "outputs/experiments/advisor_pair_monitor_20261001"
MONITOR_DIR.mkdir(parents=True, exist_ok=True)
COMMON = [
    "--warm-base",
    "--parent-run-dir", "outputs/experiments/ADVISOR_MAIN_FROM_SCRATCH_PARENT_20260923",
    "--base-db", "outputs/experiments/ADVISOR_MAIN_FROM_SCRATCH_PARENT_20260923/.runtime/runtime_sim.db",
    "--start-date", "2026-05-06",
    "--end-date", "2026-07-10",
    "--community-mode", "on",
    "--advisor-mode", "on",
    "--seed", "2",
    "--news-bundle", "preparation/rn_ab_sealed_to_20260722_v1/news.json",
    "--calendar-registry", "preparation/rn_ab_sealed_to_20260722_v1/calendar.json",
    "--price-registry", "preparation/rn_ab_sealed_to_20260722_v1/prices.json",
    "--allow-paid-api",
    "--reasoning-off-canary-audit", "outputs/canary/advisor_general_personal_20261001.jsonl",
]
RUNS = {
    "general": {
        "pid": 35054,
        "dir": "outputs/experiments/ADVISOR_GENERAL_20261001_TO_20260710",
        "artifact": "outputs/advisor/advisor_comm_on_20260919/advisor_messages_100_general.json",
    },
    "personal": {
        "pid": 35055,
        "dir": "outputs/experiments/ADVISOR_PERSONAL_20261001_TO_20260710",
        "artifact": "outputs/advisor/advisor_comm_on_20260919/advisor_messages_100_final.json",
    },
}


def record(event: dict) -> None:
    event["time_utc"] = datetime.now(timezone.utc).isoformat()
    with (MONITOR_DIR / "monitor.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def running(pid: int, run_dir: str) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "05_run_simulation.py" in result.stdout and run_dir in result.stdout


def snapshot(run_dir: Path) -> dict:
    metadata_path = run_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    journal = run_dir / ".runtime/response_journal.sqlite"
    responses = None
    if journal.exists():
        try:
            with sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=5) as conn:
                responses = conn.execute("SELECT COUNT(*) FROM logical_responses").fetchone()[0]
        except sqlite3.Error:
            pass
    return {
        "status": metadata.get("status"),
        "completed_events": len(metadata.get("completed_events", [])),
        "last_completed": (metadata.get("completed_events") or [None])[-1],
        "response_count": responses,
        "segment_complete": (run_dir / "segment_complete.json").exists(),
    }


def main() -> None:
    lock = (MONITOR_DIR / "monitor.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    attempts = {name: 0 for name in RUNS}
    record({"event": "monitor_started", "pids": {name: item["pid"] for name, item in RUNS.items()}})
    while True:
        all_done = True
        for name, item in RUNS.items():
            run_dir = ROOT / item["dir"]
            state = snapshot(run_dir)
            alive = running(item["pid"], item["dir"])
            record({"event": "heartbeat", "run": name, "pid": item["pid"], "alive": alive, **state})
            if state["segment_complete"]:
                continue
            all_done = False
            if alive:
                continue
            if not (run_dir / ".runtime/checkpoint.json").exists() or attempts[name] >= 3:
                record({"event": "needs_manual_review", "run": name, **state})
                continue
            attempts[name] += 1
            command = [
                PYTHON, "scripts/05_run_simulation.py", *COMMON,
                "--advisor-artifact", item["artifact"],
                "--run-dir", item["dir"], "--resume",
            ]
            with (MONITOR_DIR / f"{name}.resume_{attempts[name]}.log").open("ab") as log:
                process = subprocess.Popen(
                    command, cwd=ROOT, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                )
            item["pid"] = process.pid
            record({"event": "resume_started", "run": name, "pid": process.pid, "attempt": attempts[name]})
        if all_done:
            record({"event": "all_segments_complete"})
            return
        time.sleep(120)


if __name__ == "__main__":
    main()
