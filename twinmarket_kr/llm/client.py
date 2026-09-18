from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

try:
    from openai import AsyncOpenAI
except ModuleNotFoundError:
    AsyncOpenAI = None  # type: ignore[assignment]

import config

if TYPE_CHECKING:
    from twinmarket_kr.llm.call_policy import StrictCallPolicy


class UnexpectedModelError(RuntimeError):
    pass


class ReasoningPolicyError(ValueError):
    """A call tried to remove or alter the paper model's reasoning-off body."""


class APIAuditIntegrityError(RuntimeError):
    """Run-local provider telemetry cannot be bound to an accepted response."""


_PAPER_REASONING_OFF_BODY = {"effort": "none", "exclude": True}


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return int(value)


def _slot_namespace(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("slot_namespace must be a non-empty string when supplied")
    return value.strip()


def _audit_path(value: Path | str | None) -> Path | None:
    """Normalize an explicitly supplied audit file without changing legacy defaults."""

    if value is None:
        return None
    if not isinstance(value, (Path, str)):
        raise ValueError("audit_path must be a filesystem path when supplied")
    path = Path(value)
    if not path.name:
        raise ValueError("audit_path must name a file")
    return path


def _audit_context(value: Mapping[str, str] | None) -> dict[str, str]:
    """Keep audit provenance small, serializable, and unambiguous."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("audit_context must be a string mapping when supplied")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip():
            raise ValueError("audit_context keys and values must be non-empty strings")
        normalized[key.strip()] = item.strip()
    return dict(sorted(normalized.items()))


def stable_llm_seed(base_seed: int, *parts: object) -> int:
    payload = "|".join([str(base_seed), *(str(part) for part in parts)])
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def validate_experiment_call_policy() -> dict[str, Any]:
    """Fail before an experiment can send a non-baseline OpenRouter request."""
    errors: list[str] = []
    if config.OPENROUTER_MODEL != config.PAPER_OPENROUTER_MODEL:
        errors.append(
            f"OPENROUTER_MODEL must be {config.PAPER_OPENROUTER_MODEL!r}"
        )
    if config.OPENROUTER_COMMUNITY_MODEL != config.PAPER_OPENROUTER_MODEL:
        errors.append(
            f"OPENROUTER_COMMUNITY_MODEL must be {config.PAPER_OPENROUTER_MODEL!r}"
        )
    if not config.OPENROUTER_REQUIRE_PARAMETERS:
        errors.append("OPENROUTER_REQUIRE_PARAMETERS must be true")
    if config.OPENROUTER_ALLOW_FALLBACKS:
        errors.append("OPENROUTER_ALLOW_FALLBACKS must be false")
    expected_providers = [config.PAPER_OPENROUTER_PROVIDER]
    if config.OPENROUTER_PROVIDER_ORDER != expected_providers:
        errors.append(
            "OPENROUTER_PROVIDER_ORDER must contain only "
            f"{config.PAPER_OPENROUTER_PROVIDER!r}"
        )
    if errors:
        raise ReasoningPolicyError("; ".join(errors))
    return {
        "model": config.PAPER_OPENROUTER_MODEL,
        "reasoning": dict(_PAPER_REASONING_OFF_BODY),
        "provider": {
            "only": expected_providers,
            "order": expected_providers,
            "allow_fallbacks": False,
            "require_parameters": True,
        },
    }


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_retries: int | None = None,
        timeout: float = 120.0,
        concurrency_limit: int | None = None,
        slot_namespace: str | None = None,
        audit_path: Path | str | None = None,
        audit_context: Mapping[str, str] | None = None,
    ) -> None:
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self.base_url = base_url or config.OPENROUTER_BASE_URL
        self.model = model or config.OPENROUTER_MODEL
        self.max_retries = config.OPENROUTER_MAX_RETRIES if max_retries is None else max_retries
        self.timeout = timeout
        self.concurrency_limit = (
            config.OPENROUTER_GLOBAL_CONCURRENCY
            if concurrency_limit is None
            else _positive_int(concurrency_limit, "concurrency_limit")
        )
        self.slot_namespace = _slot_namespace(slot_namespace)
        # Legacy callers deliberately retain the process-wide audit location.
        # The RN paper factory supplies a run/arm-local file plus immutable
        # context, so a gate never has to infer a study from a shared log.
        self.audit_path = _audit_path(audit_path)
        self.audit_context = _audit_context(audit_context)
        self.offline = os.getenv("TWINMARKET_OFFLINE_LLM", "").strip().lower() in {"1", "true", "yes"}
        if self.offline:
            self.client = None
            return
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set.")
        if AsyncOpenAI is None:
            raise RuntimeError("openai package is not installed. Run pip install -r requirements.txt.")
        # Disable the SDK's hidden retry layer so every physical retry is governed
        # by this client, counted in the audit log, and covered by the global slot.
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout,
            max_retries=0,
        )

    @property
    def is_offline(self) -> bool:
        """Expose stub mode so a paper launcher can reject it before any work starts."""
        return self.offline

    def request_policy(self) -> dict[str, Any]:
        """Return the legacy/default provider policy for shared exploratory code.

        The RealNews Community AB runner passes its sealed strict policy through
        :meth:`chat` explicitly.  Keeping the default here unchanged avoids
        silently changing the historic 0720 / exploratory execution path.
        """
        provider: dict[str, Any] = {
            "require_parameters": config.OPENROUTER_REQUIRE_PARAMETERS,
            "allow_fallbacks": config.OPENROUTER_ALLOW_FALLBACKS,
        }
        if config.OPENROUTER_PROVIDER_ORDER:
            provider["order"] = list(config.OPENROUTER_PROVIDER_ORDER)
            provider["only"] = list(config.OPENROUTER_PROVIDER_ORDER)
        policy: dict[str, Any] = {"provider": provider}
        if self.model == config.PAPER_REASONING_DISABLED_MODEL:
            # This exact object is sent under ``extra_body`` by ``chat``.
            # ``effort: none`` disables reasoning; ``exclude`` only prevents
            # a response trace from being returned.
            policy["reasoning"] = dict(_PAPER_REASONING_OFF_BODY)
        return policy

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.2,
        seed: int | None = None,
        max_tokens: int | None = None,
        audit_label: str = "unspecified",
        logical_call_id: str | None = None,
        phase_attempt_id: str | None = None,
        request_policy: Mapping[str, Any] | None = None,
    ) -> Any:
        if self.offline:
            return _offline_response(messages)

        requested_model = str(model or self.model)
        kwargs: dict[str, Any] = {
            "model": requested_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            kwargs["response_format"] = response_format
        if seed is not None:
            kwargs["seed"] = int(seed)
        normalized_max_tokens = (
            None if max_tokens is None else _positive_int(max_tokens, "max_tokens")
        )
        if (
            not self.offline
            and getattr(self, "audit_context", {}).get("artifact")
            == "integrated_experiment_openrouter_attempt"
            and normalized_max_tokens is None
        ):
            raise ValueError(
                "Integrated production calls require an explicit max_tokens budget"
            )
        if normalized_max_tokens is not None:
            kwargs["max_tokens"] = normalized_max_tokens
        supplied_request_policy = (
            dict(request_policy) if request_policy is not None else self.request_policy()
        )
        final_request_policy = _enforce_paper_reasoning_off(
            requested_model,
            supplied_request_policy,
        )
        kwargs["extra_body"] = final_request_policy

        delay = 1.0
        last_error: Exception | None = None
        prompt_sha256 = hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for attempt in range(1, self.max_retries + 1):
            started = time.perf_counter()
            response = None
            try:
                async with _global_openrouter_slot(
                    self.concurrency_limit,
                    slot_namespace=self.slot_namespace,
                ):
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(**kwargs),
                        timeout=self.timeout + 5,
                    )
                returned_model = getattr(response, "model", None)
                if returned_model and str(returned_model) != str(kwargs["model"]):
                    raise UnexpectedModelError(
                        f"OpenRouter returned {returned_model!r} for requested model "
                        f"{kwargs['model']!r}"
                    )
                audit_row = _record_api_audit(
                    label=audit_label,
                    requested_model=str(kwargs["model"]),
                    seed=seed,
                    attempt=attempt,
                    latency_seconds=time.perf_counter() - started,
                    prompt_sha256=prompt_sha256,
                    response=response,
                    request_policy=final_request_policy,
                    temperature=temperature,
                    response_format=response_format,
                    max_tokens=normalized_max_tokens,
                    logical_call_id=logical_call_id,
                    phase_attempt_id=phase_attempt_id,
                    audit_path=getattr(self, "audit_path", None),
                    audit_context=getattr(self, "audit_context", None),
                )
                if (
                    logical_call_id
                    and phase_attempt_id
                    and audit_row["status"] == "provider_returned"
                ):
                    pending = getattr(self, "_pending_provider_audits", None)
                    if pending is None:
                        pending = {}
                        self._pending_provider_audits = pending
                    pending[(logical_call_id, phase_attempt_id)] = audit_row
                return response
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                _record_api_audit(
                    label=audit_label,
                    requested_model=str(kwargs["model"]),
                    seed=seed,
                    attempt=attempt,
                    latency_seconds=time.perf_counter() - started,
                    prompt_sha256=prompt_sha256,
                    response=response,
                    error=exc,
                    request_policy=final_request_policy,
                    temperature=temperature,
                    response_format=response_format,
                    max_tokens=normalized_max_tokens,
                    logical_call_id=logical_call_id,
                    phase_attempt_id=phase_attempt_id,
                    audit_path=getattr(self, "audit_path", None),
                    audit_context=getattr(self, "audit_context", None),
                )
                if attempt >= self.max_retries or not _is_retryable_error(exc):
                    break
                retry_after = _retry_after_seconds(exc)
                wait_seconds = retry_after if retry_after is not None else delay
                await asyncio.sleep(min(config.OPENROUTER_RETRY_MAX_DELAY, max(0.0, wait_seconds)))
                delay = min(config.OPENROUTER_RETRY_MAX_DELAY, delay * 2)
        raise RuntimeError(
            f"OpenRouter chat failed after {self.max_retries} attempts: {last_error}"
        ) from last_error

    async def chat_strict_reasoning_off(
        self,
        messages: list[dict[str, str]],
        *,
        policy: "StrictCallPolicy",
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.2,
        seed: int | None = None,
        max_tokens: int,
        audit_label: str,
        logical_call_id: str,
        phase_attempt_id: str,
    ) -> Any:
        """RN-only path that forbids reasoning for the pinned paper model.

        The explicit model comparison is intentional: unlike the generic
        exploratory client, RN cannot silently move to another model merely
        because it supports a similarly named reasoning option.

        ``max_tokens`` is required (with no default) so a paper call cannot
        accidentally rely on a provider/model default.  OpenRouter's pinned
        provider policy also requires support for every supplied parameter.
        """
        if self.offline:
            raise RuntimeError("Strict RN calls reject offline LLM stubs")
        normalized_max_tokens = _positive_int(max_tokens, "RN strict max_tokens")
        policy.validate()
        if policy.model != config.PAPER_REASONING_DISABLED_MODEL:
            raise UnexpectedModelError(
                "RN strict path only permits "
                f"{config.PAPER_REASONING_DISABLED_MODEL!r}, not {policy.model!r}"
            )
        # Keep JSON-object enforcement at the provider boundary as well as in
        # the stage adapter.  A future direct caller of this RN-only method
        # must not be able to silently omit the response-format contract or
        # tune sampling while still receiving a strict reasoning-off policy.
        from twinmarket_kr.llm.call_policy import (
            RN_STRICT_RESPONSE_FORMAT,
            RN_STRICT_TEMPERATURE,
        )

        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or float(temperature) != RN_STRICT_TEMPERATURE
        ):
            raise ValueError(
                "RN strict calls require the sealed temperature "
                f"{RN_STRICT_TEMPERATURE}"
            )
        if response_format != dict(RN_STRICT_RESPONSE_FORMAT):
            raise ValueError("RN strict calls require the sealed JSON-object response format")
        final_policy = policy.as_request_policy()
        policy.assert_final_request_policy(final_policy)
        return await self.chat(
            messages,
            model=config.PAPER_REASONING_DISABLED_MODEL,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            temperature=temperature,
            seed=seed,
            max_tokens=normalized_max_tokens,
            audit_label=audit_label,
            logical_call_id=logical_call_id,
            phase_attempt_id=phase_attempt_id,
            request_policy=final_policy,
        )

    async def ping(self) -> str:
        response = await self.chat(
            [{"role": "user", "content": "Reply with pong."}],
            temperature=0,
        )
        return response_content(response)

    def record_experiment_acceptance(
        self,
        *,
        logical_call_id: str,
        phase_attempt_id: str,
        accepted_response_sha256: str,
    ) -> None:
        """Append an experiment-acceptance event after strict schema validation.

        The provider-attempt row is written as soon as OpenRouter returns.  It
        is deliberately *not* called successful scientific work at that point.
        The stage/community adapter invokes this method only after its exact
        JSON parser, stage validator, and durable response journal have all
        accepted the object.  On restart the method can reconstruct a missing
        acceptance event from the immutable provider row and journal digest.
        """

        _require_sha256(accepted_response_sha256, label="accepted_response_sha256")
        if not logical_call_id or not phase_attempt_id:
            raise APIAuditIntegrityError(
                "Acceptance requires logical_call_id and phase_attempt_id"
            )
        path = _audit_path(getattr(self, "audit_path", None))
        if path is None:
            raise APIAuditIntegrityError(
                "Experiment acceptance requires an explicit run-local audit path"
            )
        expected_context = _audit_context(getattr(self, "audit_context", None))
        pending = getattr(self, "_pending_provider_audits", {})
        provider_row = pending.get((logical_call_id, phase_attempt_id))
        if provider_row is not None:
            _validate_provider_row_for_acceptance(
                provider_row,
                logical_call_id=logical_call_id,
                accepted_response_sha256=accepted_response_sha256,
                expected_context=expected_context,
            )
            acceptance = _acceptance_row(
                provider_row,
                accepted_response_sha256=accepted_response_sha256,
            )
            _append_api_audit_payload(path, acceptance)
            pending.pop((logical_call_id, phase_attempt_id), None)
            return

        # Restart repair: the journal may already contain an accepted response
        # while the process died before the small acceptance event was flushed.
        # Read and append under one file lock so two recovery processes cannot
        # create duplicate acceptance events.
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                rows = _read_audit_handle(handle, path=path)
                accepted = [
                    row
                    for row in rows
                    if row.get("audit_event") == "experiment_acceptance"
                    and row.get("logical_call_id") == logical_call_id
                ]
                if accepted:
                    if (
                        len(accepted) == 1
                        and accepted[0].get("accepted_response_sha256")
                        == accepted_response_sha256
                    ):
                        return
                    raise APIAuditIntegrityError(
                        f"Conflicting or duplicate acceptance audit for {logical_call_id}"
                    )
                candidates = [
                    row
                    for row in rows
                    if row.get("audit_event") == "provider_attempt"
                    and row.get("status") == "provider_returned"
                    and row.get("logical_call_id") == logical_call_id
                    and row.get("provider_canonical_json_sha256")
                    == accepted_response_sha256
                ]
                if not candidates:
                    raise APIAuditIntegrityError(
                        f"No exact provider response can bind accepted journal response "
                        f"{logical_call_id}"
                    )
                # Identical response bodies from repeated physical attempts are
                # scientifically equivalent.  Bind the latest deterministic
                # row, which is the only one that could have immediately
                # preceded the durable journal write on a recovery path.
                provider_row = candidates[-1]
                _validate_provider_row_for_acceptance(
                    provider_row,
                    logical_call_id=logical_call_id,
                    accepted_response_sha256=accepted_response_sha256,
                    expected_context=expected_context,
                )
                acceptance = _acceptance_row(
                    provider_row,
                    accepted_response_sha256=accepted_response_sha256,
                )
                handle.seek(0, os.SEEK_END)
                handle.write(
                    json.dumps(acceptance, ensure_ascii=False, default=str) + "\n"
                )
                handle.flush()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def response_content(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return str(message.get("content") or "")
        return ""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", "") or "")


def response_finish_reason(response: Any) -> str | None:
    """Return the first completion finish reason without accepting a default."""

    if isinstance(response, dict):
        choices = response.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return None
        value = choices[0].get("finish_reason")
    else:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return None
        value = getattr(choices[0], "finish_reason", None)
    return value if isinstance(value, str) else None


def _enforce_paper_reasoning_off(
    model: str,
    request_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Put OpenRouter's exact off switch in every Qwen paper request.

    This runs after all caller/default policy selection and immediately before
    ``extra_body`` is assigned.  Thus an accidental caller merge cannot send
    the pinned paper model without ``reasoning.effort = 'none'``.
    """
    final_policy = dict(request_policy)
    if model != config.PAPER_REASONING_DISABLED_MODEL:
        return final_policy
    reasoning = final_policy.get("reasoning")
    if reasoning is not None and reasoning != _PAPER_REASONING_OFF_BODY:
        raise ReasoningPolicyError(
            "The pinned paper model requires reasoning={'effort': 'none', 'exclude': True}"
        )
    final_policy["reasoning"] = dict(_PAPER_REASONING_OFF_BODY)
    return final_policy


def _retry_after_seconds(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _is_retryable_error(error: BaseException) -> bool:
    if isinstance(error, UnexpectedModelError):
        return False
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code is None:
        # Only known network/transport failures are safe to retry. A local
        # ValueError/TypeError or an unknown SDK failure will not improve by
        # replaying the same request six times.
        retryable_names = {
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
        return (
            isinstance(error, (TimeoutError, ConnectionError))
            or type(error).__name__ in retryable_names
        )
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return False
    return code in {408, 409, 429} or 500 <= code <= 599


def _record_api_audit(
    *,
    label: str,
    requested_model: str,
    seed: int | None,
    attempt: int,
    latency_seconds: float,
    prompt_sha256: str,
    response: Any | None = None,
    error: BaseException | None = None,
    request_policy: dict[str, Any] | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    logical_call_id: str | None = None,
    phase_attempt_id: str | None = None,
    audit_path: Path | str | None = None,
    audit_context: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    path = _audit_path(audit_path) or Path(config.OPENROUTER_AUDIT_LOG)
    context = _audit_context(audit_context)
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = getattr(response, "usage", None) if response is not None else None
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    elif usage is not None and not isinstance(usage, dict):
        usage = {
            key: getattr(usage, key, None)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
    usage_payload = _usage_payload(usage)
    is_rn_experiment_attempt = bool(
        logical_call_id
        and phase_attempt_id
        and context.get("artifact")
        in {
            "rn_ab_strict_openrouter_attempt",
            "integrated_experiment_openrouter_attempt",
        }
    )
    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "label": label,
        "audit_event": "provider_attempt",
        "status": (
            "error"
            if error is not None
            else ("provider_returned" if is_rn_experiment_attempt else "success")
        ),
        "requested_model": requested_model,
        "returned_model": getattr(response, "model", None) if response is not None else None,
        "provider": _response_provider(response),
        "request_id": getattr(response, "id", None) if response is not None else None,
        "seed": seed,
        "attempt": attempt,
        "latency_seconds": round(latency_seconds, 6),
        "prompt_sha256": prompt_sha256,
        "usage": usage_payload,
        "reasoning_tokens": _reasoning_tokens(usage_payload),
        "response_reasoning_present": _response_reasoning_present(response),
        "finish_reason": response_finish_reason(response),
        "provider_response_sha256": (
            _provider_response_sha256(response) if error is None else None
        ),
        "provider_canonical_json_sha256": (
            _provider_json_object_sha256(response) if error is None else None
        ),
        "accepted_response_sha256": None,
        "provider_attempt_sha256": None,
        "request_policy": request_policy or {},
        "temperature": temperature,
        "response_format": response_format,
        "max_tokens": max_tokens,
        "request_policy_sha256": hashlib.sha256(
            json.dumps(request_policy or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "logical_call_id": logical_call_id,
        "phase_attempt_id": phase_attempt_id,
        "audit_context": context,
        "error_type": type(error).__name__ if error is not None else None,
        "error": str(error) if error is not None else None,
    }
    _append_api_audit_payload(path, payload)
    return payload


def _append_api_audit_payload(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _provider_json_object_sha256(response: Any) -> str | None:
    """Hash canonical JSON only; stage-specific acceptance remains separate."""

    raw = response_content(response)
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_strict_audit_json_object,
            parse_constant=_reject_audit_json_constant,
        )
        if not isinstance(parsed, dict):
            return None
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _provider_response_sha256(response: Any) -> str:
    """Preserve every provider return, including malformed/non-object JSON."""

    return hashlib.sha256(response_content(response).encode("utf-8")).hexdigest()


def _strict_audit_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_audit_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _require_sha256(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value.lower())
    ):
        raise APIAuditIntegrityError(f"{label} must be a lowercase SHA-256 digest")


def _canonical_audit_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_provider_row_for_acceptance(
    row: Mapping[str, Any],
    *,
    logical_call_id: str,
    accepted_response_sha256: str,
    expected_context: Mapping[str, str],
) -> None:
    if (
        row.get("audit_event") != "provider_attempt"
        or row.get("status") != "provider_returned"
        or row.get("logical_call_id") != logical_call_id
        or row.get("provider_canonical_json_sha256") != accepted_response_sha256
        or row.get("accepted_response_sha256") is not None
        or row.get("provider_attempt_sha256") is not None
        or row.get("audit_context") != dict(expected_context)
    ):
        raise APIAuditIntegrityError(
            f"Provider audit does not exactly match accepted response {logical_call_id}"
        )


def _acceptance_row(
    provider_row: Mapping[str, Any],
    *,
    accepted_response_sha256: str,
) -> dict[str, Any]:
    acceptance = dict(provider_row)
    acceptance.update(
        {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "audit_event": "experiment_acceptance",
            "status": "accepted",
            "accepted_response_sha256": accepted_response_sha256,
            "provider_attempt_sha256": _canonical_audit_sha256(provider_row),
        }
    )
    return acceptance


def _read_audit_handle(handle: Any, *, path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(handle, start=1):
        raw = line.strip()
        if not raw:
            continue
        try:
            row = json.loads(
                raw,
                object_pairs_hook=_strict_audit_json_object,
                parse_constant=_reject_audit_json_constant,
            )
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise APIAuditIntegrityError(
                f"Malformed audit row at {path}:{line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise APIAuditIntegrityError(
                f"Non-object audit row at {path}:{line_number}"
            )
        rows.append(row)
    return rows


def _usage_payload(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        dumped = usage.model_dump()
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    return {
        key: getattr(usage, key, None)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")
    }


def _reasoning_tokens(usage: dict[str, Any] | None) -> int | None:
    if usage is None:
        return None
    candidates: list[Any] = [usage.get("reasoning_tokens")]
    for nested_key in ("completion_tokens_details", "prompt_tokens_details", "details"):
        nested = usage.get(nested_key)
        if isinstance(nested, dict):
            candidates.append(nested.get("reasoning_tokens"))
    for value in candidates:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _response_provider(response: Any | None) -> str | None:
    if response is None:
        return None
    for field in ("provider", "provider_name"):
        value = getattr(response, field, None)
        if value:
            return str(value)
    if isinstance(response, dict):
        for field in ("provider", "provider_name"):
            if response.get(field):
                return str(response[field])
    return None


def _response_reasoning_present(response: Any | None) -> bool:
    if response is None:
        return False
    if isinstance(response, dict):
        choices = response.get("choices") or []
    else:
        choices = getattr(response, "choices", None) or []
    if not choices:
        return False
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else getattr(first, "message", None)
    if isinstance(message, dict):
        values = (message.get("reasoning"), message.get("reasoning_content"))
    else:
        values = (getattr(message, "reasoning", None), getattr(message, "reasoning_content", None))
    return any(value not in (None, "", [], {}) for value in values)


@asynccontextmanager
async def _global_openrouter_slot(limit: int, *, slot_namespace: str | None = None):
    """Cross-process semaphore shared by all six condition runners on one machine."""
    if limit < 1:
        yield
        return
    slot_dir = Path(config.OPENROUTER_SLOT_DIR)
    if slot_namespace is not None:
        # The directory is a concurrency namespace, not a user-facing name.
        # Hash it so a run/condition identifier cannot influence the path.
        slot_dir = slot_dir / hashlib.sha256(slot_namespace.encode("utf-8")).hexdigest()
    slot_dir.mkdir(parents=True, exist_ok=True)
    handle = None
    while handle is None:
        for index in range(limit):
            candidate = (slot_dir / f"slot_{index:03d}.lock").open("a+", encoding="utf-8")
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                candidate.close()
                continue
            handle = candidate
            break
        if handle is None:
            await asyncio.sleep(0.05)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _offline_response(messages: list[dict[str, str]]) -> str:
    prompt = messages[-1].get("content", "") if messages else ""
    if '"schema_version": "simulation-stb-input-v1"' in prompt:
        payload = _extract_json_after_label(prompt, "입력 정보(JSON):")
        evidence_ids = [
            str(value)
            for value in payload.get("sanitized_evidence_registry", [])
            if str(value)
        ]
        first_id = evidence_ids[:1]
        dimension_evidence = {
            f"dim_{index}": {
                "support": list(first_id),
                "contradict": [],
            }
            for index in range(1, 7)
        }
        return json.dumps(
            {
                "dim_1": "현재 정보는 한 달 방향을 단정하기 어려워 신중한 관점을 지지한다.",
                "dim_2": "현재 정보만으로 밸류에이션의 고평가 여부를 확정하기 어렵다.",
                "dim_3": "거시와 반도체 업황 신호를 계속 확인해야 한다.",
                "dim_4": "현재 투자 심리는 한 방향보다 혼조에 가깝다.",
                "dim_5": "새 정보는 과신보다 제한적인 대응이 필요함을 시사한다.",
                # STB dim_6은 과거 성과 회고가 아니라 오늘 정보의 한계와 주의점이다.
                # validate_stb_dim_6_scope가 두 구획 표시를 모두 요구한다.
                "dim_6": "정보 한계: 제목과 요약만 제공됨 / 주의점: 제공되지 않은 수치를 추정하지 않기",
                "dimension_evidence": dimension_evidence,
            },
            ensure_ascii=False,
        )
    if '"schema_version": "simulation-post-fill-ltb-input-v1"' in prompt:
        payload = _extract_json_after_label(prompt, "입력 정보(JSON):")
        current_stb = payload.get("current_stb") or {}
        raw_evidence = current_stb.get("dimension_evidence") or {}
        integration_evidence = {
            f"dim_{index}": {
                "support": list(
                    (raw_evidence.get(f"dim_{index}") or {}).get("support") or []
                ),
                "contradict": list(
                    (raw_evidence.get(f"dim_{index}") or {}).get("contradict") or []
                ),
            }
            for index in range(1, 7)
        }
        due_outcomes = payload.get("eligible_price_outcomes_dim_6_only") or []
        # Keep the offline smoke stub subject to the same scientific evidence
        # contract as a live response. Positive action-aligned markouts support
        # the prior action, negative markouts contradict it, and an exactly
        # flat outcome is consumed in support by deterministic convention.
        from twinmarket_kr.outcome_schedule import outcome_evidence_relation

        # 가격 결과는 인용이 아니라 순서대로 판정만 낸다(라이브와 같은 계약).
        verdicts = [
            outcome_evidence_relation(row.get("action_aligned_markout"))
            or "support"
            for row in due_outcomes
            if isinstance(row, dict)
        ]
        event = payload.get("event") or {}
        turn = int(event.get("turn") or 0)
        extra = {"outcome_verdicts": verdicts} if verdicts else {}
        return json.dumps(
            {
                **extra,
                "dim_1": f"{turn}번째 정보까지 통합해 한 달 방향을 신중하게 본다.",
                "dim_2": f"{turn}번째 판단에서도 밸류에이션 확신을 제한한다.",
                "dim_3": f"{turn}번째 갱신 후에도 거시와 업황 확인을 이어간다.",
                "dim_4": f"{turn}번째 갱신에서 시장 심리를 혼조로 본다.",
                "dim_5": f"{turn}번째 새 정보를 누적 관점에 선별적으로 반영한다.",
                "dim_6": f"{turn}번째 실제 체결과 도래한 성과를 바탕으로 과신을 경계한다.",
                "integration_evidence": integration_evidence,
            },
            ensure_ascii=False,
        )
    # analysis/decision은 봉인 stage payload 없이 이름 있는 슬롯만 받는다.
    # 따라서 stage 식별과 필드 추출 모두 프롬프트 본문의 슬롯 라벨을 기준으로 한다.
    if "거래 전 시장 분석을 JSON으로 작성" in prompt:
        market = _extract_json_after_label(prompt, "(source 이름: market):")
        execution_state = _extract_json_after_label(
            prompt, "(source 이름: execution_state):"
        )
        market_field = next(iter(market), "close")
        execution_field = next(iter(execution_state), "available_cash")
        return json.dumps(
            {
                "market_view": "현재 신호는 혼조로 해석한다.",
                "valuation_view": "현재 가격만으로 저평가 여부를 단정하지 않는다.",
                "technical_view": "가격과 거래량 신호를 함께 확인해야 한다.",
                "news_view": "뉴스 영향은 긍정과 부정이 섞여 있다.",
                "portfolio_view": "실행 가능한 범위에서 제한적으로 대응한다.",
                "key_risks": ["변동성", "정보 불확실성"],
                "opportunity": ["과도하지 않은 제한적 대응"],
                "caution": ["집중 위험을 피한다"],
                "confidence": "medium",
                "directional_stance": "uncertain",
                "evidence_references": [
                    {"source": "previous_ltb", "field": "dim_1"},
                    {"source": "current_stb", "field": "dim_1"},
                    {"source": "market", "field": market_field},
                    {"source": "execution_state", "field": execution_field},
                ],
            },
            ensure_ascii=False,
        )
    if "【공시가 기반 체결 규칙】" in prompt:
        constraints = _extract_json_after_label(prompt, "거래 제약:")
        allowed = list(constraints.get("allowed_actions") or ["buy"])
        action = "buy" if "buy" in allowed else "sell"
        max_quantity = int(
            constraints.get(
                "max_buy_quantity" if action == "buy" else "max_sell_quantity"
            )
            or 1
        )
        return json.dumps(
            {
                "action": action,
                "requested_quantity": max(1, min(max_quantity, 10)),
                "reason": "두 belief와 실행 제약을 함께 고려한 제한적 거래다.",
                "risk_control": "현금과 보유량 제약 안에서 수량을 제한한다.",
            },
            ensure_ascii=False,
        )
    if "observed_sentiment" in prompt and "source_exposure_id" in prompt:
        # 실제 렌더링처럼 [인용 N] 번호와 노출 ID를 읽어 claim 하나를 만든다.
        # 번호 인용 계약(supporting_quote_ref)이 스텁에서도 검증되게 한다.
        quote_refs = [int(v) for v in re.findall(r"\[인용 (\d+)\]", prompt)]
        exposure_ids = re.findall(r"source_exposure_id: (\S+)", prompt)
        claims = (
            [
                {
                    "claim_text": "커뮤니티에서 반복 관찰된 주장을 정리했다.",
                    "claim_stance": "uncertain",
                    "source_exposure_ids": [exposure_ids[0]],
                    "supporting_quote_ref": quote_refs[0],
                }
            ]
            if quote_refs and exposure_ids
            else []
        )
        return json.dumps(
            {
                "observed_sentiment": "uncertain",
                "claims": claims,
                "agreement_disagreement": "검증 가능한 주장 없이 분위기만 관찰했다.",
                "uncertainty": "커뮤니티 글만으로 거래 방향을 단정하지 않는다.",
            },
            ensure_ascii=False,
        )
    if "[모드: react]" in prompt:
        post_ids = [int(value) for value in re.findall(r"post_id=(\d+)", prompt)]
        reactions = [
            {"post_id": post_id, "reaction": ("like" if index % 3 == 0 else "none")}
            for index, post_id in enumerate(post_ids)
        ]
        return json.dumps({"reactions": reactions}, ensure_ascii=False)
    if "[모드: select]" in prompt:
        limit_match = re.search(r"최대\s+(\d+)개", prompt)
        limit = int(limit_match.group(1)) if limit_match else 3
        post_ids = [int(value) for value in re.findall(r"post_id=(\d+)", prompt)]
        return json.dumps({"selected_post_ids": post_ids[:limit]}, ensure_ascii=False)
    if "will_post" in prompt or "게시글 타입 6종" in prompt:
        return json.dumps(
            {
                "will_post": True,
                "post_type": "impression",
                "title": "오늘 삼성전자 흐름 메모",
                "content": "가격 흐름과 뉴스가 엇갈려 보입니다. 무리하지 않고 수량을 제한해 대응하겠습니다.",
            },
            ensure_ascii=False,
        )
    if "[어제 커뮤니티 Best 게시글]" in prompt and "자유 형식 텍스트" in prompt:
        return "커뮤니티는 혼조 분위기입니다. 가격 리스크와 장기 성장성을 함께 보겠습니다."
    if "거래 제약:" in prompt and "최종 결정을 출력" in prompt:
        constraints = _extract_json_after_label(prompt, "거래 제약:")
        allowed = constraints.get("allowed_actions") or ["buy"]
        action = "buy" if "buy" in allowed else "sell"
        max_quantity = int(constraints.get("max_buy_quantity" if action == "buy" else "max_sell_quantity") or 1)
        quantity = max(1, min(max_quantity, 10))
        return json.dumps(
            {
                "action": action,
                "requested_quantity": quantity,
                "reason": "Offline smoke run: mixed signals justify a small constrained trade.",
                "risk_control": "Keep position size small and preserve cash for later turns.",
            },
            ensure_ascii=False,
        )
    if "거래 전 시장 분석" in prompt:
        return json.dumps(
            {
                "market_view": "Mixed short-term setup with both price risk and recovery potential.",
                "valuation_view": "Valuation is not decisive in this offline smoke run.",
                "technical_view": "Price and volume signals are treated as mixed.",
                "news_view": "News impact is mixed and requires cautious sizing.",
                "portfolio_view": "Portfolio risk is managed through small order size.",
                "key_risks": ["volatility", "news uncertainty"],
                "opportunity": ["limited entry after weakness"],
                "caution": ["avoid excessive concentration"],
                "confidence": "medium",
            },
            ensure_ascii=False,
        )
    if (
        "Belief를 JSON" in prompt
        or "투자 Belief를 JSON" in prompt
        or ("belief_summary" in prompt and "dim_1" in prompt)
    ):
        return json.dumps(
            {
                "dim_1": "Short-term direction is mixed, so I will respond cautiously.",
                "dim_2": "Valuation needs confirmation from market data.",
                "dim_3": "Macro and semiconductor cycle signals remain important.",
                "dim_4": "Investor sentiment looks mixed rather than one-sided.",
                "dim_5": "News flow supports caution and selective action.",
                "dim_6": "I should avoid overconfidence and keep trades small.",
                "belief_summary": "Samsung Electronics has mixed signals today. I will trade conservatively within constraints.",
                "view_change": "Maintained a cautious view based on mixed information.",
            },
            ensure_ascii=False,
        )
    if "작업 모드:\npost_search" in prompt:
        return json.dumps(
            {
                "new_findings": [],
                "view_change": "유지",
                "view_change_detail": "Offline smoke run did not add search results.",
                "unresolved_questions": [],
            },
            ensure_ascii=False,
        )
    if "작업 모드:\npre_search" in prompt:
        return json.dumps(
            {
                "key_findings": ["Offline smoke run checks the trailing news window."],
                "curiosity_points": ["HBM demand", "exchange rate", "foreign flows"],
                "search_rationale": "Verify the seven-day cutoff and search audit path.",
                "search_keywords": ["HBM", "환율", "외국인"],
            },
            ensure_ascii=False,
        )
    if "뉴스" in prompt:
        return json.dumps(
            {
                "selected_news": [],
                "news_sentiment": "mixed",
                "short_term_impact": "Short-term impact is mixed.",
                "long_term_impact": "Long-term impact depends on earnings and semiconductor demand.",
                "persona_interpretation": "The investor stays cautious and avoids oversized trades.",
                "confidence": "medium",
                "reason": "Offline smoke run summary.",
            },
            ensure_ascii=False,
        )
    return "pong"


def _extract_json_after_label(prompt: str, label: str) -> dict[str, Any]:
    start = prompt.find(label)
    if start < 0:
        return {}
    start = prompt.find("{", start)
    if start < 0:
        return {}
    depth = 0
    for index in range(start, len(prompt)):
        char = prompt[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(prompt[start : index + 1])
                except json.JSONDecodeError:
                    return {}
                return data if isinstance(data, dict) else {}
    return {}
