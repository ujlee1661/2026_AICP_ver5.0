#!/usr/bin/env python3
"""통합 메인 실행기가 사용하는 봉인 실험 입력을 재생성한다.

네트워크와 LLM에는 접촉하지 않는다. 구조화된 ``sys_100.db``에서
페르소나를 재투영하고, 달력·가격·뉴스·프롬프트·StudySpec을 하나의
검증 가능한 입력 묶음으로 만든다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from twinmarket_kr.experiment_runtime import (  # noqa: E402
    canonical_sha256,
    file_sha256,
)
from twinmarket_kr.agents.news_agent import SealedNewsBundle  # noqa: E402
from twinmarket_kr.db.connection import connect  # noqa: E402
from twinmarket_kr.persona.select import (  # noqa: E402
    PERSONA_RENDERER_ID,
    generate_persona_prompt,
    persona_renderer_sha256,
    structured_persona_sha256,
)
from twinmarket_kr.study_spec import (  # noqa: E402
    PRODUCTION_PROMPT_FILENAMES,
    integrated_prompt_bundle_sha256,
)

TZ = "+09:00"


def _cbytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(label: str) -> str:
    return hashlib.sha256(f"rn-sealed::{label}".encode("utf-8")).hexdigest()


def _write(path: Path, value) -> bytes:
    b = _cbytes(value)
    path.write_bytes(b)
    return b


def trading_dates(
    sim_db: Path,
    *,
    stock_code: str,
    start_date: str,
    end_date: str,
) -> list[str]:
    with connect(sim_db, read_only=True) as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM StockData WHERE stock_id=? AND date BETWEEN ? AND ? ORDER BY date",
            (stock_code, start_date, end_date)).fetchall()]


def stock_prices(
    sim_db: Path,
    *,
    stock_code: str,
    start_date: str,
    end_date: str,
) -> dict[str, dict]:
    with connect(sim_db, read_only=True) as c:
        rows = c.execute(
            "SELECT date, open_price, close_price FROM StockData WHERE stock_id=? AND date BETWEEN ? AND ?",
            (stock_code, start_date, end_date)).fetchall()
    out = {}
    prev_close = None
    for d, op, cp in sorted(rows, key=lambda row: str(row[0])):
        out[d] = {"open": op, "close": cp, "prev_close": prev_close if prev_close is not None else op}
        prev_close = cp
    return out


def build_event(day: str, prev_day: str | None, ordinal: int, subturn: str) -> dict:
    # 첫 거래일은 이전 거래일이 없으므로 전 캘린더일(day-1)을 AM 윈도우 시작으로 쓴다.
    prev = prev_day or (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if subturn == "AM":
        ts = f"{day}T09:00:00{TZ}"
        win = {"start_exclusive": f"{prev}T15:30:00{TZ}", "end_inclusive": f"{day}T08:59:00{TZ}"}
    else:
        ts = f"{day}T15:30:00{TZ}"
        win = {"start_exclusive": f"{day}T08:59:00{TZ}", "end_inclusive": ts}
    return {
        "decision_event_id": f"{day}/{subturn}",
        "event_ordinal_in_date": ordinal,
        "subturn": subturn,
        "decision_timestamp": ts,
        "news_window": win,
        "market_feature_as_of": ts,
        "execution_price_field": "actual_open" if subturn == "AM" else "actual_close",
        "consume_scheduled_community": subturn == "AM",
        "decision_enabled": True,
    }


def build_calendar(dates: list[str]) -> dict:
    date_rows = []
    for i, d in enumerate(dates):
        prev = dates[i - 1] if i > 0 else None
        date_rows.append({
            "date": d,
            "timezone": "Asia/Seoul",
            "decision_events": [build_event(d, prev, 1, "AM"), build_event(d, prev, 2, "PM")],
            "post_decision_phases": [{
                "phase_id": f"{d}/community",
                "after_event_id": f"{d}/PM",
                "next_visible_event_rule": "next-approved-AM",
            }],
        })
    return {"artifact_type": "calendar_event_registry", "version": "calendar-v1", "dates": date_rows}


def build_stage_inputs(calendar: dict, prices: dict) -> dict:
    events = []
    for dr in calendar["dates"]:
        px = prices[dr["date"]]
        for ev in dr["decision_events"]:
            ref = px["open"] if ev["subturn"] == "AM" else px["close"]
            events.append({
                "event_id": ev["decision_event_id"],
                "date": dr["date"],
                "subturn": ev["subturn"].lower(),
                "news_cutoff_timestamp": ev["news_window"]["end_inclusive"],
                "market_feature_as_of": ev["market_feature_as_of"],
                "market": {
                    "reference_price": ref,
                    "previous_close": px["prev_close"],
                    "open_price": px["open"],
                    "as_of_timestamp": ev["market_feature_as_of"],
                },
            })
    return {
        "artifact_type": "rn_stage_input_registry",
        "version": "stage-input-v1",
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "events": events,
    }


def build_prices(calendar: dict, prices: dict, *, stock_code: str) -> dict:
    events = []
    for dr in calendar["dates"]:
        px = prices[dr["date"]]
        for ev in dr["decision_events"]:
            events.append({
                "decision_event_id": ev["decision_event_id"],
                "date": dr["date"],
                "subturn": ev["subturn"],
                "execution_price_field": ev["execution_price_field"],
                "execution_price": px["open"] if ev["subturn"] == "AM" else px["close"],
            })
    return {
        "artifact_type": "event_price_registry",
        "version": "prices-v1",
        "stock_code": stock_code,
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "events": events,
    }


def build_persona_projection(
    sys_db: Path,
    *,
    agent_ids: list[str] | None = None,
    instrument_name: str = "삼성전자",
) -> dict:
    with connect(sys_db, read_only=True) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM agents ORDER BY agent_id"
            ).fetchall()
        ]
    by_id = {str(row["agent_id"]): row for row in rows}
    if len(by_id) != len(rows):
        raise RuntimeError("structured persona DB contains duplicate agent IDs")
    selected_ids = (
        sorted(by_id)
        if agent_ids is None
        else [str(agent_id).strip() for agent_id in agent_ids]
    )
    if (
        not selected_ids
        or any(not agent_id for agent_id in selected_ids)
        or len(selected_ids) != len(set(selected_ids))
    ):
        raise ValueError("selected agent IDs must be non-empty and unique")
    missing = [agent_id for agent_id in selected_ids if agent_id not in by_id]
    if missing:
        raise ValueError(
            f"selected agent IDs are absent from the structured DB: {missing[:5]}"
        )
    agents = []
    for ordinal, agent_id in enumerate(selected_ids, start=1):
        row = by_id[agent_id]
        prompt = generate_persona_prompt(
            row,
            instrument_name=instrument_name,
        )
        agents.append(
            {
                "ordinal": ordinal,
                "agent_id": str(row["agent_id"]),
                "news_depth": int(row["news_depth"]),
                "can_post_community": int(row["can_post_community"]),
                "initial_cash": int(row["ini_cash"]),
                "structured_persona_sha256": structured_persona_sha256(
                    row
                ),
                "persona_sha256": hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest(),
            }
        )
    return {
        "artifact_type": "persona_projection_manifest",
        "version": "integrated-persona-projection-v2",
        "source_db_sha256": file_sha256(sys_db),
        "renderer": {
            "id": PERSONA_RENDERER_ID,
            "sha256": persona_renderer_sha256(),
            "normalization": (
                "NFC; LF-only; exactly-one-trailing-LF; legacy-content"
            ),
        },
        "agents": agents,
    }


def build_cohort(persona_projection: dict, slots_csv: Path) -> dict:
    meta = {
        str(row["agent_id"]): row
        for row in persona_projection["agents"]
    }
    slot_sha = {}
    with slots_csv.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            slot_sha[row["agent_id"]] = hashlib.sha256(_cbytes(row)).hexdigest()
    agents = []
    ordered_ids = [
        str(row["agent_id"])
        for row in persona_projection["agents"]
    ]
    for ordinal, aid in enumerate(ordered_ids, start=1):
        agents.append({
            "ordinal": ordinal,
            "agent_id": aid,
            "news_depth": int(meta[aid]["news_depth"]),
            "can_post_community": int(meta[aid]["can_post_community"]),
            "initial_cash": int(meta[aid]["initial_cash"]),
            "persona_sha256": str(meta[aid]["persona_sha256"]),
            "fixed_slot_sha256": slot_sha.get(aid, _digest(f"{aid}-slot")),
        })
    return {"artifact_type": "cohort_registry", "version": "cohort-v2", "agents": agents}


def load_agent_ids(path: Path | None) -> list[str] | None:
    """Load an explicit cohort as a JSON array or one agent ID per line."""

    if path is None:
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("agent ID file must not be empty")
    if text.startswith("["):
        value = json.loads(text)
        if not isinstance(value, list):
            raise ValueError("agent ID JSON must be an array")
        agent_ids = [str(item).strip() for item in value]
    else:
        agent_ids = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not agent_ids or any(not agent_id for agent_id in agent_ids):
        raise ValueError("agent ID file must contain at least one valid ID")
    if len(agent_ids) != len(set(agent_ids)):
        raise ValueError("agent ID file contains duplicate IDs")
    return agent_ids


def build_review(
    *,
    news_bundle: SealedNewsBundle,
    calendar: dict,
    stage_inputs: dict,
    news_file_sha256: str,
) -> dict:
    """Record only checks that the sealer actually executed.

    The previous sealer synthesized reviewer credentials and fixed review
    timestamps.  Those values did not prove a real review.  The canonical
    artifact now records the deterministic validation boundary that was
    actually run by :meth:`SealedNewsBundle.load`.

    ``news_file_sha256`` is the digest of the ``news.json`` this sealer wrote
    beside this review, not of the source candidate manifest it read.  A reader
    of the sealed directory can therefore verify the field against the bundle
    that is actually shipped here.
    """

    return {
        "artifact_type": "deterministic_news_safety_validation",
        "version": "sealed-news-validation-v1",
        "validator": (
            "twinmarket_kr.agents.news_agent.SealedNewsBundle.load"
        ),
        "real_news_bundle_manifest_sha256": news_bundle.bundle_sha256,
        "real_news_bundle_file_sha256": news_file_sha256,
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "stage_input_registry_canonical_sha256": canonical_sha256(stage_inputs),
        "checks": [
            "exact_bundle_and_article_schema",
            "bundle_and_payload_sha256",
            "real_only_fake_registry_closure",
            "unique_article_and_payload_per_event",
            "published_observed_modified_not_after_event_cutoff",
            "shortage_record_matches_delivered_slots",
            "visible_text_fake_and_target_leakage_pattern_scan",
        ],
        "article_count": len(news_bundle.articles),
        "event_count": len(news_bundle.slots_by_event),
        "shortage_event_count": len(news_bundle.accepted_shortages),
        "validation_pass": True,
    }


def build_spec(
    *,
    cohort,
    calendar,
    prices,
    stage_inputs,
    stage_file_sha,
    persona_projection,
    prompt_bundle_sha,
    news_sha,
    injection_sha,
    review_sha,
    dates,
    cohort_obj,
    stock_code,
    instrument_name,
    target_real_news_per_event,
) -> dict:
    news_exposure_policy = {
        "version": "rn-news-exposure-policy-v1",
        "target_real_news_per_event": target_real_news_per_event,
        "fake_news_per_event": 0,
        "shortage_policy": "accepted_shortage_no_synthetic_or_duplicate_v1",
        "category_targets": {
            "stock": 5,
            "sector": 3,
            "economy": 2,
        },
        "cross_category_backfill": False,
    }
    community_timing_policy = {
        "timezone": "Asia/Seoul", "pm_phase_not_before": "15:30:00", "pm_phase_not_after": "23:59:59",
        "next_am_delivery_not_before": "08:00:00", "next_am_delivery_not_after": "09:00:00",
    }
    depth_counts = Counter(str(a["news_depth"]) for a in cohort_obj["agents"])
    post_permission_counts = Counter(
        str(a["can_post_community"])
        for a in cohort_obj["agents"]
    )
    cash_counts = Counter(str(a["initial_cash"]) for a in cohort_obj["agents"])
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT).decode().strip()
    return {
        "artifact_type": "study_spec",
        "study_id": "rn-community-ab-2026",
        "design_version": "3.0.0",
        "baseline_commit": commit,
        "required_agent_count": len(cohort_obj["agents"]),
        "cohort_registry_sha256": canonical_sha256(cohort),
        "persona_projection_manifest_sha256": canonical_sha256(
            persona_projection
        ),
        "persona_assignment_policy": "frozen-db-map-prompt-projection-only",
        "persona_renderer_sha256": persona_renderer_sha256(),
        "prompt_bundle_sha256": prompt_bundle_sha,
        # 한도의 정본은 config 하나다. 여기에 literal을 다시 적으면 config를
        # 바꿔도 봉인 spec이 따라오지 않는다.
        "belief_limits": dict(config.BELIEF_LIMITS),
        "cohort_assertions": {
            "depth_counts": dict(depth_counts),
            "initial_cash_counts": dict(cash_counts),
            "community_post_permission_counts": dict(post_permission_counts),
        },
        "condition_treatments": {
            "RN_COMM_OFF": {"community_mode": "off", "news_treatment": "real_only"},
            "RN_COMM_ON": {"community_mode": "on", "news_treatment": "real_only"},
        },
        "instrument": {
            "stock_code": stock_code,
            "display_name": instrument_name,
        },
        "paired_condition_groups": [["RN_COMM_OFF", "RN_COMM_ON"]],
        "treatment_diff_allowlist": ["community_mode"],
        "calendar_event_registry_sha256": canonical_sha256(calendar),
        "event_price_registry_sha256": canonical_sha256(prices),
        "burn_in_date_ids": dates[:3],
        "regime_policy_sha256": _digest("regime"),
        "real_news_bundle_manifest_sha256": news_sha,
        "known_injection_registry_sha256": injection_sha,
        "article_version_leakage_review_manifest_sha256": review_sha,
        "news_exposure_policy_sha256": canonical_sha256(news_exposure_policy),
        "news_exposure_policy": news_exposure_policy,
        "community_policy": {
            "best_k": 5, "best_selection_policy": "top_k_or_fewer_available_no_forced_posting",
            "posting_permission_from_cohort_flag": "can_post_community",
            "reading_permissions_from_cohort_depth_map": True,
            "depth1_selective_read_cap": 5,
            "depth2_selective_read_cap": 5, "best_payload": "title_plus_full_frozen_body",
            "visibility": "next_approved_am_decision_event",
        },
        "community_timing_policy": community_timing_policy,
        "community_timing_policy_sha256": canonical_sha256(community_timing_policy),
        "context_window_policy": {
            "decision_historical_order_or_fill_direct_visibility": "forbidden",
            "community_public_author_private_portfolio_or_trade_visibility": (
                "d2_frozen_pm_portfolio_summary_plus_recent_3_filled_trades"
            ),
            "trade_memory_visibility": "postfill-ltb-only",
            "depth2_search_lookback": 7, "depth2_search_lookback_unit": "calendar_days",
            "depth2_search_top_k": 5,
            "news_category_targets": {"stock": 5, "sector": 3, "economy": 2},
            "market_feature_policy_sha256": _digest("market-features"),
        },
        "memory_policy": {
            "version": "stb-ltb-v4", "cadence": "each_manifest_decision_event",
            "trade_belief_blocks": "previous_ltb_plus_current_stb_separate_blocks",
            "ltb_update_timing": "after-fill-before-commit",
            "current_transaction_episode_input": "once-same-turn-dim6",
            "price_outcome_input": "eligible-earlier-dim6",
            "outcome_horizons": [
                "next-decision-event",
                "same-subturn-plus-1-trading-date",
                "same-subturn-plus-5-trading-dates",
            ],
        },
        "trade_policy": {
            "stock_code": stock_code, "decision_space": ["buy", "sell"], "allow_hold": False,
            "max_single_trade_cash_ratio": 0.5, "fill_policy": "full_fill_at_event_reference_price",
            "commission_rate": 0.0, "commission_applies_to": [], "sell_tax_rate": 0.0,
            "fee_policy": "zero_fee_baseline_all_fee_amounts_must_be_zero",
            "target_direction_notional": "gross_signed_fill_value",
        },
        "model_policy": {
            "model": config.PAPER_OPENROUTER_MODEL,
            "provider": config.PAPER_OPENROUTER_PROVIDER,
            "reasoning": {"effort": "none", "exclude": True},
            "per_arm_max_concurrent_llm_calls": 8,
            # 논리 호출 1건이 보낼 수 있는 물리 HTTP 시도 상한. 이전 봉인은 1로
            # 적었지만 실제 클라이언트는 config.OPENROUTER_MAX_RETRIES 만큼
            # 재시도한다. 선언과 구현이 갈라지지 않도록 config에서 읽는다.
            "max_physical_http_attempts_per_logical_call": int(
                config.OPENROUTER_MAX_RETRIES
            ),
            "allow_provider_fallbacks": False, "require_parameters": True,
            "reasoning_off_canary_required": True,
            "reasoning_off_success_contract": "provider-model-match-reasoning-empty-tokens-zero",
        },
        "study_seed": 2, "seed_namespace": "study-agent-event-stage-attempt-v1",
        "retry_policy_sha256": _digest("retry"),
        "runtime_policy_sha256": _digest("runtime"),
        "evaluation_policy_sha256": _digest("evaluation"),
        "stage_input_registry_file_sha256": stage_file_sha,
        "stage_input_registry_canonical_sha256": canonical_sha256(stage_inputs),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="통합 메인 실험 입력 세트 생성.")
    p.add_argument("--out", type=Path, default=PROJECT_ROOT / "preparation/rn_ab_sealed_v1")
    p.add_argument("--sys-db", type=Path, default=config.SYS_100_DB)
    p.add_argument(
        "--agent-ids-file",
        type=Path,
        default=None,
        help=(
            "선택 사항. JSON 배열 또는 한 줄당 한 agent_id로 봉인 cohort를 "
            "명시합니다. 생략하면 structured DB의 모든 agent를 사용합니다."
        ),
    )
    p.add_argument("--slots-csv", type=Path, default=PROJECT_ROOT / "data/fixed_slots.csv")
    p.add_argument("--sim-db", type=Path, default=PROJECT_ROOT / "outputs/sim.db")
    p.add_argument("--prompt-dir", type=Path, default=config.PROMPT_DIR)
    p.add_argument("--news", type=Path,
                   default=PROJECT_ROOT / "preparation/rn_ab_source_candidate_v1/real_news_bundle_manifest.json")
    p.add_argument(
        "--stock-code",
        default=None,
        help="기본값은 봉인 뉴스 bundle의 stock_code입니다.",
    )
    p.add_argument(
        "--instrument-name",
        default="삼성전자",
        help=(
            "봉인 profile과 LTB₀에 기록할 사람이 읽는 종목명입니다. "
            "다른 종목 profile을 만들 때 반드시 함께 바꾸십시오."
        ),
    )
    p.add_argument(
        "--start-date",
        default=None,
        help="기본값은 뉴스 slot에 들어 있는 첫 거래일입니다.",
    )
    p.add_argument(
        "--end-date",
        default=None,
        help="기본값은 뉴스 slot에 들어 있는 마지막 거래일입니다.",
    )
    args = p.parse_args(argv)

    out = args.out
    if out.exists():
        raise FileExistsError(
            f"refusing to overwrite an existing sealed profile: {out}"
        )

    news_bundle = SealedNewsBundle.load(
        args.news,
        expected_stock_code=None,
    )
    news = json.loads(args.news.read_text(encoding="utf-8"))
    news_dates = sorted(
        {
            str(slot["event_id"]).split("/", 1)[0]
            for slot in news["slots"]
        }
    )
    if not news_dates:
        raise RuntimeError("sealed news bundle has no event dates")
    stock_code = str(args.stock_code or news_bundle.stock_code)
    instrument_name = str(args.instrument_name).strip()
    if not instrument_name:
        raise ValueError("--instrument-name must be non-empty")
    if stock_code != news_bundle.stock_code:
        raise RuntimeError(
            "--stock-code differs from the sealed news bundle stock_code"
        )
    start_date = str(args.start_date or news_dates[0])
    end_date = str(args.end_date or news_dates[-1])
    if start_date > end_date:
        raise ValueError("--start-date must not follow --end-date")

    out.mkdir(parents=True)
    prompt_dir = out / "prompts"
    prompt_dir.mkdir(exist_ok=True)
    for filename in PRODUCTION_PROMPT_FILENAMES:
        shutil.copy2(args.prompt_dir / filename, prompt_dir / filename)
    prompt_bundle_sha = integrated_prompt_bundle_sha256(prompt_dir)

    dates = trading_dates(
        args.sim_db,
        stock_code=stock_code,
        start_date=start_date,
        end_date=end_date,
    )
    if not dates:
        raise RuntimeError(
            "the selected stock/date range has no StockData trading dates"
        )
    selected_event_ids = {
        f"{day}/{subturn}"
        for day in dates
        for subturn in ("AM", "PM")
    }
    bundled_event_ids = set(news_bundle.slots_by_event)
    if bundled_event_ids != selected_event_ids:
        raise RuntimeError(
            "news event coverage differs from the selected StockData calendar; "
            f"missing={sorted(selected_event_ids - bundled_event_ids)[:10]} "
            f"unexpected={sorted(bundled_event_ids - selected_event_ids)[:10]}"
        )
    prices = stock_prices(
        args.sim_db,
        stock_code=stock_code,
        start_date=start_date,
        end_date=end_date,
    )
    calendar = build_calendar(dates)
    stage_inputs = build_stage_inputs(calendar, prices)
    price_reg = build_prices(calendar, prices, stock_code=stock_code)
    persona_projection = build_persona_projection(
        args.sys_db,
        agent_ids=load_agent_ids(args.agent_ids_file),
        instrument_name=instrument_name,
    )
    cohort = build_cohort(persona_projection, args.slots_csv)
    injection = {"artifact_type": "known_injection_registry", "version": "known-injections-v1", "entries": []}
    _write(out / "cohort.json", cohort)
    _write(out / "persona_projection.json", persona_projection)
    _write(out / "calendar.json", calendar)
    stage_bytes = _write(out / "stage_inputs.json", stage_inputs)
    _write(out / "prices.json", price_reg)
    news_bytes = _write(out / "news.json", news)
    _write(out / "known_injection.json", injection)

    review = build_review(
        news_bundle=news_bundle,
        calendar=calendar,
        stage_inputs=stage_inputs,
        news_file_sha256=hashlib.sha256(news_bytes).hexdigest(),
    )
    _write(out / "review.json", review)

    spec = build_spec(
        cohort=cohort, calendar=calendar, prices=price_reg,
        stage_inputs=stage_inputs,
        stage_file_sha=hashlib.sha256(stage_bytes).hexdigest(),
        persona_projection=persona_projection,
        prompt_bundle_sha=prompt_bundle_sha,
        news_sha=news["bundle_sha256"], injection_sha=canonical_sha256(injection),
        review_sha=canonical_sha256(review), dates=dates, cohort_obj=cohort,
        stock_code=stock_code,
        instrument_name=instrument_name,
        target_real_news_per_event=news_bundle.target_real_count,
    )
    _write(out / "study_spec.json", spec)

    print("=" * 60)
    print("통합 메인 실험 입력 세트 생성 완료")
    print("=" * 60)
    print(f"거래일 {len(dates)} · event {len(dates)*2} · agent {len(cohort['agents'])}")
    print(
        f"뉴스 기사 {len(news['articles'])} · 슬롯 {len(news['slots'])} · "
        f"shortage event {review['shortage_event_count']}"
    )
    print(f"산출물: {out}/")
    for f in ["study_spec.json", "cohort.json", "persona_projection.json",
              "calendar.json", "stage_inputs.json",
              "prices.json", "news.json", "known_injection.json", "review.json", "prompts/"]:
        print(f"  - {f}")
    print("\n다음: 메인 사전 검증")
    print("  .venv/bin/python scripts/99_validate.py --help")
    print(
        "  TWINMARKET_OFFLINE_LLM=1 .venv/bin/python "
        "scripts/05_run_simulation.py --max-agents 1 --max-days 1 "
        "--community-mode off"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
