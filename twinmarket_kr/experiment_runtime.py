from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from twinmarket_kr.belief_projection import render_belief_summary
from twinmarket_kr.db.connection import connect, init_sim_db

import config


# Canonical main-run tables that must never cross an experiment-base boundary.
#
# The order is child-first where the schema permits it.  The integrated
# STB/LTB/analysis/decision/fill graph deliberately contains a cycle:
# a fill points to the previously visible LTB, while the post-fill LTB points
# back to that fill and decision.  ``build_clean_experiment_base`` therefore
# defers foreign-key enforcement for the one deletion transaction and performs
# a foreign-key check after the complete graph has been removed.
#
# ``belief_history`` and ``portfolio_state`` are handled separately because
# their turn-zero rows are experiment inputs.  ``StockData`` is static market
# input and is never deleted here. The numbered main runner initializes and
# writes this canonical integrated schema only.
RUNTIME_TABLES = (
    "simulation_outcome_consumptions",
    "trade_log",
    "community_interactions",
    "community_posts",
    "community_logs",
    "simulation_trade_outcomes",
    "simulation_ltb_states",
    "simulation_fills",
    "simulation_decisions",
    "simulation_analyses",
    "simulation_stb_states",
    "TradingDetails",
    "agent_system_messages",
)

DETERMINISTIC_FAILURE_NAMES = (
    "AnalysisValidationError",
    "BeliefValidationError",
    "CommunityValidationError",
    "DecisionConstraintError",
    "DecisionValidationError",
    "LLMValidationError",
    "UnexpectedModelError",
)


class ParallelTaskError(RuntimeError):
    """Preserve every child failure so restart classification cannot miss a mixed error."""

    def __init__(self, message: str, errors: list[BaseException]) -> None:
        super().__init__(message)
        self.errors = tuple(errors)


class ExperimentCheckpointError(RuntimeError):
    """The durable event checkpoint cannot be trusted or resumed safely."""


CHECKPOINT_SCHEMA_VERSION = "integrated-event-checkpoint-v1"

_CONTROL_ARTIFACTS = frozenset(
    {
        "checkpoint.json",
        "openrouter_calls.jsonl",
        # 검증 거부 응답 발췌는 openrouter_calls.jsonl과 같은 텔레메트리다.
        # 롤백 대상에 두면 event가 실패할 때마다 그 실패의 진단 근거가 함께
        # 삭제된다. 실제로 2일 유료 실행에서 community_thinking이 10회
        # 소진으로 죽었는데 거부 응답 기록이 전부 사라져 원인 규명이
        # 불가능했다. 과학 상태가 아니므로 보존한다.
        "llm_validation_errors.jsonl",
        "paused.json",
        "run_complete.json",
        "run_metadata.json",
        "run_signature.json",
        "segment_complete.json",
    }
)
_REWRITE_ARTIFACTS = frozenset({"community_best_posts.csv"})


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        chain.append(current)
        seen.add(id(current))
        nested = getattr(current, "errors", ())
        if isinstance(nested, (tuple, list)):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return chain


def classify_restart_safety(error: BaseException) -> dict[str, Any]:
    """Permit process restarts only for clearly transient provider/transport failures."""
    chain = _exception_chain(error)
    chain_types = [type(item).__name__ for item in chain]
    combined_text = "\n".join(f"{type(item).__name__}: {item}" for item in chain)
    deterministic = sorted(
        name for name in DETERMINISTIC_FAILURE_NAMES if name in combined_text
    )
    if deterministic:
        return {
            "auto_restart_allowed": False,
            "failure_class": "deterministic_validation_or_model",
            "exception_chain": chain_types,
            "matched_markers": deterministic,
        }

    transient_markers: list[str] = []
    transient_type_names = {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "NetworkError",
        "ReadError",
        "ReadTimeout",
        "TimeoutError",
    }
    for item in chain:
        item_type = type(item).__name__
        if item_type in transient_type_names or isinstance(item, TimeoutError):
            transient_markers.append(item_type)
        status_code = getattr(item, "status_code", None)
        if status_code is None:
            response = getattr(item, "response", None)
            status_code = getattr(response, "status_code", None)
        try:
            code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            code = None
        if code in {408, 409, 429} or (code is not None and 500 <= code <= 599):
            transient_markers.append(f"http_{code}")
    if transient_markers:
        return {
            "auto_restart_allowed": True,
            "failure_class": "transient_provider_or_transport",
            "exception_chain": chain_types,
            "matched_markers": sorted(set(transient_markers)),
        }
    return {
        "auto_restart_allowed": False,
        "failure_class": "unknown_or_local_error",
        "exception_chain": chain_types,
        "matched_markers": [],
    }


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    """Hash one JSON value with a stable, fail-closed representation."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_run_signature_payload(
    *,
    parameters: Mapping[str, Any],
    input_files: Mapping[str, Path | str],
    prompt_dir: Path | str,
    code_files: Sequence[Path | str],
    call_policy: Mapping[str, Any],
    initial_database_sha256: str,
) -> dict[str, Any]:
    """Resolve every immutable input used by a restartable experiment.

    The caller supplies logical labels for sealed inputs so the manifest remains
    readable.  File contents, active prompt templates, common engine code,
    resolved experiment parameters, and the exact model/provider policy all
    contribute to the signature.  Mutable run outputs are deliberately absent.
    """

    if not initial_database_sha256:
        raise ValueError("initial_database_sha256 is required")
    resolved_inputs: dict[str, dict[str, str]] = {}
    for label, raw_path in sorted(input_files.items()):
        if not isinstance(label, str) or not label.strip():
            raise ValueError("input file labels must be non-empty strings")
        path = Path(raw_path)
        if not path.is_file():
            raise FileNotFoundError(f"Run-signature input is missing: {path}")
        resolved_inputs[label] = {
            "path": str(path.resolve()),
            "sha256": file_sha256(path),
        }

    prompts_root = Path(prompt_dir)
    if not prompts_root.is_dir():
        raise FileNotFoundError(f"Prompt directory is missing: {prompts_root}")
    prompt_files = sorted(
        path for path in prompts_root.rglob("*.txt") if path.is_file()
    )
    if not prompt_files:
        raise ExperimentCheckpointError(
            f"Prompt directory contains no production .txt templates: {prompts_root}"
        )
    prompt_manifest = {
        str(path.relative_to(prompts_root)): file_sha256(path)
        for path in prompt_files
    }

    code_manifest: dict[str, str] = {}
    for raw_path in sorted({Path(value) for value in code_files}, key=lambda value: str(value)):
        if not raw_path.is_file():
            raise FileNotFoundError(f"Run-signature code file is missing: {raw_path}")
        code_manifest[str(raw_path.resolve())] = file_sha256(raw_path)
    if not code_manifest:
        raise ValueError("At least one engine code file is required")

    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "parameters": dict(parameters),
        "sealed_inputs": resolved_inputs,
        "prompts": {
            "root": str(prompts_root.resolve()),
            "files": prompt_manifest,
            "tree_sha256": canonical_sha256(prompt_manifest),
        },
        "code": {
            "files": code_manifest,
            "tree_sha256": canonical_sha256(code_manifest),
        },
        "call_policy": dict(call_policy),
        "initial_database_sha256": str(initial_database_sha256),
    }
    # Validate JSON serializability and reject NaN/Infinity before anything is
    # written to the run directory.
    canonical_sha256(payload)
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path | str, payload: Mapping[str, Any]) -> None:
    """Durably replace one JSON control file, including its parent entry."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        dict(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def run_directory_lock(run_dir: Path | str) -> Iterator[Path]:
    """Hold an advisory process lock and always release it on every exit path."""

    directory = Path(run_dir)
    lock_path = directory.parent / f".{directory.name}.runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExperimentCheckpointError(
                f"Another simulation process is using run directory: {directory}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield lock_path
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _artifact_paths(run_dir: Path) -> list[Path]:
    if not run_dir.exists():
        return []
    return sorted(
        (
            path
            for path in run_dir.rglob("*")
            if path.is_file()
            and ".runtime" not in path.relative_to(run_dir).parts
            and path.relative_to(run_dir).as_posix() not in _CONTROL_ARTIFACTS
        ),
        key=lambda path: path.relative_to(run_dir).as_posix(),
    )


def capture_artifact_state(
    run_dir: Path | str,
    *,
    backup_dir: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Capture append-only artifact prefixes for exact interrupted-event rollback."""

    root = Path(run_dir)
    backup_root = Path(backup_dir) if backup_dir is not None else None
    if backup_root is not None:
        if backup_root.exists():
            shutil.rmtree(backup_root)
        backup_root.mkdir(parents=True, exist_ok=True)
    state: dict[str, dict[str, Any]] = {}
    for path in _artifact_paths(root):
        relative = path.relative_to(root).as_posix()
        entry: dict[str, Any] = {
            "size": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        if backup_root is not None and relative in _REWRITE_ARTIFACTS:
            backup_path = backup_root / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)
            entry["backup_path"] = str(backup_path.resolve())
        state[relative] = entry
    return state


def artifact_tree_sha256(run_dir: Path | str) -> str:
    root = Path(run_dir)
    manifest = {
        path.relative_to(root).as_posix(): {
            "size": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for path in _artifact_paths(root)
    }
    return canonical_sha256(manifest)


def restore_artifact_state(
    run_dir: Path | str,
    state: Mapping[str, Mapping[str, Any]],
) -> None:
    """Remove new files and truncate append-only files to their trusted prefix.

    A logger that rewrites an old prefix is rejected instead of being silently
    "restored" from a size alone.  The integrated event path writes append-only
    CSV/JSONL files plus event-unique JSON files, so a prefix mismatch signals a
    real contract violation.
    """

    root = Path(run_dir)
    expected = set(state)
    for path in reversed(_artifact_paths(root)):
        relative = path.relative_to(root).as_posix()
        if relative not in expected:
            path.unlink()
    for relative, raw_entry in state.items():
        path = root / relative
        if not path.is_file():
            raise ExperimentCheckpointError(
                f"Cannot restore missing pre-event artifact: {path}"
            )
        size = int(raw_entry["size"])
        expected_sha = str(raw_entry["sha256"])
        if size < 0 or path.stat().st_size < size:
            raise ExperimentCheckpointError(
                f"Artifact became shorter during an event: {path}"
            )
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            remaining = size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        if remaining or digest.hexdigest() != expected_sha:
            backup_path_value = raw_entry.get("backup_path")
            backup_path = (
                Path(str(backup_path_value))
                if backup_path_value
                else None
            )
            if (
                backup_path is None
                or not backup_path.is_file()
                or file_sha256(backup_path) != expected_sha
            ):
                raise ExperimentCheckpointError(
                    f"Artifact prefix was rewritten during an event: {path}"
                )
            shutil.copy2(backup_path, path)
            continue
        with path.open("r+b") as handle:
            handle.truncate(size)


def assert_integrated_event_state(
    db_path: Path | str,
    *,
    agent_ids: Sequence[str],
    completed_events: Sequence[Mapping[str, Any]],
    stock_code: str,
) -> str:
    """Validate the canonical STB→analysis→decision→fill→LTB event ledger.

    This intentionally does not use the legacy ``belief_history`` row-count
    contract.  ``belief_history`` remains a human/compatibility artifact; the
    six-dimensional scientific state is the hierarchical table chain below.
    """

    agents = tuple(str(value) for value in agent_ids)
    if not agents or len(agents) != len(set(agents)):
        raise ExperimentCheckpointError(
            "Integrated state validation requires unique agent IDs"
        )
    events: dict[int, dict[str, Any]] = {}
    event_ids: set[str] = set()
    for raw in completed_events:
        turn = int(raw["turn"])
        event_id = str(raw["event_id"])
        event_date = str(raw["date"])
        subturn = str(raw["subturn"]).lower()
        if (
            turn < 1
            or turn in events
            or event_id in event_ids
            or subturn not in {"am", "pm"}
            or event_id != f"{event_date}/{subturn.upper()}"
        ):
            raise ExperimentCheckpointError(
                "Completed event identities are invalid or duplicated"
            )
        events[turn] = {
            "event_id": event_id,
            "turn": turn,
            "date": event_date,
            "subturn": subturn,
        }
        event_ids.add(event_id)
    if not events:
        raise ExperimentCheckpointError(
            "At least one completed event is required for state validation"
        )
    ordered_turns = tuple(sorted(events))
    predecessor_turn = ordered_turns[0] - 1
    turn_placeholders = ",".join("?" for _ in ordered_turns)
    expected = {(agent_id, turn) for agent_id in agents for turn in ordered_turns}
    placeholders = ",".join("?" for _ in agents)

    payload: dict[str, Any] = {
        "agent_ids": list(agents),
        "events": [events[turn] for turn in ordered_turns],
        "tables": {},
    }
    with connect(db_path, read_only=True) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise ExperimentCheckpointError(
                f"Runtime DB quick_check failed: {quick_check}"
            )
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ExperimentCheckpointError(
                f"Runtime DB has foreign-key violations: {foreign_keys[:5]}"
            )

        table_specs = {
            "simulation_stb_states": ("stb_id",),
            "simulation_analyses": (
                "analysis_id",
                "source_ltb_id",
                "source_stb_id",
            ),
            "simulation_decisions": (
                "decision_id",
                "analysis_id",
                "source_ltb_id",
                "source_stb_id",
            ),
            "simulation_fills": (
                "fill_id",
                "decision_id",
                "source_ltb_id",
                "source_stb_id",
                "action",
                "filled_quantity",
                "executed_price",
            ),
        }
        rows_by_table: dict[str, list[sqlite3.Row]] = {}
        for table, id_columns in table_specs.items():
            columns = ", ".join(
                [
                    "agent_id",
                    "turn",
                    "date",
                    "subturn",
                    *id_columns,
                    "scientific_sha256",
                ]
            )
            rows = connection.execute(
                f"""
                SELECT {columns}
                FROM {table}
                WHERE turn IN ({turn_placeholders})
                ORDER BY turn, agent_id
                """,
                ordered_turns,
            ).fetchall()
            rows_by_table[table] = rows
            _assert_exact_event_rows(
                table,
                rows,
                expected=expected,
                events=events,
            )
            payload["tables"][table] = [
                {
                    column: row[column]
                    for column in (*id_columns, "scientific_sha256")
                }
                for row in rows
            ]

        ltb_rows = connection.execute(
            f"""
            SELECT agent_id, turn, date, subturn, ltb_id, parent_ltb_id,
                   source_stb_id, source_decision_id, source_fill_id,
                   scientific_sha256, human_log_sha256
            FROM simulation_ltb_states
            WHERE turn IN ({turn_placeholders}) OR turn = ?
            ORDER BY turn, agent_id
            """,
            (*ordered_turns, predecessor_turn),
        ).fetchall()
        runtime_ltb_rows = [
            row for row in ltb_rows if int(row["turn"]) in events
        ]
        _assert_exact_event_rows(
            "simulation_ltb_states",
            runtime_ltb_rows,
            expected=expected,
            events=events,
        )
        predecessor_ltb = {
            str(row["agent_id"]): row
            for row in ltb_rows
            if int(row["turn"]) == predecessor_turn
            and str(row["agent_id"]) in set(agents)
        }
        if set(predecessor_ltb) != set(agents):
            raise ExperimentCheckpointError(
                "Every selected agent requires exactly one predecessor LTB"
            )

        stb_by_key = {
            (str(row["agent_id"]), int(row["turn"])): row
            for row in rows_by_table["simulation_stb_states"]
        }
        analysis_by_key = {
            (str(row["agent_id"]), int(row["turn"])): row
            for row in rows_by_table["simulation_analyses"]
        }
        decision_by_key = {
            (str(row["agent_id"]), int(row["turn"])): row
            for row in rows_by_table["simulation_decisions"]
        }
        fill_by_key = {
            (str(row["agent_id"]), int(row["turn"])): row
            for row in rows_by_table["simulation_fills"]
        }
        ltb_by_key = {
            (str(row["agent_id"]), int(row["turn"])): row
            for row in runtime_ltb_rows
        }
        for agent_id in agents:
            parent_ltb_id = str(predecessor_ltb[agent_id]["ltb_id"])
            for turn in ordered_turns:
                key = (agent_id, turn)
                stb = stb_by_key[key]
                analysis = analysis_by_key[key]
                decision = decision_by_key[key]
                fill = fill_by_key[key]
                ltb = ltb_by_key[key]
                if (
                    str(analysis["source_ltb_id"]) != parent_ltb_id
                    or str(analysis["source_stb_id"]) != str(stb["stb_id"])
                    or str(decision["analysis_id"]) != str(analysis["analysis_id"])
                    or str(decision["source_ltb_id"]) != parent_ltb_id
                    or str(decision["source_stb_id"]) != str(stb["stb_id"])
                    or str(fill["decision_id"]) != str(decision["decision_id"])
                    or str(fill["source_ltb_id"]) != parent_ltb_id
                    or str(fill["source_stb_id"]) != str(stb["stb_id"])
                    or str(ltb["parent_ltb_id"]) != parent_ltb_id
                    or str(ltb["source_stb_id"]) != str(stb["stb_id"])
                    or str(ltb["source_decision_id"]) != str(decision["decision_id"])
                    or str(ltb["source_fill_id"]) != str(fill["fill_id"])
                ):
                    raise ExperimentCheckpointError(
                        f"Hierarchical lineage mismatch for {agent_id} turn {turn}"
                    )
                parent_ltb_id = str(ltb["ltb_id"])

        fill_contract_errors = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM simulation_fills
            WHERE turn IN ({turn_placeholders})
              AND (
                    action NOT IN ('buy', 'sell')
                 OR requested_quantity <= 0
                 OR filled_quantity <> requested_quantity
                 OR executed_price <= 0
                 OR fee <> 0
                 OR stock_code <> ?
              )
            """,
            (*ordered_turns, stock_code),
        ).fetchone()[0]
        if int(fill_contract_errors):
            raise ExperimentCheckpointError(
                f"Invalid full-fill rows: {fill_contract_errors}"
            )

        trade_rows = connection.execute(
            f"""
            SELECT agent_id, turn, date, action, quantity, executed_price, fee,
                   status, filled_quantity, analysis_id, decision_id,
                   source_ltb_id, source_stb_id, fill_id, post_fill_ltb_id
            FROM trade_log
            WHERE turn IN ({turn_placeholders})
            ORDER BY turn, agent_id
            """,
            ordered_turns,
        ).fetchall()
        _assert_exact_event_rows(
            "trade_log",
            trade_rows,
            expected=expected,
            events=events,
            has_subturn=False,
        )
        for row in trade_rows:
            key = (str(row["agent_id"]), int(row["turn"]))
            fill = fill_by_key[key]
            decision = decision_by_key[key]
            analysis = analysis_by_key[key]
            ltb = ltb_by_key[key]
            if (
                row["status"] != "filled"
                or int(row["quantity"]) < 1
                or int(row["filled_quantity"]) != int(row["quantity"])
                or float(row["executed_price"]) <= 0
                or float(row["fee"]) != 0.0
                or str(row["analysis_id"]) != str(analysis["analysis_id"])
                or str(row["decision_id"]) != str(decision["decision_id"])
                or str(row["fill_id"]) != str(fill["fill_id"])
                or str(row["post_fill_ltb_id"]) != str(ltb["ltb_id"])
                or str(row["action"]) != str(fill["action"])
                or int(row["quantity"]) != int(fill["filled_quantity"])
                or abs(
                    float(row["executed_price"])
                    - float(fill["executed_price"])
                )
                > 1e-9
            ):
                raise ExperimentCheckpointError(
                    f"trade_log/full-fill lineage mismatch for {key}"
                )

        portfolio_rows = connection.execute(
            f"""
            SELECT agent_id, turn, date, cash, positions, total_value,
                   realized_pnl, total_return_rate
            FROM portfolio_state
            WHERE agent_id IN ({placeholders})
              AND turn IN ({','.join('?' for _ in (predecessor_turn, *ordered_turns))})
            ORDER BY turn, agent_id
            """,
            (*agents, predecessor_turn, *ordered_turns),
        ).fetchall()
        expected_portfolios = {
            (agent_id, turn)
            for agent_id in agents
            for turn in (predecessor_turn, *ordered_turns)
        }
        actual_portfolios = {
            (str(row["agent_id"]), int(row["turn"]))
            for row in portfolio_rows
        }
        if (
            actual_portfolios != expected_portfolios
            or len(portfolio_rows) != len(expected_portfolios)
        ):
            raise ExperimentCheckpointError(
                "portfolio_state does not exactly cover the predecessor and "
                "every completed agent-event"
            )
        for row in portfolio_rows:
            if float(row["cash"]) < -1e-6:
                raise ExperimentCheckpointError(
                    f"Negative cash for {row['agent_id']} turn {row['turn']}"
                )
            try:
                positions = json.loads(str(row["positions"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ExperimentCheckpointError(
                    "portfolio_state contains invalid positions JSON"
                ) from exc
            if not isinstance(positions, list) or any(
                not isinstance(position, dict)
                or int(position.get("quantity") or 0) < 0
                for position in positions
            ):
                raise ExperimentCheckpointError(
                    "portfolio_state contains an invalid/negative position"
                )

        expected_trade_count = len(expected)
        trading_details_count = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM TradingDetails
                WHERE user_id IN ({placeholders})
                  AND date IN ({','.join('?' for _ in {str(events[turn]['date']) for turn in ordered_turns})})
                """,
                (*agents, *sorted({str(events[turn]['date']) for turn in ordered_turns})),
            ).fetchone()[0]
        )
        if trading_details_count != expected_trade_count:
            raise ExperimentCheckpointError(
                "TradingDetails count differs from the canonical full-fill ledger"
            )
        fabricated_messages = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_system_messages WHERE message_type <> 'advisor'"
            ).fetchone()[0]
        )
        if fabricated_messages:
            raise ExperimentCheckpointError(
                "System/recovery messages were written into scientific state"
            )

        event_placeholders = ",".join("?" for _ in event_ids)
        missing_consumptions = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM simulation_trade_outcomes AS outcome
                LEFT JOIN simulation_outcome_consumptions AS consumption
                  ON consumption.outcome_id = outcome.outcome_id
                WHERE outcome.status = 'matured'
                  AND outcome.available_from_event_id IN ({event_placeholders})
                  AND consumption.outcome_id IS NULL
                """,
                tuple(sorted(event_ids)),
            ).fetchone()[0]
        )
        if missing_consumptions:
            raise ExperimentCheckpointError(
                "Matured due outcomes were not consumed by post-fill LTB"
            )
        future_consumptions = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM simulation_outcome_consumptions
                WHERE consumed_at_event_id > ?
                """,
                (str(events[ordered_turns[-1]]["event_id"]),),
            ).fetchone()[0]
        )
        if future_consumptions:
            raise ExperimentCheckpointError(
                "Outcome consumption references an uncompleted event"
            )

        payload["tables"]["simulation_ltb_states"] = [
            {
                "ltb_id": row["ltb_id"],
                "scientific_sha256": row["scientific_sha256"],
                "human_log_sha256": row["human_log_sha256"],
            }
            for row in ltb_rows
            if str(row["agent_id"]) in set(agents)
            and int(row["turn"]) in events
        ]
        payload["tables"]["trade_log"] = [
            {
                "agent_id": row["agent_id"],
                "turn": row["turn"],
                "fill_id": row["fill_id"],
                "post_fill_ltb_id": row["post_fill_ltb_id"],
            }
            for row in trade_rows
        ]
        payload["latest_portfolios"] = [
            dict(row)
            for row in portfolio_rows
            if int(row["turn"]) == ordered_turns[-1]
        ]
    return canonical_sha256(payload)


def _assert_exact_event_rows(
    table: str,
    rows: Sequence[sqlite3.Row],
    *,
    expected: set[tuple[str, int]],
    events: Mapping[int, Mapping[str, Any]],
    has_subturn: bool = True,
) -> None:
    actual = {
        (str(row["agent_id"]), int(row["turn"]))
        for row in rows
    }
    if actual != expected or len(rows) != len(expected):
        raise ExperimentCheckpointError(
            f"{table} does not exactly cover every completed agent-event; "
            f"missing={sorted(expected - actual)[:10]} "
            f"unexpected={sorted(actual - expected)[:10]}"
        )
    for row in rows:
        turn = int(row["turn"])
        event = events[turn]
        if str(row["date"]) != str(event["date"]):
            raise ExperimentCheckpointError(
                f"{table} date differs from frozen turn {turn}"
            )
        if has_subturn and str(row["subturn"]) != str(event["subturn"]):
            raise ExperimentCheckpointError(
                f"{table} subturn differs from frozen turn {turn}"
            )
        if "scientific_sha256" in row.keys():
            digest = str(row["scientific_sha256"])
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ExperimentCheckpointError(
                    f"{table} has an invalid scientific SHA-256"
                )


def backup_database(source: Path | str, target: Path | str) -> None:
    source_path = Path(source)
    target_path = Path(target)
    if not source_path.exists():
        raise FileNotFoundError(f"Simulation source database not found: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(target_path) + ".tmp")
    for candidate in (
        temporary,
        Path(str(temporary) + "-wal"),
        Path(str(temporary) + "-shm"),
    ):
        if candidate.exists():
            candidate.unlink()
    with sqlite3.connect(source_path) as src:
        quick_check = str(src.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise RuntimeError(f"Source SQLite quick_check failed for {source_path}: {quick_check}")
        with sqlite3.connect(temporary) as dst:
            src.backup(dst)
    for companion in (Path(str(target_path) + "-wal"), Path(str(target_path) + "-shm")):
        if companion.exists():
            companion.unlink()
    temporary.replace(target_path)


class EventCheckpointRuntime:
    """Crash-safe completed-prefix coordinator for the numbered simulation runner.

    One event is atomic.  AM commits by itself; a PM invocation must include its
    post-fill community lifecycle before this coordinator is asked to commit.
    The durable ``committed.db`` is always the database state for exactly the
    checkpoint's completed event prefix.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        runtime_db: Path | str,
        event_ids: Sequence[str],
        signature_payload: Mapping[str, Any],
        response_journal: Any | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.runtime_dir = self.run_dir / ".runtime"
        self.runtime_db = Path(runtime_db)
        self.event_ids = tuple(str(value) for value in event_ids)
        if not self.event_ids or any(not value for value in self.event_ids):
            raise ValueError("event_ids must contain non-empty values")
        if len(self.event_ids) != len(set(self.event_ids)):
            raise ValueError("event_ids must be unique")
        self.signature_payload = dict(signature_payload)
        self.response_journal = response_journal
        self.signature_sha256 = canonical_sha256(self.signature_payload)
        self.checkpoint_path = self.runtime_dir / "checkpoint.json"
        self.committed_db = self.runtime_dir / "committed.db"
        self.pending_db = self.runtime_dir / "pending_commit.db"
        self.artifact_backup_dir = self.runtime_dir / "artifact_rollback"
        self.signature_path = self.run_dir / "run_signature.json"

    def initialize_new(self) -> dict[str, Any]:
        """Create the first durable prefix after the runtime DB was copied."""

        if self.checkpoint_path.exists() or self.signature_path.exists():
            raise FileExistsError(
                f"Run directory already contains checkpoint state: {self.run_dir}"
            )
        if not self.runtime_db.is_file():
            raise FileNotFoundError(f"Runtime database is missing: {self.runtime_db}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        backup_database(self.runtime_db, self.committed_db)
        committed_sha = file_sha256(self.committed_db)
        signature_record = {
            "signature_sha256": self.signature_sha256,
            "signature_payload": self.signature_payload,
        }
        atomic_write_json(self.signature_path, signature_record)
        checkpoint = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "signature_sha256": self.signature_sha256,
            "runtime_db": str(self.runtime_db.resolve()),
            "event_ids": list(self.event_ids),
            "completed_events": [],
            "event_state_sha256": {},
            "event_integrity_sha256": {},
            "event_response_sha256": {},
            "committed_database_sha256": committed_sha,
            "artifact_tree_sha256": artifact_tree_sha256(self.run_dir),
            "inflight_event": None,
            "status": "running",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }
        self._write_checkpoint(checkpoint)
        return checkpoint

    def open_for_resume(self) -> dict[str, Any]:
        """Validate the immutable contract and recover an interrupted boundary."""

        checkpoint = self._load_checkpoint()
        signature_record = self._load_json_object(
            self.signature_path,
            "run signature",
        )
        if (
            signature_record.get("signature_sha256") != self.signature_sha256
            or signature_record.get("signature_payload") != self.signature_payload
            or checkpoint.get("signature_sha256") != self.signature_sha256
        ):
            raise ExperimentCheckpointError(
                "Run inputs, prompts, code, model policy, or parameters differ "
                "from the immutable run signature"
            )
        if checkpoint.get("runtime_db") != str(self.runtime_db.resolve()):
            raise ExperimentCheckpointError(
                "Runtime database path differs from the checkpoint"
            )
        if checkpoint.get("event_ids") != list(self.event_ids):
            raise ExperimentCheckpointError(
                "Requested event schedule differs from the checkpoint"
            )
        self._validate_completed_prefix(checkpoint)
        return self.recover()

    def recover(self) -> dict[str, Any]:
        """Restore a running event or finish a durable commit decision."""

        checkpoint = self._load_checkpoint()
        self._validate_completed_prefix(checkpoint)
        inflight = checkpoint.get("inflight_event")
        if inflight is None:
            self._restore_committed_database(checkpoint)
            expected_artifacts = str(checkpoint.get("artifact_tree_sha256") or "")
            observed_artifacts = artifact_tree_sha256(self.run_dir)
            if expected_artifacts != observed_artifacts:
                raise ExperimentCheckpointError(
                    "Run artifacts differ from the last completed event prefix"
                )
            return checkpoint
        if not isinstance(inflight, dict):
            raise ExperimentCheckpointError("Invalid inflight_event checkpoint")
        state = str(inflight.get("state") or "")
        if state == "running":
            self._restore_committed_database(checkpoint)
            raw_artifacts = inflight.get("artifact_state")
            if not isinstance(raw_artifacts, dict):
                raise ExperimentCheckpointError(
                    "Running event lacks its pre-event artifact state"
                )
            restore_artifact_state(self.run_dir, raw_artifacts)
            self._remove_artifact_backup()
            checkpoint["inflight_event"] = None
            checkpoint["status"] = "paused"
            checkpoint["artifact_tree_sha256"] = artifact_tree_sha256(self.run_dir)
            checkpoint["last_recovery"] = {
                "event_id": inflight.get("event_id"),
                "mode": "rollback_running_event",
                "recovered_at": _utc_now(),
            }
            self._write_checkpoint(checkpoint)
            return checkpoint
        if state == "commit_decided":
            self._finish_commit_decision(checkpoint)
            return self._load_checkpoint()
        raise ExperimentCheckpointError(
            f"Unknown inflight event state: {state!r}"
        )

    def begin_event(self, event_id: str) -> dict[str, Any]:
        checkpoint = self._load_checkpoint()
        self._validate_completed_prefix(checkpoint)
        if checkpoint.get("inflight_event") is not None:
            raise ExperimentCheckpointError(
                "Recover the prior inflight event before starting another"
            )
        expected = self.next_event(checkpoint)
        if expected is None:
            raise ExperimentCheckpointError("Every scheduled event is already complete")
        if str(event_id) != expected:
            raise ExperimentCheckpointError(
                f"Events must execute as a contiguous prefix: expected {expected}, got {event_id}"
            )
        self._restore_committed_database(checkpoint)
        current_artifact_sha = artifact_tree_sha256(self.run_dir)
        if current_artifact_sha != checkpoint.get("artifact_tree_sha256"):
            raise ExperimentCheckpointError(
                "Run artifacts changed after the last completed event"
            )
        attempts = dict(checkpoint.get("event_attempt_counts") or {})
        attempt_number = int(attempts.get(expected, 0)) + 1
        attempts[expected] = attempt_number
        inflight = {
            "state": "running",
            "event_id": expected,
            "attempt_number": attempt_number,
            "phase_attempt_id": uuid.uuid4().hex,
            "artifact_state": capture_artifact_state(
                self.run_dir,
                backup_dir=self.artifact_backup_dir,
            ),
            "started_at": _utc_now(),
        }
        checkpoint["event_attempt_counts"] = attempts
        checkpoint["inflight_event"] = inflight
        checkpoint["status"] = "running"
        checkpoint.pop("last_error", None)
        self._write_checkpoint(checkpoint)
        return inflight

    def prepare_event_commit(
        self,
        event_id: str,
        *,
        integrity_sha256: str | None = None,
        logical_response_digests: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Write the post-event DB image before making commit irrevocable."""

        checkpoint = self._load_checkpoint()
        inflight = self._require_inflight(checkpoint, event_id, state="running")
        backup_database(self.runtime_db, self.pending_db)
        pending_sha = file_sha256(self.pending_db)
        artifact_sha = artifact_tree_sha256(self.run_dir)
        if integrity_sha256 is not None and (
            len(integrity_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in integrity_sha256
            )
        ):
            raise ExperimentCheckpointError(
                "Event integrity digest must be a lowercase SHA-256"
            )
        response_digests = dict(logical_response_digests or {})
        for logical_id, digest in response_digests.items():
            if (
                not isinstance(logical_id, str)
                or not logical_id
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ExperimentCheckpointError(
                    "Logical response IDs and digests must be non-empty/SHA-256"
                )
        if self.response_journal is not None:
            if logical_response_digests is None:
                raise ExperimentCheckpointError(
                    "Journal-integrated commits require logical response digests"
                )
            self.response_journal.assert_event_digests(
                event_id,
                response_digests,
            )
        commit_decision = {
            **inflight,
            "state": "commit_decided",
            "pending_database_sha256": pending_sha,
            "artifact_tree_sha256": artifact_sha,
            "integrity_sha256": integrity_sha256,
            "logical_response_digests": response_digests,
            "commit_decided_at": _utc_now(),
        }
        checkpoint["inflight_event"] = commit_decision
        self._write_checkpoint(checkpoint)
        return commit_decision

    def finish_event_commit(self, event_id: str) -> dict[str, Any]:
        checkpoint = self._load_checkpoint()
        self._require_inflight(
            checkpoint,
            event_id,
            state="commit_decided",
        )
        self._finish_commit_decision(checkpoint)
        return self._load_checkpoint()

    def commit_event(self, event_id: str) -> dict[str, Any]:
        self.prepare_event_commit(event_id)
        return self.finish_event_commit(event_id)

    def pause_running_event(
        self,
        event_id: str,
        error: BaseException,
    ) -> dict[str, Any]:
        """Rollback only before the durable commit-decision boundary."""

        checkpoint = self._load_checkpoint()
        inflight = self._require_inflight(
            checkpoint,
            event_id,
            state="running",
        )
        self._restore_committed_database(checkpoint)
        raw_artifacts = inflight.get("artifact_state")
        if not isinstance(raw_artifacts, dict):
            raise ExperimentCheckpointError(
                "Running event lacks its pre-event artifact state"
            )
        restore_artifact_state(self.run_dir, raw_artifacts)
        self._remove_artifact_backup()
        checkpoint["inflight_event"] = None
        checkpoint["status"] = "paused"
        checkpoint["artifact_tree_sha256"] = artifact_tree_sha256(self.run_dir)
        checkpoint["last_error"] = {
            "event_id": event_id,
            "type": type(error).__name__,
            "message": str(error),
            **classify_restart_safety(error),
            "paused_at": _utc_now(),
        }
        self._write_checkpoint(checkpoint)
        return checkpoint

    def mark_complete(self, *, full_schedule: bool = True) -> dict[str, Any]:
        checkpoint = self._load_checkpoint()
        self._validate_completed_prefix(checkpoint)
        if checkpoint.get("inflight_event") is not None:
            raise ExperimentCheckpointError(
                "Cannot complete a run with an inflight event"
            )
        if checkpoint.get("completed_events") != list(self.event_ids):
            raise ExperimentCheckpointError(
                "Cannot complete a run before its full event prefix"
            )
        checkpoint["status"] = (
            "complete" if full_schedule else "segment_complete"
        )
        checkpoint["completed_at"] = _utc_now()
        self._write_checkpoint(checkpoint)
        return checkpoint

    def next_event(self, checkpoint: Mapping[str, Any] | None = None) -> str | None:
        current = dict(checkpoint or self._load_checkpoint())
        self._validate_completed_prefix(current)
        index = len(current.get("completed_events") or [])
        return self.event_ids[index] if index < len(self.event_ids) else None

    def checkpoint(self) -> dict[str, Any]:
        return self._load_checkpoint()

    def _finish_commit_decision(self, checkpoint: dict[str, Any]) -> None:
        inflight = checkpoint.get("inflight_event")
        if not isinstance(inflight, dict):
            raise ExperimentCheckpointError("Commit decision is absent")
        event_id = str(inflight.get("event_id") or "")
        self._require_inflight(
            checkpoint,
            event_id,
            state="commit_decided",
        )
        expected_artifact_sha = str(
            inflight.get("artifact_tree_sha256") or ""
        )
        if artifact_tree_sha256(self.run_dir) != expected_artifact_sha:
            raise ExperimentCheckpointError(
                f"Artifacts changed after commit decision for {event_id}"
            )
        expected_db_sha = str(
            inflight.get("pending_database_sha256") or ""
        )
        source: Path | None = None
        for candidate in (self.pending_db, self.committed_db):
            if candidate.is_file() and file_sha256(candidate) == expected_db_sha:
                source = candidate
                break
        if source is None:
            raise ExperimentCheckpointError(
                f"Committed database image is unavailable for {event_id}"
            )
        raw_response_digests = inflight.get("logical_response_digests", {})
        if not isinstance(raw_response_digests, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_response_digests.items()
        ):
            raise ExperimentCheckpointError(
                f"Commit decision has invalid response digests for {event_id}"
            )
        response_digests = {
            str(key): str(value)
            for key, value in raw_response_digests.items()
        }
        if self.response_journal is not None:
            self.response_journal.assert_event_digests(
                event_id,
                response_digests,
            )
            self.response_journal.mark_committed(response_digests)
        if source == self.pending_db:
            os.replace(self.pending_db, self.committed_db)
            directory_fd = os.open(self.runtime_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        backup_database(self.committed_db, self.runtime_db)
        completed = list(checkpoint.get("completed_events") or [])
        if self.event_ids[len(completed)] != event_id:
            raise ExperimentCheckpointError(
                f"Commit decision is outside the completed prefix: {event_id}"
            )
        completed.append(event_id)
        state_hashes = dict(checkpoint.get("event_state_sha256") or {})
        state_hashes[event_id] = expected_db_sha
        integrity_hashes = dict(
            checkpoint.get("event_integrity_sha256") or {}
        )
        integrity_sha = inflight.get("integrity_sha256")
        if integrity_sha is not None:
            integrity_hashes[event_id] = str(integrity_sha)
        checkpoint["completed_events"] = completed
        checkpoint["event_state_sha256"] = state_hashes
        checkpoint["event_integrity_sha256"] = integrity_hashes
        response_hashes = dict(
            checkpoint.get("event_response_sha256") or {}
        )
        response_hashes[event_id] = response_digests
        checkpoint["event_response_sha256"] = response_hashes
        checkpoint["committed_database_sha256"] = expected_db_sha
        checkpoint["artifact_tree_sha256"] = expected_artifact_sha
        checkpoint["inflight_event"] = None
        checkpoint["status"] = (
            "events_complete"
            if len(completed) == len(self.event_ids)
            else "running"
        )
        checkpoint["last_committed_event"] = event_id
        checkpoint["last_committed_at"] = _utc_now()
        self._write_checkpoint(checkpoint)
        self._remove_artifact_backup()

    def _restore_committed_database(self, checkpoint: Mapping[str, Any]) -> None:
        expected_sha = str(
            checkpoint.get("committed_database_sha256") or ""
        )
        if (
            not self.committed_db.is_file()
            or file_sha256(self.committed_db) != expected_sha
        ):
            raise ExperimentCheckpointError(
                "Durable committed database image is missing or corrupted"
            )
        backup_database(self.committed_db, self.runtime_db)

    def _validate_completed_prefix(self, checkpoint: Mapping[str, Any]) -> None:
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ExperimentCheckpointError("Unsupported checkpoint schema")
        completed = checkpoint.get("completed_events")
        if (
            not isinstance(completed, list)
            or any(not isinstance(value, str) for value in completed)
            or completed != list(self.event_ids[: len(completed)])
        ):
            raise ExperimentCheckpointError(
                "Completed events are not a contiguous scheduled prefix"
            )

    def _require_inflight(
        self,
        checkpoint: Mapping[str, Any],
        event_id: str,
        *,
        state: str,
    ) -> dict[str, Any]:
        inflight = checkpoint.get("inflight_event")
        if (
            not isinstance(inflight, dict)
            or inflight.get("event_id") != event_id
            or inflight.get("state") != state
        ):
            raise ExperimentCheckpointError(
                f"Expected {state} checkpoint for event {event_id}"
            )
        return inflight

    def _load_checkpoint(self) -> dict[str, Any]:
        return self._load_json_object(self.checkpoint_path, "checkpoint")

    def _remove_artifact_backup(self) -> None:
        if self.artifact_backup_dir.exists():
            shutil.rmtree(self.artifact_backup_dir)

    @staticmethod
    def _load_json_object(path: Path, label: str) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(f"{label} is missing: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExperimentCheckpointError(
                f"{label} is not valid JSON: {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise ExperimentCheckpointError(
                f"{label} must be a JSON object: {path}"
            )
        return payload

    def _write_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        checkpoint["updated_at"] = _utc_now()
        atomic_write_json(self.checkpoint_path, checkpoint)


def build_clean_experiment_base(
    source: Path | str,
    target: Path | str,
    *,
    overwrite: bool = False,
    initial_agents: Sequence[Mapping[str, Any]] | None = None,
    instrument_name: str = "삼성전자",
) -> dict[str, Any]:
    """Create a base containing market data and deterministic turn-zero state.

    When ``initial_agents`` is omitted, this compatibility helper preserves
    the source database's turn-zero belief and portfolio rows while removing
    every runtime row.  The canonical numbered pipeline always supplies the
    sealed cohort: existing turn-zero rows are then discarded and rebuilt
    deterministically without an LLM call.
    """
    source_path = Path(source)
    target_path = Path(target)
    if source_path.resolve() == target_path.resolve():
        raise ValueError("Experiment base output must differ from the source simulation DB")
    if target_path.exists() and not overwrite:
        raise FileExistsError(f"Experiment base already exists: {target_path}")
    if target_path.exists():
        target_path.unlink()
    for companion in (Path(str(target_path) + "-wal"), Path(str(target_path) + "-shm")):
        if companion.exists():
            companion.unlink()
    backup_database(source_path, target_path)
    init_sim_db(target_path)
    with sqlite3.connect(target_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        # ``defer_foreign_keys`` applies to the current transaction.  Start it
        # explicitly so Python's implicit BEGIN cannot reset the pragma before
        # the cyclic LTB/fill graph is fully removed.
        connection.execute("BEGIN")
        connection.execute("PRAGMA defer_foreign_keys = ON")
        for table in RUNTIME_TABLES:
            connection.execute(f"DELETE FROM {table}")
        if initial_agents is None:
            connection.execute("DELETE FROM belief_history WHERE turn <> 0")
            connection.execute("DELETE FROM portfolio_state WHERE turn <> 0")
        else:
            connection.execute("DELETE FROM belief_history")
            connection.execute("DELETE FROM portfolio_state")
            _insert_deterministic_turn_zero(
                connection,
                agents=initial_agents,
                instrument_name=instrument_name,
            )
        sequence_tables = (
            "TradingDetails",
            "agent_system_messages",
            "community_interactions",
            "community_posts",
            "community_logs",
        )
        placeholders = ",".join("?" for _ in sequence_tables)
        connection.execute(
            f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
            sequence_tables,
        )
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_violations:
            raise RuntimeError(
                "Clean experiment base has foreign-key violations after runtime "
                f"deletion: {foreign_key_violations[:5]}"
            )
        connection.commit()
    report = validate_clean_experiment_base(
        target_path,
        expected_agents=initial_agents,
    )
    report["source_db"] = str(source_path.resolve())
    report["base_db"] = str(target_path.resolve())
    report["sha256"] = file_sha256(target_path)
    report["turn_zero_policy"] = (
        "deterministic_sealed_cohort_v1"
        if initial_agents is not None
        else "preserve_source_turn_zero_compatibility"
    )
    return report


def deterministic_initial_belief(
    agent: Mapping[str, Any],
    *,
    instrument_name: str = "삼성전자",
) -> dict[str, str]:
    """Return the versioned, no-LLM LTB₀ compatibility projection."""

    instrument = str(instrument_name).strip()
    if not instrument:
        raise ValueError("instrument_name must be a non-empty string")
    strategy = str(agent.get("strategy") or "value").strip().lower()
    if strategy == "technical":
        dim_2 = "기술적 흐름과 거래량 신호가 확인되기 전까지 중립적으로 본다."
    else:
        dim_2 = (
            "장기 가치는 보지만 현재 가격의 저평가 여부를 확인해야 한다."
        )
    dimensions = {
        "dim_1": (
            f"초기에는 {instrument}의 한 달 방향을 중립으로 보며 "
            "확인된 신호를 기다린다."
        ),
        "dim_2": dim_2,
        "dim_3": "해당 기업과 업종의 업황, 환율, 금리 흐름이 판단의 핵심 변수다.",
        "dim_4": "시장 심리가 과열되면 신중하고, 위축되면 기회를 찾는 태도다.",
        "dim_5": (
            "뉴스는 제목과 핵심 내용을 확인하되 자신의 투자 성향에 "
            "맞게 해석한다."
        ),
        "dim_6": (
            "초기 판단에는 불확실성이 있어 현금 관리와 원칙 준수를 중시한다."
        ),
    }
    return {
        **dimensions,
        # EXPERIMENT_DESIGN.md 7.3절: belief_summary는 저장된 여섯 차원에서
        # 결정론적으로 렌더링하는 사람용 projection이다. post-fill LTB와 같은
        # renderer를 써서 LTB₀만 다른 규칙으로 채워지지 않게 한다.
        "belief_summary": render_belief_summary(dimensions),
        # LTB₀은 parent LTB가 없어 before/after 렌더링 대상이 아니다.
        "view_change": "initial",
    }


def _insert_deterministic_turn_zero(
    connection: sqlite3.Connection,
    *,
    agents: Sequence[Mapping[str, Any]],
    instrument_name: str,
) -> None:
    if not agents:
        raise ValueError("initial_agents must not be empty")
    observed_ids: set[str] = set()
    for raw_agent in agents:
        agent_id = str(raw_agent.get("agent_id") or "").strip()
        if not agent_id or agent_id in observed_ids:
            raise ValueError(
                "initial_agents must have non-empty unique agent_id values"
            )
        observed_ids.add(agent_id)
        try:
            initial_cash = float(raw_agent["ini_cash"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"initial agent {agent_id} has invalid ini_cash"
            ) from exc
        if initial_cash <= 0:
            raise ValueError(
                f"initial agent {agent_id} must have positive ini_cash"
            )
        belief = deterministic_initial_belief(
            raw_agent,
            instrument_name=instrument_name,
        )
        connection.execute(
            """
            INSERT INTO belief_history(
                belief_id, agent_id, turn, date,
                dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                belief_summary, view_change
            )
            VALUES (?, ?, 0, 't000', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"belief_{agent_id}_t000",
                agent_id,
                *(belief[f"dim_{index}"] for index in range(1, 7)),
                belief["belief_summary"],
                belief["view_change"],
            ),
        )
        connection.execute(
            """
            INSERT INTO portfolio_state(
                state_id, agent_id, turn, date, cash, positions, total_value,
                realized_pnl, total_return_rate
            )
            VALUES (?, ?, 0, 't000', ?, '[]', ?, 0, 0)
            """,
            (
                f"ps_{agent_id}_t000",
                agent_id,
                initial_cash,
                initial_cash,
            ),
        )


def validate_clean_experiment_base(
    path: Path | str,
    *,
    expected_agents: Sequence[Mapping[str, Any]] | None = None,
    agent_db: Path | str | None = None,
    expected_stock_code: str | None = None,
    expected_trading_dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    db_path = Path(path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in RUNTIME_TABLES
        }
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        belief_turns = connection.execute(
            "SELECT MIN(turn), MAX(turn), COUNT(*), COUNT(DISTINCT agent_id) FROM belief_history"
        ).fetchone()
        portfolio_turns = connection.execute(
            "SELECT MIN(turn), MAX(turn), COUNT(*), COUNT(DISTINCT agent_id) FROM portfolio_state"
        ).fetchone()
        portfolio_rows = connection.execute(
            """
            SELECT agent_id, cash, positions, total_value, realized_pnl, total_return_rate
            FROM portfolio_state
            WHERE turn = 0
            ORDER BY agent_id
            """
        ).fetchall()
        belief_rows = connection.execute(
            """
            SELECT agent_id, dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                   belief_summary, view_change
            FROM belief_history
            WHERE turn = 0
            ORDER BY agent_id
            """
        ).fetchall()
        stock_rows = int(connection.execute("SELECT COUNT(*) FROM StockData").fetchone()[0])
        target_stock_dates = (
            {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT date
                    FROM StockData
                    WHERE stock_id = ?
                    ORDER BY date
                    """,
                    (str(expected_stock_code),),
                ).fetchall()
            }
            if expected_stock_code is not None
            else set()
        )
    nonempty_runtime = {table: count for table, count in counts.items() if count}
    if nonempty_runtime:
        raise RuntimeError(f"Experiment base contains runtime rows: {nonempty_runtime}")
    if foreign_key_violations:
        raise RuntimeError(
            "Experiment base contains foreign-key violations: "
            f"{foreign_key_violations[:5]}"
        )
    if not belief_turns or belief_turns[0] != 0 or belief_turns[1] != 0:
        raise RuntimeError(f"Experiment base belief state is not turn zero only: {belief_turns}")
    if not portfolio_turns or portfolio_turns[0] != 0 or portfolio_turns[1] != 0:
        raise RuntimeError(f"Experiment base portfolio state is not turn zero only: {portfolio_turns}")
    if int(belief_turns[3]) != int(portfolio_turns[3]):
        raise RuntimeError(
            "Experiment base agent coverage differs between belief and portfolio state: "
            f"belief={belief_turns[3]} portfolio={portfolio_turns[3]}"
        )
    if int(belief_turns[2]) != int(belief_turns[3]):
        raise RuntimeError("Experiment base contains duplicate turn-zero belief rows")
    if int(portfolio_turns[2]) != int(portfolio_turns[3]):
        raise RuntimeError("Experiment base contains duplicate turn-zero portfolio rows")
    belief_agents = {str(row[0]) for row in belief_rows}
    portfolio_agents = {str(row[0]) for row in portfolio_rows}
    if belief_agents != portfolio_agents:
        raise RuntimeError("Experiment base belief and portfolio agent IDs differ")
    empty_beliefs = [
        str(row[0])
        for row in belief_rows
        if any(not str(value or "").strip() for value in row[1:])
    ]
    if empty_beliefs:
        raise RuntimeError(
            "Experiment base has incomplete initial belief dimensions/summary/view_change: "
            f"{empty_beliefs[:5]}"
        )
    invalid_positions: list[str] = []
    invalid_financial_state: list[str] = []
    for agent_id, cash, positions, total_value, realized_pnl, total_return_rate in portfolio_rows:
        try:
            decoded_positions = json.loads(positions or "[]")
        except (TypeError, json.JSONDecodeError):
            decoded_positions = None
        if not isinstance(decoded_positions, list) or any(
            not isinstance(position, dict) or int(position.get("quantity") or 0) != 0
            for position in (decoded_positions or [])
        ):
            invalid_positions.append(str(agent_id))
        if (
            float(cash) <= 0
            or abs(float(total_value) - float(cash)) > 1e-6
            or abs(float(realized_pnl)) > 1e-9
            or abs(float(total_return_rate)) > 1e-9
        ):
            invalid_financial_state.append(str(agent_id))
    if invalid_positions:
        raise RuntimeError(
            f"Experiment base turn-zero portfolios contain positions: {invalid_positions[:5]}"
        )
    if invalid_financial_state:
        raise RuntimeError(
            "Experiment base turn-zero cash/value/PnL is inconsistent: "
            f"{invalid_financial_state[:5]}"
        )
    if expected_agents is not None:
        initial_cash_by_agent = {
            str(agent["agent_id"]): float(agent["ini_cash"])
            for agent in expected_agents
        }
        if len(initial_cash_by_agent) != len(expected_agents):
            raise RuntimeError(
                "Expected experiment-base cohort has duplicate agent IDs"
            )
        agent_source_label = "supplied sealed cohort"
    else:
        resolved_agent_db = Path(agent_db or config.SYS_100_DB)
        if not resolved_agent_db.exists():
            raise FileNotFoundError(
                f"Agent source DB is missing: {resolved_agent_db}"
            )
        with sqlite3.connect(resolved_agent_db) as agent_connection:
            initial_cash_by_agent = {
                str(agent_id): float(initial_cash)
                for agent_id, initial_cash in agent_connection.execute(
                    "SELECT agent_id, ini_cash FROM agents"
                ).fetchall()
            }
        agent_source_label = str(resolved_agent_db)
    if set(initial_cash_by_agent) != portfolio_agents:
        raise RuntimeError(
            "Experiment base turn-zero cohort differs from its agent source: "
            f"base={len(portfolio_agents)} expected={len(initial_cash_by_agent)} "
            f"source={agent_source_label}"
        )
    cash_mismatches = [
        str(agent_id)
        for agent_id, cash, *_rest in portfolio_rows
        if abs(float(cash) - initial_cash_by_agent[str(agent_id)]) > 1e-6
    ]
    if cash_mismatches:
        raise RuntimeError(
            f"Experiment base initial cash differs from persona cohort: {cash_mismatches[:5]}"
        )
    if stock_rows <= 0:
        raise RuntimeError("Experiment base contains no StockData rows")
    if expected_stock_code is not None and not target_stock_dates:
        raise RuntimeError(
            "Experiment base contains no StockData rows for sealed stock_code "
            f"{expected_stock_code}"
        )
    if expected_trading_dates is not None:
        required_dates = {str(value) for value in expected_trading_dates}
        if not required_dates:
            raise ValueError("expected_trading_dates must not be empty")
        missing_dates = sorted(required_dates - target_stock_dates)
        if missing_dates:
            raise RuntimeError(
                "Experiment base is missing sealed stock/date rows: "
                f"{missing_dates[:10]}"
            )
    return {
        "status": "pass",
        "stock_rows": stock_rows,
        "turn_zero_beliefs": int(belief_turns[2]),
        "turn_zero_portfolios": int(portfolio_turns[2]),
        "agent_count": int(belief_turns[3]),
        "initial_cash_values": sorted({float(row[1]) for row in portfolio_rows}),
        "initial_positions_empty": True,
        "runtime_counts": counts,
    }


def validate_warm_experiment_base(
    path: Path | str,
    *,
    expected_agent_ids: Sequence[str],
    boundary_turn: int,
    boundary_date: str,
    community_mode: str,
) -> dict[str, Any]:
    """Fail closed unless a canonical boundary DB can continue at the next turn."""

    db_path = Path(path)
    agent_ids = tuple(str(value) for value in expected_agent_ids)
    if not db_path.is_file() or not agent_ids or len(agent_ids) != len(set(agent_ids)):
        raise RuntimeError("Warm base requires a real DB and unique expected agents")
    placeholders = ",".join("?" for _ in agent_ids)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick.lower() != "ok" or foreign_keys:
            raise RuntimeError("Warm base database integrity check failed")
        max_fill = connection.execute("SELECT MAX(turn) FROM simulation_fills").fetchone()[0]
        max_ltb = connection.execute("SELECT MAX(turn) FROM simulation_ltb_states").fetchone()[0]
        max_portfolio = connection.execute("SELECT MAX(turn) FROM portfolio_state").fetchone()[0]
        if (max_fill, max_ltb, max_portfolio) != (boundary_turn, boundary_turn, boundary_turn):
            raise RuntimeError(
                "Warm base boundary turn differs: "
                f"fill={max_fill} ltb={max_ltb} portfolio={max_portfolio} expected={boundary_turn}"
            )
        for table in ("simulation_ltb_states", "portfolio_state"):
            rows = connection.execute(
                f"SELECT agent_id, date FROM {table} WHERE turn=? ORDER BY agent_id",
                (boundary_turn,),
            ).fetchall()
            observed = tuple(str(row["agent_id"]) for row in rows)
            if observed != tuple(sorted(agent_ids)) or any(
                str(row["date"]) != boundary_date for row in rows
            ):
                raise RuntimeError(f"Warm base {table} does not cover the boundary cohort")
        pending = int(
            connection.execute(
                "SELECT COUNT(*) FROM trade_log WHERE status='pending'"
            ).fetchone()[0]
        )
        if pending:
            raise RuntimeError(f"Warm base contains {pending} pending trade rows")
        advisor_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_system_messages WHERE message_type='advisor'"
            ).fetchone()[0]
        )
        if advisor_rows:
            raise RuntimeError("Warm base must precede advisor treatment installation")
        community_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM community_logs WHERE turn=?",
                (boundary_turn,),
            ).fetchone()[0]
        )
        if community_mode == "on" and community_rows != len(agent_ids):
            raise RuntimeError("Warm Community ON base lacks the complete PM boundary board")
        if community_mode == "off" and community_rows:
            raise RuntimeError("Warm Community OFF base unexpectedly contains boundary community logs")
    return {
        "status": "pass",
        "base_db": str(db_path.resolve()),
        "sha256": file_sha256(db_path),
        "boundary_turn": int(boundary_turn),
        "boundary_date": boundary_date,
        "agent_count": len(agent_ids),
        "community_mode": community_mode,
    }
