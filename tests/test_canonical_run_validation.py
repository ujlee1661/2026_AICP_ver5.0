from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import config
from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.db.connection import connect
from twinmarket_kr.experiment_runtime import (
    artifact_tree_sha256,
    assert_integrated_event_state,
    canonical_sha256,
    file_sha256,
)
from twinmarket_kr.run_integrity import (
    CanonicalRunValidationError,
    validate_canonical_run,
    validate_response_journal_audit,
    validate_sealed_news_coverage,
)
from twinmarket_kr.llm.call_policy import RN_REASONING_AUDIT_FIELDS
from twinmarket_kr.llm.response_journal import (
    LogicalCallKey,
    ResponseJournal,
)


def _dimensions(label: str) -> dict[str, str]:
    return {
        f"dim_{index}": f"{label} dimension {index}"
        for index in range(1, 7)
    }


def _empty_dimension_evidence() -> dict[str, dict[str, list[str]]]:
    return {
        f"dim_{index}": {"support": [], "contradict": []}
        for index in range(1, 7)
    }


def _build_one_event_database(path: Path) -> None:
    memory = MemoryAgent(path)
    ltb_0 = memory.bootstrap_ltb(
        agent_id="A001",
        date="2026-02-27",
        dimensions=_dimensions("initial"),
        belief_summary="initial human log",
    )
    stb_id = memory.save_stb(
        agent_id="A001",
        turn=1,
        date="2026-02-27",
        subturn="am",
        dimensions=_dimensions("short"),
    )
    analysis_id = memory.record_analysis_lineage(
        agent_id="A001",
        turn=1,
        date="2026-02-27",
        subturn="am",
        source_ltb_id=ltb_0,
        source_stb_id=stb_id,
        analysis={"market_view": "validated"},
    )
    decision_id = memory.record_decision_lineage(
        agent_id="A001",
        turn=1,
        date="2026-02-27",
        subturn="am",
        source_ltb_id=ltb_0,
        source_stb_id=stb_id,
        analysis_id=analysis_id,
        decision={
            "action": "buy",
            "quantity": 1,
            "reason": "test",
            "risk_control": "cash",
        },
    )
    memory.append_trade_log(
        {
            "agent_id": "A001",
            "turn": 1,
            "date": "2026-02-27",
            "action": "buy",
            "stock_code": "005930",
            "quantity": 1,
            "fee": 0,
            "analysis_id": analysis_id,
            "decision_id": decision_id,
            "source_ltb_id": ltb_0,
            "source_stb_id": stb_id,
        }
    )
    fill_id = memory.record_fill_lineage(
        decision_id=decision_id,
        filled_quantity=1,
        executed_price=100,
        pre_portfolio={"cash": 1_000, "quantity": 0},
        post_portfolio={"cash": 900, "quantity": 1},
    )
    memory.update_trade_execution(
        "A001",
        1,
        filled_quantity=1,
        executed_price=100,
        fee=0,
    )
    memory.save_post_fill_ltb(
        agent_id="A001",
        turn=1,
        date="2026-02-27",
        subturn="am",
        parent_ltb_id=ltb_0,
        stb_id=stb_id,
        decision_id=decision_id,
        fill_id=fill_id,
        dimensions=_dimensions("long"),
        integration_evidence=_empty_dimension_evidence(),
        belief_summary="post-fill human log",
        view_change={"source_fill_id": fill_id},
    )
    with connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO portfolio_state (
                state_id, agent_id, turn, date, cash, positions,
                total_value, realized_pnl, total_return_rate
            ) VALUES (?, 'A001', ?, '2026-02-27', ?, ?, ?, 0, 0)
            """,
            [
                ("portfolio-A001-0", 0, 1_000, "[]", 1_000),
                (
                    "portfolio-A001-1",
                    1,
                    900,
                    json.dumps(
                        [{"stock_code": "005930", "quantity": 1}]
                    ),
                    1_000,
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO TradingDetails (
                date, stock_id, user_id, trading_direction, price, volume
            ) VALUES ('2026-02-27', '005930', 'A001', 'buy', 100, 1)
            """
        )
        connection.commit()


def _write_segment_run(root: Path) -> Path:
    runtime = root / ".runtime"
    runtime.mkdir(parents=True)
    committed_db = runtime / "committed.db"
    _build_one_event_database(committed_db)
    db_sha = file_sha256(committed_db)
    event_id = "2026-02-27/AM"
    integrity_sha = assert_integrated_event_state(
        committed_db,
        agent_ids=["A001"],
        completed_events=[
            {
                "event_id": event_id,
                "turn": 1,
                "date": "2026-02-27",
                "subturn": "am",
            }
        ],
        stock_code="005930",
    )
    news_path = Path(config.SEALED_REAL_NEWS_BUNDLE).resolve()
    signature_payload = {
        "schema_version": "integrated-event-checkpoint-v1",
        "parameters": {
            "event_ids": [event_id],
            "agent_ids": ["A001"],
            "agent_depths": {"A001": 0},
            "stock_code": "005930",
            "community_mode": "off",
            "logging_enabled": False,
            "offline_llm": True,
        },
        "sealed_inputs": {
            "news_bundle": {
                "path": str(news_path),
                "sha256": file_sha256(news_path),
            }
        },
        "prompts": {},
        "code": {},
        "call_policy": {},
        "initial_database_sha256": "0" * 64,
    }
    signature_sha = canonical_sha256(signature_payload)
    (root / "run_signature.json").write_text(
        json.dumps(
            {
                "signature_payload": signature_payload,
                "signature_sha256": signature_sha,
            }
        ),
        encoding="utf-8",
    )
    artifact_sha = artifact_tree_sha256(root)
    checkpoint = {
        "signature_sha256": signature_sha,
        "status": "segment_complete",
        "completed_events": [event_id],
        "inflight_event": None,
        "event_state_sha256": {event_id: db_sha},
        "event_integrity_sha256": {event_id: integrity_sha},
        "committed_database_sha256": db_sha,
        "artifact_tree_sha256": artifact_sha,
    }
    (runtime / "checkpoint.json").write_text(
        json.dumps(checkpoint),
        encoding="utf-8",
    )
    (root / "run_metadata.json").write_text(
        json.dumps(
            {
                "status": "segment_complete",
                "run_signature_sha256": signature_sha,
                "committed_database_sha256": db_sha,
                "artifact_tree_sha256": artifact_sha,
            }
        ),
        encoding="utf-8",
    )
    (root / "segment_complete.json").write_text(
        json.dumps(
            {
                "status": "segment_complete",
                "full_frozen_schedule": False,
                "completed_event_count": 1,
                "event_count": 1,
                "schedule_complete": False,
                "outcome_finalized": False,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_current_sealed_shortages_accept_exact_three_to_ten() -> None:
    report = validate_sealed_news_coverage(
        config.SEALED_REAL_NEWS_BUNDLE
    )
    assert report["target_real_count"] == 10
    assert report["event_count"] == 196
    assert report["delivered_real_count"] == 1_602
    assert report["shortage_event_count"] == 138
    assert min(report["slot_counts"].values()) == 3
    assert max(report["slot_counts"].values()) == 10
    for event_id, shortage in report["accepted_shortages"].items():
        assert shortage["actual_real_count"] == report["slot_counts"][event_id]
        assert shortage["missing_real_count"] == (
            10 - report["slot_counts"][event_id]
        )


def test_segment_can_be_inspected_but_is_not_publication_ready(
    tmp_path: Path,
) -> None:
    run_dir = _write_segment_run(tmp_path / "run")
    report = validate_canonical_run(
        run_dir,
        publication_ready=False,
        verify_logs=False,
    )
    assert report["status"] == "segment_valid_not_publication_ready"
    assert report["database"]["fill_count"] == 1
    assert report["news_coverage"]["slot_counts"] == {
        "2026-02-27/AM": 8
    }
    with pytest.raises(
        CanonicalRunValidationError,
        match="publication run completion marker",
    ):
        validate_canonical_run(run_dir, publication_ready=True)


def test_run_local_derivative_file_invalidates_signed_artifact_tree(
    tmp_path: Path,
) -> None:
    run_dir = _write_segment_run(tmp_path / "run")
    committed_db = run_dir / ".runtime" / "committed.db"
    committed_before = file_sha256(committed_db)
    validate_canonical_run(
        run_dir,
        publication_ready=False,
        verify_logs=False,
    )
    assert file_sha256(committed_db) == committed_before
    derivative = run_dir / "reports" / "run_report.pdf"
    derivative.parent.mkdir()
    derivative.write_bytes(b"derivative output must not be stored in the run")

    with pytest.raises(
        CanonicalRunValidationError,
        match="artifact tree",
    ):
        validate_canonical_run(
            run_dir,
            publication_ready=False,
            verify_logs=False,
        )


def test_canonical_validator_rejects_a_rehashed_lineage_tamper(
    tmp_path: Path,
) -> None:
    run_dir = _write_segment_run(tmp_path / "run")
    committed_db = run_dir / ".runtime" / "committed.db"
    with sqlite3.connect(committed_db) as connection:
        connection.execute(
            """
            UPDATE simulation_ltb_states
            SET parent_ltb_id = ltb_id
            WHERE agent_id = 'A001' AND turn = 1
            """
        )
        connection.commit()
    db_sha = file_sha256(committed_db)
    checkpoint_path = run_dir / ".runtime" / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["committed_database_sha256"] = db_sha
    checkpoint["event_state_sha256"]["2026-02-27/AM"] = db_sha
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    metadata_path = run_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["committed_database_sha256"] = db_sha
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        CanonicalRunValidationError,
        match="Hierarchical lineage mismatch",
    ):
        validate_canonical_run(
            run_dir,
            publication_ready=False,
            verify_logs=False,
        )


def _provider_audit_row(
    *,
    logical_call_id: str,
    phase_attempt_id: str,
    seed: int,
    temperature: float,
    response_sha256: str,
    request_policy: dict[str, object],
) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp_utc": "2026-07-29T00:00:00+00:00",
        "pid": 1,
        "label": "community_posting",
        "audit_event": "provider_attempt",
        "status": "provider_returned",
        "requested_model": "fixed-model",
        "returned_model": "fixed-model",
        "provider": "fixed-provider",
        "request_id": f"request-{seed}",
        "seed": seed,
        "attempt": 1,
        "latency_seconds": 0.1,
        "prompt_sha256": f"{seed:064x}",
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "reasoning_tokens": 0,
        },
        "reasoning_tokens": 0,
        "response_reasoning_present": False,
        "finish_reason": "stop",
        "provider_response_sha256": response_sha256,
        "provider_canonical_json_sha256": response_sha256,
        "accepted_response_sha256": None,
        "provider_attempt_sha256": None,
        "request_policy": request_policy,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "max_tokens": 1024,
        "request_policy_sha256": hashlib.sha256(
            json.dumps(
                request_policy,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "logical_call_id": logical_call_id,
        "phase_attempt_id": phase_attempt_id,
        "audit_context": {
            "artifact": "integrated_experiment_openrouter_attempt",
            "run_id": "run",
            "condition_id": "RN_COMM_ON",
        },
        "error_type": None,
        "error": None,
    }
    assert set(row) == RN_REASONING_AUDIT_FIELDS
    return row


def _response_audit_fixture(
    root: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    root.mkdir()
    request_policy: dict[str, object] = {
        "reasoning": {"effort": "none", "exclude": True},
        "provider": {
            "only": ["fixed-provider"],
            "order": ["fixed-provider"],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
    }
    signature: dict[str, object] = {
        "parameters": {
            "event_ids": ["2026-02-27/PM"],
            "agent_ids": ["A001"],
            "condition_id": "RN_COMM_ON",
            "llm_stage_max_tokens": {"community_posting": 1024},
            "llm_stage_schema_versions": {
                "community_posting": "posting-v1"
            },
        },
        "call_policy": {
            "model": "fixed-model",
            **request_policy,
        },
    }
    journal = ResponseJournal(
        root / ".runtime" / "response_journal.sqlite",
        manifest_sha256=canonical_sha256(signature),
    )
    key = LogicalCallKey(
        run_id=root.name,
        condition_id="RN_COMM_ON",
        agent_id="A001",
        event_id="2026-02-27/PM",
        stage="community_posting",
        schema_version="posting-v1",
    )
    request = {
        "base_prompt": "fixed",
        "semantic_inputs": {"fill_id": "fill-1"},
        "model": "fixed-model",
        "temperature_schedule": [0.7, 0.3],
        "seed_schedule": [101, 102],
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
        "request_policy": request_policy,
        "validation_attempts": 2,
        "validation_procedure_version": "posting-validation-v1",
    }
    phase_id = "phase-1"
    logical_id = journal.begin_attempt(
        key,
        request,
        phase_attempt_id=phase_id,
        event_attempt_number=1,
        validation_attempt=1,
    )
    rejected = {"will_post": False}
    journal.record_rejected(
        logical_id,
        phase_attempt_id=phase_id,
        validation_attempt=1,
        response=rejected,
        error='["body is required"]',
    )
    journal.begin_attempt(
        key,
        request,
        phase_attempt_id=phase_id,
        event_attempt_number=1,
        validation_attempt=2,
    )
    accepted = {
        "will_post": True,
        "title": "title",
        "content": "body",
    }
    accepted_sha = journal.record_success(
        logical_id,
        accepted,
        phase_attempt_id=phase_id,
        validation_attempt=2,
    )
    journal.mark_committed({logical_id: accepted_sha})
    rejected_sha = canonical_sha256(rejected)
    first = _provider_audit_row(
        logical_call_id=logical_id,
        phase_attempt_id=phase_id,
        seed=101,
        temperature=0.7,
        response_sha256=rejected_sha,
        request_policy=request_policy,
    )
    second = _provider_audit_row(
        logical_call_id=logical_id,
        phase_attempt_id=phase_id,
        seed=102,
        temperature=0.3,
        response_sha256=accepted_sha,
        request_policy=request_policy,
    )
    acceptance = {
        **second,
        "timestamp_utc": "2026-07-29T00:00:01+00:00",
        "audit_event": "experiment_acceptance",
        "status": "accepted",
        "accepted_response_sha256": accepted_sha,
        "provider_attempt_sha256": canonical_sha256(second),
    }
    (root / "openrouter_calls.jsonl").write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in (first, second, acceptance)
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = {
        "event_response_sha256": {
            "2026-02-27/PM": {logical_id: accepted_sha}
        }
    }
    terminal = {"response_journal": journal.summary()}
    return signature, checkpoint, terminal


def test_response_audit_uses_each_stage_attempt_temperature_schedule(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    signature, checkpoint, terminal = _response_audit_fixture(root)
    result = validate_response_journal_audit(
        root,
        signature_payload=signature,
        checkpoint=checkpoint,
        terminal=terminal,
    )
    assert result["logical_response_count"] == 1
    schedules = list(result["temperature_schedules"].values())
    assert schedules == [[0.7, 0.3]]


def test_response_audit_rejects_fixed_point_two_when_schedule_says_point_three(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    signature, checkpoint, terminal = _response_audit_fixture(root)
    audit_path = root / "openrouter_calls.jsonl"
    rows = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    rows[1]["temperature"] = 0.2
    audit_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        CanonicalRunValidationError,
        match="differs from journal schedule",
    ):
        validate_response_journal_audit(
            root,
            signature_payload=signature,
            checkpoint=checkpoint,
            terminal=terminal,
        )
