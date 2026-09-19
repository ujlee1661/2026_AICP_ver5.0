from __future__ import annotations

from typing import Any

import config
from twinmarket_kr.agents.fundamental_agent import FundamentalAgent
from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.agents.news_agent import NewsAgent


def collect_context(
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
    memory_agent: MemoryAgent,
    fundamental_agent: FundamentalAgent,
    news_agent: NewsAgent,
    community_agent: Any | None = None,
    stock_code: str = config.STOCK_CODE,
) -> dict[str, Any]:
    market_date = market_features_date or date
    news_date = news_max_date or date
    exec_date = execution_date or date
    previous_belief = memory_agent.get_previous_belief(agent["agent_id"], turn)
    portfolio_summary = memory_agent.get_portfolio_summary(agent["agent_id"], turn - 1)
    raw_history = memory_agent.get_recent_order_history(agent["agent_id"], last_n=5, current_date=date)
    order_history = _format_order_history(raw_history)
    action_reason = memory_agent.get_last_action_reason(agent["agent_id"])
    system_message = memory_agent.get_recent_system_message(agent["agent_id"], current_turn=turn)
    advisor_note = memory_agent.get_advisor_note(agent["agent_id"], current_turn=turn)
    news_depth = 1 if agent.get("news_depth") is None else int(agent["news_depth"])
    normalized_subturn = str(subturn).strip().upper()
    if normalized_subturn not in {"AM", "PM"}:
        raise ValueError("integrated runtime news requires an AM or PM subturn")
    news_context = news_agent.build_event_context(
        f"{date}/{normalized_subturn}",
        news_depth,
    )
    market_features = fundamental_agent.get_market_features(
        market_date,
        stock_code,
    )
    market_features["as_of_date"] = market_date
    market_features["subturn"] = subturn
    if previous_close is not None:
        market_features["previous_close"] = previous_close
    if open_price is not None:
        market_features["open_price"] = open_price
    if execution_reference:
        market_features["execution_reference"] = execution_reference
    if subturn == "am" and open_price is not None:
        price_label = "오늘 시가"
        announced_price = float(open_price)
        market_features["reference_price"] = open_price
        market_features["close"] = open_price
        if previous_close:
            market_features["intraday_return_from_prev_close"] = (open_price - previous_close) / previous_close
    else:
        price_label = "오늘 종가" if subturn == "pm" else "공시가"
        announced_price = float(market_features["close"])
    if subturn == "pm":
        market_features["reference_price"] = announced_price
        market_features["close"] = announced_price
        if open_price:
            market_features["intraday_return_from_open"] = (announced_price - open_price) / open_price
    community_log = None
    community_log_turn = None
    if community_agent is not None and turn > 1:
        # The run-scoped community object is the authoritative ON/OFF switch.
        # The prior day's post-close board is injected directly once, at AM.
        community_turn = turn - 1 if subturn == "am" else 0
        if community_turn > 0:
            community_log = community_agent.get_community_log(str(agent["agent_id"]), community_turn)
            if community_log is not None:
                community_log_turn = community_turn
    return {
        "agent_id": agent["agent_id"],
        "turn": turn,
        "date": date,
        "decision_date": date,
        "market_features_date": market_date,
        "news_start_date": news_start_date,
        "news_start_time": news_start_time,
        "news_max_date": news_date,
        "news_end_time": news_end_time,
        "execution_date": exec_date,
        "information_mode": information_mode,
        "subturn": subturn,
        "price_label": price_label,
        "announced_price": announced_price,
        "previous_belief": previous_belief,
        "action_reason": action_reason,
        "system_message": system_message,
        "advisor_note": advisor_note,
        "portfolio_summary": portfolio_summary,
        "order_history": order_history,
        "news_context": news_context,
        "market_features": market_features,
        "community_log": community_log,
        "community_log_turn": community_log_turn,
    }


def _format_order_history(raw_history: list[dict[str, Any]]) -> str:
    if not raw_history:
        return "이전 주문 이력 없음"
    lines = []
    for item in raw_history:
        executed = item.get("executed_price")
        if item.get("filled"):
            exec_text = f"{float(executed):,.0f}원" if executed else "체결"
            lines.append(
                f"{item['date']} turn {item['turn']}: {item['action']} "
                f"{int(item.get('filled_quantity') or 0):,}주 체결@{exec_text}"
            )
        else:
            lines.append(f"{item['date']} turn {item['turn']}: {item['action']} 주문 검증/체결 실패")
    return "\n".join(lines)
