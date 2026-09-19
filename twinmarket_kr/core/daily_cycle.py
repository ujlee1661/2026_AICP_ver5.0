from __future__ import annotations

import json
import asyncio
from typing import Any

import config
from twinmarket_kr.agents.fundamental_agent import FundamentalAgent
from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.agents.news_agent import (
    AM_NEWS_WINDOW_END_TIME,
    PM_NEWS_WINDOW_END_TIME,
    NewsAgent,
)
from twinmarket_kr.community.thinking import community_thinking
from twinmarket_kr.core.collect_context import collect_context
from twinmarket_kr.llm.analysis import (
    DEPTH2_MAX_SEARCH_ARTICLES,
    analyze_market,
    depth2_post_search,
    depth2_pre_search,
)
from twinmarket_kr.llm.belief import belief_dimensions, generate_short_term_belief
from twinmarket_kr.llm.client import OpenRouterClient, stable_llm_seed
from twinmarket_kr.llm.decision import build_trading_constraints, make_decision


def _portfolio_numbers(
    memory_agent: MemoryAgent,
    agent_id: str,
    turn: int,
    *,
    stock_code: str,
) -> tuple[float, int]:
    row = memory_agent._latest_portfolio(agent_id, before_or_at_turn=turn)  # internal read for orchestration
    if row is None:
        return 0.0, 0
    current_quantity = 0
    for pos in json.loads(row["positions"]):
        if pos.get("stock_code") == stock_code:
            current_quantity = int(pos.get("quantity", 0))
            break
    return float(row["cash"]), current_quantity


def _build_current_evidence(
    *,
    agent_id: str,
    turn: int,
    news_context: dict[str, Any],
    community_reflection: dict[str, Any] | None,
) -> tuple[dict[str, Any], set[str]]:
    """Project only current news/community evidence into the STB boundary."""

    read_by_id = {
        str(item["id"]): item
        for item in news_context.get("read_contents") or []
        if item.get("id")
    }
    news_rows: list[dict[str, Any]] = []
    evidence_ids: set[str] = set()
    for title_row in news_context.get("daily_titles") or []:
        evidence_id = str(title_row.get("article_id") or title_row.get("id") or "")
        if not evidence_id:
            raise ValueError("current news evidence is missing an article ID")
        item = {
            "evidence_id": evidence_id,
            "kind": "news",
            "payload_sha256": str(title_row.get("payload_sha256") or ""),
            "title": str(title_row.get("title") or ""),
            "published_at": str(
                title_row.get("published_at")
                or f"{title_row.get('date', '')}T{title_row.get('time', '')}"
            ),
        }
        readable = read_by_id.get(str(title_row.get("id") or ""))
        if readable is not None:
            item["summary"] = str(readable.get("content") or "")
        news_rows.append(item)
        evidence_ids.add(evidence_id)

    search_rows: list[dict[str, Any]] = []
    for row in news_context.get("search_read_contents") or []:
        evidence_id = str(row.get("article_id") or row.get("id") or "")
        if not evidence_id or evidence_id in evidence_ids:
            continue
        search_rows.append(
            {
                "evidence_id": evidence_id,
                "kind": "depth2_recent_search",
                "payload_sha256": str(row.get("payload_sha256") or ""),
                "title": str(row.get("title") or ""),
                "summary": str(row.get("summary") or ""),
                "published_at": str(
                    row.get("published_at")
                    or f"{row.get('date', '')}T{row.get('time', '')}"
                ),
            }
        )
        evidence_ids.add(evidence_id)

    community_claims: list[dict[str, Any]] = []
    for index, claim in enumerate(
        (community_reflection or {}).get("claims") or [],
        start=1,
    ):
        claim_id = f"community_claim:{agent_id}:t{turn:03d}:{index:02d}"
        community_claims.append(
            {
                "evidence_id": claim_id,
                "kind": "community_claim",
                "claim_text": str(claim["claim_text"]),
                "stance": str(claim["claim_stance"]),
                "source_exposure_ids": list(claim["source_exposure_ids"]),
                "supporting_quote": str(claim["supporting_quote"]),
            }
        )
        evidence_ids.add(claim_id)
    return {
        "news": news_rows,
        "depth2_search_results": search_rows,
        "community_claims": community_claims,
    }, evidence_ids


async def run_agent_turn(
    agent: dict[str, Any],
    *,
    turn: int,
    date: str,
    market_features_date: str | None = None,
    news_max_date: str | None = None,
    news_start_date: str | None = None,
    news_start_time: str | None = None,
    news_end_time: str | None = None,
    execution_date: str | None = None,
    information_mode: str = "pre_close_cutoff",
    subturn: str = "full",
    open_price: float | None = None,
    previous_close: float | None = None,
    execution_reference: str | None = None,
    decision_space: str = "buy_sell_only",
    memory_agent: MemoryAgent,
    fundamental_agent: FundamentalAgent,
    news_agent: NewsAgent,
    client: OpenRouterClient | None = None,
    event_logger: Any | None = None,
    db_write_lock: asyncio.Lock | None = None,
    community_agent: Any | None = None,
    random_seed: int = config.RANDOM_SEED,
    stock_code: str = config.STOCK_CODE,
) -> dict[str, Any]:
    agent_id = str(agent["agent_id"])
    today_context = collect_context(
        agent,
        turn=turn,
        date=date,
        market_features_date=market_features_date,
        news_max_date=news_max_date,
        news_start_date=news_start_date,
        news_start_time=news_start_time,
        news_end_time=news_end_time,
        execution_date=execution_date,
        information_mode=information_mode,
        subturn=subturn,
        open_price=open_price,
        previous_close=previous_close,
        execution_reference=execution_reference,
        memory_agent=memory_agent,
        fundamental_agent=fundamental_agent,
        news_agent=news_agent,
        community_agent=community_agent,
        stock_code=stock_code,
    )
    community_log = today_context.get("community_log")
    if (
        event_logger is not None
        and subturn == "am"
        and community_log is not None
        and today_context.get("community_log_turn") is not None
    ):
        event_logger.log_community_delivery(
            agent_id=agent_id,
            source_turn=int(today_context["community_log_turn"]),
            delivery_turn=turn,
            source_date=str(community_log.get("date") or ""),
            delivery_date=date,
            best_posts=community_log.get("best_posts_seen") or [],
            posts_read=community_log.get("posts_read") or [],
        )
    today_context["news_context"] = news_agent.expand_context_from_selection(
        base_context=today_context["news_context"],
        current_date=today_context["news_max_date"],
    )
    depth2_flow = None
    if int(agent.get("news_depth") if agent.get("news_depth") is not None else 1) >= 2:
        pre_search = await depth2_pre_search(
            agent,
            today_context["news_context"],
            client=client,
            seed=stable_llm_seed(random_seed, agent_id, turn, "depth2_pre_search"),
        )
        search_results = []
        post_search = {
            "new_findings": [],
            "view_change": "유지",
            "view_change_detail": "추가 검색을 수행하지 않았습니다.",
            "unresolved_questions": [],
        }
        if pre_search.get("search_keywords"):
            base_article_ids = {
                str(row.get("article_id") or row.get("id") or "").strip()
                for row in (
                    today_context["news_context"].get("daily_titles") or []
                )
                if str(
                    row.get("article_id") or row.get("id") or ""
                ).strip()
            }
            search_results = news_agent.search_news_flat(
                keywords=list(pre_search.get("search_keywords") or []),
                current_date=today_context["news_max_date"],
                window_end_date=today_context.get("news_max_date"),
                window_end_time=today_context.get("news_end_time")
                or (
                    AM_NEWS_WINDOW_END_TIME
                    if subturn == "am"
                    else PM_NEWS_WINDOW_END_TIME
                ),
                lookback_days=7,
                top_n=DEPTH2_MAX_SEARCH_ARTICLES,
                exclude_article_ids=base_article_ids,
            )
            post_search = await depth2_post_search(
                agent,
                today_context["news_context"],
                search_results,
                pre_search,
                client=client,
                seed=stable_llm_seed(random_seed, agent_id, turn, "depth2_post_search"),
            )
        depth2_flow = {
            "step1_base": {
                "headline_count": len(today_context["news_context"].get("daily_titles") or []),
                "summary_count": len(today_context["news_context"].get("read_contents") or []),
            },
            "step2_pre_search_thinking": pre_search,
            "step3_search": {
                "keywords": list(pre_search.get("search_keywords") or []),
                "result_count": len(search_results),
            },
            "step4_post_search_thinking": post_search,
        }
        today_context["news_context"]["search_results"] = search_results
        today_context["news_context"]["search_read_contents"] = search_results
        today_context["news_context"]["search_result_ids"] = [
            str(row.get("id")) for row in search_results if row.get("id")
        ]
        today_context["news_context"]["depth2_flow"] = depth2_flow
    # 미선택 title-only 후보는 분석 로그 전용이므로 claim 단계를 유발하지 않는다.
    # full-body 노출(전일 Best 또는 직접 선택해 읽은 글)이 있을 때만 해석을 만든다.
    should_do_community_thinking = (
        community_agent is not None
        and community_log is not None
        and bool(
            community_log.get("best_posts_seen")
            or community_log.get("posts_read")
        )
    )
    if should_do_community_thinking:
        community_reflection = await community_thinking(
            agent,
            community_log,
            delivery_turn=turn,
            delivery_date=date,
            client=client,
            seed=stable_llm_seed(random_seed, agent_id, turn, "community_thinking"),
        )
        if db_write_lock is not None:
            async with db_write_lock:
                community_agent.update_community_thinking(
                    agent_id,
                    int(today_context["community_log_turn"]),
                    community_reflection,
                )
        else:
            community_agent.update_community_thinking(
                agent_id,
                int(today_context["community_log_turn"]),
                community_reflection,
            )
    else:
        community_reflection = None
    today_context["community_thinking"] = community_reflection
    current_evidence, allowed_evidence_ids = _build_current_evidence(
        agent_id=agent_id,
        turn=turn,
        news_context=today_context["news_context"],
        community_reflection=community_reflection,
    )
    event = {
        "event_id": f"{date}/{subturn.upper()}",
        "turn": int(turn),
        "date": date,
        "subturn": subturn,
    }
    previous_ltb = memory_agent.previous_ltb(
        agent_id=agent_id,
        decision_turn=turn,
    )
    current_stb = await generate_short_term_belief(
        agent,
        event=event,
        current_evidence=current_evidence,
        allowed_evidence_ids=allowed_evidence_ids,
        client=client,
        seed=stable_llm_seed(random_seed, agent_id, turn, "short_term_belief"),
    )
    current_stb_dimensions = belief_dimensions(
        current_stb,
        label="generated_short_term_belief",
    )
    if db_write_lock is not None:
        async with db_write_lock:
            stb_id = memory_agent.save_stb(
                agent_id=agent_id,
                turn=turn,
                date=date,
                subturn=subturn,
                dimensions=current_stb_dimensions,
                evidence=current_evidence,
                dimension_evidence=current_stb["dimension_evidence"],
            )
    else:
        stb_id = memory_agent.save_stb(
            agent_id=agent_id,
            turn=turn,
            date=date,
            subturn=subturn,
            dimensions=current_stb_dimensions,
            evidence=current_evidence,
            dimension_evidence=current_stb["dimension_evidence"],
        )
    current_stb_state = memory_agent.get_stb(stb_id)
    current_price = float(today_context.get("announced_price") or today_context["market_features"]["close"])
    available_cash, current_quantity = _portfolio_numbers(
        memory_agent,
        agent["agent_id"],
        turn - 1,
        stock_code=stock_code,
    )
    constraints = build_trading_constraints(
        available_cash=available_cash,
        current_quantity=current_quantity,
        current_price=current_price,
        price_label=str(today_context.get("price_label") or "공시가"),
        allow_hold=decision_space != "buy_sell_only",
    )
    today_context["trading_constraints"] = constraints
    market_analysis = await analyze_market(
        agent,
        previous_ltb=previous_ltb,
        current_stb=current_stb_state,
        market_features=today_context["market_features"],
        portfolio_summary=today_context["portfolio_summary"],
        execution_state=constraints,
        advisor_note=today_context.get("advisor_note"),
        client=client,
        seed=stable_llm_seed(random_seed, agent_id, turn, "market_analysis"),
    )
    if db_write_lock is not None:
        async with db_write_lock:
            analysis_id = memory_agent.record_analysis_lineage(
                agent_id=agent_id,
                turn=turn,
                date=date,
                subturn=subturn,
                source_ltb_id=str(previous_ltb["ltb_id"]),
                source_stb_id=stb_id,
                analysis=market_analysis,
            )
    else:
        analysis_id = memory_agent.record_analysis_lineage(
            agent_id=agent_id,
            turn=turn,
            date=date,
            subturn=subturn,
            source_ltb_id=str(previous_ltb["ltb_id"]),
            source_stb_id=stb_id,
            analysis=market_analysis,
        )
    decision = await make_decision(
        agent,
        previous_ltb,
        current_stb_state,
        market_analysis,
        today_context["portfolio_summary"],
        constraints,
        advisor_note=today_context.get("advisor_note"),
        allow_hold=decision_space != "buy_sell_only",
        client=client,
        seed=stable_llm_seed(random_seed, agent_id, turn, "trading_decision"),
    )
    if db_write_lock is not None:
        async with db_write_lock:
            decision_id = memory_agent.record_decision_lineage(
                agent_id=agent_id,
                turn=turn,
                date=date,
                subturn=subturn,
                source_ltb_id=str(previous_ltb["ltb_id"]),
                source_stb_id=stb_id,
                analysis_id=analysis_id,
                decision=decision,
            )
    else:
        decision_id = memory_agent.record_decision_lineage(
            agent_id=agent_id,
            turn=turn,
            date=date,
            subturn=subturn,
            source_ltb_id=str(previous_ltb["ltb_id"]),
            source_stb_id=stb_id,
            analysis_id=analysis_id,
            decision=decision,
        )
    trade_log = {
        "agent_id": agent["agent_id"],
        "turn": turn,
        "date": execution_date or date,
        "action": decision["action"],
        "stock_code": stock_code,
        "quantity": decision["quantity"],
        "fee": 0,
        "action_reason": decision["reason"],
        "risk_control": decision["risk_control"],
        "order_type": "announced_price",
        "submitted_price": None,
        "status": "pending" if decision["action"] != "hold" and decision["quantity"] > 0 else "not_submitted",
        "filled_quantity": 0,
        "analysis_id": analysis_id,
        "decision_id": decision_id,
        "source_ltb_id": previous_ltb["ltb_id"],
        "source_stb_id": stb_id,
    }
    if db_write_lock is not None:
        async with db_write_lock:
            memory_agent.append_trade_log(trade_log)
    else:
        memory_agent.append_trade_log(trade_log)
    order = None
    if decision["action"] != "hold" and decision["quantity"] > 0:
        order = {
            "stock_code": stock_code,
            "user_id": agent["agent_id"],
            "direction": decision["action"],
            "quantity": decision["quantity"],
            "price": current_price,
            "announced_price": current_price,
            "timestamp": float(turn),
            "reason": decision["reason"],
            "source_ltb_id": previous_ltb["ltb_id"],
            "source_stb_id": stb_id,
            "analysis_id": analysis_id,
            "decision_id": decision_id,
            "decision_date": today_context["decision_date"],
            "market_features_date": today_context["market_features_date"],
            "news_start_date": today_context["news_start_date"],
            "news_start_time": today_context["news_start_time"],
            "news_max_date": today_context["news_max_date"],
            "news_end_time": today_context["news_end_time"],
            "execution_date": today_context["execution_date"],
            "information_mode": today_context["information_mode"],
            "subturn": today_context["subturn"],
            "decision_attempts": decision.get("decision_attempts"),
            "decision_origin": decision.get("decision_origin"),
            "one_share_reason": decision.get("one_share_reason"),
        }
    if event_logger is not None:
        cited_news_ids = {
            evidence_id
            for dimension in current_stb["dimension_evidence"].values()
            for relation in ("support", "contradict")
            for evidence_id in dimension[relation]
            if evidence_id in {
                str(item.get("article_id") or item.get("id") or "")
                for item in today_context["news_context"].get("daily_titles") or []
            }
        }
        news_interpretation = {
            "source": "short_term_belief_dimension_evidence",
            "selected_news": [{"id": value} for value in sorted(cited_news_ids)],
            "unmapped_selected_news": [],
            "news_sentiment": "encoded_in_stb",
        }
        today_context["news_interpretation"] = news_interpretation
        today_context["news_context"]["influential_news_ids"] = sorted(
            cited_news_ids
        )
        fake_news_audit = news_agent.fake_audit_for_context(
            today_context["news_context"],
            selected_news=news_interpretation.get("selected_news") or [],
        )
        event_logger.log_agent_turn(
            agent=agent,
            turn=turn,
            date=date,
            context=today_context,
            news_interpretation=news_interpretation,
            belief={"stb_id": stb_id, **current_stb},
            market_analysis=market_analysis,
            decision=decision,
            order=order,
            depth2_flow=depth2_flow,
            fake_news_audit=fake_news_audit,
            lineage={
                "source_ltb_id": previous_ltb["ltb_id"],
                "source_stb_id": stb_id,
                "analysis_id": analysis_id,
                "decision_id": decision_id,
            },
        )
    return {
        "agent": agent,
        "context": today_context,
        "previous_ltb": previous_ltb,
        "stb": current_stb_state,
        "stb_id": stb_id,
        "analysis_id": analysis_id,
        "decision": decision,
        "decision_id": decision_id,
        "order": order,
        "market_analysis": market_analysis,
    }
