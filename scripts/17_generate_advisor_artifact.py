#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.advisor.artifact import (
    ADVISOR_ARTIFACT_TYPE,
    ADVISOR_ASSIGNMENT_NAMESPACE,
    ADVISOR_ASSIGNMENT_SEED,
    ADVISOR_COHORT_SIZE,
    ADVISOR_MODEL,
    ADVISOR_PROVIDER,
    ADVISOR_REASONING_POLICY,
    canonical_sha256,
)
from twinmarket_kr.advisor.generation import advisor_client, generate_advisor_output
from twinmarket_kr.experiment_runtime import atomic_write_json
from twinmarket_kr.llm.client import stable_llm_seed


def _load_cases(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("artifact_type") != "integrated_advisor_cases_v1":
        raise ValueError("unknown advisor cases artifact")
    claimed = raw.get("artifact_sha256")
    payload = {key: value for key, value in raw.items() if key != "artifact_sha256"}
    if claimed != canonical_sha256(payload):
        raise ValueError("advisor cases artifact hash differs")
    if raw.get("assignment_namespace") != ADVISOR_ASSIGNMENT_NAMESPACE:
        raise ValueError("advisor cases assignment namespace differs")
    if int(raw.get("assignment_seed")) != ADVISOR_ASSIGNMENT_SEED:
        raise ValueError("advisor cases assignment seed differs")
    cases = raw.get("cases")
    if not isinstance(cases, list) or len(cases) != ADVISOR_COHORT_SIZE:
        raise ValueError(
            f"advisor cases artifact must contain exactly {ADVISOR_COHORT_SIZE} cases"
        )
    selected_ids = raw.get("selected_agent_ids")
    case_ids = [str(case.get("agent_id") or "") for case in cases]
    if selected_ids != case_ids or len(case_ids) != len(set(case_ids)):
        raise ValueError("advisor cases must cover the selected full cohort exactly once")
    return raw


async def _generate(args: argparse.Namespace) -> None:
    if os.getenv("TWINMARKET_OFFLINE_LLM", "").strip().lower() in {"1", "true", "yes"}:
        raise ValueError("Advisor artifact generation requires live OpenRouter")
    if not args.allow_paid_api:
        raise ValueError("Advisor generation is paid and requires --allow-paid-api")
    if args.output.exists() or args.output.is_symlink() or args.audit.exists() or args.audit.is_symlink():
        raise FileExistsError("Advisor output and audit paths must not already exist")
    source = _load_cases(args.cases)
    client = advisor_client(audit_path=args.audit)
    messages = []
    for index, case in enumerate(source["cases"]):
        agent_id = str(case["agent_id"])
        generation_seed = stable_llm_seed(2, "advisor_generation", agent_id)
        output, prompt_sha256 = await generate_advisor_output(
            case,
            client=client,
            seed=generation_seed,
            agent_id=agent_id,
        )
        body = str(output["advice_body"])
        messages.append(
            {
                "advisor_id": f"advisor-{agent_id}-20260504",
                "agent_id": agent_id,
                "source_run_id": str(source["source_run_id"]),
                "source_cutoff_event_id": "2026-05-04/PM",
                "source_state_sha256": str(case["source_state_sha256"]),
                "advisor_prompt_sha256": prompt_sha256,
                "generation_seed": generation_seed,
                "persona_basis": output["persona_basis"],
                "observed_behavior": output["observed_behavior"],
                "persona_behavior_assessment": output["persona_behavior_assessment"],
                "performance_context": output["performance_context"],
                "message_body": body,
                "message_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "valid_from_event_id": "2026-05-06/AM",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    artifact = {
        "artifact_type": ADVISOR_ARTIFACT_TYPE,
        "assignment_namespace": ADVISOR_ASSIGNMENT_NAMESPACE,
        "assignment_seed": ADVISOR_ASSIGNMENT_SEED,
        "model": ADVISOR_MODEL,
        "provider": ADVISOR_PROVIDER,
        "reasoning_policy": ADVISOR_REASONING_POLICY,
        "source_cases_sha256": str(source["artifact_sha256"]),
        "messages": messages,
    }
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    atomic_write_json(args.output, artifact)
    print(f"advisor_artifact={args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate 100 sealed Advisor messages through OpenRouter.")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--allow-paid-api", action="store_true")
    args = parser.parse_args()
    asyncio.run(_generate(args))


if __name__ == "__main__":
    main()
