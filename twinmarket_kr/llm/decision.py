from __future__ import annotations

import json
from typing import Any

import config
from twinmarket_kr.llm.belief import belief_dimensions, render_prompt
from twinmarket_kr.llm.call_policy import (
    INTEGRATED_STAGE_MAX_TOKENS_V1,
    INTEGRATED_STAGE_SCHEMA_VERSIONS_V1,
)
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed
from twinmarket_kr.llm.response_journal import (
    ResponseJournalError,
    open_journal_call,
    parse_strict_json_object,
)
from twinmarket_kr.llm.validation import (
    LLMValidationError,
    record_validation_failure,
    retry_temperature_schedule,
)


def build_trading_constraints(
    *,
    available_cash: float,
    current_quantity: int,
    current_price: float,
    min_order_unit: int = config.MIN_ORDER_UNIT,
    max_single_trade_cash_ratio: float = config.MAX_SINGLE_TRADE_CASH_RATIO,
    allow_hold: bool = False,
    price_label: str = "공시가",
) -> dict[str, Any]:
    usable_cash = max(0.0, available_cash * max_single_trade_cash_ratio)
    max_buy_quantity = int(usable_cash // current_price) if current_price > 0 else 0
    allowed_actions = []
    if max_buy_quantity >= min_order_unit:
        allowed_actions.append("buy")
    if current_quantity >= min_order_unit:
        allowed_actions.append("sell")
    if allow_hold:
        allowed_actions.append("hold")
    return {
        "available_cash": available_cash,
        "max_single_trade_cash_ratio": max_single_trade_cash_ratio,
        "max_single_trade_cash": usable_cash,
        "current_quantity": current_quantity,
        "current_price": current_price,
        "announced_price": current_price,
        "price_label": price_label,
        "min_order_unit": min_order_unit,
        "max_affordable_quantity": max_buy_quantity,
        "max_buy_quantity": max_buy_quantity,
        "max_sell_quantity": current_quantity,
        "allow_hold": allow_hold,
        "allowed_actions": allowed_actions,
    }


def parse_decision_json(content: str, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    expected_keys = {"action", "requested_quantity", "reason", "risk_control"}
    raw_keys = set(data)
    for key, default in {
        "action": "",
        "requested_quantity": 0,
        "reason": "",
        "risk_control": "",
    }.items():
        data.setdefault(key, default)
    action = str(data["action"]).strip().lower()
    corrections: list[str] = []
    validation_errors: list[str] = []
    if raw_keys != expected_keys:
        validation_errors.append(
            "top_level_keys:"
            f"missing={sorted(expected_keys - raw_keys)}:"
            f"unknown={sorted(raw_keys - expected_keys)}"
        )
    raw_quantity = data.get("requested_quantity")
    try:
        if isinstance(raw_quantity, bool):
            raise ValueError("boolean is not an order quantity")
        numeric_quantity = float(raw_quantity)
        if not numeric_quantity.is_integer():
            raise ValueError("fractional share quantity")
        quantity = int(numeric_quantity)
    except (TypeError, ValueError, OverflowError):
        quantity = 0
        validation_errors.append("invalid_quantity_format")
    order_type = "announced_price"
    price = 0.0
    if constraints:
        allow_hold = bool(constraints.get("allow_hold", False))
        reference_price = float(constraints.get("current_price") or 0)
        min_order_unit = int(constraints["min_order_unit"])
        max_buy_quantity = int(constraints.get("max_buy_quantity") or 0)
        max_sell_quantity = int(constraints["max_sell_quantity"])
        allowed_actions = set(constraints.get("allowed_actions") or [])
        if action not in allowed_actions:
            validation_errors.append(f"action_not_allowed:{action}")
        if action == "hold" and not allow_hold:
            validation_errors.append("hold_not_allowed")
        if action not in {"buy", "sell"} and not (allow_hold and action == "hold"):
            validation_errors.append(f"invalid_action:{action}")
        if quantity < min_order_unit:
            validation_errors.append("quantity_below_min")
        if action == "buy" and quantity > max_buy_quantity:
            validation_errors.append(f"buy_quantity_exceeds_max:{quantity}>{max_buy_quantity}")
        if action == "sell" and quantity > max_sell_quantity:
            validation_errors.append(f"sell_quantity_exceeds_holding:{quantity}>{max_sell_quantity}")
        price = reference_price if action in {"buy", "sell"} else 0
        if not allowed_actions:
            validation_errors.append("no_allowed_actions")
    if not str(data.get("reason") or "").strip():
        validation_errors.append("missing_reason")
    if not str(data.get("risk_control") or "").strip():
        validation_errors.append("missing_risk_control")
    return {
        "action": action,
        "requested_quantity": quantity,
        "quantity": quantity,
        "order_type": order_type,
        "price": price,
        "reason": str(data.get("reason") or ""),
        "risk_control": str(data.get("risk_control") or ""),
        "order_corrections": corrections,
        "validation_errors": validation_errors,
        "valid": not validation_errors,
    }


class DecisionValidationError(LLMValidationError):
    pass


class DecisionConstraintError(LLMValidationError):
    """No valid buy/sell can be executed under the current portfolio constraints."""


def _decision_diagnostics(
    decision: dict[str, Any],
    constraints: dict[str, Any],
    *,
    attempts: int,
) -> dict[str, Any]:
    action = str(decision.get("action") or "")
    quantity = int(decision.get("quantity") or 0)
    max_quantity = int(
        constraints.get("max_buy_quantity" if action == "buy" else "max_sell_quantity") or 0
    )
    if quantity != 1:
        one_share_reason = "not_one_share"
    elif max_quantity == 1:
        one_share_reason = "constraint_maximum_is_one"
    elif max_quantity > 1:
        one_share_reason = "llm_selected_one_with_larger_capacity"
    else:
        one_share_reason = "unknown"
    return {
        "decision_attempts": attempts,
        "decision_retry_count": attempts - 1,
        "decision_origin": "llm_first_response" if attempts == 1 else "llm_validation_retry",
        "available_cash": float(constraints.get("available_cash") or 0),
        "current_quantity": int(constraints.get("current_quantity") or 0),
        "max_buy_quantity": int(constraints.get("max_buy_quantity") or 0),
        "max_sell_quantity": int(constraints.get("max_sell_quantity") or 0),
        "selected_action_max_quantity": max_quantity,
        "one_share_reason": one_share_reason,
        "deterministic_fallback_used": False,
    }


async def make_decision(
    agent: dict[str, Any],
    previous_ltb: dict[str, Any],
    current_stb: dict[str, Any],
    market_analysis: dict[str, Any],
    portfolio_summary: str,
    trading_constraints: dict[str, Any],
    *,
    advisor_note: str | None = None,
    allow_hold: bool = False,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
    validation_attempts: int = 10,
) -> dict[str, Any]:
    if not trading_constraints.get("allowed_actions"):
        raise DecisionConstraintError(
            "No buy/sell action is executable under the current cash and holding constraints"
        )
    client = client or OpenRouterClient()
    decision_space_instruction = (
        '이번 실행에서는 action이 반드시 "buy" 또는 "sell"이어야 합니다. "hold"는 선택할 수 없습니다.'
        if not allow_hold
        else '이번 실행에서는 action으로 "buy", "sell", "hold" 중 하나를 선택할 수 있습니다.'
    )
    previous_dimensions = belief_dimensions(
        previous_ltb.get("dimensions", previous_ltb),
        label="decision.previous_ltb",
    )
    current_dimensions = belief_dimensions(
        current_stb.get("dimensions", current_stb),
        label="decision.current_stb",
    )
    # 이름 있는 슬롯 하나만으로 입력을 전달한다. 예전에는 같은 belief·analysis·
    # constraints를 <<STAGE_PAYLOAD_JSON>>으로 한 번 더 실어 보냈다.
    prompt = render_prompt(
        "make_decision.txt",
        persona_prompt=agent["persona_prompt"],
        today_belief=json.dumps(
            {
                "previous_ltb": previous_dimensions,
                "current_stb": current_dimensions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        market_analysis=json.dumps(market_analysis, ensure_ascii=False, indent=2),
        portfolio_summary=portfolio_summary,
        order_history="원시 주문 이력은 제공하지 않으며 체결 성찰은 이전 LTB에 반영되어 있습니다.",
        trading_constraints=json.dumps(
            trading_constraints, ensure_ascii=False, indent=2
        ),
        decision_space_instruction=decision_space_instruction,
        advisor_note_block=(
            "\n담당 투자 어드바이저의 행동 규율 메모:\n" + advisor_note + "\n"
            if advisor_note
            else ""
        ),
    )
    if validation_attempts < 1:
        raise ValueError("validation_attempts must be at least 1")
    invalid_history: list[list[str]] = []
    current_prompt = prompt
    stage = "trading_decision"
    temperatures = retry_temperature_schedule(0.2, validation_attempts)
    seeds = [
        stable_llm_seed(seed or 0, "decision_validation", attempt)
        for attempt in range(1, validation_attempts + 1)
    ]
    max_tokens = int(INTEGRATED_STAGE_MAX_TOKENS_V1[stage])
    journal_call = open_journal_call(
        stage=stage,
        schema_version=INTEGRATED_STAGE_SCHEMA_VERSIONS_V1[stage],
        base_prompt=prompt,
        client=client,
        temperature_schedule=temperatures,
        seed_schedule=seeds,
        max_tokens=max_tokens,
        validation_attempts=validation_attempts,
        validation_procedure_version="trading-decision-validator-v1",
        response_format={"type": "json_object"},
        semantic_inputs={
            "allow_hold": bool(allow_hold),
            "constraints": dict(trading_constraints),
        },
    )
    if journal_call is not None and journal_call.replay is not None:
        raw = dict(journal_call.replay.response)
        decision = parse_decision_json(
            json.dumps(raw, ensure_ascii=False),
            trading_constraints,
        )
        if not decision.get("valid"):
            raise ResponseJournalError(
                "accepted replay no longer passes trading decision validation: "
                f"{decision.get('validation_errors')}"
            )
        journal_call.repair_replay_acceptance(client=client)
        attempt = journal_call.replay.validation_attempt
        decision.update(
            _decision_diagnostics(
                decision,
                trading_constraints,
                attempts=attempt,
            )
        )
        decision["invalid_attempt_errors"] = (
            journal_call.context.journal.rejected_errors(
                journal_call.logical_call_id
            )
        )
        return decision
    for attempt in range(1, validation_attempts + 1):
        attempt_seed = seeds[attempt - 1]
        if journal_call is not None:
            journal_call.begin(attempt)
        try:
            response = await client.chat(
                [{"role": "user", "content": current_prompt}],
                response_format={"type": "json_object"},
                temperature=temperatures[attempt - 1],
                seed=attempt_seed,
                max_tokens=max_tokens,
                audit_label=stage,
                logical_call_id=(
                    journal_call.logical_call_id
                    if journal_call is not None
                    else None
                ),
                phase_attempt_id=(
                    journal_call.phase_attempt_id
                    if journal_call is not None
                    else None
                ),
            )
        except BaseException as exc:
            if journal_call is not None:
                journal_call.error(attempt, exc)
            raise
        raw_content = response_content(response) or "{}"
        parse_errors: list[str] = []
        raw: dict[str, Any] | None
        try:
            raw = (
                parse_strict_json_object(raw_content)
                if journal_call is not None
                else None
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raw = None
            parse_errors.append(f"strict_json_object:{exc}")
        decision = parse_decision_json(
            (
                json.dumps(raw, ensure_ascii=False)
                if journal_call is not None and raw is not None
                else raw_content
            ),
            trading_constraints,
        )
        if parse_errors:
            decision["validation_errors"] = [
                *parse_errors,
                *list(decision.get("validation_errors") or []),
            ]
            decision["valid"] = False
        if decision.get("valid"):
            if journal_call is not None:
                if raw is None:
                    raise AssertionError("validated response has no raw JSON object")
                journal_call.accept(raw, attempt, client=client)
            decision.update(_decision_diagnostics(decision, trading_constraints, attempts=attempt))
            decision["invalid_attempt_errors"] = invalid_history
            return decision
        invalid_history.append(list(decision.get("validation_errors") or []))
        if journal_call is not None:
            journal_call.rejected(
                attempt,
                response=raw,
                errors=list(decision.get("validation_errors") or []),
            )
        record_validation_failure(
            label="trading_decision",
            attempt=attempt,
            errors=list(decision.get("validation_errors") or []),
            raw_content=raw_content,
            seed=attempt_seed,
        )
        # 안내문은 위 검증기가 실제로 내는 오류와 1:1로 맞아야 한다. 안내가 없는 오류는
        # 모델이 무엇을 고쳐야 하는지 알 수 없어 같은 실패를 반복한다.
        current_prompt = (
            prompt
            + "\n\n이전 응답은 거래 제약을 위반했습니다. "
            + f"위반 항목: {decision.get('validation_errors')}. "
            + "판단 자체는 유지하거나 다시 판단해도 되지만, 설명하지 말고 JSON만 출력하세요. "
            + "최상위 key는 action, requested_quantity, reason, risk_control 네 개뿐입니다. "
            + "top_level_keys 오류의 unknown에 적힌 key는 빼고, missing에 적힌 key는 채우세요. "
            + "action은 반드시 allowed_actions 안의 값이어야 하며, allowed_actions에 hold가 "
            + "없으면 hold를 고를 수 없습니다. "
            + "requested_quantity는 따옴표 없는 정수여야 하고, 최소 주문 단위 이상, "
            + "선택한 action의 최대 수량 이하여야 합니다."
        )
    raise DecisionValidationError(
        "LLM did not produce a valid buy/sell decision after "
        f"{validation_attempts} validation attempts: {invalid_history}"
    )
