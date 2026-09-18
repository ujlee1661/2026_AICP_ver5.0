from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Sequence

import config
from twinmarket_kr.agents.exchange_agent import ExchangeAgent
from twinmarket_kr.agents.fundamental_agent import FundamentalAgent
from twinmarket_kr.agents.memory_agent import MemoryAgent, load_agents_from_sys100
from twinmarket_kr.agents.news_agent import (
    AM_NEWS_WINDOW_END_TIME,
    AM_NEWS_WINDOW_START_TIME,
    PM_NEWS_WINDOW_END_TIME,
    PM_NEWS_WINDOW_START_TIME,
    NewsAgent,
)
from twinmarket_kr.community.agent import CommunityAgent
from twinmarket_kr.community.posting import posting_decision
from twinmarket_kr.community.reading import community_reading_react, community_reading_select
from twinmarket_kr.community.validation import (
    can_author_community_post,
    expected_selective_read_limit,
    validate_selective_read_limits,
)
from twinmarket_kr.core.daily_cycle import run_agent_turn
from twinmarket_kr.db.connection import connect, init_sim_db
from twinmarket_kr.experiment_runtime import ParallelTaskError
from twinmarket_kr.llm.client import (
    OpenRouterClient,
    stable_llm_seed,
    validate_experiment_call_policy,
)
from twinmarket_kr.llm.belief import belief_dimensions, update_long_term_belief
from twinmarket_kr.llm.response_journal import (
    ResponseJournal,
    response_journal_scope,
)
from twinmarket_kr.outcome_schedule import FrozenEventSchedule
from twinmarket_kr.run_logger import (
    SimulationLogger,
    finalize_community_delivery_counts,
)

def _stock_trading_dates(
    sim_db_path: Path | str = config.SIM_DB,
    *,
    stock_code: str = config.STOCK_CODE,
) -> list[str]:
    with connect(sim_db_path) as conn:
        rows = conn.execute(
            "SELECT date FROM StockData WHERE stock_id = ? ORDER BY date",
            (stock_code,),
        ).fetchall()
    return [str(row["date"]) for row in rows]


def _previous_date_map(
    sim_db_path: Path | str = config.SIM_DB,
    *,
    stock_code: str = config.STOCK_CODE,
) -> dict[str, str]:
    dates = _stock_trading_dates(
        sim_db_path,
        stock_code=stock_code,
    )
    return {day: dates[index - 1] for index, day in enumerate(dates) if index > 0}

def _isolated_sim_db_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return config.OUTPUT_DIR / "runtime_dbs" / f"sim_{timestamp}_{os.getpid()}.db"


def _prepare_sim_db(sim_db: Path | str | None) -> Path:
    if sim_db:
        return Path(sim_db)
    source = config.SIM_DB
    if not source.exists():
        raise RuntimeError("outputs/sim.db not found. Run scripts/03_load_stock_data.py first.")
    target = _isolated_sim_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return target


def _acquire_sim_db_lock(sim_db_path: Path) -> Any:
    lock_path = sim_db_path.with_suffix(sim_db_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError(
            f"Simulation DB is already in use: {sim_db_path}. "
            "Use a separate --sim-db path for each concurrent run."
        ) from exc
    lock_file.write(f"pid={os.getpid()}\n")
    lock_file.flush()
    return lock_file


@contextmanager
def _locked_sim_db(sim_db_path: Path) -> Any:
    """Release the per-DB advisory lock on success, failure, and cancellation."""

    handle = _acquire_sim_db_lock(sim_db_path)
    try:
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _with_sim_db_lock(function: Any) -> Any:
    """Prepare one DB path once, then hold its lock for the whole coroutine."""

    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        sim_db_path = _prepare_sim_db(kwargs.get("sim_db"))
        kwargs["sim_db"] = sim_db_path
        with _locked_sim_db(sim_db_path):
            return await function(*args, **kwargs)

    return wrapped


def select_simulation_agents(
    max_agents: int | None = None,
    *,
    agent_ids: Sequence[str] | None = None,
    instrument_name: str = "삼성전자",
) -> list[dict[str, Any]]:
    """Return an exact sealed cohort, optionally truncated for offline smoke."""

    all_agents = load_agents_from_sys100(
        config.SYS_100_DB,
        instrument_name=instrument_name,
    )
    if agent_ids is not None:
        ordered_ids = [str(agent_id).strip() for agent_id in agent_ids]
        if (
            not ordered_ids
            or any(not agent_id for agent_id in ordered_ids)
            or len(ordered_ids) != len(set(ordered_ids))
        ):
            raise ValueError("agent_ids must be non-empty and unique")
        by_id = {
            str(agent["agent_id"]): agent
            for agent in all_agents
        }
        missing = [
            agent_id
            for agent_id in ordered_ids
            if agent_id not in by_id
        ]
        if missing:
            raise ValueError(
                f"agent_ids are absent from the persona DB: {missing[:5]}"
            )
        all_agents = [by_id[agent_id] for agent_id in ordered_ids]
    if max_agents is None:
        return all_agents
    if isinstance(max_agents, bool) or max_agents < 1:
        raise ValueError("max_agents must be a positive integer")
    if max_agents > len(all_agents):
        raise ValueError(
            f"max_agents exceeds the available cohort size {len(all_agents)}"
        )
    return all_agents[:max_agents]


def validate_community_runtime_policy(community_mode: str) -> None:
    """Reject a silent partial-ON arm before any experiment work starts."""
    if community_mode == "on" and not (
        config.ENABLE_COMMUNITY_POSTING
        and config.ENABLE_COMMUNITY_READING
    ):
        raise ValueError(
            "community_mode='on' requires posting and reading to both be enabled; "
            "partial community arms are not part of the baseline"
        )


@_with_sim_db_lock
async def run_simulation(
    *,
    max_agents: int | None = None,
    max_days: int | None = None,
    enable_logs: bool = True,
    random_seed: int = config.RANDOM_SEED,
    start_date: str | None = None,
    end_date: str | None = None,
    information_mode: str = "pre_close_cutoff",
    decision_space: str = "buy_sell_only",
    news_bundle: Path | str = config.SEALED_REAL_NEWS_BUNDLE,
    calendar_registry: Path | str = config.SEALED_EVENT_CALENDAR,
    price_registry: Path | str = config.SEALED_EVENT_PRICES,
    community_mode: str | None = None,
    sim_db: Path | str | None = None,
    reset_runtime_tables: bool = True,
    turn_offset: int = 0,
    day_offset: int = 0,
    log_root: Path | str | None = None,
    log_run_id: str | None = None,
    phases: tuple[str, ...] | None = None,
    append_existing_logs: bool = False,
    response_journal: ResponseJournal | None = None,
    journal_run_id: str | None = None,
    journal_condition_id: str | None = None,
    journal_event_id: str | None = None,
    phase_attempt_id: str | None = None,
    event_attempt_number: int | None = None,
    concurrency: int | None = None,
    agent_ids: Sequence[str] | None = None,
    stock_code: str | None = None,
    instrument_name: str = "삼성전자",
) -> dict[str, Any]:
    openrouter_policy = validate_experiment_call_policy()
    if information_mode not in {"pre_close_cutoff", "same_day", "prior_close"}:
        raise ValueError("information_mode must be 'pre_close_cutoff', 'same_day', or 'prior_close'")
    if decision_space != "buy_sell_only":
        raise ValueError("decision_space must be 'buy_sell_only'")
    if community_mode is None:
        community_mode = "on" if config.ENABLE_COMMUNITY else "off"
    if community_mode not in {"off", "on"}:
        raise ValueError("community_mode must be 'off' or 'on'")
    validate_community_runtime_policy(community_mode)
    if turn_offset < 0 or day_offset < 0:
        raise ValueError("turn_offset and day_offset must be non-negative")
    if turn_offset != day_offset * 2:
        raise ValueError("turn_offset must equal day_offset * 2 for the two-turn daily cycle")
    requested_phases = phases or ("am", "pm", "community")
    phase_order = {"am": 0, "pm": 1, "community": 2}
    if not requested_phases or any(phase not in phase_order for phase in requested_phases):
        raise ValueError("phases must contain am, pm, and/or community")
    if list(requested_phases) != sorted(requested_phases, key=phase_order.__getitem__):
        raise ValueError("phases must be ordered as am, pm, community")
    journal_values = (
        journal_run_id,
        journal_condition_id,
        journal_event_id,
        phase_attempt_id,
        event_attempt_number,
    )
    if response_journal is None and any(value is not None for value in journal_values):
        raise ValueError("journal event fields require response_journal")
    if response_journal is not None and any(value is None for value in journal_values):
        raise ValueError("response_journal requires complete event attempt context")
    resolved_concurrency = (
        config.SIMULATION_CONCURRENCY
        if concurrency is None
        else concurrency
    )
    if (
        isinstance(resolved_concurrency, bool)
        or not isinstance(resolved_concurrency, int)
        or resolved_concurrency < 1
    ):
        raise ValueError("concurrency must be a positive integer")
    concurrency = resolved_concurrency
    sim_db_path = _prepare_sim_db(sim_db)
    agents = select_simulation_agents(
        max_agents,
        agent_ids=agent_ids,
        instrument_name=instrument_name,
    )
    event_schedule = FrozenEventSchedule.from_sealed_files(
        calendar_registry,
        price_registry,
        expected_stock_code=stock_code,
    )
    resolved_stock_code = event_schedule.stock_code
    news = NewsAgent(
        news_bundle_path=news_bundle,
        expected_stock_code=resolved_stock_code,
    )
    previous_by_date = _previous_date_map(
        sim_db_path,
        stock_code=resolved_stock_code,
    )
    uses_previous_market = information_mode in {"pre_close_cutoff", "prior_close"}
    sealed_subturns_by_date: dict[str, set[str]] = defaultdict(set)
    for event_id in news.sealed_event_ids:
        event_date, event_subturn = event_id.split("/", 1)
        sealed_subturns_by_date[event_date].add(event_subturn)
    incomplete_dates = {
        day: sorted(subturns)
        for day, subturns in sealed_subturns_by_date.items()
        if subturns != {"AM", "PM"}
    }
    if incomplete_dates:
        raise RuntimeError(
            f"sealed news calendar has incomplete AM/PM dates: {incomplete_dates}"
        )
    schedule_event_ids = [
        str(event["event_id"])
        for event in event_schedule.events
    ]
    if list(news.sealed_event_ids) != schedule_event_ids:
        raise RuntimeError(
            "sealed news event order differs from the frozen calendar/price schedule"
        )
    stock_dates = set(
        _stock_trading_dates(
            sim_db_path,
            stock_code=resolved_stock_code,
        )
    )
    schedule_dates = [
        str(event["date"])
        for event in event_schedule.events
        if str(event["subturn"]) == "am"
    ]
    missing_stock_dates = [
        day for day in schedule_dates if day not in stock_dates
    ]
    if missing_stock_dates:
        raise RuntimeError(
            "StockData is missing frozen schedule dates: "
            + ",".join(missing_stock_dates)
        )
    dates = [
        str(event["date"])
        for event in event_schedule.events
        if str(event["subturn"]) == "am"
        and (start_date is None or str(event["date"]) >= start_date)
        and (end_date is None or str(event["date"]) <= end_date)
    ]
    if uses_previous_market:
        missing_previous_dates = [
            day for day in dates if day not in previous_by_date
        ]
        if missing_previous_dates:
            raise RuntimeError(
                "previous-market mode lacks a prior StockData row for: "
                + ",".join(missing_previous_dates)
            )
    if max_days:
        dates = dates[:max_days]
    if not dates:
        raise RuntimeError("No StockData rows found. Run scripts/03_load_stock_data.py first.")
    if requested_phases != ("am", "pm", "community") and len(dates) != 1:
        raise ValueError("Phase-level execution accepts exactly one trading date per call")
    selected_first_turn = int(
        event_schedule.event(f"{dates[0]}/AM")["turn"]
    )
    selected_last_turn = int(
        event_schedule.event(f"{dates[-1]}/PM")["turn"]
    )
    resolved_turn_offset = selected_first_turn - 1
    resolved_day_offset = (selected_first_turn - 1) // 2
    if (turn_offset or day_offset) and (
        turn_offset != resolved_turn_offset
        or day_offset != resolved_day_offset
    ):
        raise ValueError(
            "turn_offset/day_offset differ from the frozen event schedule"
        )

    if reset_runtime_tables:
        _reset_runtime_tables(sim_db_path)
    memory = MemoryAgent(
        sim_db_path,
        event_schedule=event_schedule,
    )
    for agent in agents:
        memory.ensure_initial_ltb(str(agent["agent_id"]))
    fundamental = FundamentalAgent(sim_db_path)
    exchange = ExchangeAgent(sim_db_path)
    execution_prices = _load_execution_prices(
        fundamental,
        dates,
        event_schedule=event_schedule,
        stock_code=resolved_stock_code,
    )
    community_enabled = community_mode == "on"
    community = CommunityAgent(sim_db_path) if community_enabled else None
    client = OpenRouterClient(
        audit_path=(
            config.OPENROUTER_AUDIT_LOG
            if response_journal is not None
            else None
        ),
        audit_context=(
            {
                "artifact": "integrated_experiment_openrouter_attempt",
                "run_id": str(journal_run_id),
                "condition_id": str(journal_condition_id),
            }
            if response_journal is not None
            else None
        ),
    )
    semaphore = asyncio.Semaphore(concurrency)
    db_write_lock = asyncio.Lock()
    logger = (
        SimulationLogger(
            root_dir=log_root or config.LOG_DIR,
            run_id=log_run_id,
            metadata={
                "max_agents": max_agents,
                "max_days": max_days,
                "concurrency": concurrency,
                "agent_count": len(agents),
                "date_count": len(dates),
                "trading_dates": list(dates),
                "turn_count": len(dates) * 2,
                "sim_db": str(sim_db_path),
                "random_agents": False,
                "random_seed": random_seed,
                "start_date": start_date,
                "end_date": end_date,
                "information_mode": information_mode,
                "decision_space": decision_space,
                "limit_only_orders": False,
                "exchange_mode": "announced_price_binary",
                "agent_selection": "first_n",
                "news_source": news.news_source,
                "news_bundle": str(news.news_bundle_path),
                "news_bundle_sha256": news.news_bundle_sha256,
                "news_bundle_file_sha256": news.news_bundle_file_sha256,
                "calendar_registry": str(Path(calendar_registry)),
                "calendar_registry_sha256": event_schedule.calendar_sha256,
                "price_registry": str(Path(price_registry)),
                "price_registry_sha256": event_schedule.prices_sha256,
                "news_treatment": "real_only",
                "stock_code": resolved_stock_code,
                "community_mode": community_mode,
                "community_posting": bool(community_enabled and config.ENABLE_COMMUNITY_POSTING),
                "community_reading": bool(community_enabled and config.ENABLE_COMMUNITY_READING),
                "agent_ids": [agent["agent_id"] for agent in agents],
                "agent_depths": {agent["agent_id"]: int(agent.get("news_depth") or 0) for agent in agents},
                "community_post_permissions": {
                    agent["agent_id"]: can_author_community_post(agent)
                    for agent in agents
                },
                "persona_prompt_source": "structured_sys_100_projection",
                "persona_prompt_regenerated_count": sum(
                    bool(agent.get("persona_prompt_regenerated"))
                    for agent in agents
                ),
                "persona_prompt_depth_match_count": len(agents),
                "openrouter_call_policy": openrouter_policy,
                "subturns": ["am", "pm"],
                "turn_offset": resolved_turn_offset,
                "day_offset": resolved_day_offset,
                "global_turn_start": selected_first_turn,
                "global_turn_end": selected_last_turn,
                "phases": list(requested_phases),
            },
            append_existing=append_existing_logs,
        )
        if enable_logs
        else None
    )

    async def guarded_turn(
        agent: dict[str, Any],
        turn: int,
        day: str,
        market_features_date: str,
        news_max_date: str,
        news_start_date: str | None,
        news_start_time: str | None,
        news_end_time: str | None,
        subturn: str,
        open_price: float,
        previous_close: float,
        execution_reference: str,
    ) -> dict[str, Any]:
        async with semaphore:
            try:
                scope = (
                    response_journal_scope(
                        journal=response_journal,
                        run_id=str(journal_run_id),
                        condition_id=str(journal_condition_id),
                        event_id=str(journal_event_id),
                        phase_attempt_id=str(phase_attempt_id),
                        event_attempt_number=int(event_attempt_number or 0),
                        agent_id=str(agent["agent_id"]),
                    )
                    if response_journal is not None
                    else nullcontext()
                )
                with scope:
                    return await run_agent_turn(
                        agent,
                        turn=turn,
                        date=day,
                        market_features_date=market_features_date,
                        news_max_date=news_max_date,
                        news_start_date=news_start_date,
                        news_start_time=news_start_time,
                        news_end_time=news_end_time,
                        execution_date=day,
                        information_mode=information_mode,
                        subturn=subturn,
                        open_price=open_price,
                        previous_close=previous_close,
                        execution_reference=execution_reference,
                        decision_space=decision_space,
                        memory_agent=memory,
                        fundamental_agent=fundamental,
                        news_agent=news,
                        client=client,
                        event_logger=logger,
                        db_write_lock=db_write_lock,
                        community_agent=community,
                        random_seed=random_seed,
                        stock_code=resolved_stock_code,
                    )
            except Exception as exc:
                if logger is not None:
                    logger.log_agent_error(agent=agent, turn=turn, date=day, error=exc)
                # A missing agent decision makes the subturn scientifically incomplete.
                # Let the canonical event runtime pause and restore the pre-event snapshot
                # instead of silently fabricating a system message or continuing.
                raise

    for day_index, day in enumerate(dates, start=1):
        scheduled_am = event_schedule.event(f"{day}/AM")
        scheduled_pm = event_schedule.event(f"{day}/PM")
        am_turn = int(scheduled_am["turn"])
        pm_turn = int(scheduled_pm["turn"])
        global_day_index = (am_turn + 1) // 2
        previous_execution_date = previous_by_date.get(day)
        previous_close = (
            fundamental.get_market_features(
                previous_execution_date,
                resolved_stock_code,
            )["close"]
            if previous_execution_date
            else fundamental.get_market_features(
                day,
                resolved_stock_code,
            )["close"]
        )
        prices = execution_prices[day]
        if information_mode == "prior_close":
            am_news_max_date = previous_by_date[day]
            pm_news_max_date = previous_by_date[day]
        else:
            am_news_max_date = day
            pm_news_max_date = day

        am_results = None
        if "am" in requested_phases:
            am_results = await _run_subturn(
                subturn="am",
                turn=am_turn,
                day_index=global_day_index,
                day=day,
                agents=agents,
                guarded_turn=guarded_turn,
                exchange=exchange,
                memory=memory,
                logger=logger,
                announced_price=prices["open"],
                last_price=previous_close,
                current_price=prices["open"],
                market_features_date=previous_by_date[day] if information_mode != "same_day" else day,
                news_max_date=am_news_max_date,
                news_start_date=previous_by_date[day] if information_mode == "pre_close_cutoff" else None,
                news_start_time=AM_NEWS_WINDOW_START_TIME if information_mode == "pre_close_cutoff" else None,
                news_end_time=AM_NEWS_WINDOW_END_TIME if information_mode == "pre_close_cutoff" else None,
                open_price=prices["open"],
                previous_close=previous_close,
                execution_reference="open price",
                client=client,
                concurrency=concurrency,
                random_seed=random_seed,
                response_journal=response_journal,
                journal_run_id=journal_run_id,
                journal_condition_id=journal_condition_id,
                journal_event_id=journal_event_id,
                phase_attempt_id=phase_attempt_id,
                event_attempt_number=event_attempt_number,
                stock_code=resolved_stock_code,
            )

        pm_results = None
        if "pm" in requested_phases:
            pm_results = await _run_subturn(
                subturn="pm",
                turn=pm_turn,
                day_index=global_day_index,
                day=day,
                agents=agents,
                guarded_turn=guarded_turn,
                exchange=exchange,
                memory=memory,
                logger=logger,
                announced_price=prices["close"],
                last_price=previous_close,
                current_price=prices["close"],
                market_features_date=day,
                news_max_date=pm_news_max_date,
                news_start_date=day if information_mode == "pre_close_cutoff" else None,
                news_start_time=PM_NEWS_WINDOW_START_TIME if information_mode == "pre_close_cutoff" else None,
                news_end_time=PM_NEWS_WINDOW_END_TIME if information_mode == "pre_close_cutoff" else None,
                open_price=prices["open"],
                previous_close=previous_close,
                execution_reference="close price",
                client=client,
                concurrency=concurrency,
                random_seed=random_seed,
                response_journal=response_journal,
                journal_run_id=journal_run_id,
                journal_condition_id=journal_condition_id,
                journal_event_id=journal_event_id,
                phase_attempt_id=phase_attempt_id,
                event_attempt_number=event_attempt_number,
                stock_code=resolved_stock_code,
            )
            if logger is not None:
                _write_pm_posting_payload(logger, day, pm_results)

        if "community" in requested_phases and pm_results is None and community_enabled:
            if logger is None:
                raise RuntimeError("Community-only resume requires experiment logs")
            pm_results = _load_pm_posting_payload(logger.run_dir, day)

        if (
            "community" in requested_phases
            and community_enabled
            and config.ENABLE_COMMUNITY_POSTING
            and community is not None
        ):
            if pm_results is None:
                raise RuntimeError("PM posting payload is unavailable for the community phase")
            await post_trade_posting_phase(
                turn_results=pm_results["turn_results"],
                community_agent=community,
                execution_by_agent=pm_results["execution_by_agent"],
                turn=pm_turn,
                date=day,
                client=client,
                concurrency=concurrency,
                event_logger=logger,
                random_seed=random_seed,
                response_journal=response_journal,
                journal_run_id=journal_run_id,
                journal_condition_id=journal_condition_id,
                journal_event_id=journal_event_id,
                phase_attempt_id=phase_attempt_id,
                event_attempt_number=event_attempt_number,
            )
        if "community" in requested_phases and community_enabled and community is not None:
            await community_phase(
                agents=agents,
                community_agent=community,
                memory_agent=memory,
                sim_db_path=sim_db_path,
                turn=pm_turn,
                date=day,
                client=client,
                concurrency=concurrency,
                event_logger=logger,
                random_seed=random_seed,
                response_journal=response_journal,
                journal_run_id=journal_run_id,
                journal_condition_id=journal_condition_id,
                journal_event_id=journal_event_id,
                phase_attempt_id=phase_attempt_id,
                event_attempt_number=event_attempt_number,
            )
        am_text = (
            f"am_orders={am_results['order_count']} am_volume={am_results['volume']}"
            if am_results is not None
            else "am=skipped"
        )
        pm_text = (
            f"pm_orders={pm_results['order_count']} pm_volume={pm_results['volume']}"
            if pm_results is not None and "order_count" in pm_results
            else "pm=skipped_or_loaded"
        )
        print(f"{day} turns={am_turn}/{pm_turn} phases={','.join(requested_phases)} {am_text} {pm_text}")
    outcome_finalized = False
    right_censored_outcome_ids: tuple[str, ...] = ()
    delivery_summary: dict[str, Any] | None = None
    final_schedule_date = str(event_schedule.events[-1]["date"])
    if dates[-1] == final_schedule_date and "pm" in requested_phases:
        _assert_complete_fill_grid(
            sim_db_path,
            agent_ids=[str(agent["agent_id"]) for agent in agents],
            event_turns=[
                int(event["turn"])
                for event in event_schedule.events
            ],
        )
        right_censored_outcome_ids = (
            memory.right_censor_unavailable_outcomes()
        )
        outcome_finalized = True
        if logger is not None:
            logger.write_jsonl(
                "trade_outcomes.jsonl",
                {
                    "run_id": logger.run_id,
                    "event": "outcomes_right_censored",
                    "event_id": event_schedule.last_event_id,
                    "outcome_ids": list(right_censored_outcome_ids),
                    "outcome_count": len(right_censored_outcome_ids),
                },
            )
    if logger is not None:
        delivery_summary = (
            finalize_community_delivery_counts(logger.run_dir)
            if outcome_finalized and "community" in requested_phases
            else None
        )
        full_invocation_completed_schedule = (
            outcome_finalized
            and requested_phases == ("am", "pm", "community")
        )
        completion_filename = (
            "run_complete.json"
            if full_invocation_completed_schedule
            else f"phase_complete_{dates[-1]}_{requested_phases[-1]}.json"
        )
        logger.write_json(
            completion_filename,
            {
                "run_id": logger.run_id,
                "agent_count": len(agents),
                "date_count": len(dates),
                "information_mode": information_mode,
                "decision_space": decision_space,
                "turn_offset": resolved_turn_offset,
                "day_offset": resolved_day_offset,
                "global_turn_start": selected_first_turn,
                "global_turn_end": selected_last_turn,
                "phases": list(requested_phases),
                "community_delivery_summary": delivery_summary,
                "outcome_finalized": outcome_finalized,
                "right_censored_outcome_count": len(
                    right_censored_outcome_ids
                ),
                "schedule_complete": outcome_finalized,
                "log_dir": str(logger.run_dir),
            },
        )
        print(f"log_dir={logger.run_dir}")
    return {
        "outcome_finalized": outcome_finalized,
        "right_censored_outcome_count": len(right_censored_outcome_ids),
        "community_delivery_summary": delivery_summary,
        "schedule_complete": outcome_finalized,
        "phases": list(requested_phases),
    }


def _assert_complete_fill_grid(
    db_path: Path | str,
    *,
    agent_ids: list[str],
    event_turns: list[int],
) -> None:
    expected = {
        (agent_id, int(turn))
        for agent_id in agent_ids
        for turn in event_turns
    }
    with connect(db_path, read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT agent_id, turn
            FROM simulation_fills
            ORDER BY turn, agent_id
            """
        ).fetchall()
    actual = {
        (str(row["agent_id"]), int(row["turn"]))
        for row in rows
    }
    if actual != expected or len(rows) != len(expected):
        raise RuntimeError(
            "final outcome censoring requires exactly one fill for every "
            "agent-event; "
            f"missing={sorted(expected - actual)[:20]} "
            f"unexpected={sorted(actual - expected)[:20]}"
        )


def _write_pm_posting_payload(
    logger: SimulationLogger,
    day: str,
    pm_results: dict[str, Any],
) -> None:
    logger.write_json(
        f"pm_posting_payload_{day}.json",
        {
            "turn_results": [
                {
                    "agent": result["agent"],
                    "analysis_id": result["analysis_id"],
                    "decision": result["decision"],
                    "decision_id": result["decision_id"],
                    "stb_id": result["stb_id"],
                    "fill_id": result["fill_id"],
                    "fill": result["fill"],
                    "ltb_id": result["ltb_id"],
                    "ltb": result["ltb"],
                }
                for result in pm_results["turn_results"]
            ],
            "execution_by_agent": pm_results["execution_by_agent"],
        },
    )


def _load_pm_posting_payload(run_dir: Path, day: str) -> dict[str, Any]:
    path = run_dir / f"pm_posting_payload_{day}.json"
    if not path.exists():
        raise RuntimeError(f"PM posting payload not found for community resume: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("turn_results") or "execution_by_agent" not in payload:
        raise RuntimeError(f"PM posting payload is incomplete: {path}")
    return {
        "turn_results": payload["turn_results"],
        "execution_by_agent": payload["execution_by_agent"],
    }


def _load_execution_prices(
    fundamental: FundamentalAgent,
    dates: list[str],
    *,
    event_schedule: FrozenEventSchedule,
    stock_code: str,
) -> dict[str, dict[str, float]]:
    prices: dict[str, dict[str, float]] = {}
    for day in dates:
        daily = fundamental.get_daily_prices(day, stock_code)
        sealed_open = float(
            event_schedule.event(f"{day}/AM")["execution_price"]
        )
        sealed_close = float(
            event_schedule.event(f"{day}/PM")["execution_price"]
        )
        db_open = float(daily["open"])
        db_close = float(daily["close"])
        if abs(db_open - sealed_open) > 1e-6 or abs(db_close - sealed_close) > 1e-6:
            raise RuntimeError(
                f"StockData execution prices differ from sealed prices for {day}: "
                f"db_open={db_open}, sealed_open={sealed_open}, "
                f"db_close={db_close}, sealed_close={sealed_close}"
            )
        prices[day] = {
            "open": sealed_open,
            "close": sealed_close,
        }
    return prices


async def _run_subturn(
    *,
    subturn: str,
    turn: int,
    day_index: int,
    day: str,
    agents: list[dict[str, Any]],
    guarded_turn: Any,
    exchange: ExchangeAgent,
    memory: MemoryAgent,
    logger: SimulationLogger | None,
    announced_price: float,
    last_price: float,
    current_price: float,
    market_features_date: str,
    news_max_date: str,
    news_start_date: str | None,
    news_start_time: str | None,
    news_end_time: str | None,
    open_price: float,
    previous_close: float,
    execution_reference: str,
    client: OpenRouterClient,
    concurrency: int,
    random_seed: int,
    response_journal: ResponseJournal | None = None,
    journal_run_id: str | None = None,
    journal_condition_id: str | None = None,
    journal_event_id: str | None = None,
    phase_attempt_id: str | None = None,
    event_attempt_number: int | None = None,
    stock_code: str = config.STOCK_CODE,
) -> dict[str, Any]:
    event_id = f"{day}/{subturn.upper()}"
    matured_outcome_ids = memory.mature_outcomes_for_event(event_id)
    if logger is not None:
        logger.write_jsonl(
            "trade_outcomes.jsonl",
            {
                "run_id": logger.run_id,
                "event": "outcomes_matured",
                "event_id": event_id,
                "date": day,
                "turn": turn,
                "subturn": subturn,
                "outcome_ids": list(matured_outcome_ids),
                "outcome_count": len(matured_outcome_ids),
            },
        )
    parallel_results = await asyncio.gather(
        *(
            guarded_turn(
                agent,
                turn,
                day,
                market_features_date,
                news_max_date,
                news_start_date,
                news_start_time,
                news_end_time,
                subturn,
                open_price,
                previous_close,
                execution_reference,
            )
            for agent in agents
        ),
        return_exceptions=True,
    )
    turn_errors = [result for result in parallel_results if isinstance(result, BaseException)]
    if turn_errors:
        details = "; ".join(
            f"{type(error).__name__}: {error}" for error in turn_errors[:5]
        )
        raise ParallelTaskError(
            f"{len(turn_errors)} agent turn(s) failed after all parallel tasks settled: {details}",
            turn_errors,
        ) from turn_errors[0]
    turn_results = [result for result in parallel_results if isinstance(result, dict)]
    if len(turn_results) != len(agents):
        raise RuntimeError(
            f"parallel turn result count mismatch: results={len(turn_results)} agents={len(agents)}"
        )
    orders = [result["order"] for result in turn_results if result.get("order") is not None]
    portfolio_snapshots = _portfolio_snapshots(
        memory,
        agents,
        turn - 1,
        stock_code=stock_code,
    )
    results = exchange.process_daily_orders(
        orders,
        {stock_code: float(announced_price)},
        {stock_code: float(last_price)},
        current_date=day,
        day_number=day_index,
        portfolios=portfolio_snapshots,
    )
    execution_by_agent = _update_portfolios_from_results(
        memory=memory,
        agents=agents,
        turn=turn,
        date=day,
        orders=orders,
        results=results,
        current_prices={stock_code: float(current_price)},
        pre_portfolios=portfolio_snapshots,
        turn_results=turn_results,
        logger=logger,
        stock_code=stock_code,
    )
    turn_results_by_agent = {
        str(result["agent"]["agent_id"]): result
        for result in turn_results
    }
    missing_execution = sorted(set(turn_results_by_agent) - set(execution_by_agent))
    if missing_execution:
        raise RuntimeError(
            "post-fill LTB cannot be generated without one execution episode per agent: "
            + ",".join(missing_execution)
        )

    ltb_semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _generate_post_fill_ltb(agent_id: str) -> tuple[str, dict[str, Any]]:
        turn_result = turn_results_by_agent[agent_id]
        execution = execution_by_agent[agent_id]
        eligible_outcomes = memory.eligible_outcomes(agent_id, event_id)
        async with ltb_semaphore:
            scope = (
                response_journal_scope(
                    journal=response_journal,
                    run_id=str(journal_run_id),
                    condition_id=str(journal_condition_id),
                    event_id=str(journal_event_id),
                    phase_attempt_id=str(phase_attempt_id),
                    event_attempt_number=int(event_attempt_number or 0),
                    agent_id=agent_id,
                )
                if response_journal is not None
                else nullcontext()
            )
            with scope:
                generated = await update_long_term_belief(
                    turn_result["agent"],
                    event={
                        "event_id": f"{day}/{subturn.upper()}",
                        "turn": int(turn),
                        "date": day,
                        "subturn": subturn,
                    },
                    previous_ltb=turn_result["previous_ltb"],
                    current_stb=turn_result["stb"],
                    transaction_episode=execution["fill"],
                    eligible_price_outcomes_dim_6_only=eligible_outcomes,
                    client=client,
                    seed=stable_llm_seed(
                        random_seed,
                        agent_id,
                        turn,
                        "post_fill_long_term_belief",
                    ),
                )
        generated["eligible_price_outcomes_dim_6_only"] = eligible_outcomes
        return agent_id, generated

    generated_ltb_results = await asyncio.gather(
        *(
            _generate_post_fill_ltb(agent_id)
            for agent_id in sorted(turn_results_by_agent)
        ),
        return_exceptions=True,
    )
    ltb_errors = [
        result
        for result in generated_ltb_results
        if isinstance(result, BaseException)
    ]
    if ltb_errors:
        details = "; ".join(
            f"{type(error).__name__}: {error}" for error in ltb_errors[:5]
        )
        raise ParallelTaskError(
            f"{len(ltb_errors)} post-fill LTB call(s) failed: {details}",
            ltb_errors,
        ) from ltb_errors[0]
    generated_ltb_by_agent = {
        agent_id: generated
        for agent_id, generated in generated_ltb_results
        if isinstance(agent_id, str)
    }
    if len(generated_ltb_by_agent) != len(turn_results_by_agent):
        raise RuntimeError("post-fill LTB result count mismatch")

    for agent_id in sorted(turn_results_by_agent):
        turn_result = turn_results_by_agent[agent_id]
        execution = execution_by_agent[agent_id]
        generated_ltb = generated_ltb_by_agent[agent_id]
        generated_ltb_dimensions = belief_dimensions(
            generated_ltb,
            label="generated_post_fill_long_term_belief",
        )
        ltb_id = memory.save_post_fill_ltb(
            agent_id=agent_id,
            turn=turn,
            date=day,
            subturn=subturn,
            parent_ltb_id=str(turn_result["previous_ltb"]["ltb_id"]),
            stb_id=str(turn_result["stb_id"]),
            decision_id=str(turn_result["decision_id"]),
            fill_id=str(execution["fill_id"]),
            dimensions=generated_ltb_dimensions,
            integration_evidence=generated_ltb["integration_evidence"],
            ignored_outcome_ids=generated_ltb.get("ignored_outcome_ids") or [],
            belief_summary=str(generated_ltb["belief_summary"]),
            view_change=generated_ltb["view_change"],
        )
        committed_ltb = {
            **memory.get_ltb(ltb_id),
            **memory.get_ltb_human_log(ltb_id),
        }
        turn_result["fill_id"] = execution["fill_id"]
        turn_result["fill"] = execution["fill"]
        turn_result["ltb_id"] = ltb_id
        turn_result["ltb"] = committed_ltb
        execution["ltb_id"] = ltb_id
        lineage = {
            "source_ltb_id": turn_result["previous_ltb"]["ltb_id"],
            "source_stb_id": turn_result["stb_id"],
            "analysis_id": turn_result["analysis_id"],
            "decision_id": turn_result["decision_id"],
            "fill_id": execution["fill_id"],
            "post_fill_ltb_id": ltb_id,
        }
        for result in results.values():
            for transaction in result.get("transactions") or []:
                transaction_agent_id = str(
                    transaction.get("agent_id")
                    or transaction.get("user_id")
                    or ""
                )
                if transaction_agent_id == agent_id:
                    transaction.update(lineage)
        if logger is not None:
            logger.write_jsonl(
                "memory_lineage.jsonl",
                {
                    "run_id": logger.run_id,
                    "event": "post_fill_ltb_committed",
                    "date": day,
                    "turn": turn,
                    "subturn": subturn,
                    "agent_id": agent_id,
                    "source_ltb_id": turn_result["previous_ltb"]["ltb_id"],
                    "source_stb_id": turn_result["stb_id"],
                    "analysis_id": turn_result["analysis_id"],
                    "decision_id": turn_result["decision_id"],
                    "fill_id": execution["fill_id"],
                    "ltb_id": ltb_id,
                    "scientific_sha256": committed_ltb["scientific_sha256"],
                    "human_log_sha256": committed_ltb["human_log_sha256"],
                    "eligible_outcome_ids": [
                        outcome["outcome_id"]
                        for outcome in generated_ltb[
                            "eligible_price_outcomes_dim_6_only"
                        ]
                    ],
                },
            )
    if logger is not None:
        logger.log_daily_exchange(
            date=day,
            turn=turn,
            orders=orders,
            results=results,
        )
    return {
        "turn_results": turn_results,
        "execution_by_agent": execution_by_agent,
        "order_count": len(orders),
        "volume": results[stock_code]["volume"],
    }


def _portfolio_snapshots(
    memory: MemoryAgent,
    agents: list[dict[str, Any]],
    turn: int,
    *,
    stock_code: str,
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for agent in agents:
        agent_id = str(agent["agent_id"])
        row = memory._latest_portfolio(agent_id, before_or_at_turn=turn)
        if row is None:
            raise ValueError(f"portfolio not found for {agent_id} at turn {turn}")
        position = 0
        for pos in json.loads(row["positions"]):
            if pos.get("stock_code") == stock_code:
                position = int(pos.get("quantity", 0))
                break
        snapshots[agent_id] = {
            "cash": float(row["cash"]),
            "position": position,
        }
    return snapshots


def _update_portfolios_from_results(
    *,
    memory: MemoryAgent,
    agents: list[dict[str, Any]],
    turn: int,
    date: str,
    orders: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    current_prices: dict[str, float],
    pre_portfolios: dict[str, dict[str, Any]],
    turn_results: list[dict[str, Any]],
    logger: SimulationLogger | None,
    stock_code: str,
) -> dict[str, dict[str, Any]]:
    fills_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result_stock_code, result in results.items():
        for tx in result.get("transactions") or []:
            user_id = str(tx.get("agent_id") or tx.get("user_id") or "")
            if not user_id:
                continue
            quantity = int(tx.get("quantity") or tx.get("executed_quantity", 0))
            price = float(tx.get("executed_price", 0))
            fills_by_agent[user_id].append(
                {
                    "user_id": user_id,
                    "stock_code": tx.get(
                        "stock_code",
                        result_stock_code,
                    ),
                    "direction": tx.get("action") or tx.get("direction"),
                    "quantity": quantity,
                    "price": price,
                    "fee": 0.0,
                }
            )
    submitted_agent_ids = {str(order.get("user_id")) for order in orders if order.get("user_id")}
    expected_agent_ids = {
        str(result["agent"]["agent_id"])
        for result in turn_results
    }
    if submitted_agent_ids != expected_agent_ids:
        missing = sorted(expected_agent_ids - submitted_agent_ids)
        unexpected = sorted(submitted_agent_ids - expected_agent_ids)
        raise RuntimeError(
            "buy/sell-only experiment requires exactly one submitted order per agent; "
            f"missing={missing} unexpected={unexpected}"
        )
    for agent_id in submitted_agent_ids:
        fills = fills_by_agent.get(agent_id, [])
        filled_quantity = sum(int(fill["quantity"]) for fill in fills)
        total_value = sum(float(fill["quantity"]) * float(fill["price"]) for fill in fills)
        total_fee = sum(float(fill.get("fee", 0)) for fill in fills)
        executed_price = total_value / filled_quantity if filled_quantity else None
        memory.update_trade_execution(
            agent_id,
            turn,
            filled_quantity=filled_quantity,
            executed_price=executed_price,
            fee=total_fee,
        )
    execution_by_agent: dict[str, dict[str, Any]] = {}
    for agent in agents:
        agent_id = str(agent["agent_id"])
        fills = fills_by_agent.get(agent_id, [])
        filled_quantity = sum(int(fill["quantity"]) for fill in fills)
        total_value = sum(float(fill["quantity"]) * float(fill["price"]) for fill in fills)
        total_fee = sum(float(fill.get("fee", 0)) for fill in fills)
        execution_by_agent[agent_id] = {
            "fills": fills,
            "filled_quantity": filled_quantity,
            "executed_price": total_value / filled_quantity if filled_quantity else None,
            "fee": total_fee,
        }
        state = memory.update_portfolio(
            agent_id,
            turn,
            date,
            fills,
            current_prices=current_prices,
        )
        turn_result = next(
            result
            for result in turn_results
            if str(result["agent"]["agent_id"]) == agent_id
        )
        decision = turn_result["decision"]
        if len(fills) != 1:
            raise RuntimeError(
                f"full-fill experiment requires exactly one fill for {agent_id}; "
                f"observed={len(fills)}"
            )
        fill = fills[0]
        requested_quantity = int(
            decision.get("requested_quantity", decision.get("quantity", 0))
        )
        if (
            int(fill["quantity"]) != requested_quantity
            or str(fill["direction"]) != str(decision["action"])
            or float(fill["price"]) <= 0
            or float(fill.get("fee", 0.0)) != 0.0
        ):
            raise RuntimeError(
                f"fill differs from committed decision for {agent_id}"
            )
        post_quantity = 0
        for position in state["positions"]:
            if position.get("stock_code") == stock_code:
                post_quantity = int(position.get("quantity", 0))
                break
        fill_id = memory.record_fill_lineage(
            decision_id=str(turn_result["decision_id"]),
            filled_quantity=int(fill["quantity"]),
            executed_price=float(fill["price"]),
            fee=float(fill.get("fee", 0.0)),
            pre_portfolio={
                "cash": float(pre_portfolios[agent_id]["cash"]),
                "stock_code": stock_code,
                "quantity": int(pre_portfolios[agent_id]["position"]),
            },
            post_portfolio={
                "cash": float(state["cash"]),
                "stock_code": stock_code,
                "quantity": post_quantity,
                "total_value": float(state["total_value"]),
                "realized_pnl": float(state["realized_pnl"]),
                "total_return_rate": float(state["total_return_rate"]),
            },
            stock_code=stock_code,
        )
        structured_fill = memory.get_fill_lineage(fill_id)
        execution_by_agent[agent_id]["fill_id"] = fill_id
        execution_by_agent[agent_id]["fill"] = structured_fill
        if logger is not None:
            logger.write_jsonl(
                "portfolio_updates.jsonl",
                {
                    "run_id": logger.run_id,
                    "event": "portfolio_update",
                    "date": date,
                    "turn": turn,
                    "agent_id": agent_id,
                    "source_ltb_id": turn_result["previous_ltb"]["ltb_id"],
                    "source_stb_id": turn_result["stb_id"],
                    "analysis_id": turn_result["analysis_id"],
                    "decision_id": turn_result["decision_id"],
                    "fill_id": fill_id,
                    "fills": fills,
                    "state": state,
                },
            )
    return execution_by_agent


async def post_trade_posting_phase(
    *,
    turn_results: list[dict[str, Any]],
    community_agent: CommunityAgent,
    execution_by_agent: dict[str, dict[str, Any]],
    turn: int,
    date: str,
    client: OpenRouterClient,
    concurrency: int = config.SIMULATION_CONCURRENCY,
    event_logger: SimulationLogger | None = None,
    random_seed: int = config.RANDOM_SEED,
    response_journal: ResponseJournal | None = None,
    journal_run_id: str | None = None,
    journal_condition_id: str | None = None,
    journal_event_id: str | None = None,
    phase_attempt_id: str | None = None,
    event_attempt_number: int | None = None,
) -> None:
    active_results = [
        result
        for result in turn_results
        if can_author_community_post(result.get("agent", {}))
    ]
    if not active_results:
        return

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one_post(result: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        async with semaphore:
            agent = result["agent"]
            agent_id = str(agent["agent_id"])
            execution = execution_by_agent.get(agent_id) or {}
            committed_fill = execution.get("fill") or result.get("fill")
            committed_ltb = result.get("ltb")
            if not isinstance(committed_fill, dict) or not isinstance(
                committed_ltb,
                dict,
            ):
                raise RuntimeError(
                    f"community posting lineage is incomplete for {agent_id}"
                )
            scope = (
                response_journal_scope(
                    journal=response_journal,
                    run_id=str(journal_run_id),
                    condition_id=str(journal_condition_id),
                    event_id=str(journal_event_id),
                    phase_attempt_id=str(phase_attempt_id),
                    event_attempt_number=int(event_attempt_number or 0),
                    agent_id=agent_id,
                )
                if response_journal is not None
                else nullcontext()
            )
            with scope:
                post_result = await posting_decision(
                    agent,
                    committed_ltb=committed_ltb,
                    committed_fill=committed_fill,
                    date=date,
                    client=client,
                    seed=stable_llm_seed(
                        random_seed,
                        agent_id,
                        turn,
                        "community_posting",
                    ),
                )
            if post_result is None:
                return agent_id, None
            return agent_id, post_result

    parallel_posts = await asyncio.gather(
        *(_one_post(result) for result in active_results),
        return_exceptions=True,
    )
    posting_errors = [result for result in parallel_posts if isinstance(result, BaseException)]
    if posting_errors:
        details = "; ".join(
            f"{type(error).__name__}: {error}" for error in posting_errors[:5]
        )
        raise ParallelTaskError(
            f"{len(posting_errors)} community posting call(s) failed: {details}",
            posting_errors,
        ) from posting_errors[0]
    generated_posts = [result for result in parallel_posts if isinstance(result, tuple)]
    if len(generated_posts) != len(active_results):
        raise RuntimeError("community posting result count mismatch")
    # LLM calls remain parallel, but persistent IDs must not depend on response latency.
    for agent_id, post_result in sorted(generated_posts, key=lambda item: item[0]):
        if post_result is None:
            continue
        post_id = community_agent.save_post(
            agent_id=agent_id,
            turn=turn,
            date=date,
            post_type=post_result["post_type"],
            title=post_result["title"],
            content=post_result["content"],
            source_ltb_id=post_result["source_ltb_id"],
            source_fill_id=post_result["source_fill_id"],
            source_decision_id=post_result["source_decision_id"],
        )
        if event_logger is not None:
            event_logger.log_community_post(
                agent_id=agent_id,
                turn=turn,
                date=date,
                post={**post_result, "post_id": post_id},
            )


async def community_phase(
    *,
    agents: list[dict[str, Any]],
    community_agent: CommunityAgent,
    memory_agent: MemoryAgent,
    sim_db_path: Path | str,
    turn: int,
    date: str,
    client: OpenRouterClient,
    concurrency: int = config.SIMULATION_CONCURRENCY,
    event_logger: SimulationLogger | None = None,
    random_seed: int = config.RANDOM_SEED,
    response_journal: ResponseJournal | None = None,
    journal_run_id: str | None = None,
    journal_condition_id: str | None = None,
    journal_event_id: str | None = None,
    phase_attempt_id: str | None = None,
    event_attempt_number: int | None = None,
) -> None:
    validate_selective_read_limits(
        depth1=config.COMMUNITY_DEPTH1_READ_LIMIT,
        depth2=config.COMMUNITY_DEPTH2_READ_LIMIT,
    )
    cohort_agent_ids = {str(agent["agent_id"]) for agent in agents}

    def _freeze_best_and_save_logs(
        posts_read_by_agent: dict[str, list[dict[str, Any]]],
        candidate_posts_by_agent: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        frozen_best = community_agent.freeze_best_posts(
            date=date,
            turn=turn,
            n=config.COMMUNITY_BEST_POST_COUNT,
            memory_agent=memory_agent,
        )
        for post in frozen_best:
            self_excluded = int(str(post["author_agent_id"]) in cohort_agent_ids)
            post["audience_count"] = len(agents) - self_excluded
            post["scheduled_delivery_count"] = len(agents) - self_excluded
            post["actual_delivery_count"] = None
            post["self_excluded_count"] = self_excluded
            post["self_exclusion_policy"] = "exclude_author_no_backfill"
            post["delivery_status"] = "scheduled_next_am"
        if event_logger is not None:
            event_logger.log_community_best_posts(
                turn=turn,
                date=date,
                best_posts=frozen_best,
            )
        for agent in agents:
            agent_id = str(agent["agent_id"])
            depth = int(agent.get("news_depth") or 0)
            projected_best = community_agent.project_best_posts_for_reader(
                frozen_best,
                recipient_agent_id=agent_id,
                depth=depth,
            )
            posts_read = posts_read_by_agent.get(agent_id, [])
            candidate_posts = candidate_posts_by_agent.get(agent_id, [])
            community_agent.save_community_log(
                agent_id=agent_id,
                turn=turn,
                date=date,
                best_posts=projected_best,
                posts_read=posts_read,
                candidate_posts_seen=candidate_posts,
                thinking="",
            )
            if event_logger is not None:
                event_logger.log_community_log(
                    agent_id=agent_id,
                    turn=turn,
                    date=date,
                    best_posts=projected_best,
                    posts_read=posts_read,
                    candidate_posts_seen=candidate_posts,
                )
        return frozen_best

    if not config.ENABLE_COMMUNITY_READING:
        _freeze_best_and_save_logs({}, {})
        return

    active_agents = [agent for agent in agents if int(agent.get("news_depth") or 0) >= 1]
    if not active_agents:
        _freeze_best_and_save_logs({}, {})
        return

    semaphore = asyncio.Semaphore(max(1, concurrency))
    post_list_snapshot = community_agent.get_today_posts(date)

    async def _one_agent_reading(
        agent: dict[str, Any],
    ) -> tuple[str, list[int], list[dict[str, Any]], list[dict[str, Any]]]:
        async with semaphore:
            depth = int(agent.get("news_depth") or 0)
            read_limit = (
                config.COMMUNITY_DEPTH2_READ_LIMIT
                if depth >= 2
                else config.COMMUNITY_DEPTH1_READ_LIMIT
            )
            if read_limit != expected_selective_read_limit(depth):
                raise RuntimeError(
                    f"community read limit drifted for depth {depth}: {read_limit}"
                )
            agent_id = str(agent["agent_id"])
            if not post_list_snapshot:
                return agent_id, [], [], []
            visible_posts = [
                dict(post)
                for post in post_list_snapshot
                if str(post["agent_id"]) != agent_id
            ]
            scope = (
                response_journal_scope(
                    journal=response_journal,
                    run_id=str(journal_run_id),
                    condition_id=str(journal_condition_id),
                    event_id=str(journal_event_id),
                    phase_attempt_id=str(phase_attempt_id),
                    event_attempt_number=int(event_attempt_number or 0),
                    agent_id=agent_id,
                )
                if response_journal is not None
                else nullcontext()
            )
            with scope:
                selected_ids = await community_reading_select(
                    agent,
                    visible_posts,
                    read_limit,
                    client=client,
                    seed=stable_llm_seed(
                        random_seed,
                        agent_id,
                        turn,
                        "community_read_select",
                    ),
                )
            if event_logger is not None:
                event_logger.log_community_selection_input(
                    agent_id=agent_id,
                    turn=turn,
                    date=date,
                    depth=depth,
                    read_limit=read_limit,
                    visible_posts=visible_posts,
                    selected_post_ids=selected_ids,
                )
            selected_id_set = {int(post_id) for post_id in selected_ids}
            candidate_posts = [
                {
                    "post_id": int(post["post_id"]),
                    "anonymous_code": str(post.get("anonymous_code") or ""),
                    "post_type": str(post.get("post_type") or ""),
                    "title": str(post.get("title") or ""),
                    "like_count": int(post.get("like_count") or 0),
                    "unlike_count": int(post.get("unlike_count") or 0),
                    "score": int(post.get("score") or 0),
                    "selected": int(post["post_id"]) in selected_id_set,
                    "exposure_level": "title_only",
                    "is_best": False,
                }
                for post in visible_posts
            ]
            if not selected_ids:
                return agent_id, [], [], candidate_posts

            posts_content: list[dict[str, Any]] = []
            for post_id in selected_ids:
                content = community_agent.get_post_content(post_id)
                if not content or str(content.get("agent_id")) == agent_id:
                    continue
                author_agent_id = str(content.get("agent_id"))
                content["author_profile"] = (
                    community_agent.get_author_profile(author_agent_id, memory_agent, turn)
                    if depth == 2
                    else None
                )
                content["profile_scope"] = "detailed" if depth == 2 else "minimal"
                posts_content.append(content)

            reaction_scope = (
                response_journal_scope(
                    journal=response_journal,
                    run_id=str(journal_run_id),
                    condition_id=str(journal_condition_id),
                    event_id=str(journal_event_id),
                    phase_attempt_id=str(phase_attempt_id),
                    event_attempt_number=int(event_attempt_number or 0),
                    agent_id=agent_id,
                )
                if response_journal is not None
                else nullcontext()
            )
            with reaction_scope:
                reactions = await community_reading_react(
                    agent,
                    posts_content,
                    client=client,
                    seed=stable_llm_seed(
                        random_seed,
                        agent_id,
                        turn,
                        "community_read_react",
                    ),
                )
            reaction_map = {int(item["post_id"]): item["reaction"] for item in reactions}
            posts_read: list[dict[str, Any]] = []
            for post in posts_content:
                post_id = int(post["post_id"])
                reaction = reaction_map.get(post_id, "none")
                posts_read.append(
                    {
                        "post_id": post_id,
                        "title": post.get("title", ""),
                        "post_type": post.get("post_type", ""),
                        "content": post.get("content", ""),
                        "body_sha256": hashlib.sha256(
                            str(post.get("content") or "").encode("utf-8")
                        ).hexdigest(),
                        "reaction": reaction,
                        "anonymous_code": post.get("anonymous_code", ""),
                        "author_profile": post.get("author_profile"),
                        "profile_scope": post.get("profile_scope"),
                        "exposure_level": "full_body",
                        "is_best": False,
                    }
                )
            return agent_id, selected_ids, posts_read, candidate_posts

    parallel_reading = await asyncio.gather(
        *(_one_agent_reading(agent) for agent in active_agents),
        return_exceptions=True,
    )
    reading_errors = [result for result in parallel_reading if isinstance(result, BaseException)]
    if reading_errors:
        details = "; ".join(
            f"{type(error).__name__}: {error}" for error in reading_errors[:5]
        )
        raise ParallelTaskError(
            f"{len(reading_errors)} community reading call(s) failed: {details}",
            reading_errors,
        ) from reading_errors[0]
    results = [result for result in parallel_reading if isinstance(result, tuple)]
    if len(results) != len(active_agents):
        raise RuntimeError("community reading result count mismatch")
    # Apply reactions only after every agent has seen the same frozen board.
    for agent_id, selected_ids, posts_read, _candidate_posts in sorted(
        results,
        key=lambda item: item[0],
    ):
        for post in posts_read:
            reaction = str(post.get("reaction") or "none")
            post_id = int(post["post_id"])
            recorded = community_agent.record_reaction(agent_id, post_id, turn, date, reaction)
            if recorded and reaction in {"like", "unlike"}:
                community_agent.update_post_score_live(post_id, reaction)
        if event_logger is not None:
            event_logger.log_community_reading(
                agent_id=agent_id,
                turn=turn,
                date=date,
                selected_post_ids=selected_ids,
                posts_read=posts_read,
            )
    posts_read_by_agent = {
        agent_id: posts_read
        for agent_id, _selected_ids, posts_read, _candidate_posts in results
    }
    candidate_posts_by_agent = {
        agent_id: candidate_posts
        for agent_id, _selected_ids, _posts_read, candidate_posts in results
    }
    best_posts = _freeze_best_and_save_logs(
        posts_read_by_agent,
        candidate_posts_by_agent,
    )
    print(f"  community_phase done: {len(active_agents)} agents, {len(best_posts)} best posts")


def _reset_runtime_tables(db_path: str) -> None:
    init_sim_db(db_path)
    with connect(db_path) as conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")
        conn.execute("DELETE FROM community_interactions")
        conn.execute("DELETE FROM community_logs WHERE turn > 0")
        conn.execute("DELETE FROM community_posts")
        conn.execute("DELETE FROM trade_log")
        conn.execute("DELETE FROM simulation_outcome_consumptions")
        conn.execute("DELETE FROM simulation_trade_outcomes")
        conn.execute("DELETE FROM simulation_ltb_states")
        conn.execute("DELETE FROM simulation_fills")
        conn.execute("DELETE FROM simulation_decisions")
        conn.execute("DELETE FROM simulation_analyses")
        conn.execute("DELETE FROM simulation_stb_states")
        conn.execute("DELETE FROM TradingDetails")
        conn.execute("DELETE FROM belief_history WHERE turn > 0")
        conn.execute("DELETE FROM portfolio_state WHERE turn > 0")
        conn.execute("DELETE FROM agent_system_messages")
        runtime_tables = (
            "TradingDetails",
            "trade_log",
            "community_posts",
            "community_interactions",
            "agent_system_messages",
        )
        placeholders = ",".join("?" for _ in runtime_tables)
        conn.execute(
            f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
            runtime_tables,
        )
        conn.commit()
