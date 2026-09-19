from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import config
from twinmarket_kr.agents.news_agent import (
    AM_NEWS_WINDOW_END_TIME,
    AM_NEWS_WINDOW_START_TIME,
    PM_NEWS_WINDOW_END_TIME,
    PM_NEWS_WINDOW_START_TIME,
    SealedNewsBundle,
)
from twinmarket_kr.db.connection import connect
from twinmarket_kr.experiment_runtime import (
    ExperimentCheckpointError,
    artifact_tree_sha256,
    assert_integrated_event_state,
    canonical_sha256,
    file_sha256,
)
from twinmarket_kr.llm.call_policy import RN_REASONING_AUDIT_FIELDS


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSONL at {path}:{line_number}") from exc
    return rows


def validate_community_artifacts(
    *,
    community_mode: str,
    agent_ids: list[str],
    depth_by_agent: dict[str, int],
    community_rows: list[dict[str, str]],
    post_rows: list[dict[str, str]],
    interaction_rows: list[dict[str, str]],
    best_rows: list[dict[str, str]],
    selection_rows: list[dict[str, str]],
    post_permission_by_agent: Mapping[str, bool] | None = None,
) -> list[str]:
    """Validate the analysis-visible community contract without DB access."""
    artifacts = {
        "community_logs": community_rows,
        "community_posts": post_rows,
        "community_interactions": interaction_rows,
        "community_best_posts": best_rows,
        "community_selection_inputs": selection_rows,
    }
    if community_mode == "off":
        return [
            f"{name} found while community mode is off: rows={len(rows)}"
            for name, rows in artifacts.items()
            if rows
        ]

    errors: list[str] = []
    cohort = set(agent_ids)
    if set(depth_by_agent) != cohort:
        errors.append("community depth map differs from the run cohort")
        return errors
    effective_post_permissions = (
        {
            agent_id: depth_by_agent.get(agent_id) in {1, 2}
            for agent_id in agent_ids
        }
        if post_permission_by_agent is None
        else {
            str(agent_id): bool(allowed)
            for agent_id, allowed in post_permission_by_agent.items()
        }
    )
    if set(effective_post_permissions) != cohort:
        errors.append("community post permission map differs from the run cohort")
        return errors

    for row in post_rows:
        agent_id = str(row.get("agent_id") or "")
        if not effective_post_permissions.get(agent_id, False):
            errors.append(f"ineligible community post author={agent_id}")
        content = str(row.get("content") or "")
        if not content.strip() or len(content) > 500:
            errors.append(
                f"invalid community post body agent={agent_id} length={len(content)}"
            )

    for row in selection_rows:
        agent_id = str(row.get("agent_id") or "")
        depth = depth_by_agent.get(agent_id, -1)
        try:
            read_limit = int(row.get("read_limit") or -1)
        except ValueError:
            read_limit = -1
        expected_limit = 5 if depth in {1, 2} else 0
        if depth not in {1, 2} or read_limit != expected_limit:
            errors.append(
                f"invalid community selection permission agent={agent_id} "
                f"depth={depth} limit={read_limit}"
            )

    best_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    best_groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in best_rows:
        key = (
            str(row.get("date") or ""),
            str(row.get("turn") or ""),
            str(row.get("post_id") or ""),
        )
        if key in best_by_key:
            errors.append(f"duplicate Best post key={key}")
            continue
        best_by_key[key] = row
        best_groups[key[:2]].append(row)
        content = str(row.get("content") or "")
        body_sha256 = str(row.get("body_sha256") or "")
        if (
            not content
            or body_sha256
            != hashlib.sha256(content.encode("utf-8")).hexdigest()
        ):
            errors.append(f"Best body/hash mismatch key={key}")
        if str(row.get("self_exclusion_policy") or "") != "exclude_author_no_backfill":
            errors.append(f"Best self-exclusion policy mismatch key={key}")

    for group_key, rows in best_groups.items():
        if len(rows) > 5:
            errors.append(f"Best count exceeds 5 at {group_key}: {len(rows)}")
        try:
            ranked = sorted(rows, key=lambda row: int(row.get("rank") or -1))
            ranks = [int(row.get("rank") or -1) for row in ranked]
            observed_order = [
                (
                    -int(float(row.get("score") or 0)),
                    -int(float(row.get("like_count") or 0)),
                    int(row.get("post_id") or -1),
                )
                for row in ranked
            ]
        except ValueError:
            errors.append(f"invalid Best ranking fields at {group_key}")
            continue
        if ranks != list(range(1, len(rows) + 1)):
            errors.append(f"non-contiguous Best ranks at {group_key}: {ranks}")
        if observed_order != sorted(observed_order):
            errors.append(f"Best ranking order mismatch at {group_key}")

    selected_candidates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    pm_full_bodies: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    best_deliveries: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in interaction_rows:
        agent_id = str(row.get("agent_id") or "")
        depth = depth_by_agent.get(agent_id, -1)
        exposure = str(row.get("exposure_level") or "")
        delivery_status = str(row.get("delivery_status") or "")
        post_id = str(row.get("post_id") or "")
        key = (
            str(row.get("source_date") or row.get("date") or ""),
            str(row.get("source_turn") or row.get("turn") or ""),
            post_id,
        )
        if agent_id not in cohort:
            errors.append(f"community exposure reader outside cohort={agent_id}")
            continue
        if exposure == "title_only":
            if depth not in {1, 2}:
                errors.append(f"Depth 0 title-only exposure agent={agent_id}")
            if row.get("content"):
                errors.append(f"title-only row contains body agent={agent_id} post={post_id}")
            if _truthy(row.get("selected")):
                selected_candidates[
                    (str(row.get("date") or ""), str(row.get("turn") or ""), agent_id)
                ].add(post_id)
            continue
        if exposure != "full_body":
            errors.append(
                f"unknown community exposure level agent={agent_id} post={post_id}: {exposure}"
            )
            continue
        content = str(row.get("content") or "")
        if (
            not content
            or str(row.get("body_sha256") or "")
            != hashlib.sha256(content.encode("utf-8")).hexdigest()
        ):
            errors.append(f"community body/hash mismatch agent={agent_id} post={post_id}")
        profile_present = str(row.get("author_profile") or "") not in {
            "",
            "null",
            "None",
        }
        if depth == 2:
            if not profile_present or str(row.get("profile_scope") or "") != "detailed":
                errors.append(f"Depth 2 full-body profile missing agent={agent_id} post={post_id}")
        elif profile_present:
            errors.append(f"private profile leaked to depth {depth} agent={agent_id}")

        if delivery_status == "read_pm":
            if depth not in {1, 2}:
                errors.append(f"Depth 0 selective full-body exposure agent={agent_id}")
            if str(row.get("reaction") or "") not in {"like", "unlike", "none"}:
                errors.append(
                    f"invalid or lost community reaction agent={agent_id} post={post_id}"
                )
            pm_full_bodies[
                (str(row.get("date") or ""), str(row.get("turn") or ""), agent_id)
            ].add(post_id)
        elif delivery_status == "delivered_am" and _truthy(row.get("is_best")):
            if _truthy(row.get("selected")) or _truthy(row.get("replay")):
                errors.append(f"Best delivery mislabeled as selected/replay key={key}")
            best_deliveries[key].append(row)
        elif delivery_status != "delivered_am":
            errors.append(
                f"unknown full-body delivery status agent={agent_id} post={post_id}: "
                f"{delivery_status}"
            )

    for selection_key in sorted(set(selected_candidates) | set(pm_full_bodies)):
        selected = selected_candidates.get(selection_key, set())
        full_bodies = pm_full_bodies.get(selection_key, set())
        if selected != full_bodies:
            errors.append(
                f"selected/full-body mismatch key={selection_key} "
                f"selected={sorted(selected)} full={sorted(full_bodies)}"
            )
        depth = depth_by_agent.get(selection_key[2], -1)
        cap = 5 if depth in {1, 2} else 0
        if len(selected) > cap:
            errors.append(
                f"community selective-read cap exceeded key={selection_key} "
                f"count={len(selected)} cap={cap}"
            )

    for key, best in best_by_key.items():
        author = str(best.get("author_agent_id") or "")
        expected_recipients = cohort - ({author} if author in cohort else set())
        try:
            scheduled = int(best.get("scheduled_delivery_count") or 0)
            actual = int(best.get("actual_delivery_count") or 0)
        except ValueError:
            errors.append(f"invalid Best delivery counts key={key}")
            continue
        if scheduled != len(expected_recipients):
            errors.append(
                f"Best scheduled audience mismatch key={key} "
                f"scheduled={scheduled} expected={len(expected_recipients)}"
            )
        delivered_rows = best_deliveries.get(key, [])
        delivered_recipients = {
            str(row.get("agent_id") or "")
            for row in delivered_rows
        }
        if author in delivered_recipients:
            errors.append(f"Best author received own post key={key}")
        status = str(best.get("delivery_status") or "")
        if status == "delivered_am":
            if delivered_recipients != expected_recipients or actual != len(delivered_rows):
                errors.append(
                    f"Best actual audience mismatch key={key} "
                    f"actual={sorted(delivered_recipients)} "
                    f"expected={sorted(expected_recipients)}"
                )
        elif status in {"right_censored", "no_eligible_recipient"}:
            if delivered_rows or actual != 0:
                errors.append(f"non-delivered Best has actual exposure key={key}")
        elif status not in {"scheduled_next_am"}:
            errors.append(f"incomplete Best delivery status key={key}: {status}")

    for key, rows in best_deliveries.items():
        if key not in best_by_key:
            # A checkpoint chunk can contain next-AM delivery while its source
            # Best row lives in the preceding chunk. The merged final bundle
            # validates the complete relation.
            continue
        if len({str(row.get("agent_id") or "") for row in rows}) != len(rows):
            errors.append(f"duplicate Best recipient rows key={key}")

    for row in community_rows:
        agent_id = str(row.get("agent_id") or "")
        if depth_by_agent.get(agent_id, -1) == 0:
            try:
                posts_read = json.loads(row.get("posts_read_json") or "[]")
                best_posts = json.loads(row.get("best_posts_json") or "[]")
            except json.JSONDecodeError:
                errors.append(f"invalid Depth 0 community log JSON agent={agent_id}")
                continue
            if posts_read:
                errors.append(f"Depth 0 has selective posts_read agent={agent_id}")
            if any(not str(post.get("content") or "") for post in best_posts):
                errors.append(f"Depth 0 Best log omits full body agent={agent_id}")
    return errors


def summarize_api_audit(path: Path | str, *, expected_model: str) -> dict[str, Any]:
    rows = _read_jsonl(Path(path))
    if not rows:
        raise RuntimeError(f"OpenRouter audit log is empty: {path}")
    requested_models = sorted({str(row.get("requested_model") or "") for row in rows})
    if requested_models != [expected_model]:
        raise RuntimeError(
            f"Unexpected requested models in API audit: {requested_models}, expected={expected_model}"
        )
    successful = [row for row in rows if str(row.get("status")) == "success"]
    if not successful:
        raise RuntimeError("OpenRouter audit contains no successful calls")
    returned_models = sorted(
        {str(row.get("returned_model")) for row in successful if row.get("returned_model")}
    )
    unexpected_returned_models = [
        model for model in returned_models if model != expected_model
    ]
    if unexpected_returned_models:
        raise RuntimeError(
            "OpenRouter returned a model other than the paper model: "
            f"{unexpected_returned_models}, expected={expected_model}"
        )
    return {
        "api_call_attempts": len(rows),
        "api_successes": len(successful),
        "api_errors": len(rows) - len(successful),
        "api_requested_model_set": requested_models,
        "api_returned_model_set": returned_models,
        "api_missing_returned_model": sum(
            not bool(row.get("returned_model")) for row in successful
        ),
        "api_provider_set": sorted(
            {str(row.get("provider")) for row in successful if row.get("provider")}
        ),
    }


def scheduled_fake_dates(daily_news_csv: Path | str, dates: Iterable[str]) -> set[str]:
    allowed = set(dates)
    result: set[str] = set()
    for row in _read_csv(Path(daily_news_csv)):
        day = str(row.get("date") or "")
        if day not in allowed:
            continue
        if _truthy(row.get("is_fake")) or str(row.get("synthetic_id") or "").strip():
            result.add(day)
    return result


def validate_sealed_news_coverage(
    news_bundle_path: Path | str,
    *,
    event_ids: Sequence[str] | None = None,
    minimum_real_count: int = 0,
    expected_stock_code: str | None = config.STOCK_CODE,
) -> dict[str, Any]:
    """Validate the exact target/shortage contract of the sealed real-news feed.

    ``target_real_news_per_event`` is the requested quota, while the slot count
    is the delivered scientific input.  A short event is valid only because
    :class:`SealedNewsBundle` proves that its accepted-shortage record exactly
    binds the ordered article IDs and payload hashes.  This validator adds the
    sealed shortage record and optionally restricts the report to the events
    selected by a run. Shortage is recorded, not treated as a run failure.
    """

    if minimum_real_count < 0:
        raise ValueError("minimum_real_count must be non-negative")
    bundle = SealedNewsBundle.load(
        news_bundle_path,
        expected_stock_code=expected_stock_code,
    )
    selected_event_ids = (
        tuple(str(value) for value in event_ids)
        if event_ids is not None
        else tuple(sorted(bundle.slots_by_event))
    )
    if (
        not selected_event_ids
        or len(selected_event_ids) != len(set(selected_event_ids))
    ):
        raise RuntimeError("Sealed-news validation requires unique event IDs")
    unknown = sorted(set(selected_event_ids) - set(bundle.slots_by_event))
    if unknown:
        raise RuntimeError(
            f"Run events are absent from the sealed news bundle: {unknown[:5]}"
        )

    lower_bound = min(int(minimum_real_count), bundle.target_real_count)
    slot_counts: dict[str, int] = {}
    shortages: dict[str, dict[str, Any]] = {}
    for event_id in selected_event_ids:
        slots = bundle.slots_by_event[event_id]
        count = len(slots)
        if count < lower_bound or count > bundle.target_real_count:
            raise RuntimeError(
                f"Sealed real-news count is outside {lower_bound}.."
                f"{bundle.target_real_count} at {event_id}: {count}"
            )
        slot_counts[event_id] = count
        shortage = bundle.accepted_shortages.get(event_id)
        if count < bundle.target_real_count:
            # SealedNewsBundle.load() already proved the exact ordered IDs,
            # hashes, delivered count, and missing count.  Retain that record
            # in the validation result so downstream reports can cite it.
            if shortage is None:
                raise RuntimeError(
                    f"Short event lacks accepted-shortage provenance: {event_id}"
                )
            shortages[event_id] = dict(shortage)
        elif shortage is not None:
            raise RuntimeError(
                f"Complete event unexpectedly has a shortage record: {event_id}"
            )

    return {
        "status": "pass",
        "bundle_path": str(Path(news_bundle_path).resolve()),
        "bundle_file_sha256": bundle.file_sha256,
        "bundle_sha256": bundle.bundle_sha256,
        "target_real_count": bundle.target_real_count,
        "minimum_real_count": lower_bound,
        "event_count": len(selected_event_ids),
        "delivered_real_count": sum(slot_counts.values()),
        "shortage_event_count": len(shortages),
        "slot_counts": slot_counts,
        "accepted_shortages": shortages,
    }


def validate_news_inputs(
    *,
    processed_news_csv: Path | str,
    daily_news_csv: Path | str,
    baseline_processed_csv: Path | str,
    baseline_daily_csv: Path | str,
    sim_db_path: Path | str,
    dates: list[str],
    fake_news_mode: str,
    market_close_time: str,  # noqa: ARG001 - 주문 마감 시각; 뉴스 윈도우는 news_agent 상수를 쓴다
    sealed_news_bundle: Path | str | None = None,
    stock_code: str = config.STOCK_CODE,
) -> dict[str, Any]:
    """Audit feed identity, 10(+1) structure, and temporal eligibility before API calls."""
    processed = _read_csv(Path(processed_news_csv))
    daily = _read_csv(Path(daily_news_csv))
    if not processed or not daily:
        raise RuntimeError("News input CSV is empty or missing")

    processed_ids = [str(row.get("id") or "") for row in processed]
    daily_ids = [str(row.get("id") or "") for row in daily]
    if any(not value for value in processed_ids + daily_ids):
        raise RuntimeError("News input contains an empty public ID")
    if len(processed_ids) != len(set(processed_ids)):
        raise RuntimeError("Processed news contains duplicate public IDs")
    if len(daily_ids) != len(set(daily_ids)):
        raise RuntimeError("Daily news selection contains duplicate public IDs")
    missing_content = sorted(set(daily_ids) - set(processed_ids))
    if missing_content:
        raise RuntimeError(f"Daily feed IDs missing from processed news: {missing_content[:5]}")

    def is_fake(row: dict[str, Any]) -> bool:
        return _truthy(row.get("is_fake")) or bool(str(row.get("synthetic_id") or "").strip())

    processed_fake_ids = {str(row["id"]) for row in processed if is_fake(row)}
    daily_fake_ids = {str(row["id"]) for row in daily if is_fake(row)}
    if processed_fake_ids != daily_fake_ids:
        raise RuntimeError(
            "Fake-news IDs differ between processed and daily files: "
            f"processed={len(processed_fake_ids)} daily={len(daily_fake_ids)}"
        )
    if fake_news_mode == "off" and daily_fake_ids:
        raise RuntimeError("Fake-news rows are present in a fake-off feed")
    if fake_news_mode == "on":
        if len(daily_fake_ids) != 30:
            raise RuntimeError(f"Paper fake-on feed requires 30 stimuli, found {len(daily_fake_ids)}")
        baseline_processed = _read_csv(Path(baseline_processed_csv))
        baseline_daily = _read_csv(Path(baseline_daily_csv))
        public_processed_fields = ("id", "title", "date", "time", "category", "summary")
        public_daily_fields = ("id", "title", "date", "time", "category")

        def public_rows(rows: list[dict[str, str]], fields: tuple[str, ...]) -> list[tuple[str, ...]]:
            return sorted(
                tuple(str(row.get(field) or "") for field in fields)
                for row in rows
                if not is_fake(row)
            )

        if public_rows(processed, public_processed_fields) != public_rows(
            baseline_processed, public_processed_fields
        ):
            raise RuntimeError("Fake-on processed news does not preserve the baseline real-news pool")
        if public_rows(daily, public_daily_fields) != public_rows(baseline_daily, public_daily_fields):
            raise RuntimeError("Fake-on daily feed does not preserve the baseline real-news selection")

    with connect(sim_db_path, read_only=True) as connection:
        stock_dates = [
            str(row[0])
            for row in connection.execute(
                "SELECT date FROM StockData WHERE stock_id = ? ORDER BY date",
                (stock_code,),
            ).fetchall()
        ]
    previous_by_date = {
        day: stock_dates[index - 1] for index, day in enumerate(stock_dates) if index > 0
    }
    rows_with_time: list[tuple[datetime, dict[str, str]]] = []
    for row in daily:
        try:
            timestamp = datetime.strptime(
                f"{str(row.get('date') or '')[:10]} {str(row.get('time') or '')[:5]}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            continue
        rows_with_time.append((timestamp, row))

    selected_event_ids = [
        f"{day}/{subturn.upper()}"
        for day in dates
        for subturn in ("am", "pm")
    ]
    sealed_coverage = (
        validate_sealed_news_coverage(
            sealed_news_bundle,
            event_ids=selected_event_ids,
        )
        if sealed_news_bundle is not None
        else None
    )
    sealed_slot_counts = (
        {
            str(event_id): int(count)
            for event_id, count in sealed_coverage["slot_counts"].items()
        }
        if sealed_coverage is not None
        else {}
    )

    slot_report: list[dict[str, Any]] = []
    fake_dates: list[str] = []
    for day in dates:
        if day not in previous_by_date:
            raise RuntimeError(f"No prior trading date exists for news cutoff: {day}")
        # 뉴스 윈도우는 news_agent의 선정 로직과 동일한 상수·양끝 포함 비교를 쓴다.
        # 검증기가 독자적으로 경계를 계산하면 선정과 노출이 조용히 어긋날 수 있다.
        windows = (
            (
                "am",
                datetime.strptime(
                    f"{previous_by_date[day]} {AM_NEWS_WINDOW_START_TIME}", "%Y-%m-%d %H:%M"
                ),
                datetime.strptime(
                    f"{day} {AM_NEWS_WINDOW_END_TIME}", "%Y-%m-%d %H:%M"
                ),
            ),
            (
                "pm",
                datetime.strptime(
                    f"{day} {PM_NEWS_WINDOW_START_TIME}", "%Y-%m-%d %H:%M"
                ),
                datetime.strptime(
                    f"{day} {PM_NEWS_WINDOW_END_TIME}", "%Y-%m-%d %H:%M"
                ),
            ),
        )
        for subturn, start, end in windows:
            slot_rows = [row for timestamp, row in rows_with_time if start <= timestamp <= end]
            fake_count = sum(is_fake(row) for row in slot_rows)
            real_count = len(slot_rows) - fake_count
            event_id = f"{day}/{subturn.upper()}"
            if not 5 <= real_count <= 10:
                raise RuntimeError(
                    f"Real-news feed count must be within 5..10 at "
                    f"{day}:{subturn}, found {real_count}"
                )
            if sealed_coverage is None and real_count < 10:
                raise RuntimeError(
                    f"Short real-news slot lacks sealed accepted-shortage "
                    f"provenance at {event_id}: {real_count}/10"
                )
            if (
                sealed_coverage is not None
                and real_count != sealed_slot_counts[event_id]
            ):
                raise RuntimeError(
                    f"CSV feed differs from sealed delivered count at "
                    f"{event_id}: csv={real_count} "
                    f"sealed={sealed_slot_counts[event_id]}"
                )
            if fake_count not in {0, 1}:
                raise RuntimeError(
                    f"At most one fake stimulus is allowed at {day}:{subturn}, found {fake_count}"
                )
            if fake_count and real_count != 10:
                raise RuntimeError(
                    f"Fake stimulus must append to 10 real items at {day}:{subturn}"
                )
            if fake_count:
                fake_dates.append(day)
            slot_report.append(
                {
                    "date": day,
                    "subturn": subturn,
                    "real_count": real_count,
                    "fake_count": fake_count,
                }
            )
    if fake_news_mode == "on":
        if len(fake_dates) != 30 or len(set(fake_dates)) != 30:
            raise RuntimeError(
                "Fake stimuli must occupy 30 distinct experiment dates/slots: "
                f"slots={len(fake_dates)} dates={len(set(fake_dates))}"
            )
    elif fake_dates:
        raise RuntimeError(f"Unexpected fake-news slots in fake-off mode: {fake_dates[:5]}")

    short_real_slots = [
        f"{row['date']}:{row['subturn']}"
        for row in slot_report
        if row["real_count"] < 10
    ]
    return {
        "status": "pass",
        "processed_rows": len(processed),
        "daily_rows": len(daily),
        "experiment_slot_count": len(slot_report),
        "fake_stimulus_count": len(daily_fake_ids),
        "fake_experiment_slots": len(fake_dates),
        "short_real_news_slots": short_real_slots,
        "slot_counts": slot_report,
        "sealed_coverage": sealed_coverage,
    }


def assert_runtime_state(
    db_path: Path | str,
    *,
    agent_ids: list[str],
    expected_turn: int,
    phase: str,
) -> str:
    """Validate and fingerprint belief/portfolio state at a chunk boundary."""
    if not agent_ids:
        raise ValueError("agent_ids must not be empty")
    placeholders = ",".join("?" for _ in agent_ids)
    payload: list[dict[str, Any]] = []
    with connect(db_path, read_only=True) as connection:
        for table in ("portfolio_state", "belief_history"):
            maximum = connection.execute(
                f"SELECT MAX(turn) FROM {table} WHERE agent_id IN ({placeholders})",
                agent_ids,
            ).fetchone()[0]
            if maximum is None or int(maximum) != expected_turn:
                raise RuntimeError(
                    f"{phase}: {table} max turn is {maximum}, expected {expected_turn}"
                )

        expected_state_rows = len(agent_ids) * (expected_turn + 1)
        for table in ("portfolio_state", "belief_history"):
            state_rows = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE agent_id IN ({placeholders}) AND turn BETWEEN 0 AND ?
                    """,
                    [*agent_ids, expected_turn],
                ).fetchone()[0]
            )
            if state_rows != expected_state_rows:
                raise RuntimeError(
                    f"{phase}: {table} rows={state_rows}, expected={expected_state_rows}"
                )
            foreign_runtime_agents = connection.execute(
                f"""
                SELECT DISTINCT agent_id FROM {table}
                WHERE turn > 0 AND agent_id NOT IN ({placeholders})
                ORDER BY agent_id
                LIMIT 5
                """,
                agent_ids,
            ).fetchall()
            if foreign_runtime_agents:
                raise RuntimeError(
                    f"{phase}: {table} contains out-of-cohort runtime agents: "
                    f"{[str(row[0]) for row in foreign_runtime_agents]}"
                )
        trade_rows = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM trade_log
                WHERE agent_id IN ({placeholders}) AND turn BETWEEN 1 AND ?
                """,
                [*agent_ids, expected_turn],
            ).fetchone()[0]
        )
        expected_trade_rows = len(agent_ids) * expected_turn
        if trade_rows != expected_trade_rows:
            raise RuntimeError(
                f"{phase}: trade_log rows={trade_rows}, expected={expected_trade_rows}"
            )
        invalid_trade_rows = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM trade_log
                WHERE agent_id IN ({placeholders}) AND turn BETWEEN 1 AND ?
                  AND (status <> 'filled' OR filled_quantity <> quantity OR executed_price IS NULL)
                """,
                [*agent_ids, expected_turn],
            ).fetchone()[0]
        )
        if invalid_trade_rows:
            raise RuntimeError(
                f"{phase}: non-full or unresolved trade_log rows={invalid_trade_rows}"
            )
        trading_detail_rows = int(
            connection.execute(
                f"SELECT COUNT(*) FROM TradingDetails WHERE user_id IN ({placeholders})",
                agent_ids,
            ).fetchone()[0]
        )
        if trading_detail_rows != expected_trade_rows:
            raise RuntimeError(
                f"{phase}: TradingDetails rows={trading_detail_rows}, expected={expected_trade_rows}"
            )
        system_message_rows = int(
            connection.execute("SELECT COUNT(*) FROM agent_system_messages").fetchone()[0]
        )
        if system_message_rows:
            raise RuntimeError(
                f"{phase}: fabricated/system recovery messages detected: {system_message_rows}"
            )

        portfolios = connection.execute(
            f"""
            SELECT agent_id, turn, date, cash, positions, total_value,
                   realized_pnl, total_return_rate
            FROM portfolio_state
            WHERE turn = ? AND agent_id IN ({placeholders})
            ORDER BY agent_id
            """,
            [expected_turn, *agent_ids],
        ).fetchall()
        beliefs = connection.execute(
            f"""
            SELECT agent_id, turn, date, belief_summary, COALESCE(view_change, '') AS view_change
            FROM belief_history
            WHERE turn = ? AND agent_id IN ({placeholders})
            ORDER BY agent_id
            """,
            [expected_turn, *agent_ids],
        ).fetchall()

    portfolio_agents = {str(row["agent_id"]) for row in portfolios}
    belief_agents = {str(row["agent_id"]) for row in beliefs}
    expected_agents = set(agent_ids)
    if portfolio_agents != expected_agents:
        missing = sorted(expected_agents - portfolio_agents)
        extra = sorted(portfolio_agents - expected_agents)
        raise RuntimeError(f"{phase}: portfolio boundary mismatch missing={missing} extra={extra}")
    if belief_agents != expected_agents:
        missing = sorted(expected_agents - belief_agents)
        extra = sorted(belief_agents - expected_agents)
        raise RuntimeError(f"{phase}: belief boundary mismatch missing={missing} extra={extra}")

    portfolio_by_agent = {str(row["agent_id"]): dict(row) for row in portfolios}
    belief_by_agent = {str(row["agent_id"]): dict(row) for row in beliefs}
    for agent_id in sorted(expected_agents):
        portfolio = portfolio_by_agent[agent_id]
        if float(portfolio["cash"]) < -1e-6:
            raise RuntimeError(f"{phase}: negative cash for {agent_id}: {portfolio['cash']}")
        try:
            positions = json.loads(portfolio["positions"] or "[]")
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{phase}: invalid positions JSON for {agent_id}") from exc
        if any(int(position.get("quantity") or 0) < 0 for position in positions):
            raise RuntimeError(f"{phase}: negative position for {agent_id}")
        payload.append(
            {
                "agent_id": agent_id,
                "portfolio": portfolio_by_agent[agent_id],
                "belief": belief_by_agent[agent_id],
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_log_bundle(
    run_dir: Path | str,
    *,
    dates: list[str],
    agent_ids: list[str],
    community_audience_agent_ids: list[str],
    turn_offset: int,
    fake_news_mode: str,
    daily_news_csv: Path | str | None,
    community_mode: str,
    sealed_news_bundle: Path | str | None = None,
    stock_code: str = config.STOCK_CODE,
    post_permission_by_agent: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Fail fast when a chunk or merged run is incomplete or temporally inconsistent."""
    root = Path(run_dir)
    errors: list[str] = []
    agent_turns = _read_csv(root / "agent_turns.csv")
    expected_agent_turns = len(dates) * 2 * len(agent_ids)
    if len(agent_turns) != expected_agent_turns:
        errors.append(f"agent_turns rows={len(agent_turns)} expected={expected_agent_turns}")

    expected_turn_by_date_subturn: dict[tuple[str, str], int] = {}
    for index, day in enumerate(dates):
        am_turn = turn_offset + index * 2 + 1
        expected_turn_by_date_subturn[(day, "am")] = am_turn
        expected_turn_by_date_subturn[(day, "pm")] = am_turn + 1

    selected_event_ids = [
        f"{day}/{subturn.upper()}"
        for day in dates
        for subturn in ("am", "pm")
    ]
    sealed_coverage = (
        validate_sealed_news_coverage(
            sealed_news_bundle,
            event_ids=selected_event_ids,
            expected_stock_code=stock_code,
        )
        if sealed_news_bundle is not None
        else None
    )
    expected_feed_ids: dict[tuple[str, str], tuple[str, ...]] = {}
    if sealed_news_bundle is not None:
        bundle = SealedNewsBundle.load(
            sealed_news_bundle,
            expected_stock_code=stock_code,
        )
        expected_feed_ids = {
            tuple(event_id.rsplit("/", 1)): tuple(
                slot.article_id
                for slot in bundle.slots_by_event[event_id]
            )
            for event_id in selected_event_ids
        }

    seen_agent_turns: Counter[tuple[str, str, str]] = Counter()
    observed_agents = set()
    depth_by_agent: dict[str, int] = {}
    feed_signatures: dict[tuple[str, str], set[tuple[str, ...]]] = defaultdict(set)
    visible_counts: dict[tuple[str, str], set[int]] = defaultdict(set)
    fake_visible_counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in agent_turns:
        day = str(row.get("date") or "")
        subturn = str(row.get("subturn") or "")
        agent_id = str(row.get("agent_id") or "")
        observed_agents.add(agent_id)
        seen_agent_turns[(day, subturn, agent_id)] += 1
        expected_turn = expected_turn_by_date_subturn.get((day, subturn))
        try:
            actual_turn = int(row.get("turn") or -1)
        except ValueError:
            actual_turn = -1
        if expected_turn is None or actual_turn != expected_turn:
            errors.append(
                f"unexpected turn mapping date={day} subturn={subturn} agent={agent_id} "
                f"actual={actual_turn} expected={expected_turn}"
            )
            if len(errors) >= 20:
                break
        action = str(row.get("action") or "").lower()
        try:
            quantity = int(float(row.get("quantity") or 0))
            decision_attempts = int(float(row.get("decision_attempts") or 0))
            belief_attempts = int(float(row.get("belief_generation_attempts") or 0))
            depth = int(float(row.get("news_depth") or 0))
            read_count = int(float(row.get("read_news_count") or 0))
            search_count = int(float(row.get("search_read_count") or 0))
        except ValueError:
            errors.append(f"invalid numeric decision/log fields agent={agent_id} date={day}")
            continue
        previous_depth = depth_by_agent.setdefault(agent_id, depth)
        if previous_depth != depth:
            errors.append(
                f"agent depth changed within run agent={agent_id} "
                f"previous={previous_depth} actual={depth}"
            )
        visible_ids = tuple(
            value.strip()
            for value in str(row.get("visible_news_ids") or "").split(",")
            if value.strip()
        )
        visible_count = len(visible_ids)
        feed_key = (day, subturn)
        feed_signatures[feed_key].add(visible_ids)
        visible_counts[feed_key].add(visible_count)
        if _truthy(row.get("fake_visible")):
            fake_visible_counts[feed_key] += 1
        if len(visible_ids) != len(set(visible_ids)):
            errors.append(f"duplicate visible news IDs date={day} subturn={subturn} agent={agent_id}")
        if action not in {"buy", "sell"} or quantity < 1:
            errors.append(
                f"invalid buy/sell-only decision agent={agent_id} date={day} "
                f"action={action} quantity={quantity}"
            )
        if decision_attempts < 1:
            errors.append(f"missing decision attempt audit agent={agent_id} date={day}")
        if belief_attempts < 1:
            errors.append(f"missing belief attempt audit agent={agent_id} date={day}")
        if _truthy(row.get("deterministic_fallback_used")):
            errors.append(f"deterministic fallback detected agent={agent_id} date={day}")
        if depth == 0 and (read_count != 0 or search_count != 0):
            errors.append(f"depth0 read/search violation agent={agent_id} date={day}")
        if depth == 1 and (read_count != visible_count or search_count != 0):
            errors.append(
                f"depth1 full-read/no-search violation agent={agent_id} date={day} "
                f"visible={visible_count} read={read_count} search={search_count}"
            )
        if depth >= 2 and (read_count != visible_count or search_count > 5):
            errors.append(
                f"depth2 full-read/search-limit violation agent={agent_id} date={day} "
                f"visible={visible_count} read={read_count} search={search_count}"
            )
    duplicates = [key for key, count in seen_agent_turns.items() if count != 1]
    if duplicates:
        errors.append(f"agent_turn duplicate/missing keys sample={duplicates[:5]}")
    if observed_agents != set(agent_ids):
        errors.append(
            f"agent cohort mismatch observed={sorted(observed_agents)} expected={sorted(agent_ids)}"
        )
    for feed_key, signatures in sorted(feed_signatures.items()):
        if len(signatures) != 1:
            errors.append(f"agents received different feed candidates at {feed_key}")
            continue
        observed_ids = next(iter(signatures))
        count = len(observed_ids)
        fake_rows_in_slot = fake_visible_counts.get(feed_key, 0)
        if fake_rows_in_slot not in {0, len(agent_ids)}:
            errors.append(
                f"partial fake feed exposure at {feed_key}: {fake_rows_in_slot}/{len(agent_ids)}"
            )
        if fake_rows_in_slot:
            if count != 11:
                errors.append(f"fake slot must contain 10 real + 1 fake at {feed_key}: count={count}")
        elif sealed_news_bundle is not None:
            expected_ids = expected_feed_ids.get(
                (feed_key[0], feed_key[1].upper())
            )
            if expected_ids is None:
                errors.append(
                    f"feed event is absent from sealed news: {feed_key}"
                )
            elif observed_ids != expected_ids:
                errors.append(
                    f"visible news differs from sealed slot order at {feed_key}: "
                    f"observed={list(observed_ids)} expected={list(expected_ids)}"
                )
        elif not 5 <= count <= 10:
            errors.append(
                f"real-news slot must contain 5..10 items at {feed_key}: count={count}"
            )

    portfolio_updates = _read_jsonl(root / "portfolio_updates.jsonl")
    portfolio_keys = Counter(
        (str(row.get("date") or ""), int(row.get("turn") or -1), str(row.get("agent_id") or ""))
        for row in portfolio_updates
    )
    if len(portfolio_updates) != expected_agent_turns:
        errors.append(
            f"portfolio_updates rows={len(portfolio_updates)} expected={expected_agent_turns}"
        )
    duplicate_portfolios = [key for key, count in portfolio_keys.items() if count != 1]
    if duplicate_portfolios:
        errors.append(f"portfolio update duplicate keys sample={duplicate_portfolios[:5]}")
    for row in portfolio_updates:
        state = row.get("state") or {}
        if int(state.get("turn") or -1) != int(row.get("turn") or -1):
            errors.append(
                f"portfolio state/event turn mismatch agent={row.get('agent_id')} date={row.get('date')}"
            )
            break

    exchange_rows = _read_csv(root / "daily_exchange_summary.csv")
    expected_exchange_rows = len(dates) * 2
    exchange_keys = Counter(
        (str(row.get("date") or ""), str(row.get("turn") or ""), str(row.get("stock_code") or ""))
        for row in exchange_rows
    )
    if len(exchange_rows) != expected_exchange_rows:
        errors.append(
            f"daily_exchange_summary rows={len(exchange_rows)} expected={expected_exchange_rows}"
        )
    duplicate_exchange = [key for key, count in exchange_keys.items() if count != 1]
    if duplicate_exchange:
        errors.append(f"daily exchange duplicate keys sample={duplicate_exchange[:5]}")

    order_rows = _read_csv(root / "submitted_orders.csv")
    expected_orders = sum(_truthy(row.get("submitted_order")) for row in agent_turns)
    if len(order_rows) != expected_orders:
        errors.append(f"submitted_orders rows={len(order_rows)} expected={expected_orders}")
    order_quantity: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for row in order_rows:
        key = (
            str(row.get("date") or ""),
            str(row.get("turn") or ""),
            str(row.get("agent_id") or ""),
            str(row.get("action") or ""),
        )
        order_quantity[key] += int(float(row.get("quantity") or 0))
    fill_rows = _read_csv(root / "exchange_fills.csv")
    fill_quantity: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for row in fill_rows:
        key = (
            str(row.get("date") or ""),
            str(row.get("turn") or ""),
            str(row.get("agent_id") or ""),
            str(row.get("action") or ""),
        )
        filled = int(float(row.get("quantity") or 0))
        fill_quantity[key] += filled
        if key not in order_quantity or filled > order_quantity[key]:
            errors.append(f"fill has no matching order or exceeds it key={key} filled={filled}")
            break
    if len(fill_rows) != len(order_rows):
        errors.append(f"exchange_fills rows={len(fill_rows)} expected_orders={len(order_rows)}")
    if fill_quantity != order_quantity:
        mismatch_keys = [
            key
            for key in sorted(set(fill_quantity) | set(order_quantity))
            if order_quantity.get(key, 0) != fill_quantity.get(key, 0)
        ][:5]
        errors.append(
            "full-fill invariant failed sample="
            + str(
                [
                    (key, order_quantity.get(key, 0), fill_quantity.get(key, 0))
                    for key in mismatch_keys
                    if order_quantity.get(key, 0) != fill_quantity.get(key, 0)
                ]
            )
        )

    expected_fake = (
        scheduled_fake_dates(daily_news_csv, dates)
        if daily_news_csv is not None
        else set()
    )
    visible_by_date: dict[str, list[str]] = defaultdict(list)
    for row in agent_turns:
        if _truthy(row.get("fake_visible")):
            visible_by_date[str(row.get("date") or "")].append(str(row.get("agent_id") or ""))
    if fake_news_mode == "off":
        if visible_by_date:
            errors.append(f"fake feed visibility found while fake mode is off: {sorted(visible_by_date)}")
    else:
        actual_fake = set(visible_by_date)
        if actual_fake != expected_fake:
            errors.append(
                f"fake feed dates actual={sorted(actual_fake)} expected={sorted(expected_fake)}"
            )
        for day in sorted(expected_fake):
            visible = visible_by_date.get(day, [])
            if len(visible) != len(agent_ids) or set(visible) != set(agent_ids):
                errors.append(
                    f"fake feed cohort mismatch date={day} rows={len(visible)} "
                    f"unique={len(set(visible))} expected={len(agent_ids)}"
                )

    community_rows = _read_csv(root / "community_logs.csv")
    community_post_rows = _read_csv(root / "community_posts.csv")
    community_interaction_rows = _read_csv(root / "community_interactions.csv")
    community_best_rows = _read_csv(root / "community_best_posts.csv")
    community_selection_rows = _read_csv(root / "community_selection_inputs.csv")
    if community_mode == "on":
        if set(community_audience_agent_ids) != set(agent_ids):
            errors.append(
                "community audience must include the whole cohort, including depth 0"
            )
        expected_community_rows = len(dates) * len(community_audience_agent_ids)
        if len(community_rows) != expected_community_rows:
            errors.append(
                f"community_logs rows={len(community_rows)} expected={expected_community_rows}"
            )
        community_keys = Counter(
            (str(row.get("date") or ""), str(row.get("agent_id") or ""))
            for row in community_rows
        )
        duplicate_community = [key for key, count in community_keys.items() if count != 1]
        if duplicate_community:
            errors.append(f"community log duplicate keys sample={duplicate_community[:5]}")
    elif community_mode != "off":
        errors.append(f"unknown community mode: {community_mode}")

    errors.extend(
        validate_community_artifacts(
            community_mode=community_mode,
            agent_ids=agent_ids,
            depth_by_agent=depth_by_agent,
            community_rows=community_rows,
            post_rows=community_post_rows,
            interaction_rows=community_interaction_rows,
            best_rows=community_best_rows,
            selection_rows=community_selection_rows,
            post_permission_by_agent=post_permission_by_agent,
        )
    )

    agent_errors = _read_jsonl(root / "errors.jsonl")
    if agent_errors:
        errors.append(f"agent errors logged: {len(agent_errors)}")

    if errors:
        raise RuntimeError("Run integrity validation failed:\n- " + "\n- ".join(errors[:25]))

    return {
        "status": "pass",
        "run_dir": str(root),
        "date_count": len(dates),
        "agent_count": len(agent_ids),
        "turn_offset": turn_offset,
        "expected_agent_turns": expected_agent_turns,
        "scheduled_fake_dates": sorted(expected_fake),
        "community_agent_count": len(community_audience_agent_ids),
        "visible_news_counts": {
            f"{day}:{subturn}": sorted(counts)
            for (day, subturn), counts in sorted(visible_counts.items())
        },
        "sealed_news_coverage": sealed_coverage,
    }


class CanonicalRunValidationError(RuntimeError):
    """A run is not safe to expose as a scientific result."""


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise CanonicalRunValidationError(f"{label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanonicalRunValidationError(
            f"{label} is not valid JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise CanonicalRunValidationError(
            f"{label} must be a JSON object: {path}"
        )
    return value


def _sha256_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_canonical_database(
    db_path: Path,
    *,
    agent_ids: Sequence[str],
    depth_by_agent: Mapping[str, int],
    post_permission_by_agent: Mapping[str, bool],
    completed_events: Sequence[Mapping[str, Any]],
    community_mode: str,
    full_schedule: bool,
    outcome_schedule_complete: bool | None = None,
) -> dict[str, Any]:
    if outcome_schedule_complete is None:
        outcome_schedule_complete = full_schedule
    event_by_id = {
        str(event["event_id"]): dict(event)
        for event in completed_events
    }
    pm_turns = {
        int(event["turn"])
        for event in completed_events
        if str(event["subturn"]).lower() == "pm"
    }
    cohort = set(agent_ids)
    with connect(db_path, read_only=True) as connection:
        # A warm continuation keeps its canonical parent prefix in the same DB.
        # Outcome rows may therefore mature at valid events before this
        # segment's first event; include those ledger-backed event identities.
        for event_date, subturn, turn in connection.execute(
            """
            SELECT date, subturn, MIN(turn)
            FROM simulation_stb_states
            GROUP BY date, subturn
            """
        ).fetchall():
            event_id = f"{event_date}/{str(subturn).upper()}"
            event_by_id.setdefault(
                event_id,
                {
                    "event_id": event_id,
                    "date": str(event_date),
                    "subturn": str(subturn),
                    "turn": int(turn),
                },
            )
        fill_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM simulation_fills WHERE turn > 0"
            ).fetchone()[0]
        )
        outcomes = connection.execute(
            """
            SELECT outcome_id, fill_id, horizon, due_event_id,
                   available_from_event_id, observed_event_id, mark_price,
                   status, scientific_sha256
            FROM simulation_trade_outcomes
            ORDER BY fill_id, horizon
            """
        ).fetchall()
        outcome_keys = {
            (str(row["fill_id"]), str(row["horizon"]))
            for row in outcomes
        }
        expected_horizons = {"next_turn", "h1", "h5"}
        if len(outcome_keys) != len(outcomes):
            raise CanonicalRunValidationError(
                "Outcome ledger contains duplicate fill/horizon keys"
            )
        for row in outcomes:
            fill_id = str(row["fill_id"])
            horizon = str(row["horizon"])
            status = str(row["status"])
            if (
                horizon not in expected_horizons
                or str(row["outcome_id"]) != f"outcome:{fill_id}:{horizon}"
                or not _sha256_text(row["scientific_sha256"])
            ):
                raise CanonicalRunValidationError(
                    f"Outcome identity/hash mismatch: {fill_id}/{horizon}"
                )
            if status == "matured":
                due_event_id = str(row["due_event_id"] or "")
                if (
                    due_event_id not in event_by_id
                    or str(row["available_from_event_id"]) != due_event_id
                    or str(row["observed_event_id"]) != due_event_id
                    or row["mark_price"] is None
                    or float(row["mark_price"]) <= 0
                ):
                    raise CanonicalRunValidationError(
                        f"Invalid matured outcome: {fill_id}/{horizon}"
                    )
            elif status == "right_censored":
                if (
                    not outcome_schedule_complete
                    or row["due_event_id"] is not None
                    or row["available_from_event_id"] is not None
                    or row["observed_event_id"] is not None
                    or row["mark_price"] is not None
                ):
                    raise CanonicalRunValidationError(
                        f"Premature/invalid right censoring: {fill_id}/{horizon}"
                    )
            else:
                raise CanonicalRunValidationError(
                    f"Unknown outcome status: {status}"
                )
        if outcome_schedule_complete:
            expected_outcome_count = fill_count * len(expected_horizons)
            if len(outcomes) != expected_outcome_count:
                raise CanonicalRunValidationError(
                    f"Final outcome ledger rows={len(outcomes)} "
                    f"expected={expected_outcome_count}"
                )
            by_fill: Counter[str] = Counter(
                str(row["fill_id"]) for row in outcomes
            )
            invalid_fill_horizons = [
                fill_id
                for fill_id, count in by_fill.items()
                if count != len(expected_horizons)
            ]
            if invalid_fill_horizons:
                raise CanonicalRunValidationError(
                    "Final outcome ledger does not contain all three horizons "
                    f"for every fill: {invalid_fill_horizons[:5]}"
                )

        invalid_consumptions = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM simulation_outcome_consumptions AS consumption
                JOIN simulation_trade_outcomes AS outcome
                  ON outcome.outcome_id = consumption.outcome_id
                JOIN simulation_fills AS fill
                  ON fill.fill_id = outcome.fill_id
                JOIN simulation_ltb_states AS ltb
                  ON ltb.ltb_id = consumption.ltb_id
                WHERE outcome.status <> 'matured'
                   OR consumption.fill_id <> outcome.fill_id
                   OR consumption.horizon <> outcome.horizon
                   OR consumption.consumed_at_event_id
                      <> outcome.available_from_event_id
                   OR ltb.agent_id <> fill.agent_id
                   OR (
                        ltb.date || '/' || UPPER(ltb.subturn)
                      ) <> consumption.consumed_at_event_id
                """
            ).fetchone()[0]
        )
        missing_consumptions = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM simulation_trade_outcomes AS outcome
                LEFT JOIN simulation_outcome_consumptions AS consumption
                  ON consumption.outcome_id = outcome.outcome_id
                WHERE outcome.status = 'matured'
                  AND consumption.outcome_id IS NULL
                """
            ).fetchone()[0]
        )
        if invalid_consumptions or missing_consumptions:
            raise CanonicalRunValidationError(
                "Outcome/LTB consumption mismatch: "
                f"invalid={invalid_consumptions} missing={missing_consumptions}"
            )

        community_tables = (
            "community_posts",
            "community_interactions",
            "community_logs",
        )
        community_counts = {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            for table in community_tables
        }
        if community_mode == "off":
            nonempty = {
                table: count
                for table, count in community_counts.items()
                if count
            }
            if nonempty:
                raise CanonicalRunValidationError(
                    f"Community-off DB contains community state: {nonempty}"
                )
        elif community_mode == "on":
            posts = connection.execute(
                """
                SELECT post.*, ltb.agent_id AS ltb_agent,
                       ltb.turn AS ltb_turn,
                       fill.agent_id AS fill_agent,
                       fill.turn AS fill_turn,
                       decision.agent_id AS decision_agent,
                       decision.turn AS decision_turn
                FROM community_posts AS post
                LEFT JOIN simulation_ltb_states AS ltb
                  ON ltb.ltb_id = post.source_ltb_id
                LEFT JOIN simulation_fills AS fill
                  ON fill.fill_id = post.source_fill_id
                LEFT JOIN simulation_decisions AS decision
                  ON decision.decision_id = post.source_decision_id
                ORDER BY post.post_id
                """
            ).fetchall()
            for post in posts:
                author = str(post["agent_id"])
                turn = int(post["turn"])
                body = str(post["content"] or "")
                if (
                    author not in cohort
                    or not post_permission_by_agent.get(author, False)
                    or turn not in pm_turns
                    or not body.strip()
                    or len(body) > 500
                    or int(post["score"])
                    != int(post["like_count"]) - int(post["unlike_count"])
                    or str(post["ltb_agent"] or "") != author
                    or int(post["ltb_turn"] or -1) != turn
                    or str(post["fill_agent"] or "") != author
                    or int(post["fill_turn"] or -1) != turn
                    or str(post["decision_agent"] or "") != author
                    or int(post["decision_turn"] or -1) != turn
                ):
                    raise CanonicalRunValidationError(
                        f"Community post/lineage mismatch post_id={post['post_id']}"
                    )

            reactions = connection.execute(
                """
                SELECT interaction.*, post.agent_id AS author_agent_id,
                       post.turn AS post_turn, post.date AS post_date
                FROM community_interactions AS interaction
                LEFT JOIN community_posts AS post
                  ON post.post_id = interaction.post_id
                ORDER BY interaction.interaction_id
                """
            ).fetchall()
            for reaction in reactions:
                reader = str(reaction["agent_id"])
                if (
                    reader not in cohort
                    or int(depth_by_agent.get(reader, -1)) not in {1, 2}
                    or reader == str(reaction["author_agent_id"] or "")
                    or str(reaction["reaction"]) not in {
                        "like",
                        "unlike",
                        "none",
                    }
                    or int(reaction["turn"])
                    != int(reaction["post_turn"] or -1)
                    or str(reaction["date"])
                    != str(reaction["post_date"] or "")
                ):
                    raise CanonicalRunValidationError(
                        "Community interaction permission/lineage mismatch "
                        f"interaction_id={reaction['interaction_id']}"
                    )
            score_mismatches = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM community_posts AS post
                    LEFT JOIN (
                        SELECT post_id,
                               SUM(CASE WHEN reaction = 'like' THEN 1 ELSE 0 END)
                                 AS likes,
                               SUM(CASE WHEN reaction = 'unlike' THEN 1 ELSE 0 END)
                                 AS unlikes
                        FROM community_interactions
                        GROUP BY post_id
                    ) AS reaction
                      ON reaction.post_id = post.post_id
                    WHERE post.like_count <> COALESCE(reaction.likes, 0)
                       OR post.unlike_count <> COALESCE(reaction.unlikes, 0)
                       OR post.score <> (
                            COALESCE(reaction.likes, 0)
                          - COALESCE(reaction.unlikes, 0)
                       )
                    """
                ).fetchone()[0]
            )
            if score_mismatches:
                raise CanonicalRunValidationError(
                    f"Community score/reaction mismatch rows={score_mismatches}"
                )

            logs = connection.execute(
                """
                SELECT agent_id, turn, date, best_posts_seen, posts_read,
                       candidate_posts_seen
                FROM community_logs
                ORDER BY turn, agent_id
                """
            ).fetchall()
            expected_log_keys = {
                (agent_id, turn)
                for agent_id in agent_ids
                for turn in pm_turns
            }
            actual_log_keys = {
                (str(row["agent_id"]), int(row["turn"]))
                for row in logs
            }
            if (
                actual_log_keys != expected_log_keys
                or len(logs) != len(expected_log_keys)
            ):
                raise CanonicalRunValidationError(
                    "Community logs do not exactly cover every agent/PM event"
                )
            for row in logs:
                event = next(
                    event
                    for event in completed_events
                    if int(event["turn"]) == int(row["turn"])
                )
                if str(row["date"]) != str(event["date"]):
                    raise CanonicalRunValidationError(
                        "Community log date differs from its PM turn"
                    )
                try:
                    decoded = [
                        json.loads(str(row[column] or "[]"))
                        for column in (
                            "best_posts_seen",
                            "posts_read",
                            "candidate_posts_seen",
                        )
                    ]
                except json.JSONDecodeError as exc:
                    raise CanonicalRunValidationError(
                        "Community log contains invalid JSON"
                    ) from exc
                if any(not isinstance(value, list) for value in decoded):
                    raise CanonicalRunValidationError(
                        "Community exposure fields must be JSON arrays"
                    )

            posts_by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for post in posts:
                posts_by_date[str(post["date"])].append(post)
            for day, day_posts in posts_by_date.items():
                ranked = sorted(
                    day_posts,
                    key=lambda post: (
                        -int(post["score"]),
                        -int(post["like_count"]),
                        int(post["post_id"]),
                    ),
                )
                expected_best = {
                    int(post["post_id"]) for post in ranked[:5]
                }
                actual_best = {
                    int(post["post_id"])
                    for post in day_posts
                    if int(post["is_best"]) == 1
                }
                if actual_best != expected_best:
                    raise CanonicalRunValidationError(
                        f"Community Best ranking mismatch date={day}"
                    )
        else:
            raise CanonicalRunValidationError(
                f"Unknown community mode: {community_mode}"
            )

        matured_count = sum(
            str(row["status"]) == "matured" for row in outcomes
        )
        censored_count = sum(
            str(row["status"]) == "right_censored" for row in outcomes
        )
        consumption_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM simulation_outcome_consumptions"
            ).fetchone()[0]
        )
    return {
        "fill_count": fill_count,
        "outcome_count": len(outcomes),
        "matured_outcome_count": matured_count,
        "right_censored_outcome_count": censored_count,
        "outcome_consumption_count": consumption_count,
        "community_counts": community_counts,
    }


def _strict_json_mapping(raw: str, *, label: str) -> dict[str, Any]:
    def reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise CanonicalRunValidationError(
            f"{label} is not strict JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CanonicalRunValidationError(f"{label} must be a JSON object")
    return value


def _expected_request_policy(
    call_policy: Mapping[str, Any],
) -> dict[str, Any]:
    reasoning = call_policy.get("reasoning")
    provider_policy = call_policy.get("provider")
    if (
        reasoning != {"effort": "none", "exclude": True}
        or not isinstance(provider_policy, Mapping)
        or not isinstance(provider_policy.get("only"), list)
        or len(provider_policy["only"]) != 1
        or provider_policy.get("order") != provider_policy.get("only")
        or provider_policy.get("allow_fallbacks") is not False
        or provider_policy.get("require_parameters") is not True
    ):
        raise CanonicalRunValidationError(
            "Run call policy does not prove strict reasoning-off/provider pinning"
        )
    return {
        "reasoning": dict(reasoning),
        "provider": dict(provider_policy),
    }


def validate_response_journal_audit(
    run_dir: Path | str,
    *,
    signature_payload: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconcile committed logical responses with exact provider attempts.

    Temperature is deliberately read from each logical request's
    ``temperature_schedule``.  The integrated stages do not share a fixed
    0.2 temperature: posting/select/reaction and validation retries can use
    different schedules.
    """

    root = Path(run_dir)
    journal_path = root / ".runtime" / "response_journal.sqlite"
    audit_path = root / "openrouter_calls.jsonl"
    if not journal_path.is_file():
        raise CanonicalRunValidationError(
            f"Response journal is missing: {journal_path}"
        )
    if not audit_path.is_file():
        raise CanonicalRunValidationError(
            f"Run-local OpenRouter audit is missing: {audit_path}"
        )
    manifest_sha256 = canonical_sha256(dict(signature_payload))
    parameters = signature_payload.get("parameters")
    call_policy = signature_payload.get("call_policy")
    if not isinstance(parameters, Mapping) or not isinstance(
        call_policy,
        Mapping,
    ):
        raise CanonicalRunValidationError(
            "Signature lacks parameters/call policy for response audit"
        )
    expected_policy = _expected_request_policy(call_policy)
    expected_model = str(call_policy.get("model") or "")
    expected_provider = str(expected_policy["provider"]["only"][0])
    event_ids = [str(value) for value in parameters.get("event_ids", [])]
    agent_ids = {str(value) for value in parameters.get("agent_ids", [])}
    condition_id = str(parameters.get("condition_id") or "")
    stage_budgets = parameters.get("llm_stage_max_tokens")
    stage_schemas = parameters.get("llm_stage_schema_versions")
    if not isinstance(stage_budgets, Mapping) or not isinstance(
        stage_schemas,
        Mapping,
    ):
        raise CanonicalRunValidationError(
            "Signature lacks stage token/schema policies"
        )

    with connect(journal_path, read_only=True) as connection:
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        if quick_check.lower() != "ok":
            raise CanonicalRunValidationError(
                f"Response journal quick_check failed: {quick_check}"
            )
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM journal_meta"
            ).fetchall()
        }
        if (
            meta.get("schema_version")
            != "integrated-response-journal-v1"
            or meta.get("manifest_sha256") != manifest_sha256
        ):
            raise CanonicalRunValidationError(
                "Response journal metadata differs from the run signature"
            )
        logical_rows = connection.execute(
            """
            SELECT *
            FROM logical_responses
            ORDER BY logical_call_id
            """
        ).fetchall()
        physical_rows = connection.execute(
            """
            SELECT *
            FROM physical_attempts
            ORDER BY logical_call_id, attempt_id
            """
        ).fetchall()
    if not logical_rows:
        raise CanonicalRunValidationError(
            "Publication response journal has no logical responses"
        )

    expected_request_fields = {
        "base_prompt",
        "semantic_inputs",
        "model",
        "temperature_schedule",
        "seed_schedule",
        "max_tokens",
        "response_format",
        "request_policy",
        "validation_attempts",
        "validation_procedure_version",
    }
    logical_by_id: dict[str, dict[str, Any]] = {}
    schedule_by_logical: dict[str, dict[str, Any]] = {}
    for row in logical_rows:
        logical_id = str(row["logical_call_id"])
        stage = str(row["stage"])
        raw_request = str(row["request_json"] or "")
        request = _strict_json_mapping(
            raw_request,
            label=f"Journal request {logical_id}",
        )
        canonical_request = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        response = _strict_json_mapping(
            str(row["response_json"] or ""),
            label=f"Journal response {logical_id}",
        )
        if (
            set(request) != expected_request_fields
            or canonical_request != raw_request
            or hashlib.sha256(raw_request.encode("utf-8")).hexdigest()
            != str(row["request_sha256"])
            or canonical_sha256(response) != str(row["response_sha256"])
            or str(row["manifest_sha256"]) != manifest_sha256
            or str(row["run_id"]) != root.name
            or str(row["condition_id"]) != condition_id
            or str(row["agent_id"]) not in agent_ids
            or str(row["event_id"]) not in event_ids
            or str(row["validation_status"]) != "accepted"
            or str(row["commit_status"]) != "committed"
            or not _sha256_text(row["response_sha256"])
        ):
            raise CanonicalRunValidationError(
                f"Committed journal row is incomplete/drifted: {logical_id}"
            )
        if (
            stage not in stage_budgets
            or str(stage_schemas.get(stage) or "")
            != str(row["schema_version"])
            or request.get("model") != expected_model
            or request.get("request_policy") != expected_policy
            or request.get("response_format") != {
                "type": "json_object"
            }
            or request.get("max_tokens") != int(stage_budgets[stage])
        ):
            raise CanonicalRunValidationError(
                f"Journal request differs from the sealed stage policy: {logical_id}"
            )
        attempts = request.get("validation_attempts")
        temperatures = request.get("temperature_schedule")
        seeds = request.get("seed_schedule")
        accepted_attempt = row["accepted_validation_attempt"]
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 1
            or not isinstance(temperatures, list)
            or len(temperatures) != attempts
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 2
                for value in temperatures
            )
            or not isinstance(seeds, list)
            or len(seeds) != attempts
            or len(set(seeds)) != len(seeds)
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in seeds
            )
            or isinstance(accepted_attempt, bool)
            or not isinstance(accepted_attempt, int)
            or not 1 <= accepted_attempt <= attempts
        ):
            raise CanonicalRunValidationError(
                f"Journal attempt schedule is invalid/ambiguous: {logical_id}"
            )
        logical_by_id[logical_id] = dict(row)
        schedule_by_logical[logical_id] = {
            "temperatures": [float(value) for value in temperatures],
            "seeds": [int(value) for value in seeds],
            "max_tokens": int(request["max_tokens"]),
            "response_format": dict(request["response_format"]),
            "request_policy": dict(request["request_policy"]),
            "accepted_validation_attempt": accepted_attempt,
        }

    physical_by_identity: dict[
        tuple[str, str, int],
        dict[str, Any],
    ] = {}
    accepted_physical_by_logical: dict[str, dict[str, Any]] = {}
    for row in physical_rows:
        logical_id = str(row["logical_call_id"])
        logical = logical_by_id.get(logical_id)
        if logical is None:
            raise CanonicalRunValidationError(
                f"Orphan physical journal attempt: {logical_id}"
            )
        validation_attempt = int(row["validation_attempt"])
        schedule = schedule_by_logical[logical_id]
        if not 1 <= validation_attempt <= len(schedule["seeds"]):
            raise CanonicalRunValidationError(
                f"Physical validation attempt is out of range: {logical_id}"
            )
        identity = (
            logical_id,
            str(row["phase_attempt_id"]),
            schedule["seeds"][validation_attempt - 1],
        )
        if identity in physical_by_identity:
            raise CanonicalRunValidationError(
                f"Physical attempt cannot be identified by exact seed: {identity}"
            )
        status = str(row["status"])
        if status not in {"rejected", "error", "accepted"}:
            raise CanonicalRunValidationError(
                f"Unfinished journal physical attempt: {identity}"
            )
        if (
            status == "accepted"
            and validation_attempt
            != schedule["accepted_validation_attempt"]
        ):
            raise CanonicalRunValidationError(
                f"Accepted physical attempt differs from logical row: {logical_id}"
            )
        physical_by_identity[identity] = dict(row)
        if status == "accepted":
            if logical_id in accepted_physical_by_logical:
                raise CanonicalRunValidationError(
                    f"Logical response has multiple accepted physical attempts: {logical_id}"
                )
            accepted_physical_by_logical[logical_id] = dict(row)
    if set(accepted_physical_by_logical) != set(logical_by_id):
        raise CanonicalRunValidationError(
            "Every committed logical response requires one accepted physical attempt"
        )

    audit_rows = _read_jsonl(audit_path)
    if not audit_rows:
        raise CanonicalRunValidationError("OpenRouter audit is empty")
    provider_rows: list[dict[str, Any]] = []
    acceptance_rows: list[dict[str, Any]] = []
    provider_rows_by_hash: dict[str, dict[str, Any]] = {}
    provider_returned_identities: set[tuple[str, str, int]] = set()
    audit_identities: set[tuple[str, str, int]] = set()
    for index, row in enumerate(audit_rows, start=1):
        if set(row) != RN_REASONING_AUDIT_FIELDS:
            raise CanonicalRunValidationError(
                f"OpenRouter audit row {index} has schema drift"
            )
        logical_id = str(row.get("logical_call_id") or "")
        phase_attempt_id = str(row.get("phase_attempt_id") or "")
        schedule = schedule_by_logical.get(logical_id)
        seed = row.get("seed")
        if (
            schedule is None
            or not phase_attempt_id
            or isinstance(seed, bool)
            or not isinstance(seed, int)
        ):
            raise CanonicalRunValidationError(
                f"OpenRouter audit row {index} has no journal identity"
            )
        identity = (logical_id, phase_attempt_id, seed)
        physical = physical_by_identity.get(identity)
        if physical is None:
            raise CanonicalRunValidationError(
                f"OpenRouter audit row {index} cannot map to a validation attempt"
            )
        validation_attempt = int(physical["validation_attempt"])
        expected_temperature = schedule["temperatures"][
            validation_attempt - 1
        ]
        if (
            row.get("requested_model") != expected_model
            or row.get("request_policy") != schedule["request_policy"]
            or row.get("response_format") != schedule["response_format"]
            or row.get("max_tokens") != schedule["max_tokens"]
            or isinstance(row.get("temperature"), bool)
            or not isinstance(row.get("temperature"), (int, float))
            or float(row["temperature"]) != expected_temperature
        ):
            raise CanonicalRunValidationError(
                f"Provider request differs from journal schedule at audit row {index}"
            )
        expected_policy_sha = hashlib.sha256(
            json.dumps(
                schedule["request_policy"],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if row.get("request_policy_sha256") != expected_policy_sha:
            raise CanonicalRunValidationError(
                f"Provider request-policy hash mismatch at audit row {index}"
            )
        audit_event = str(row.get("audit_event") or "")
        status = str(row.get("status") or "")
        if audit_event == "provider_attempt":
            if status not in {"provider_returned", "error"}:
                raise CanonicalRunValidationError(
                    f"Invalid provider attempt status at audit row {index}"
                )
            if (
                row.get("accepted_response_sha256") is not None
                or row.get("provider_attempt_sha256") is not None
            ):
                raise CanonicalRunValidationError(
                    f"Provider attempt claims acceptance at audit row {index}"
                )
            provider_rows.append(row)
            provider_rows_by_hash[canonical_sha256(row)] = row
            audit_identities.add(identity)
            if status == "provider_returned":
                provider_returned_identities.add(identity)
        elif audit_event == "experiment_acceptance":
            if status != "accepted":
                raise CanonicalRunValidationError(
                    f"Invalid acceptance status at audit row {index}"
                )
            acceptance_rows.append(row)
        else:
            raise CanonicalRunValidationError(
                f"Unknown audit event at row {index}"
            )
        if status == "error":
            if (
                row.get("error_type") in {None, ""}
                or row.get("provider_response_sha256") is not None
                or row.get("provider_canonical_json_sha256") is not None
            ):
                raise CanonicalRunValidationError(
                    f"Malformed provider error at audit row {index}"
                )
            continue
        if (
            row.get("error_type") is not None
            or row.get("error") is not None
            or str(row.get("returned_model") or "") != expected_model
            # OpenRouter는 요청에 슬러그("alibaba")를 받고 응답에는 표시명
            # ("Alibaba")을 돌려준다. 정확 문자열 비교는 정상 응답을 전부
            # 거부해서, 완주한 run도 사후 검증을 통과할 수 없었다. call_policy는
            # 같은 이유로 이미 casefold 비교를 쓰고 있었는데 이쪽만 어긋나 있었다.
            # 라우팅 자체는 request_policy의 provider.only와 allow_fallbacks=false가
            # 고정하므로 여기서는 표기 차이만 흡수한다.
            or str(row.get("provider") or "").casefold()
            != expected_provider.casefold()
            or row.get("response_reasoning_present") is not False
            or row.get("reasoning_tokens") != 0
            or not isinstance(row.get("usage"), dict)
            or not str(row.get("request_id") or "").strip()
            or not _sha256_text(row.get("provider_response_sha256"))
        ):
            raise CanonicalRunValidationError(
                f"Provider return does not prove strict successful execution "
                f"at audit row {index}"
            )
        # provider가 200 본문 안에서 finish_reason="error"로 실패를 알리는 경우가
        # 있다(전송 오류가 아니라 정상 응답 형식). 그 시도는 검증기가 거부하고
        # 다음 시도가 채택되므로 오염된 본문이 실험에 들어가지 않는다. 지켜야 할
        # 성질은 "모든 provider 반환이 깨끗했다"가 아니라 "채택된 응답은 전부
        # 깨끗한 실행에서 나왔다"이므로, 채택된 시도에만 stop을 요구한다.
        # 45일 완주 run에서 78,251건 중 1건이 이 경로였고(A083 2026-04-03/AM
        # market_analysis, attempt 1 거부 → attempt 2 채택), 옛 검사는 그 1건
        # 때문에 정상 run 전체를 publication_ready에서 탈락시켰다.
        if (
            str(physical.get("status") or "") == "accepted"
            and str(row.get("finish_reason") or "") != "stop"
        ):
            raise CanonicalRunValidationError(
                f"Accepted response came from a non-stop provider return "
                f"at audit row {index}"
            )

    # A physical stage attempt invokes one provider call with possible internal
    # retries.  Every journal attempt must therefore have provider telemetry;
    # every accepted/rejected validation attempt must have a returned body.
    if set(physical_by_identity) - audit_identities:
        raise CanonicalRunValidationError(
            "Journal physical attempts are missing provider telemetry"
        )
    for identity, physical in physical_by_identity.items():
        if (
            str(physical["status"]) in {"accepted", "rejected"}
            and identity not in provider_returned_identities
        ):
            raise CanonicalRunValidationError(
                f"Validated journal attempt lacks a provider return: {identity}"
            )

    acceptance_by_logical: dict[str, dict[str, Any]] = {}
    for row in acceptance_rows:
        logical_id = str(row["logical_call_id"])
        if logical_id in acceptance_by_logical:
            raise CanonicalRunValidationError(
                f"Duplicate experiment acceptance: {logical_id}"
            )
        provider_row = provider_rows_by_hash.get(
            str(row.get("provider_attempt_sha256") or "")
        )
        if provider_row is None:
            raise CanonicalRunValidationError(
                f"Acceptance does not bind an exact provider row: {logical_id}"
            )
        logical = logical_by_id.get(logical_id)
        accepted_physical = accepted_physical_by_logical.get(logical_id)
        if logical is None or accepted_physical is None:
            raise CanonicalRunValidationError(
                f"Acceptance has no committed journal row: {logical_id}"
            )
        schedule = schedule_by_logical[logical_id]
        expected_seed = schedule["seeds"][
            schedule["accepted_validation_attempt"] - 1
        ]
        if (
            row.get("phase_attempt_id")
            != accepted_physical["phase_attempt_id"]
            or row.get("seed") != expected_seed
            or row.get("accepted_response_sha256")
            != logical["response_sha256"]
            or row.get("provider_canonical_json_sha256")
            != logical["response_sha256"]
        ):
            raise CanonicalRunValidationError(
                f"Acceptance differs from committed journal response: {logical_id}"
            )
        acceptance_by_logical[logical_id] = row
    if set(acceptance_by_logical) != set(logical_by_id):
        raise CanonicalRunValidationError(
            "Every committed logical response requires one provider acceptance"
        )

    checkpoint_responses = checkpoint.get("event_response_sha256")
    if not isinstance(checkpoint_responses, Mapping):
        raise CanonicalRunValidationError(
            "Checkpoint lacks event response digests"
        )
    observed_by_event: dict[str, dict[str, str]] = defaultdict(dict)
    for logical_id, row in logical_by_id.items():
        observed_by_event[str(row["event_id"])][logical_id] = str(
            row["response_sha256"]
        )
    expected_by_event = {
        event_id: {
            str(logical_id): str(digest)
            for logical_id, digest in dict(
                checkpoint_responses.get(event_id) or {}
            ).items()
        }
        for event_id in event_ids
    }
    if (
        set(checkpoint_responses) != set(event_ids)
        or dict(observed_by_event) != expected_by_event
    ):
        raise CanonicalRunValidationError(
            "Committed response journal differs from event checkpoint digests"
        )

    summary = {
        "schema_version": "integrated-response-journal-v1",
        "path": str(journal_path),
        "logical_counts": {
            "accepted/committed": len(logical_rows)
        },
        "physical_attempt_counts": dict(
            Counter(str(row["status"]) for row in physical_rows)
        ),
        "stage_labels": sorted(
            {str(row["stage"]) for row in logical_rows}
        ),
    }
    terminal_summary = terminal.get("response_journal")
    if terminal_summary != summary:
        raise CanonicalRunValidationError(
            "run_complete response-journal summary differs from the DB"
        )
    return {
        "status": "pass",
        "logical_response_count": len(logical_rows),
        "physical_validation_attempt_count": len(physical_rows),
        "provider_attempt_count": len(provider_rows),
        "experiment_acceptance_count": len(acceptance_rows),
        "stage_labels": summary["stage_labels"],
        "temperature_schedules": {
            logical_id: schedule["temperatures"]
            for logical_id, schedule in schedule_by_logical.items()
        },
    }


def validate_canonical_run(
    run_dir: Path | str,
    *,
    publication_ready: bool = True,
    verify_logs: bool = True,
) -> dict[str, Any]:
    """Validate the run-local canonical ledger before analysis or reporting.

    Publication mode accepts only a complete frozen schedule with finalized
    outcomes.  A segment can be inspected by passing ``publication_ready=False``,
    but the returned status remains ``segment_valid_not_publication_ready``.
    """

    root = Path(run_dir).resolve()
    runtime_root = root / ".runtime"
    signature_record = _read_json_object(
        root / "run_signature.json",
        "run signature",
    )
    checkpoint = _read_json_object(
        runtime_root / "checkpoint.json",
        "event checkpoint",
    )
    metadata = _read_json_object(
        root / "run_metadata.json",
        "run metadata",
    )
    signature_payload = signature_record.get("signature_payload")
    if not isinstance(signature_payload, dict):
        raise CanonicalRunValidationError(
            "Run signature lacks signature_payload"
        )
    signature_sha256 = canonical_sha256(signature_payload)
    if (
        signature_record.get("signature_sha256") != signature_sha256
        or checkpoint.get("signature_sha256") != signature_sha256
        or metadata.get("run_signature_sha256") != signature_sha256
    ):
        raise CanonicalRunValidationError(
            "Run signature/checkpoint/metadata hashes disagree"
        )
    parameters = signature_payload.get("parameters")
    if not isinstance(parameters, dict):
        raise CanonicalRunValidationError(
            "Run signature parameters are missing"
        )
    event_ids = parameters.get("event_ids")
    agent_ids = parameters.get("agent_ids")
    raw_depths = parameters.get("agent_depths")
    raw_post_permissions = parameters.get("community_post_permissions")
    if (
        not isinstance(event_ids, list)
        or not event_ids
        or any(not isinstance(value, str) for value in event_ids)
        or len(event_ids) != len(set(event_ids))
        or not isinstance(agent_ids, list)
        or not agent_ids
        or any(not isinstance(value, str) for value in agent_ids)
        or len(agent_ids) != len(set(agent_ids))
        or not isinstance(raw_depths, dict)
    ):
        raise CanonicalRunValidationError(
            "Run signature has an invalid event/cohort definition"
        )
    depth_by_agent = {
        str(agent_id): int(depth)
        for agent_id, depth in raw_depths.items()
    }
    if (
        set(depth_by_agent) != set(agent_ids)
        or any(depth not in {0, 1, 2} for depth in depth_by_agent.values())
    ):
        raise CanonicalRunValidationError(
            "Run signature depth map differs from the cohort"
        )
    if raw_post_permissions is None:
        # Compatibility reader for runs created before posting permission was
        # separated from depth. New signatures always bind the explicit map.
        post_permission_by_agent = {
            agent_id: depth_by_agent[agent_id] in {1, 2}
            for agent_id in agent_ids
        }
    elif not isinstance(raw_post_permissions, dict):
        raise CanonicalRunValidationError(
            "Run signature has an invalid community post permission map"
        )
    else:
        if any(
            not isinstance(permission, bool)
            for permission in raw_post_permissions.values()
        ):
            raise CanonicalRunValidationError(
                "Run signature community post permissions must be booleans"
            )
        post_permission_by_agent = {
            str(agent_id): permission
            for agent_id, permission in raw_post_permissions.items()
        }
        if set(post_permission_by_agent) != set(agent_ids):
            raise CanonicalRunValidationError(
                "Run signature community post permission map differs from the cohort"
            )
    completed = checkpoint.get("completed_events")
    if completed != event_ids or checkpoint.get("inflight_event") is not None:
        raise CanonicalRunValidationError(
            "Run checkpoint is incomplete or has an inflight event"
        )

    run_complete_path = root / "run_complete.json"
    segment_complete_path = root / "segment_complete.json"
    if publication_ready:
        terminal = _read_json_object(
            run_complete_path,
            "publication run completion marker",
        )
        expected_status = "complete"
        full_schedule = True
        if segment_complete_path.exists():
            raise CanonicalRunValidationError(
                "A publication run may not also carry a segment marker"
            )
    else:
        if run_complete_path.is_file():
            terminal = _read_json_object(
                run_complete_path,
                "run completion marker",
            )
            expected_status = "complete"
            full_schedule = True
        else:
            terminal = _read_json_object(
                segment_complete_path,
                "segment completion marker",
            )
            expected_status = "segment_complete"
            full_schedule = False

    if (
        checkpoint.get("status") != expected_status
        or metadata.get("status") != expected_status
        or terminal.get("status") != expected_status
        or bool(terminal.get("full_frozen_schedule")) != full_schedule
        or int(terminal.get("completed_event_count") or -1) != len(event_ids)
        or int(terminal.get("event_count") or -1) != len(event_ids)
    ):
        raise CanonicalRunValidationError(
            "Terminal marker, checkpoint, and metadata status disagree"
        )
    if publication_ready and (
        not bool(terminal.get("schedule_complete"))
        or not bool(terminal.get("outcome_finalized"))
    ):
        raise CanonicalRunValidationError(
            "Publication run lacks finalized schedule/outcomes"
        )
    if publication_ready and (
        parameters.get("logging_enabled") is not True
        or parameters.get("offline_llm") is True
    ):
        raise CanonicalRunValidationError(
            "Publication runs require provenance logs and a non-offline model"
        )

    state_hashes = checkpoint.get("event_state_sha256")
    integrity_hashes = checkpoint.get("event_integrity_sha256")
    if (
        not isinstance(state_hashes, dict)
        or set(state_hashes) != set(event_ids)
        or not isinstance(integrity_hashes, dict)
        or set(integrity_hashes) != set(event_ids)
        or any(not _sha256_text(value) for value in state_hashes.values())
        or any(not _sha256_text(value) for value in integrity_hashes.values())
    ):
        raise CanonicalRunValidationError(
            "Event state/integrity digest coverage is incomplete"
        )
    committed_db = runtime_root / "committed.db"
    if not committed_db.is_file():
        raise CanonicalRunValidationError(
            f"Canonical committed DB is missing: {committed_db}"
        )
    committed_sha256 = file_sha256(committed_db)
    if (
        checkpoint.get("committed_database_sha256") != committed_sha256
        or state_hashes[event_ids[-1]] != committed_sha256
        or metadata.get("committed_database_sha256") != committed_sha256
    ):
        raise CanonicalRunValidationError(
            "Canonical committed DB hash differs from checkpoint/metadata"
        )
    observed_artifact_sha256 = artifact_tree_sha256(root)
    if (
        checkpoint.get("artifact_tree_sha256")
        != observed_artifact_sha256
        or metadata.get("artifact_tree_sha256")
        != observed_artifact_sha256
    ):
        raise CanonicalRunValidationError(
            "Analysis-visible artifact tree differs from the committed prefix"
        )

    completed_events: list[dict[str, Any]] = []
    with sqlite3.connect(committed_db) as connection:
        turn_rows = connection.execute(
            """
            SELECT date, subturn, MIN(turn), MAX(turn)
            FROM simulation_stb_states
            GROUP BY date, subturn
            """
        ).fetchall()
    turn_by_event = {}
    for event_date, subturn, min_turn, max_turn in turn_rows:
        if int(min_turn) != int(max_turn):
            raise CanonicalRunValidationError(
                f"Event maps to multiple turns: {event_date}/{str(subturn).upper()}"
            )
        turn_by_event[f"{event_date}/{str(subturn).upper()}"] = int(min_turn)
    for event_id in event_ids:
        try:
            event_date, raw_subturn = event_id.rsplit("/", 1)
        except ValueError as exc:
            raise CanonicalRunValidationError(
                f"Invalid event ID: {event_id}"
            ) from exc
        subturn = raw_subturn.lower()
        if subturn not in {"am", "pm"}:
            raise CanonicalRunValidationError(
                f"Invalid event subturn: {event_id}"
            )
        if event_id not in turn_by_event:
            raise CanonicalRunValidationError(
                f"Canonical DB has no turn for event: {event_id}"
            )
        completed_events.append(
            {
                "event_id": event_id,
                "turn": turn_by_event[event_id],
                "date": event_date,
                "subturn": subturn,
            }
        )
    try:
        final_integrity_sha256 = assert_integrated_event_state(
            committed_db,
            agent_ids=agent_ids,
            completed_events=completed_events,
            stock_code=str(parameters.get("stock_code") or ""),
        )
    except ExperimentCheckpointError as exc:
        raise CanonicalRunValidationError(
            f"Canonical STB→analysis→decision→fill→LTB ledger failed: {exc}"
        ) from exc
    if integrity_hashes[event_ids[-1]] != final_integrity_sha256:
        raise CanonicalRunValidationError(
            "Final event integrity digest does not match the committed DB"
        )

    sealed_inputs = signature_payload.get("sealed_inputs")
    if not isinstance(sealed_inputs, dict):
        raise CanonicalRunValidationError(
            "Run signature sealed_inputs are missing"
        )
    news_input = sealed_inputs.get("news_bundle")
    if not isinstance(news_input, dict):
        raise CanonicalRunValidationError(
            "Run signature does not bind the sealed news bundle"
        )
    news_path = Path(str(news_input.get("path") or ""))
    if (
        not news_path.is_file()
        or file_sha256(news_path) != str(news_input.get("sha256") or "")
    ):
        raise CanonicalRunValidationError(
            "Sealed news file is missing or differs from the run signature"
        )
    news_coverage = validate_sealed_news_coverage(
        news_path,
        event_ids=event_ids,
        expected_stock_code=str(parameters.get("stock_code") or ""),
    )
    bundle = SealedNewsBundle.load(
        news_path,
        expected_stock_code=str(parameters.get("stock_code") or ""),
    )
    if publication_ready:
        if set(event_ids) != set(bundle.slots_by_event):
            raise CanonicalRunValidationError(
                "Publication run does not cover the complete sealed news schedule"
            )

    community_mode = str(parameters.get("community_mode") or "")
    database_report = _validate_canonical_database(
        committed_db,
        agent_ids=agent_ids,
        depth_by_agent=depth_by_agent,
        post_permission_by_agent=post_permission_by_agent,
        completed_events=completed_events,
        community_mode=community_mode,
        full_schedule=full_schedule,
        outcome_schedule_complete=(
            event_ids[-1] == max(bundle.slots_by_event)
        ),
    )
    log_report: dict[str, Any] | None = None
    if verify_logs:
        try:
            log_report = validate_log_bundle(
                root,
                dates=[
                    str(event["date"])
                    for event in completed_events
                    if str(event["subturn"]) == "am"
                ],
                agent_ids=list(agent_ids),
                community_audience_agent_ids=list(agent_ids),
                turn_offset=min(int(event["turn"]) for event in completed_events) - 1,
                fake_news_mode="off",
                daily_news_csv=None,
                community_mode=community_mode,
                sealed_news_bundle=news_path,
                stock_code=str(parameters.get("stock_code") or ""),
                post_permission_by_agent=post_permission_by_agent,
            )
        except RuntimeError as exc:
            raise CanonicalRunValidationError(
                f"Analysis-visible log bundle failed: {exc}"
            ) from exc
    elif publication_ready:
        raise CanonicalRunValidationError(
            "Publication validation may not skip provenance logs"
        )
    response_audit_report: dict[str, Any] | None = None
    if publication_ready:
        response_audit_report = validate_response_journal_audit(
            root,
            signature_payload=signature_payload,
            checkpoint=checkpoint,
            terminal=terminal,
        )

    return {
        "status": (
            "publication_ready"
            if publication_ready
            else (
                "complete_valid"
                if full_schedule
                else "segment_valid_not_publication_ready"
            )
        ),
        "run_dir": str(root),
        "canonical_db": str(committed_db),
        "run_signature_sha256": signature_sha256,
        "committed_database_sha256": committed_sha256,
        "final_integrity_sha256": final_integrity_sha256,
        "artifact_tree_sha256": observed_artifact_sha256,
        "event_count": len(event_ids),
        "agent_count": len(agent_ids),
        "community_mode": community_mode,
        "news_coverage": news_coverage,
        "database": database_report,
        "logs": log_report,
        "response_audit": response_audit_report,
    }


def require_publication_ready_run(
    run_dir: Path | str,
) -> dict[str, Any]:
    """Fail-closed entry point for report and paper-analysis code."""

    return validate_canonical_run(
        run_dir,
        publication_ready=True,
        verify_logs=True,
    )
