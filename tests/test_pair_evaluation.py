from __future__ import annotations

import csv
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

import config
from twinmarket_kr.db.connection import init_sim_db
from twinmarket_kr.experiment_runtime import (
    canonical_sha256,
    file_sha256,
)
from twinmarket_kr.pair_evaluation import (
    OFF_CONDITION,
    ON_CONDITION,
    PairEvaluationError,
    RunSource,
    _validate_pair_invariants,
    _validated_fill_rows,
    finalize_realnews_community_pair,
)


def _sealed_entry(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": file_sha256(path)}


def _profile_contract() -> tuple[
    list[str],
    list[dict[str, object]],
    dict[str, int],
]:
    root = Path(config.SEALED_REAL_NEWS_BUNDLE).parent
    cohort = json.loads((root / "cohort.json").read_text(encoding="utf-8"))
    prices = json.loads((root / "prices.json").read_text(encoding="utf-8"))
    agent_ids = [str(row["agent_id"]) for row in cohort["agents"]]
    initial_cash = {
        str(row["agent_id"]): int(row["initial_cash"])
        for row in cohort["agents"]
    }
    return agent_ids, prices["events"], initial_cash


def _write_database(
    path: Path,
    *,
    condition_id: str,
    agents: list[str],
    events: list[dict[str, object]],
    initial_cash: dict[str, int],
) -> None:
    init_sim_db(path)
    portfolios = [
        (
            f"{condition_id}-portfolio-{agent_id}-0",
            agent_id,
            str(initial_cash[agent_id]),
            str(initial_cash[agent_id]),
        )
        for agent_id in agents
    ]
    fills: list[tuple[object, ...]] = []
    for turn, event in enumerate(events, start=1):
        date = str(event["date"])
        subturn = str(event["subturn"]).lower()
        price = float(event["execution_price"])
        for ordinal, agent_id in enumerate(agents, start=1):
            action = (
                "buy"
                if (turn + ordinal + (condition_id == ON_CONDITION)) % 3
                else "sell"
            )
            fill_id = f"{condition_id}-fill-{turn:03d}-{agent_id}"
            fills.append(
                (
                    fill_id,
                    agent_id,
                    turn,
                    date,
                    subturn,
                    "005930",
                    action,
                    1,
                    1,
                    price,
                    0,
                    f"{condition_id}-ltb-{turn - 1:03d}-{agent_id}",
                    f"{condition_id}-stb-{turn:03d}-{agent_id}",
                    f"{condition_id}-decision-{turn:03d}-{agent_id}",
                    canonical_sha256(
                        {
                            "condition_id": condition_id,
                            "agent_id": agent_id,
                            "turn": turn,
                            "action": action,
                            "price": price,
                        }
                    ),
                )
            )
    connection = sqlite3.connect(path)
    try:
        connection.executemany(
            """
            INSERT INTO portfolio_state (
                state_id, agent_id, turn, date, cash, positions,
                total_value, realized_pnl, total_return_rate
            ) VALUES (?, ?, 0, '2026-02-27', ?, '[]', ?, 0, 0)
            """,
            portfolios,
        )
        connection.executemany(
            """
            INSERT INTO simulation_fills (
                fill_id, agent_id, turn, date, subturn, stock_code, action,
                requested_quantity, filled_quantity, executed_price, fee,
                source_ltb_id, source_stb_id, decision_id, scientific_sha256,
                pre_portfolio_json, post_portfolio_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}')
            """,
            fills,
        )
        connection.commit()
    finally:
        connection.close()


def _write_run(
    root: Path,
    *,
    condition_id: str,
    agents: list[str],
    events: list[dict[str, object]],
    initial_cash: dict[str, int],
) -> Path:
    runtime = root / ".runtime"
    runtime.mkdir(parents=True)
    database = runtime / "committed.db"
    _write_database(
        database,
        condition_id=condition_id,
        agents=agents,
        events=events,
        initial_cash=initial_cash,
    )
    profile_root = Path(config.SEALED_REAL_NEWS_BUNDLE).parent
    study_path = profile_root / "study_spec.json"
    cohort_path = profile_root / "cohort.json"
    event_ids = [str(row["decision_event_id"]) for row in events]
    prompt_tree_sha = "a" * 64
    parameters = {
        "condition_id": condition_id,
        "community_mode": "off" if condition_id == OFF_CONDITION else "on",
        "news_treatment": "real_only",
        "seed": 2,
        "stock_code": "005930",
        "agent_count": len(agents),
        "agent_ids": agents,
        "event_ids": event_ids,
        "study_spec_sha256": file_sha256(study_path),
        "cohort_sha256": file_sha256(cohort_path),
        "prompt_bundle_sha256": prompt_tree_sha,
    }
    payload = {
        "schema_version": "integrated-event-checkpoint-v1",
        "parameters": parameters,
        "sealed_inputs": {
            "news_bundle": _sealed_entry(profile_root / "news.json"),
            "calendar_registry": _sealed_entry(
                profile_root / "calendar.json"
            ),
            "price_registry": _sealed_entry(profile_root / "prices.json"),
            "sealed_cohort": _sealed_entry(cohort_path),
            "sealed_study_spec": _sealed_entry(study_path),
        },
        "prompts": {
            "root": str((Path(config.PROMPT_DIR)).resolve()),
            "files": {},
            "tree_sha256": prompt_tree_sha,
        },
        "code": {"files": {}, "tree_sha256": "b" * 64},
        "call_policy": {
            "model": "test/model",
            "reasoning": {"effort": "none", "exclude": True},
            "provider": {
                "only": ["test-provider"],
                "order": ["test-provider"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        },
        "initial_database_sha256": "c" * 64,
    }
    signature_sha = canonical_sha256(payload)
    (root / "run_signature.json").write_text(
        json.dumps(
            {
                "signature_sha256": signature_sha,
                "signature_payload": payload,
            }
        ),
        encoding="utf-8",
    )
    database_sha = file_sha256(database)
    checkpoint = {
        "signature_sha256": signature_sha,
        "status": "complete",
        "completed_events": event_ids,
        "committed_database_sha256": database_sha,
    }
    (runtime / "checkpoint.json").write_text(
        json.dumps(checkpoint),
        encoding="utf-8",
    )
    metadata = {
        "run_id": root.name,
        "status": "complete",
        "run_signature_sha256": signature_sha,
        "completed_events": event_ids,
        "committed_database_sha256": database_sha,
        **parameters,
    }
    (root / "run_metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    (root / "run_complete.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "full_frozen_schedule": True,
                "schedule_complete": True,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_pair_finalizer_reads_common_runs_and_writes_one_hash_index(
    tmp_path: Path,
) -> None:
    agents, events, initial_cash = _profile_contract()
    off = _write_run(
        tmp_path / OFF_CONDITION,
        condition_id=OFF_CONDITION,
        agents=agents,
        events=events,
        initial_cash=initial_cash,
    )
    on = _write_run(
        tmp_path / ON_CONDITION,
        condition_id=ON_CONDITION,
        agents=agents,
        events=events,
        initial_cash=initial_cash,
    )
    target_csv = tmp_path / "individual_targets.csv"
    date_ids = list(dict.fromkeys(str(event["date"]) for event in events))
    with target_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Date", "Individuals"])
        writer.writeheader()
        for index, date_id in enumerate(date_ids):
            writer.writerow(
                {"Date": date_id, "Individuals": 1 if index % 2 == 0 else -1}
            )
    output = tmp_path / "pair_evaluation.json"
    artifact = finalize_realnews_community_pair(
        off_run_dir=off,
        on_run_dir=on,
        target_csv=target_csv,
        output_path=output,
    )

    assert output.is_file()
    assert artifact["status"] == "pass"
    assert artifact["invariant_contract"]["cohort_agent_count"] == 100
    assert artifact["invariant_contract"]["event_count"] == 196
    assert (
        artifact["invariant_contract"]["expected_fill_rows_per_arm"]
        == 19_600
    )
    assert len(artifact["schedule"]["burn_in_dates"]) == 3
    assert len(artifact["schedule"]["evaluation_dates"]) == 95
    assert len(artifact["daily_gross_signed_fill_value"]) == 98
    assert artifact["hash_index"][OFF_CONDITION][
        "canonical_fill_rows_sha256"
    ]
    body = dict(artifact)
    content_sha = body.pop("content_sha256")
    assert content_sha == canonical_sha256(body)
    assert finalize_realnews_community_pair(
        off_run_dir=off,
        on_run_dir=on,
        target_csv=target_csv,
        output_path=output,
    ) == artifact


def test_pair_invariant_rejects_seed_change_outside_community_mode(
    tmp_path: Path,
) -> None:
    base = {
        "parameters": {
            "condition_id": OFF_CONDITION,
            "community_mode": "off",
            "seed": 2,
        },
        "sealed_inputs": {"news": {"sha256": "a" * 64}},
    }
    changed = json.loads(json.dumps(base))
    changed["parameters"]["condition_id"] = ON_CONDITION
    changed["parameters"]["community_mode"] = "on"
    changed["parameters"]["seed"] = 3
    source = lambda condition, payload: RunSource(  # noqa: E731
        condition_id=condition,
        run_dir=tmp_path,
        metadata={},
        signature_payload=payload,
        checkpoint={},
        database=tmp_path / "unused.db",
    )
    with pytest.raises(PairEvaluationError, match="immutable inputs differ"):
        _validate_pair_invariants(
            source(OFF_CONDITION, base),
            source(ON_CONDITION, changed),
        )


def test_fill_validator_rejects_wrong_price_and_nonzero_fee(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fills.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE simulation_fills (
                fill_id TEXT, agent_id TEXT, turn INTEGER, date TEXT,
                subturn TEXT, stock_code TEXT, action TEXT,
                requested_quantity INTEGER, filled_quantity INTEGER,
                executed_price REAL, fee REAL, source_ltb_id TEXT,
                source_stb_id TEXT, decision_id TEXT,
                scientific_sha256 TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO simulation_fills VALUES (
                'fill-1', 'A001', 1, '2026-02-27', 'am', '005930',
                'buy', 1, 1, 99, 1, 'ltb-0', 'stb-1', 'decision-1', ?
            )
            """,
            ("a" * 64,),
        )
        connection.commit()
    event = {
        "event_id": "2026-02-27/AM",
        "turn": 1,
        "date": "2026-02-27",
        "session": "AM",
    }
    with pytest.raises(PairEvaluationError, match="fill price"):
        _validated_fill_rows(
            database,
            condition_id=OFF_CONDITION,
            agent_ids=["A001"],
            events=[event],
            prices={"2026-02-27/AM": Decimal("100")},
            stock_code="005930",
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE simulation_fills SET executed_price=100"
        )
        connection.commit()
    with pytest.raises(PairEvaluationError, match="fill fee"):
        _validated_fill_rows(
            database,
            condition_id=OFF_CONDITION,
            agent_ids=["A001"],
            events=[event],
            prices={"2026-02-27/AM": Decimal("100")},
            stock_code="005930",
        )
