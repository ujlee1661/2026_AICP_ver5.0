#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.news_agent import SealedNewsBundle
from twinmarket_kr.advisor.artifact import AdvisorArtifact
from twinmarket_kr.community.validation import can_author_community_post
from twinmarket_kr.experiment_runtime import (
    EventCheckpointRuntime,
    ExperimentCheckpointError,
    assert_integrated_event_state,
    atomic_write_json,
    backup_database,
    build_run_signature_payload,
    canonical_sha256,
    file_sha256,
    run_directory_lock,
    validate_clean_experiment_base,
    validate_warm_experiment_base,
)
from twinmarket_kr.llm.client import (
    OpenRouterClient,
    response_content,
    validate_experiment_call_policy,
)
from twinmarket_kr.llm.call_policy import (
    INTEGRATED_STAGE_MAX_TOKENS_V1,
    INTEGRATED_STAGE_SCHEMA_VERSIONS_V1,
    RN_STRICT_RESPONSE_FORMAT,
    RN_STRICT_TEMPERATURE,
    StrictCallPolicy,
    validate_reasoning_audit,
)
from twinmarket_kr.llm.response_journal import ResponseJournal
from twinmarket_kr.outcome_schedule import FrozenEventSchedule
from twinmarket_kr.simulation import (
    run_simulation,
    select_simulation_agents,
)
from twinmarket_kr.study_spec import (
    ResolvedStudyProfile,
    validate_integrated_study_profile,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the integrated, event-checkpointed AM/PM simulation with one "
            "hash-validated sealed news bundle."
        )
    )
    parser.add_argument("--max-agents", type=int, default=None)
    parser.add_argument("--max-days", type=int, default=None)
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Default: the sealed study profile's study_seed.",
    )
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument(
        "--community-mode",
        choices=("off", "on"),
        default="on" if config.ENABLE_COMMUNITY else "off",
        help="Run the same sealed real-news baseline with community disabled/enabled.",
    )
    parser.add_argument(
        "--advisor-mode",
        choices=("off", "on"),
        default="off",
        help="Enable the sealed one-time advisor treatment for all 100 cohort agents.",
    )
    parser.add_argument(
        "--advisor-artifact",
        type=Path,
        default=None,
        help="Required with --advisor-mode on; forbidden when advisor mode is off.",
    )
    parser.add_argument(
        "--news-bundle",
        type=Path,
        default=config.SEALED_REAL_NEWS_BUNDLE,
        help=(
            "Hash-validated event-slot news bundle. The baseline default is "
            "preparation/rn_ab_sealed_to_20260722_v1/news.json."
        ),
    )
    parser.add_argument(
        "--calendar-registry",
        type=Path,
        default=config.SEALED_EVENT_CALENDAR,
        help="Hash-validated ordered AM/PM calendar registry.",
    )
    parser.add_argument(
        "--price-registry",
        type=Path,
        default=config.SEALED_EVENT_PRICES,
        help="Calendar-bound AM/open and PM/close execution-price registry.",
    )
    parser.add_argument(
        "--base-db",
        type=Path,
        default=config.EXPERIMENT_BASE_DB,
        help=(
            "Clean turn-zero experiment DB produced by "
            "scripts/04_build_experiment_base.py. It is copied once into the "
            "run-local runtime DB and never mutated by this runner."
        ),
    )
    parser.add_argument(
        "--warm-base",
        action="store_true",
        help="Continue from a validated 2026-05-04 turn-90 canonical parent DB.",
    )
    parser.add_argument(
        "--parent-run-dir",
        type=Path,
        default=None,
        help="Required with --warm-base; supplies immutable parent provenance.",
    )
    parser.add_argument(
        "--sim-db",
        type=Path,
        default=None,
        help=(
            "Explicit mutable runtime DB path. By default it is kept under "
            "<run-dir>/.runtime/runtime_sim.db."
        ),
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help=(
            "Explicit output directory. Supply the same path with --resume "
            "after interruption; no outputs/current pointer is used."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume only if the complete immutable signature matches.",
    )
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help=(
            "Development-only offline smoke option. Online/paper runs reject "
            "it because provenance and report artifacts are mandatory."
        ),
    )
    parser.add_argument(
        "--allow-paid-api",
        action="store_true",
        help=(
            "Explicitly authorize paid OpenRouter calls. Offline smoke runs do "
            "not require this flag; live runs fail before client construction "
            "without it."
        ),
    )
    parser.add_argument(
        "--reasoning-off-canary-audit",
        type=Path,
        default=None,
        help=(
            "Previously captured live canary JSONL proving the pinned model/"
            "provider returned zero reasoning tokens. Required for live runs."
        ),
    )
    parser.add_argument(
        "--capture-reasoning-off-canary",
        type=Path,
        default=None,
        metavar="AUDIT_JSONL",
        help=(
            "Make one explicitly authorized paid telemetry call and write a "
            "reasoning-off canary audit. This mode does not start a simulation."
        ),
    )
    return parser


def _new_run_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return config.LOG_DIR / f"simulation_{timestamp}_{os.getpid()}"


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentCheckpointError(
            f"{label} is not valid JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise ExperimentCheckpointError(f"{label} must be a JSON object")
    return value


def _selected_events(
    schedule: FrozenEventSchedule,
    *,
    start_date: str | None,
    end_date: str | None,
    max_days: int | None,
) -> tuple[list[str], list[str]]:
    if max_days is not None and max_days < 1:
        raise ValueError("--max-days must be a positive integer")
    all_dates = [
        str(event["date"])
        for event in schedule.events
        if str(event["subturn"]) == "am"
    ]
    dates = [
        day
        for day in all_dates
        if (start_date is None or day >= start_date)
        and (end_date is None or day <= end_date)
    ]
    if max_days is not None:
        dates = dates[:max_days]
    if not dates:
        raise ExperimentCheckpointError(
            "The requested date bounds select no frozen trading events"
        )
    event_ids = [
        str(event["event_id"])
        for event in schedule.events
        if str(event["date"]) in set(dates)
    ]
    expected_ids = [
        event_id
        for day in dates
        for event_id in (f"{day}/AM", f"{day}/PM")
    ]
    if event_ids != expected_ids:
        raise ExperimentCheckpointError(
            "Selected frozen events are not ordered AM/PM trading-day pairs"
        )
    return dates, event_ids


def _production_code_files() -> list[Path]:
    files = [PROJECT_ROOT / "config.py", Path(__file__).resolve()]
    files.extend(
        path
        for path in sorted((PROJECT_ROOT / "twinmarket_kr").rglob("*.py"))
        if "rn_ab" not in path.relative_to(PROJECT_ROOT / "twinmarket_kr").parts
    )
    return files


def _signature_input_files(args: argparse.Namespace) -> dict[str, Path]:
    inputs = {
        "news_bundle": Path(args.news_bundle),
        "calendar_registry": Path(args.calendar_registry),
        "price_registry": Path(args.price_registry),
        "persona_db": Path(config.SYS_100_DB),
    }
    if args.advisor_artifact is not None:
        inputs["advisor_artifact"] = Path(args.advisor_artifact)
    sealed_root = Path(args.news_bundle).parent
    for label, filename in (
        ("sealed_cohort", "cohort.json"),
        ("sealed_study_spec", "study_spec.json"),
        ("sealed_stage_inputs", "stage_inputs.json"),
        ("sealed_known_injection", "known_injection.json"),
    ):
        candidate = sealed_root / filename
        if candidate.is_file():
            inputs[label] = candidate
    return inputs


def _build_signature(
    args: argparse.Namespace,
    *,
    dates: list[str],
    event_ids: list[str],
    agents: list[dict[str, Any]],
    call_policy: dict[str, Any],
    initial_database_sha256: str,
    study_profile: ResolvedStudyProfile,
    canary_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    condition_id = (
        "RN_COMM_ON" if args.community_mode == "on" else "RN_COMM_OFF"
    ) + ("_ADVISOR_ON" if args.advisor_mode == "on" else "_ADVISOR_OFF")
    parameters = {
        "condition_id": condition_id,
        "study_spec_sha256": study_profile.study_spec_sha256,
        "cohort_sha256": study_profile.cohort_sha256,
        "prompt_bundle_sha256": study_profile.prompt_bundle_sha256,
        "news_treatment": "real_only",
        "community_mode": args.community_mode,
        "advisor_mode": args.advisor_mode,
        "advisor_artifact_sha256": (
            file_sha256(args.advisor_artifact)
            if args.advisor_artifact is not None
            else None
        ),
        "stock_code": study_profile.stock_code,
        "instrument_name": study_profile.instrument_name,
        "base_db_source": str(Path(args.base_db).resolve()),
        "warm_base": bool(args.warm_base),
        "parent_run_dir": (
            str(args.parent_run_dir.resolve()) if args.parent_run_dir is not None else None
        ),
        "parent_run_signature_sha256": (
            file_sha256(args.parent_run_dir / "run_signature.json")
            if args.parent_run_dir is not None
            else None
        ),
        "parent_terminal_sha256": (
            file_sha256(args.parent_run_dir / "segment_complete.json")
            if args.parent_run_dir is not None
            else None
        ),
        "start_date": dates[0],
        "end_date": dates[-1],
        "trading_dates": dates,
        "event_ids": event_ids,
        "agent_count": len(agents),
        "agent_ids": [str(agent["agent_id"]) for agent in agents],
        "agent_depths": {
            str(agent["agent_id"]): int(agent.get("news_depth") or 0)
            for agent in agents
        },
        "community_post_permissions": {
            str(agent["agent_id"]): can_author_community_post(agent)
            for agent in agents
        },
        "seed": int(args.seed),
        "information_mode": "pre_close_cutoff",
        "decision_space": "buy_sell_only",
        "min_order_unit": int(config.MIN_ORDER_UNIT),
        "simulation_concurrency": int(
            study_profile.per_arm_concurrency
        ),
        "global_api_concurrency": int(config.OPENROUTER_GLOBAL_CONCURRENCY),
        "openrouter_base_url": str(config.OPENROUTER_BASE_URL),
        "openrouter_max_retries": int(config.OPENROUTER_MAX_RETRIES),
        "llm_stage_max_tokens": dict(
            INTEGRATED_STAGE_MAX_TOKENS_V1
        ),
        "llm_stage_schema_versions": dict(
            INTEGRATED_STAGE_SCHEMA_VERSIONS_V1
        ),
        "openrouter_retry_max_delay": float(
            config.OPENROUTER_RETRY_MAX_DELAY
        ),
        "commission_rate": float(config.COMMISSION_RATE),
        "belief_limits": dict(config.BELIEF_LIMITS),
        "max_single_trade_cash_ratio": float(
            config.MAX_SINGLE_TRADE_CASH_RATIO
        ),
        "community_posting_enabled": bool(
            config.ENABLE_COMMUNITY_POSTING
        ),
        "community_reading_enabled": bool(
            config.ENABLE_COMMUNITY_READING
        ),
        "community_depth1_read_limit": int(
            config.COMMUNITY_DEPTH1_READ_LIMIT
        ),
        "community_depth2_read_limit": int(
            config.COMMUNITY_DEPTH2_READ_LIMIT
        ),
        "community_best_post_count": int(
            config.COMMUNITY_BEST_POST_COUNT
        ),
        "logging_enabled": not args.no_logs,
        "offline_llm": os.getenv(
            "TWINMARKET_OFFLINE_LLM",
            "",
        ).strip().lower()
        in {"1", "true", "yes"},
        "paid_api_authorized": bool(
            args.allow_paid_api
            and os.getenv(
                "TWINMARKET_OFFLINE_LLM",
                "",
            ).strip().lower()
            not in {"1", "true", "yes"}
        ),
        "reasoning_off_canary_evidence": canary_evidence,
        "python_version": platform.python_version(),
        "openai_package_version": _package_version("openai"),
    }
    return build_run_signature_payload(
        parameters=parameters,
        input_files=_signature_input_files(args),
        prompt_dir=config.PROMPT_DIR,
        code_files=_production_code_files(),
        call_policy=call_policy,
        initial_database_sha256=initial_database_sha256,
    )


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _read_jsonl_objects(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise ExperimentCheckpointError(
            f"{label} must be a real JSONL file: {path}"
        )
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExperimentCheckpointError(
                f"{label} line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ExperimentCheckpointError(
                f"{label} line {line_number} must be an object"
            )
        rows.append(value)
    return rows


def _validate_live_execution_gate(
    args: argparse.Namespace,
    *,
    offline_llm: bool,
    study_profile: ResolvedStudyProfile,
) -> dict[str, Any] | None:
    """Require explicit cost authorization and real reasoning-off evidence."""

    if offline_llm:
        if args.reasoning_off_canary_audit is not None:
            raise ValueError(
                "--reasoning-off-canary-audit is for live execution only"
            )
        return None
    if not args.allow_paid_api:
        raise ValueError(
            "Live execution is disabled by default. Re-run with "
            "--allow-paid-api only after reviewing the expected cost."
        )
    if args.reasoning_off_canary_audit is None:
        raise ValueError(
            "Live execution requires --reasoning-off-canary-audit; the "
            "request parameter alone is not evidence that reasoning was off"
        )
    path = args.reasoning_off_canary_audit.resolve()
    policy = StrictCallPolicy(
        model=config.PAPER_OPENROUTER_MODEL,
        provider=config.PAPER_OPENROUTER_PROVIDER,
        max_retries=config.OPENROUTER_MAX_RETRIES,
        concurrency=study_profile.per_arm_concurrency,
        reasoning_effort="none",
        reasoning_exclude=True,
        allow_fallbacks=False,
        require_parameters=True,
    )
    summary = validate_reasoning_audit(
        _read_jsonl_objects(path, "reasoning-off canary audit"),
        policy=policy,
        require_success=True,
    )
    return {
        "path": str(path),
        "sha256": file_sha256(path),
        **summary,
        "model": policy.model,
        "provider": policy.provider,
        "reasoning_tokens": 0,
    }


async def _capture_reasoning_off_canary(
    args: argparse.Namespace,
) -> Path:
    """Capture one real provider response and immediately revalidate it."""

    offline_llm = os.getenv(
        "TWINMARKET_OFFLINE_LLM",
        "",
    ).strip().lower() in {"1", "true", "yes"}
    if offline_llm:
        raise ValueError(
            "Canary capture requires a real provider and rejects "
            "TWINMARKET_OFFLINE_LLM"
        )
    if not args.allow_paid_api:
        raise ValueError(
            "Canary capture is a paid call and requires --allow-paid-api"
        )
    if args.resume or args.run_dir is not None:
        raise ValueError(
            "Canary capture does not accept --resume or --run-dir"
        )
    output = args.capture_reasoning_off_canary.resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError(
            f"Canary audit output already exists: {output}"
        )
    validate_experiment_call_policy()
    condition_id = (
        "RN_COMM_ON" if args.community_mode == "on" else "RN_COMM_OFF"
    )
    profile = validate_integrated_study_profile(
        Path(args.news_bundle).parent,
        agents=select_simulation_agents(None),
        condition_id=condition_id,
        prompt_dir=config.PROMPT_DIR,
        expected_stock_code=None,
        expected_model=config.PAPER_OPENROUTER_MODEL,
        expected_provider=config.PAPER_OPENROUTER_PROVIDER,
    )
    if args.seed is None:
        args.seed = profile.study_seed
    if int(args.seed) != profile.study_seed:
        raise ExperimentCheckpointError(
            "Canary seed differs from the sealed study seed"
        )
    policy = StrictCallPolicy(
        model=config.PAPER_OPENROUTER_MODEL,
        provider=config.PAPER_OPENROUTER_PROVIDER,
        max_retries=1,
        concurrency=1,
        reasoning_effort="none",
        reasoning_exclude=True,
        allow_fallbacks=False,
        require_parameters=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    logical_call_id = (
        f"reasoning-off-canary:{profile.study_spec_sha256}"
    )
    phase_attempt_id = "reasoning-off-canary-attempt-1"
    client = OpenRouterClient(
        max_retries=1,
        concurrency_limit=1,
        audit_path=output,
        audit_context={
            "artifact": "integrated_experiment_openrouter_attempt",
            "purpose": "reasoning_off_canary",
            "study_spec_sha256": profile.study_spec_sha256,
        },
    )
    response = await client.chat_strict_reasoning_off(
        [
            {
                "role": "user",
                "content": (
                    'Return exactly one JSON object: {"canary":"ok"}. '
                    "Do not add any other field."
                ),
            }
        ],
        policy=policy,
        response_format=dict(RN_STRICT_RESPONSE_FORMAT),
        temperature=RN_STRICT_TEMPERATURE,
        seed=profile.study_seed,
        max_tokens=32,
        audit_label="integrated_reasoning_off_canary",
        logical_call_id=logical_call_id,
        phase_attempt_id=phase_attempt_id,
    )
    try:
        parsed = json.loads(response_content(response))
    except json.JSONDecodeError as exc:
        raise ExperimentCheckpointError(
            "Canary provider response is not valid JSON"
        ) from exc
    if parsed != {"canary": "ok"}:
        raise ExperimentCheckpointError(
            "Canary provider response differs from the exact schema"
        )
    client.record_experiment_acceptance(
        logical_call_id=logical_call_id,
        phase_attempt_id=phase_attempt_id,
        accepted_response_sha256=canonical_sha256(parsed),
    )
    summary = validate_reasoning_audit(
        _read_jsonl_objects(output, "reasoning-off canary audit"),
        policy=policy,
        require_success=True,
    )
    evidence_path = output.with_suffix(output.suffix + ".evidence.json")
    atomic_write_json(
        evidence_path,
        {
            "artifact_type": "integrated_reasoning_off_canary_evidence",
            "status": "validated",
            "study_spec_sha256": profile.study_spec_sha256,
            "audit_path": str(output),
            "audit_sha256": file_sha256(output),
            "model": policy.model,
            "provider": policy.provider,
            "reasoning_tokens": 0,
            **summary,
        },
    )
    return output


def _saved_initial_database_sha256(run_dir: Path) -> str:
    record = _read_json_object(run_dir / "run_signature.json", "run signature")
    payload = record.get("signature_payload")
    if not isinstance(payload, dict):
        raise ExperimentCheckpointError(
            "Run signature has no signature_payload object"
        )
    value = payload.get("initial_database_sha256")
    if not isinstance(value, str) or not value:
        raise ExperimentCheckpointError(
            "Run signature has no initial database digest"
        )
    return value


def _saved_runtime_db(run_dir: Path) -> Path:
    checkpoint = _read_json_object(
        run_dir / ".runtime" / "checkpoint.json",
        "event checkpoint",
    )
    value = checkpoint.get("runtime_db")
    if not isinstance(value, str) or not value:
        raise ExperimentCheckpointError(
            "Event checkpoint has no runtime database path"
        )
    return Path(value)


def _write_run_metadata(
    run_dir: Path,
    signature_payload: dict[str, Any],
    *,
    status: str,
    checkpoint: dict[str, Any],
    response_journal: ResponseJournal,
) -> None:
    parameters = dict(signature_payload["parameters"])
    atomic_write_json(
        run_dir / "run_metadata.json",
        {
            "run_id": run_dir.name,
            "status": status,
            "run_signature_sha256": checkpoint["signature_sha256"],
            **parameters,
            "runtime_db": checkpoint["runtime_db"],
            "completed_events": checkpoint["completed_events"],
            "event_state_sha256": checkpoint["event_state_sha256"],
            "event_integrity_sha256": checkpoint[
                "event_integrity_sha256"
            ],
            "event_response_sha256": checkpoint.get(
                "event_response_sha256",
                {},
            ),
            "committed_database_sha256": checkpoint[
                "committed_database_sha256"
            ],
            "artifact_tree_sha256": checkpoint[
                "artifact_tree_sha256"
            ],
            "openrouter_call_policy": dict(
                signature_payload["call_policy"]
            ),
            "call_policy": dict(signature_payload["call_policy"]),
            "response_replay_status": "integrated",
            "response_replay_note": (
                "Every validated common-stage JSON response is keyed by "
                "run/condition/agent/event/stage/schema and committed with "
                "the matching event checkpoint."
            ),
            "response_journal": response_journal.summary(),
        },
    )


async def _run(args: argparse.Namespace) -> Path:
    # argparse always supplies these fields. Direct callers and older tests may
    # still construct a smaller Namespace, so preserve that internal API.
    for name, default in (
        ("advisor_mode", "off"),
        ("advisor_artifact", None),
        ("warm_base", False),
        ("parent_run_dir", None),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    if args.resume and args.run_dir is None:
        raise ValueError("--resume requires an explicit --run-dir")
    if args.max_agents is not None and args.max_agents < 1:
        raise ValueError("--max-agents must be a positive integer")
    if args.max_days is not None and args.max_days < 1:
        raise ValueError("--max-days must be a positive integer")
    if args.advisor_mode == "on" and args.advisor_artifact is None:
        raise ValueError("--advisor-mode on requires --advisor-artifact")
    if args.advisor_mode == "off" and args.advisor_artifact is not None:
        raise ValueError("--advisor-artifact is forbidden when --advisor-mode is off")
    if args.warm_base and args.parent_run_dir is None:
        raise ValueError("--warm-base requires --parent-run-dir")
    if not args.warm_base and args.parent_run_dir is not None:
        raise ValueError("--parent-run-dir requires --warm-base")
    offline_llm = os.getenv(
        "TWINMARKET_OFFLINE_LLM",
        "",
    ).strip().lower() in {"1", "true", "yes"}
    if args.no_logs and not offline_llm:
        raise ValueError(
            "--no-logs is development-only and may be used only with "
            "TWINMARKET_OFFLINE_LLM=1; paper runs require provenance logs"
        )
    if args.max_agents is not None and not offline_llm:
        raise ValueError(
            "--max-agents is an offline smoke-only truncation. Seal an explicit "
            "cohort profile for a live experiment with fewer agents."
        )
    call_policy = validate_experiment_call_policy()
    # Validate the complete sealed inputs before creating a model client or
    # mutating a runtime database.
    news_bundle = SealedNewsBundle.load(
        args.news_bundle,
        expected_stock_code=None,
    )
    schedule = FrozenEventSchedule.from_sealed_files(
        args.calendar_registry,
        args.price_registry,
        expected_stock_code=None,
    )
    if news_bundle.stock_code != schedule.stock_code:
        raise ExperimentCheckpointError(
            "Sealed news and price registry stock codes differ"
        )
    schedule_event_ids = [
        str(event["event_id"]) for event in schedule.events
    ]
    if sorted(news_bundle.slots_by_event) != schedule_event_ids:
        raise ExperimentCheckpointError(
            "Sealed news event order differs from the frozen event schedule"
        )
    dates, event_ids = _selected_events(
        schedule,
        start_date=args.start_date,
        end_date=args.end_date,
        max_days=args.max_days,
    )
    full_frozen_schedule = event_ids == schedule_event_ids
    available_agents = select_simulation_agents(None)
    condition_id = (
        "RN_COMM_ON" if args.community_mode == "on" else "RN_COMM_OFF"
    )
    study_profile = validate_integrated_study_profile(
        Path(args.news_bundle).parent,
        agents=available_agents,
        condition_id=condition_id,
        prompt_dir=config.PROMPT_DIR,
        expected_stock_code=schedule.stock_code,
        expected_model=config.PAPER_OPENROUTER_MODEL,
        expected_provider=config.PAPER_OPENROUTER_PROVIDER,
    )
    if args.seed is None:
        args.seed = study_profile.study_seed
    if int(args.seed) != study_profile.study_seed:
        raise ExperimentCheckpointError(
            f"--seed {args.seed} differs from sealed study_seed "
            f"{study_profile.study_seed}"
        )
    if (
        schedule.calendar_sha256 != study_profile.calendar_sha256
        or schedule.prices_sha256
        != study_profile.price_registry_sha256
    ):
        raise ExperimentCheckpointError(
            "Supplied calendar/price registries differ from the sealed profile"
        )
    canary_evidence = _validate_live_execution_gate(
        args,
        offline_llm=offline_llm,
        study_profile=study_profile,
    )
    run_dir = (args.run_dir or _new_run_dir()).resolve()
    sealed_agents = select_simulation_agents(
        agent_ids=study_profile.agent_ids,
        instrument_name=study_profile.instrument_name,
    )
    agents = (
        sealed_agents
        if args.max_agents is None
        else sealed_agents[: args.max_agents]
    )
    if not agents:
        raise ExperimentCheckpointError("The selected agent cohort is empty")
    full_cohort_size = len(sealed_agents)
    if args.max_agents is not None and args.max_agents > full_cohort_size:
        raise ExperimentCheckpointError(
            f"--max-agents exceeds the sealed cohort size {full_cohort_size}"
        )
    if args.advisor_artifact is not None:
        AdvisorArtifact.load(
            args.advisor_artifact,
            cohort_agent_ids=[str(agent["agent_id"]) for agent in sealed_agents],
        )

    with run_directory_lock(run_dir):
        if args.resume:
            runtime_db = (
                args.sim_db.resolve()
                if args.sim_db is not None
                else _saved_runtime_db(run_dir)
            )
            initial_database_sha256 = _saved_initial_database_sha256(
                run_dir
            )
        else:
            if run_dir.exists():
                raise FileExistsError(
                    f"New run directory already exists: {run_dir}"
                )
            if not Path(args.base_db).is_file():
                raise FileNotFoundError(
                    f"Base simulation DB is missing: {args.base_db}"
                )
            if args.warm_base:
                if dates[0] != "2026-05-06":
                    raise ExperimentCheckpointError(
                        "Advisor warm continuation must start on 2026-05-06"
                    )
                parent_dir = args.parent_run_dir.resolve()
                parent_terminal = _read_json_object(
                    parent_dir / "segment_complete.json",
                    "parent segment completion",
                )
                if (
                    parent_terminal.get("status") != "segment_complete"
                    or int(parent_terminal.get("completed_event_count") or 0) != 90
                ):
                    raise ExperimentCheckpointError(
                        "Warm parent must be a complete 90-event canonical segment"
                    )
                parent_runtime = Path(str(parent_terminal.get("runtime_db") or "")).resolve()
                if parent_runtime != Path(args.base_db).resolve():
                    raise ExperimentCheckpointError(
                        "--base-db must be the parent segment canonical runtime DB"
                    )
                validate_warm_experiment_base(
                    args.base_db,
                    expected_agent_ids=[str(agent["agent_id"]) for agent in agents],
                    boundary_turn=90,
                    boundary_date="2026-05-04",
                    community_mode=args.community_mode,
                )
            else:
                validate_clean_experiment_base(
                    args.base_db,
                    expected_agents=sealed_agents,
                    expected_stock_code=study_profile.stock_code,
                    expected_trading_dates=study_profile.schedule_date_ids,
                )
            runtime_db = (
                args.sim_db.resolve()
                if args.sim_db is not None
                else run_dir / ".runtime" / "runtime_sim.db"
            )
            if runtime_db.exists():
                raise FileExistsError(
                    f"New runtime database already exists: {runtime_db}"
                )
            backup_database(args.base_db, runtime_db)
            initial_database_sha256 = file_sha256(runtime_db)

        signature_payload = _build_signature(
            args,
            dates=dates,
            event_ids=event_ids,
            agents=agents,
            call_policy=call_policy,
            initial_database_sha256=initial_database_sha256,
            study_profile=study_profile,
            canary_evidence=canary_evidence,
        )
        response_journal = ResponseJournal(
            run_dir / ".runtime" / "response_journal.sqlite",
            manifest_sha256=canonical_sha256(signature_payload),
        )
        runtime = EventCheckpointRuntime(
            run_dir,
            runtime_db=runtime_db,
            event_ids=event_ids,
            signature_payload=signature_payload,
            response_journal=response_journal,
        )
        checkpoint = (
            runtime.open_for_resume()
            if args.resume
            else runtime.initialize_new()
        )
        _write_run_metadata(
            run_dir,
            signature_payload,
            status=str(checkpoint["status"]),
            checkpoint=checkpoint,
            response_journal=response_journal,
        )
        config.OPENROUTER_AUDIT_LOG = run_dir / "openrouter_calls.jsonl"
        paused_path = run_dir / "paused.json"
        if paused_path.exists():
            paused_path.unlink()

        last_phase_result: dict[str, Any] | None = None
        condition_id = str(
            signature_payload["parameters"]["condition_id"]
        )
        while True:
            event_id = runtime.next_event()
            if event_id is None:
                break
            day, subturn = event_id.split("/", 1)
            phases = (
                ("am",)
                if subturn == "AM"
                else ("pm", "community")
            )
            inflight = runtime.begin_event(event_id)
            try:
                last_phase_result = await run_simulation(
                    max_agents=args.max_agents,
                    agent_ids=study_profile.agent_ids,
                    max_days=1,
                    enable_logs=not args.no_logs,
                    random_seed=args.seed,
                    start_date=day,
                    end_date=day,
                    information_mode="pre_close_cutoff",
                    decision_space="buy_sell_only",
                    news_bundle=args.news_bundle,
                    calendar_registry=args.calendar_registry,
                    price_registry=args.price_registry,
                    advisor_artifact=args.advisor_artifact,
                    advisor_cohort_agent_ids=study_profile.agent_ids,
                    community_mode=args.community_mode,
                    sim_db=runtime_db,
                    reset_runtime_tables=(
                        not args.warm_base
                        and not runtime.checkpoint()["completed_events"]
                    ),
                    log_root=run_dir.parent,
                    log_run_id=run_dir.name,
                    phases=phases,
                    append_existing_logs=True,
                    response_journal=response_journal,
                    journal_run_id=run_dir.name,
                    journal_condition_id=condition_id,
                    journal_event_id=event_id,
                    phase_attempt_id=str(
                        inflight["phase_attempt_id"]
                    ),
                    event_attempt_number=int(
                        inflight["attempt_number"]
                    ),
                    concurrency=study_profile.per_arm_concurrency,
                    stock_code=study_profile.stock_code,
                    instrument_name=study_profile.instrument_name,
                )
                completed_with_current = [
                    *runtime.checkpoint()["completed_events"],
                    event_id,
                ]
                integrity_sha256 = assert_integrated_event_state(
                    runtime_db,
                    agent_ids=[
                        str(agent["agent_id"]) for agent in agents
                    ],
                    completed_events=[
                        schedule.event(value)
                        for value in completed_with_current
                    ],
                    stock_code=study_profile.stock_code,
                )
                logical_response_digests = (
                    response_journal.accepted_event_digests(event_id)
                )
                runtime.prepare_event_commit(
                    event_id,
                    integrity_sha256=integrity_sha256,
                    logical_response_digests=logical_response_digests,
                )
                checkpoint = runtime.finish_event_commit(event_id)
            except BaseException as exc:
                terminal_error: BaseException = exc
                current = runtime.checkpoint()
                inflight = current.get("inflight_event")
                if (
                    isinstance(inflight, dict)
                    and inflight.get("state") == "running"
                ):
                    checkpoint = runtime.pause_running_event(
                        event_id,
                        exc,
                    )
                elif (
                    isinstance(inflight, dict)
                    and inflight.get("state") == "commit_decided"
                ):
                    # The scientific state is already irrevocably decided. A
                    # restart must finish that commit, never roll it back.
                    try:
                        checkpoint = runtime.recover()
                    except BaseException as recovery_error:
                        checkpoint = current
                        terminal_error = recovery_error
                    else:
                        if event_id in checkpoint["completed_events"]:
                            continue
                else:
                    checkpoint = current
                atomic_write_json(
                    paused_path,
                    {
                        "status": "paused",
                        "event_id": event_id,
                        "error_type": type(terminal_error).__name__,
                        "error": str(terminal_error),
                        "restart_command": (
                            "rerun the identical command with --resume and "
                            f"--run-dir {run_dir}"
                        ),
                        "checkpoint_status": checkpoint.get("status"),
                    },
                )
                _write_run_metadata(
                    run_dir,
                    signature_payload,
                    status="paused",
                    checkpoint=checkpoint,
                    response_journal=response_journal,
                )
                raise terminal_error

            _write_run_metadata(
                run_dir,
                signature_payload,
                status=str(checkpoint["status"]),
                checkpoint=checkpoint,
                response_journal=response_journal,
            )

        checkpoint = runtime.mark_complete(
            full_schedule=full_frozen_schedule
        )
        terminal_status = (
            "complete" if full_frozen_schedule else "segment_complete"
        )
        terminal_name = (
            "run_complete.json"
            if full_frozen_schedule
            else "segment_complete.json"
        )
        if last_phase_result is None and full_frozen_schedule:
            final_day = event_ids[-1].split("/", 1)[0]
            phase_marker = (
                run_dir / f"phase_complete_{final_day}_community.json"
            )
            if phase_marker.is_file():
                loaded_phase_result = _read_json_object(
                    phase_marker,
                    "final phase marker",
                )
                last_phase_result = {
                    "community_delivery_summary": loaded_phase_result.get(
                        "community_delivery_summary"
                    ),
                    "outcome_finalized": bool(
                        loaded_phase_result.get("outcome_finalized")
                    ),
                    "right_censored_outcome_count": int(
                        loaded_phase_result.get(
                            "right_censored_outcome_count",
                            0,
                        )
                    ),
                    "schedule_complete": bool(
                        loaded_phase_result.get("schedule_complete")
                    ),
                }
        terminal_phase = dict(last_phase_result or {})
        atomic_write_json(
            run_dir / terminal_name,
            {
                "run_id": run_dir.name,
                "status": terminal_status,
                "full_frozen_schedule": full_frozen_schedule,
                "completed_event_count": len(
                    checkpoint["completed_events"]
                ),
                "event_count": len(event_ids),
                "runtime_db": checkpoint["runtime_db"],
                "log_dir": str(run_dir),
                "community_delivery_summary": terminal_phase.get(
                    "community_delivery_summary"
                ),
                "outcome_finalized": bool(
                    terminal_phase.get("outcome_finalized")
                ),
                "right_censored_outcome_count": int(
                    terminal_phase.get(
                        "right_censored_outcome_count",
                        0,
                    )
                ),
                "schedule_complete": bool(
                    terminal_phase.get("schedule_complete")
                ),
                "response_journal": response_journal.summary(),
            },
        )
        _write_run_metadata(
            run_dir,
            signature_payload,
            status=terminal_status,
            checkpoint=checkpoint,
            response_journal=response_journal,
        )
        if paused_path.exists():
            paused_path.unlink()
        return run_dir


def main() -> None:
    args = build_parser().parse_args()
    if args.capture_reasoning_off_canary is not None:
        audit_path = asyncio.run(_capture_reasoning_off_canary(args))
        print(f"canary_audit={audit_path}")
        return
    run_dir = asyncio.run(_run(args))
    print(f"log_dir={run_dir}")


if __name__ == "__main__":
    main()
