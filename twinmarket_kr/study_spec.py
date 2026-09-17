"""Fail-closed validation for the integrated numbered experiment runner.

This is the small common-runtime contract needed by
``scripts/05_run_simulation.py``.  It deliberately does not import the old
``rn_ab`` package: the numbered runner reads one sealed profile and verifies
that its cohort, prompts, news policy, memory policy, trade policy, and model
policy agree with the code that will actually run.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import config
from twinmarket_kr.experiment_runtime import canonical_sha256, file_sha256
from twinmarket_kr.outcome_schedule import FrozenEventSchedule
from twinmarket_kr.persona.select import (
    PERSONA_RENDERER_ID,
    generate_persona_prompt,
    persona_renderer_sha256,
    structured_persona_sha256,
)


class IntegratedStudySpecError(ValueError):
    """The sealed study profile cannot safely drive the common runtime."""


CONDITIONS = {
    "RN_COMM_OFF": {
        "community_mode": "off",
        "news_treatment": "real_only",
    },
    "RN_COMM_ON": {
        "community_mode": "on",
        "news_treatment": "real_only",
    },
}
OUTCOME_HORIZONS = [
    "next-decision-event",
    "same-subturn-plus-1-trading-date",
    "same-subturn-plus-5-trading-dates",
]

_CORE_PROMPTS = (
    ("stb", "update_short_term_belief.txt"),
    ("analysis", "market_analysis.txt"),
    ("decision", "make_decision.txt"),
    ("post_fill_ltb", "update_long_term_belief.txt"),
)
_SUPPORT_PROMPTS = (
    (
        "initial_belief",
        "initial_belief.txt",
        "sealed_compatibility_only_deterministic_ltb0_no_model_call",
    ),
    (
        "news_pre_search",
        "news_agent_pre_search.txt",
        "conditional_depth2_pre_search_journaled_call",
    ),
    (
        "news_post_search",
        "news_agent_post_search.txt",
        "conditional_depth2_post_search_journaled_call",
    ),
    (
        "news_interpretation",
        "news_interpretation.txt",
        "sealed_compatibility_only_current_stb_owns_interpretation",
    ),
    (
        "community_posting",
        "posting_decision.txt",
        "conditional_post_pm_journaled_call",
    ),
    (
        "community_reading",
        "community_reading.txt",
        "conditional_select_and_react_journaled_calls",
    ),
    (
        "community_interpretation",
        "community_thinking.txt",
        "conditional_next_am_journaled_call",
    ),
)

PRODUCTION_PROMPT_FILENAMES = tuple(
    filename
    for filename in dict.fromkeys(
        [
            *(filename for _, filename in _CORE_PROMPTS),
            *(filename for _, filename, _ in _SUPPORT_PROMPTS),
        ]
    )
)


@dataclass(frozen=True)
class ResolvedStudyProfile:
    condition_id: str
    stock_code: str
    instrument_name: str
    required_agent_count: int
    agent_ids: tuple[str, ...]
    schedule_date_ids: tuple[str, ...]
    per_arm_concurrency: int
    study_seed: int
    burn_in_dates: tuple[str, ...]
    calendar_sha256: str
    price_registry_sha256: str
    study_spec_sha256: str
    cohort_sha256: str
    prompt_bundle_sha256: str


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise IntegratedStudySpecError(
            f"{label} must be a real file: {path}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegratedStudySpecError(
            f"{label} is not valid UTF-8 JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise IntegratedStudySpecError(f"{label} must be a JSON object")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntegratedStudySpecError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise IntegratedStudySpecError(f"{label} must be an array")
    return value


def _expect(value: Any, expected: Any, label: str) -> None:
    if value != expected:
        raise IntegratedStudySpecError(
            f"{label} differs from the integrated baseline: "
            f"observed={value!r} expected={expected!r}"
        )


def integrated_prompt_bundle_sha256(prompt_dir: Path | str) -> str:
    """Hash the exact top-level production prompt set used by the runner."""

    root = Path(prompt_dir)
    if not root.is_dir() or root.is_symlink():
        raise IntegratedStudySpecError(
            f"Production prompt directory must be a real directory: {root}"
        )

    def digest(filename: str) -> str:
        path = root / filename
        if not path.is_file() or path.is_symlink():
            raise IntegratedStudySpecError(
                f"Production prompt is missing or symlinked: {path}"
            )
        return hashlib.sha256(path.read_bytes()).hexdigest()

    payload = {
        "artifact_type": "rn_prompt_bundle",
        "version": "rn-prompt-bundle-v2",
        "core_templates": [
            {
                "stage": stage,
                "filename": filename,
                "sha256": digest(filename),
            }
            for stage, filename in _CORE_PROMPTS
        ],
        "support_templates": [
            {
                "stage": stage,
                "filename": filename,
                "sha256": digest(filename),
                "runtime_use": runtime_use,
            }
            for stage, filename, runtime_use in _SUPPORT_PROMPTS
        ],
    }
    return canonical_sha256(payload)


def _validate_cohort(
    cohort: Mapping[str, Any],
    *,
    agents: Sequence[Mapping[str, Any]],
    expected_count: int,
    expected_depth_counts: Mapping[int, int],
    expected_cash_counts: Mapping[int, int],
    expected_post_permission_counts: Mapping[int, int],
    instrument_name: str,
) -> tuple[str, ...]:
    _expect(cohort.get("artifact_type"), "cohort_registry", "cohort.artifact_type")
    _expect(cohort.get("version"), "cohort-v2", "cohort.version")
    rows = _sequence(cohort.get("agents"), "cohort.agents")
    if len(rows) != expected_count:
        raise IntegratedStudySpecError(
            "cohort.agents length differs from required_agent_count"
        )
    observed_ids: set[str] = set()
    depth_counts: Counter[int] = Counter()
    cash_counts: Counter[int] = Counter()
    post_permission_counts: Counter[int] = Counter()
    runtime_by_id = {str(agent["agent_id"]): agent for agent in agents}
    if len(runtime_by_id) != len(agents):
        raise IntegratedStudySpecError(
            "runtime agent source contains duplicate agent IDs"
        )
    ordered_ids: list[str] = []
    for ordinal, raw in enumerate(rows, start=1):
        row = _mapping(raw, f"cohort.agents[{ordinal - 1}]")
        agent_id = str(row.get("agent_id") or "")
        if not agent_id or agent_id in observed_ids:
            raise IntegratedStudySpecError(
                "cohort agent IDs must be non-empty and unique"
            )
        observed_ids.add(agent_id)
        ordered_ids.append(agent_id)
        _expect(row.get("ordinal"), ordinal, f"cohort[{agent_id}].ordinal")
        if agent_id not in runtime_by_id:
            raise IntegratedStudySpecError(
                f"cohort agent is absent from sys_100.db: {agent_id}"
            )
        runtime = runtime_by_id[agent_id]
        depth = int(row.get("news_depth"))
        can_post = int(row.get("can_post_community"))
        cash = int(row.get("initial_cash"))
        _expect(
            int(runtime.get("news_depth")),
            depth,
            f"cohort[{agent_id}].news_depth",
        )
        _expect(
            int(runtime.get("can_post_community")),
            can_post,
            f"cohort[{agent_id}].can_post_community",
        )
        if can_post not in {0, 1} or (can_post == 1 and depth not in {1, 2}):
            raise IntegratedStudySpecError(
                f"cohort[{agent_id}] has invalid community posting permission"
            )
        _expect(
            int(runtime.get("ini_cash")),
            cash,
            f"cohort[{agent_id}].initial_cash",
        )
        rendered_persona_sha256 = hashlib.sha256(
            generate_persona_prompt(
                dict(runtime),
                instrument_name=instrument_name,
            ).encode("utf-8")
        ).hexdigest()
        _expect(
            rendered_persona_sha256,
            row.get("persona_sha256"),
            f"cohort[{agent_id}].persona_sha256",
        )
        depth_counts[depth] += 1
        cash_counts[cash] += 1
        post_permission_counts[can_post] += 1
    _expect(
        dict(sorted(depth_counts.items())),
        dict(sorted(expected_depth_counts.items())),
        "cohort depth counts",
    )
    _expect(
        dict(sorted(cash_counts.items())),
        dict(sorted(expected_cash_counts.items())),
        "cohort initial-cash counts",
    )
    _expect(
        dict(sorted(post_permission_counts.items())),
        dict(sorted(expected_post_permission_counts.items())),
        "cohort community-post permission counts",
    )
    return tuple(ordered_ids)


def _validate_persona_projection(
    projection: Mapping[str, Any],
    *,
    agents: Sequence[Mapping[str, Any]],
    expected_agent_ids: Sequence[str],
    instrument_name: str,
) -> None:
    """Verify the sealed prompt projection without a second runtime DB."""

    _expect(
        projection.get("artifact_type"),
        "persona_projection_manifest",
        "persona projection artifact_type",
    )
    _expect(
        projection.get("version"),
        "integrated-persona-projection-v2",
        "persona projection version",
    )
    renderer = _mapping(
        projection.get("renderer"),
        "persona projection renderer",
    )
    _expect(
        renderer,
        {
            "id": PERSONA_RENDERER_ID,
            "sha256": persona_renderer_sha256(),
            "normalization": (
                "NFC; LF-only; exactly-one-trailing-LF; legacy-content"
            ),
        },
        "persona projection renderer",
    )
    rows = _sequence(
        projection.get("agents"),
        "persona projection agents",
    )
    if len(rows) != len(expected_agent_ids):
        raise IntegratedStudySpecError(
            "Persona projection length differs from the sealed cohort"
        )
    runtime_by_id = {str(agent["agent_id"]): agent for agent in agents}
    observed: set[str] = set()
    for ordinal, raw in enumerate(rows, start=1):
        row = _mapping(
            raw,
            f"persona projection agents[{ordinal - 1}]",
        )
        agent_id = str(row.get("agent_id") or "")
        if not agent_id or agent_id in observed or agent_id not in runtime_by_id:
            raise IntegratedStudySpecError(
                "Persona projection agent IDs must exactly match the runtime cohort"
            )
        observed.add(agent_id)
        runtime = runtime_by_id[agent_id]
        _expect(
            row,
            {
                "ordinal": ordinal,
                "agent_id": agent_id,
                "news_depth": int(runtime["news_depth"]),
                "can_post_community": int(runtime["can_post_community"]),
                "initial_cash": int(runtime["ini_cash"]),
                "structured_persona_sha256": structured_persona_sha256(
                    dict(runtime)
                ),
                "persona_sha256": hashlib.sha256(
                    generate_persona_prompt(
                        dict(runtime),
                        instrument_name=instrument_name,
                    ).encode("utf-8")
                ).hexdigest(),
            },
            f"persona projection[{agent_id}]",
        )
    _expect(
        tuple(
            str(_mapping(row, "persona projection row").get("agent_id") or "")
            for row in rows
        ),
        tuple(expected_agent_ids),
        "persona projection ordered agent IDs",
    )
    _expect(set(expected_agent_ids), observed, "persona projection agent ID set")


def _positive_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IntegratedStudySpecError(f"{label} must be a positive integer")
    return value


def _count_assertion(
    value: Any,
    *,
    label: str,
    expected_total: int,
) -> dict[int, int]:
    raw = _mapping(value, label)
    normalized: dict[int, int] = {}
    for key, count in raw.items():
        try:
            numeric_key = int(str(key))
        except ValueError as exc:
            raise IntegratedStudySpecError(
                f"{label} keys must be integer strings"
            ) from exc
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or numeric_key in normalized
        ):
            raise IntegratedStudySpecError(
                f"{label} values must be non-negative integer counts"
            )
        if count:
            normalized[numeric_key] = count
    if sum(normalized.values()) != expected_total:
        raise IntegratedStudySpecError(
            f"{label} counts do not sum to required_agent_count"
        )
    return normalized


def _validate_policy(
    spec: Mapping[str, Any],
    *,
    expected_stock_code: str,
    expected_model: str,
    expected_provider: str,
) -> int:
    _expect(spec.get("condition_treatments"), CONDITIONS, "condition_treatments")
    _expect(
        spec.get("paired_condition_groups"),
        [["RN_COMM_OFF", "RN_COMM_ON"]],
        "paired_condition_groups",
    )
    _expect(
        spec.get("treatment_diff_allowlist"),
        ["community_mode"],
        "treatment_diff_allowlist",
    )
    # 한도의 정본은 ``config.BELIEF_LIMITS`` 하나다. 예전에는 이 검증기와
    # ``scripts/15_seal_study.py``가 각각 같은 값을 literal로 들고 있어서,
    # config만 바꾸면 봉인 spec은 옛 값을 유지한 채 런타임 프롬프트와
    # ``belief_dimensions``만 새 값으로 도는 조용한 불일치가 가능했다.
    _expect(
        spec.get("belief_limits"),
        dict(config.BELIEF_LIMITS),
        "belief_limits",
    )
    news = _mapping(spec.get("news_exposure_policy"), "news_exposure_policy")
    _expect(news.get("target_real_news_per_event"), 10, "news target")
    _expect(news.get("fake_news_per_event"), 0, "baseline fake-news count")
    _expect(
        news.get("shortage_policy"),
        "accepted_shortage_no_synthetic_or_duplicate_v1",
        "news shortage policy",
    )
    _expect(
        news.get("category_targets"),
        {"stock": 5, "sector": 3, "economy": 2},
        "news category targets",
    )
    _expect(
        news.get("cross_category_backfill"),
        False,
        "news cross-category backfill policy",
    )
    community = _mapping(spec.get("community_policy"), "community_policy")
    _expect(community.get("best_k"), 5, "community best_k")
    _expect(community.get("depth1_selective_read_cap"), 5, "community D1 cap")
    _expect(community.get("depth2_selective_read_cap"), 5, "community D2 cap")
    _expect(
        community.get("posting_permission_from_cohort_flag"),
        "can_post_community",
        "community posting permission source",
    )
    _expect(
        community.get("reading_permissions_from_cohort_depth_map"),
        True,
        "community reading permission source",
    )
    _expect(
        community.get("best_payload"),
        "title_plus_full_frozen_body",
        "community Best payload",
    )
    context = _mapping(spec.get("context_window_policy"), "context_window_policy")
    _expect(context.get("depth2_search_lookback"), 7, "news D2 lookback")
    _expect(
        context.get("depth2_search_lookback_unit"),
        "calendar_days",
        "news D2 lookback unit",
    )
    _expect(context.get("depth2_search_top_k"), 5, "news D2 top_k")
    _expect(
        context.get(
            "community_public_author_private_portfolio_or_trade_visibility"
        ),
        "d2_frozen_pm_portfolio_summary_plus_recent_3_filled_trades",
        "community D2 frozen author profile policy",
    )
    memory = _mapping(spec.get("memory_policy"), "memory_policy")
    _expect(
        memory.get("trade_belief_blocks"),
        "previous_ltb_plus_current_stb_separate_blocks",
        "memory decision blocks",
    )
    _expect(
        memory.get("ltb_update_timing"),
        "after-fill-before-commit",
        "LTB update timing",
    )
    _expect(
        list(_sequence(memory.get("outcome_horizons"), "memory outcome horizons")),
        OUTCOME_HORIZONS,
        "memory outcome horizons",
    )
    trade = _mapping(spec.get("trade_policy"), "trade_policy")
    _expect(trade.get("stock_code"), expected_stock_code, "trade stock code")
    _expect(trade.get("decision_space"), ["buy", "sell"], "trade decision space")
    _expect(trade.get("allow_hold"), False, "trade hold policy")
    _expect(trade.get("commission_rate"), 0.0, "commission rate")
    _expect(trade.get("sell_tax_rate"), 0.0, "sell tax rate")
    model = _mapping(spec.get("model_policy"), "model_policy")
    _expect(model.get("model"), expected_model, "model pin")
    _expect(model.get("provider"), expected_provider, "provider pin")
    _expect(
        model.get("reasoning"),
        {"effort": "none", "exclude": True},
        "reasoning-off policy",
    )
    _expect(model.get("allow_provider_fallbacks"), False, "provider fallback policy")
    _expect(model.get("require_parameters"), True, "provider parameter policy")
    _expect(model.get("reasoning_off_canary_required"), True, "canary requirement")
    concurrency = model.get("per_arm_max_concurrent_llm_calls")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise IntegratedStudySpecError(
            "model_policy.per_arm_max_concurrent_llm_calls must be positive"
        )
    # The retired ``physical_http_attempts_per_phase_attempt`` field was written
    # by the sealer but read by nobody, so it silently disagreed with the real
    # client retry budget.  Bind the replacement to config so a future drift
    # fails here instead of misdescribing the run.
    _expect(
        model.get("max_physical_http_attempts_per_logical_call"),
        int(config.OPENROUTER_MAX_RETRIES),
        "physical HTTP attempt budget",
    )
    if "physical_http_attempts_per_phase_attempt" in model:
        raise IntegratedStudySpecError(
            "model_policy.physical_http_attempts_per_phase_attempt is retired; "
            "re-seal with max_physical_http_attempts_per_logical_call"
        )
    return concurrency


def validate_integrated_study_profile(
    sealed_root: Path | str,
    *,
    agents: Sequence[Mapping[str, Any]],
    condition_id: str,
    prompt_dir: Path | str,
    expected_stock_code: str,
    expected_model: str,
    expected_provider: str,
) -> ResolvedStudyProfile:
    """Resolve the one real-news baseline profile before any run mutation."""

    root = Path(sealed_root)
    if not root.is_dir() or root.is_symlink():
        raise IntegratedStudySpecError(
            f"Sealed study root must be a real directory: {root}"
        )
    if condition_id not in CONDITIONS:
        raise IntegratedStudySpecError(f"Unsupported condition: {condition_id}")
    spec_path = root / "study_spec.json"
    cohort_path = root / "cohort.json"
    spec = _read_json_object(spec_path, "study spec")
    cohort = _read_json_object(cohort_path, "cohort registry")
    instrument = _mapping(spec.get("instrument"), "instrument")
    stock_code = str(instrument.get("stock_code") or "").strip()
    instrument_name = str(instrument.get("display_name") or "").strip()
    if not stock_code or not instrument_name:
        raise IntegratedStudySpecError(
            "instrument.stock_code and instrument.display_name must be non-empty"
        )
    persona_projection = _read_json_object(
        root / "persona_projection.json",
        "persona projection manifest",
    )
    _expect(spec.get("artifact_type"), "study_spec", "study_spec.artifact_type")
    required_agent_count = _positive_count(
        spec.get("required_agent_count"),
        "required_agent_count",
    )
    _expect(
        spec.get("cohort_registry_sha256"),
        canonical_sha256(cohort),
        "cohort registry hash",
    )
    assertions = _mapping(
        spec.get("cohort_assertions"),
        "cohort assertions",
    )
    depth_counts = _count_assertion(
        assertions.get("depth_counts"),
        label="cohort_assertions.depth_counts",
        expected_total=required_agent_count,
    )
    if not set(depth_counts).issubset({0, 1, 2}):
        raise IntegratedStudySpecError(
            "cohort depth assertions may contain only D0/D1/D2"
        )
    cash_counts = _count_assertion(
        assertions.get("initial_cash_counts"),
        label="cohort_assertions.initial_cash_counts",
        expected_total=required_agent_count,
    )
    post_permission_counts = _count_assertion(
        assertions.get("community_post_permission_counts"),
        label="cohort_assertions.community_post_permission_counts",
        expected_total=required_agent_count,
    )
    if not set(post_permission_counts).issubset({0, 1}):
        raise IntegratedStudySpecError(
            "community post permission assertions may contain only 0/1"
        )
    agent_ids = _validate_cohort(
        cohort,
        agents=agents,
        expected_count=required_agent_count,
        expected_depth_counts=depth_counts,
        expected_cash_counts=cash_counts,
        expected_post_permission_counts=post_permission_counts,
        instrument_name=instrument_name,
    )
    _expect(
        spec.get("persona_projection_manifest_sha256"),
        canonical_sha256(persona_projection),
        "persona projection manifest hash",
    )
    _expect(
        spec.get("persona_renderer_sha256"),
        persona_renderer_sha256(),
        "persona renderer hash",
    )
    _validate_persona_projection(
        persona_projection,
        agents=agents,
        expected_agent_ids=agent_ids,
        instrument_name=instrument_name,
    )
    calendar = _read_json_object(root / "calendar.json", "calendar registry")
    _expect(
        spec.get("calendar_event_registry_sha256"),
        canonical_sha256(calendar),
        "calendar registry hash",
    )
    prices = _read_json_object(root / "prices.json", "event price registry")
    schedule = FrozenEventSchedule.from_sealed_files(
        root / "calendar.json",
        root / "prices.json",
        expected_stock_code=None,
    )
    _expect(
        spec.get("event_price_registry_sha256"),
        canonical_sha256(prices),
        "event price registry hash",
    )
    _expect(schedule.stock_code, stock_code, "instrument/price stock code")
    if expected_stock_code is not None:
        _expect(stock_code, expected_stock_code, "expected stock code")
    stage_path = root / "stage_inputs.json"
    stage_inputs = _read_json_object(stage_path, "stage input registry")
    _expect(
        spec.get("stage_input_registry_file_sha256"),
        file_sha256(stage_path),
        "stage input file hash",
    )
    _expect(
        spec.get("stage_input_registry_canonical_sha256"),
        canonical_sha256(stage_inputs),
        "stage input canonical hash",
    )
    known_injection = _read_json_object(
        root / "known_injection.json",
        "known-injection registry",
    )
    _expect(
        spec.get("known_injection_registry_sha256"),
        canonical_sha256(known_injection),
        "known-injection registry hash",
    )
    review = _read_json_object(root / "review.json", "leakage review")
    _expect(
        spec.get("article_version_leakage_review_manifest_sha256"),
        canonical_sha256(review),
        "leakage-review hash",
    )
    news = _read_json_object(root / "news.json", "real-news bundle")
    _expect(news.get("stock_code"), stock_code, "news stock code")
    _expect(
        spec.get("real_news_bundle_manifest_sha256"),
        news.get("bundle_sha256"),
        "real-news bundle manifest hash",
    )
    prompt_sha = integrated_prompt_bundle_sha256(prompt_dir)
    _expect(spec.get("prompt_bundle_sha256"), prompt_sha, "prompt bundle hash")
    per_arm_concurrency = _validate_policy(
        spec,
        expected_stock_code=stock_code,
        expected_model=expected_model,
        expected_provider=expected_provider,
    )
    seed = spec.get("study_seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise IntegratedStudySpecError("study_seed must be an integer")
    burn_in_dates = tuple(
        str(value)
        for value in _sequence(spec.get("burn_in_date_ids"), "burn_in_date_ids")
    )
    if len(burn_in_dates) != 3:
        raise IntegratedStudySpecError(
            "The baseline must identify exactly three burn-in dates"
        )
    schedule_date_ids = tuple(
        str(event["date"])
        for event in schedule.events
        if str(event["subturn"]) == "am"
    )
    if burn_in_dates != schedule_date_ids[: len(burn_in_dates)]:
        raise IntegratedStudySpecError(
            "burn_in_date_ids must be the leading frozen trading dates"
        )
    return ResolvedStudyProfile(
        condition_id=condition_id,
        stock_code=stock_code,
        instrument_name=instrument_name,
        required_agent_count=required_agent_count,
        agent_ids=agent_ids,
        schedule_date_ids=schedule_date_ids,
        per_arm_concurrency=per_arm_concurrency,
        study_seed=seed,
        burn_in_dates=burn_in_dates,
        calendar_sha256=schedule.calendar_sha256,
        price_registry_sha256=schedule.prices_sha256,
        study_spec_sha256=file_sha256(spec_path),
        cohort_sha256=file_sha256(cohort_path),
        prompt_bundle_sha256=prompt_sha,
    )
