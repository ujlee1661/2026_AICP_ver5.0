from __future__ import annotations

import csv
import hashlib
import inspect
import json
import random
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import config
from twinmarket_kr.db.connection import connect, init_agents_db
from twinmarket_kr.persona.segments import get_behavioral_profile, segment_key


VALUE_MAP = {
    "高": "high",
    "中": "medium",
    "低": "low",
    "技术面": "technical",
    "基本面": "value",
    "普通股民": "ordinary",
    "小博主": "small_influencer",
    "大V": "big_influencer",
}

BEHAVIORAL_COLUMNS = [
    "user_type",
    "bh_disposition_effect_category",
    "bh_lottery_preference_category",
    "bh_total_return_category",
    "bh_underdiversification_category",
    "strategy",
    "trad_pro",
    "fol_ind",
    "sys_prompt",
    "prompt",
    "self_description",
]

LOCATION_WEIGHTS = {
    "경기": 29,
    "서울": 26,
    "부산": 6,
    "인천": 5,
    "경남": 5,
    "대구": 4,
    "경북": 4,
    "충남": 3,
    "대전": 3,
    "광주": 3,
    "전북": 2,
    "충북": 2,
    "울산": 2,
    "전남": 2,
    "강원": 2,
    "제주": 1,
    "세종": 1,
}

PERSONA_RENDERER_ID = (
    # Distribution-matched TwinMarket cohort plus the momentum/contrarian entry-timing
    # axis introduced by sys_100_ko_ver5.db. Persona text is derived only from the
    # behavioural axes + momentum_contrarian + news_depth + initial cash. Demographic
    # slot fields (gender/age/location) are not rendered. New ID so a cohort sealed
    # under the 7-axis renderer cannot silently validate against this one.
    "twinmarket-dist-matched-8axis-momentum-ko-v1"
)
STRUCTURED_PERSONA_FIELDS = (
    "agent_id",
    "source_user_id",
    "user_type",
    "gender",
    "age",
    "age_group",
    "location",
    "bh_disposition_effect_category",
    "bh_lottery_preference_category",
    "bh_total_return_category",
    "bh_underdiversification_category",
    "strategy",
    "trad_pro",
    "fol_ind",
    "ini_cash",
    "news_depth",
    "segment_key",
    "match_score",
    "momentum_contrarian",
)


def _map_value(value: object) -> object:
    if value is None:
        return value
    if isinstance(value, str):
        return VALUE_MAP.get(value.strip(), value.strip())
    return value


def _normalize_agent(row: dict) -> dict:
    agent = {"user_id": str(row["user_id"])}
    for col in BEHAVIORAL_COLUMNS:
        agent[col] = _map_value(row.get(col))
    agent["trad_pro"] = int(agent.get("trad_pro") or 0)
    agent["fol_ind"] = json.dumps(["전기전자", "반도체"], ensure_ascii=False)
    return agent


def load_pool(source: Path | None = None) -> list[dict]:
    source = source or (config.SYS_1000_DB if config.SYS_1000_DB.exists() else config.SYS_1000_CSV)
    if not source.exists():
        raise FileNotFoundError(f"sys_1000 input not found: {source}")

    if source.suffix == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as f:
            return [_normalize_agent(row) for row in csv.DictReader(f)]

    conn = sqlite3.connect(source)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM Profiles").fetchall()
        return [_normalize_agent(dict(row)) for row in rows]
    finally:
        conn.close()


def score_agent(agent: dict, preferred: dict[str, list[str]]) -> int:
    score = 0
    for col, priority_values in preferred.items():
        agent_val = agent.get(col)
        for rank, val in enumerate(priority_values):
            if agent_val == val:
                score += max(1, 3 - rank)
                break
    return score + 1


def assign_location(rng: random.Random) -> str:
    return rng.choices(
        list(LOCATION_WEIGHTS.keys()),
        weights=list(LOCATION_WEIGHTS.values()),
        k=1,
    )[0]


def assign_news_depth(index: int, total: int) -> int:
    """Deprecated. Kept only so old callers fail loudly instead of silently
    reintroducing the slot-index confound.

    The previous implementation gave depth 2 to the LAST ``round(total * ratio)``
    slot indices. Because the slot table is ordered by age, that put every deep
    reader in the oldest cohort -- news depth was confounded with age. Depth is now
    drawn at random by ``assign_news_depths``.
    """

    raise NotImplementedError(
        "assign_news_depth is retired; use assign_news_depths(agents, rng)"
    )


def assign_news_depths(agents: list[dict], rng: random.Random) -> None:
    """Assign depth 0/1/2 at random, hitting the design counts exactly.

    Random rather than index-based so depth stays independent of every other
    persona axis; the counts are exact so the sealed design split is preserved.
    """

    total = len(agents)
    n_depth2 = round(total * config.NEWS_DEPTH2_RATIO)
    n_depth0 = min(config.NEWS_DEPTH0_COUNT, total - n_depth2)
    pool = [2] * n_depth2 + [0] * n_depth0 + [1] * (total - n_depth2 - n_depth0)
    rng.shuffle(pool)
    for agent, depth in zip(agents, pool):
        agent["news_depth"] = depth


def assign_depth0_agents(agents: list[dict], rng: random.Random) -> None:
    """Deprecated. ``assign_news_depths`` now draws 0/1/2 in one shot."""

    raise NotImplementedError(
        "assign_depth0_agents is retired; use assign_news_depths(agents, rng)"
    )


def generate_persona_prompt(
    agent: dict,
    *,
    instrument_name: str = "삼성전자",
) -> str:
    """Render the canonical persona projection from structured DB fields.

    The stored ``persona_prompt`` blob is intentionally ignored.  In the
    source cohort it can disagree with ``news_depth``; rebuilding the prompt
    here keeps the 30/55/15 structured depth map as the sole authority.
    """

    user_type_desc = {
        "ordinary": "당신은 팔로워가 많지 않은 평범한 개인 투자자다.",
        "small_influencer": "당신은 어느 정도 팬을 보유한 소규모 인플루언서다.",
        "big_influencer": "당신은 널리 주목받는 대형 인플루언서다.",
    }
    disposition_desc = {
        "high": (
            "당신은 보유 자산이 10% 넘게 오르면 곧바로 팔아 이익을 확정한다. "
            "반대로 보유 자산이 하락하면 한번 걸어보는 쪽을 택해, 스스로 유망하다고 "
            "본 자산에 대해서는 포지션 위험을 무시하고 떨어질수록 더 사들인다."
        ),
        "medium": (
            "당신은 보유 자산이 10% 이상 오르면 매도를 고려하고, 20% 오르면 전량 "
            "정리해 이익을 확정하는 편이다. 보유 자산이 하락하면 즉시 분석해서 한번 "
            "걸어보며 물타기를 계속할지 아니면 손절매할지를 판단한다."
        ),
        "low": (
            "당신은 보유 자산이 20% 오른 뒤에는 이익을 확정할 매도 시점을 제때 "
            "살핀다. 보유 자산이 10% 넘게 하락하면 즉시 이성적으로 분석해 손절매할지를 "
            "판단한다."
        ),
    }
    lottery_desc = {
        "high": (
            "당신은 '복권형' 자산에 투자하기를 특히 좋아해서, 위험이 크지만 잠재 "
            "수익이 막대한 자산, 그중에서도 최근 급등한 자산을 고르는 경향이 있으며 "
            "하룻밤에 큰돈을 벌기를 기대한다."
        ),
        "medium": (
            "당신은 최근 급등한 '복권형' 자산에 이따금 자금 일부를 넣어, "
            "포트폴리오에 약간의 자극과 잠재 수익을 더하려 한다."
        ),
        "low": (
            "시장에는 최근 급등한 '복권형' 자산이 이따금 등장하지만, 그런 자산은 "
            "당신의 투자 판단에 거의 영향을 주지 않는다."
        ),
    }
    return_desc = {
        "high": "당신의 포트폴리오는 과거에 뛰어난 성과를 냈고, 총투자수익률이 시장 평균을 웃돈다.",
        "medium": "당신의 포트폴리오는 총투자수익률이 중간 수준으로, 시장에서 중위권에 있다.",
        "low": "당신의 포트폴리오는 과거 총투자수익률이 다소 낮아 시장 평균에 이르지 못했다.",
    }
    underdiv_desc = {
        "medium": "당신은 여러 종목에 투자를 분산해 위험을 낮추는 편이다.",
        "low": (
            "당신은 투자를 집중해 유망하다고 보는 특정 종목에 크게 베팅하는 편이며, "
            "그래야만 두둑한 수익을 얻을 수 있다고 믿는다."
        ),
    }
    strategy_desc = {
        "technical": (
            "기술적 분석 투자자로서 당신은 단기 시장 변동에서 이익을 얻는 데 "
            "집중한다. 당신의 목표는 기술적 지표 분석과 시장 추세 예측을 통해 빠르고 "
            "효율적인 매매 판단을 내리는 것이다."
        ),
        "value": (
            "기본적 분석 투자자로서 당신은 자산의 내재가치와 성장성을 파악하는 데 "
            "집중한다. 당신의 목표는 종목이 저평가되었을 때 매수하고 고평가되었을 때 "
            "매도하는 것이다. 당신은 기술적 지표에는 거의 주의를 기울이지 않는다."
        ),
    }
    # Entry-timing axis. Unlike the behavioural axes above, this one is NOT derived
    # from TwinMarket -- it is a treatment variable assigned independently per agent.
    # The strings below are byte-identical to the sentences stored in the cohort DB.
    # Both are scoped to *new buying* so that value x momentum does not read as
    # "it got expensive, therefore buy".
    momentum_desc = {
        "momentum": (
            "당신은 신규 매수를 결정할 때 최근 가격이 꾸준히 상승한 국면을 "
            "선호합니다. 상승 추세가 단기간 이어질 가능성이 높다고 보아 그런 "
            "국면에서 신규 매수하거나 비중을 확대합니다. 반대로 최근 가격이 "
            "하락하는 국면에서는 신규 매수를 피합니다."
        ),
        "contrarian": (
            "당신은 신규 매수를 결정할 때 최근 가격이 하락한 국면을 선호합니다. "
            "가격 하락이 과도해 향후 반등할 가능성이 있다고 판단되면 신규 "
            "매수합니다. 반대로 최근 가격이 크게 상승한 국면은 과열되었다고 보아 "
            "추격 매수를 피합니다."
        ),
        "neutral": (
            "당신은 신규 매수를 결정할 때 최근 가격의 상승·하락 추세 자체보다 "
            "기업의 가치와 이용 가능한 정보가 투자 판단을 뒷받침하는지를 우선 "
            "검토합니다. 최근 가격 흐름만을 이유로 신규 매수나 추격 매수를 "
            "결정하지 않습니다."
        ),
    }
    depth_desc = {
        0: (
            "뉴스는 각 AM/PM 거래 판단 시점에 제공된 기본 뉴스의 기사 "
            "제목(헤드라인)을 모두 확인하고, 기사 요약은 확인하지 않는 "
            "헤드라인 스캔형입니다."
        ),
        1: (
            "뉴스는 각 AM/PM 거래 판단 시점에 제공된 기본 뉴스의 기사 "
            "제목(헤드라인)과 그에 연결된 기사 요약을 모두 확인하는 "
            "요약 전독형입니다."
        ),
        2: (
            "뉴스는 각 AM/PM 거래 판단 시점에 제공된 기본 뉴스의 기사 "
            "제목(헤드라인)과 기사 요약을 모두 확인한 뒤, 필요하면 최근 "
            "7일 범위에서 추가 뉴스 최대 5건을 탐색하는 심층 탐색형입니다."
        ),
    }
    # gender/age/location are deliberately absent: the cohort is matched to
    # TwinMarket's behavioural distribution, not to a demographic slot table, so
    # those columns carry a sentinel and must never reach the prompt.
    required_codes = {
        "user_type": user_type_desc,
        "bh_disposition_effect_category": disposition_desc,
        "bh_lottery_preference_category": lottery_desc,
        "bh_total_return_category": return_desc,
        "bh_underdiversification_category": underdiv_desc,
        "strategy": strategy_desc,
        "momentum_contrarian": momentum_desc,
    }
    for field, labels in required_codes.items():
        if agent.get(field) not in labels:
            raise ValueError(
                f"Unsupported structured persona value {field}={agent.get(field)!r}"
            )
    depth = int(agent["news_depth"])
    if depth not in depth_desc:
        raise ValueError("news_depth must be one of 0, 1, 2")
    initial_cash = int(agent["ini_cash"])
    if initial_cash < 1:
        raise ValueError("ini_cash must be positive")
    instrument = str(instrument_name).strip()
    if not instrument:
        raise ValueError("instrument_name must be non-empty")
    prompt = "\n".join(
        (
            f"당신은 한국의 {instrument} 개인투자자입니다.",
            user_type_desc[agent["user_type"]],
            disposition_desc[agent["bh_disposition_effect_category"]],
            lottery_desc[agent["bh_lottery_preference_category"]],
            return_desc[agent["bh_total_return_category"]],
            underdiv_desc[agent["bh_underdiversification_category"]],
            strategy_desc[agent["strategy"]],
            momentum_desc[agent["momentum_contrarian"]],
            depth_desc[depth],
            (
                f"이번 실험에서는 {instrument} 단일 자산만 거래하며, "
                f"초기에는 주식 없이 현금 {initial_cash:,}원만 보유한 "
                "상태로 시장에 진입합니다."
            ),
        )
    )
    return (
        unicodedata.normalize("NFC", prompt)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .rstrip("\n")
        + "\n"
    )


def persona_renderer_sha256() -> str:
    """Bind a sealed cohort to the common structured-field renderer.

    The digest is intentionally based on the implementation and a stable
    renderer ID, not on a local file path or on the stale ``persona_prompt``
    blobs stored in the source database.
    """

    payload = json.dumps(
        {
            "renderer_id": PERSONA_RENDERER_ID,
            "render_source": inspect.getsource(generate_persona_prompt),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def structured_persona_sha256(agent: dict) -> str:
    """Hash every authoritative structured persona field.

    ``persona_prompt`` is deliberately excluded because it is the stale
    projection being repaired. Matching metadata stays pinned even though it
    is not shown to the model.
    """

    missing = [
        field
        for field in STRUCTURED_PERSONA_FIELDS
        if field not in agent
    ]
    if missing:
        raise ValueError(
            "structured persona fields are missing: " + ",".join(missing)
        )
    integer_fields = {
        "age",
        "trad_pro",
        "ini_cash",
        "news_depth",
        "match_score",
    }
    payload: dict[str, object] = {}
    for field in STRUCTURED_PERSONA_FIELDS:
        value = agent[field]
        if field in integer_fields:
            payload[field] = int(value)
        elif field == "fol_ind":
            try:
                industries = json.loads(str(value))
            except json.JSONDecodeError as exc:
                raise ValueError("fol_ind must be a JSON array") from exc
            if not isinstance(industries, list):
                raise ValueError("fol_ind must be a JSON array")
            payload[field] = [
                unicodedata.normalize("NFC", str(item)).strip()
                for item in industries
            ]
        else:
            payload[field] = unicodedata.normalize(
                "NFC",
                str(value),
            ).strip()
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# This selector is used only by the explicit ``--create-new-cohort`` path.  The
# frozen baseline is never rebuilt through it.  New cohorts must still receive all
# current structured axes and the current 73/27 cash split before validation.
def match_agents(pool: list[dict], slots: list[dict], seed: int = config.RANDOM_SEED) -> list[dict]:
    if len(pool) < len(slots):
        raise ValueError(f"pool has {len(pool)} agents but {len(slots)} slots are required")

    rng = random.Random(seed)
    used_source_ids: set[str] = set()
    selected: list[dict] = []

    for index, slot in enumerate(slots):
        preferred = get_behavioral_profile(slot["age_group"], slot["gender"], slot["ini_cash"])
        candidates = [agent for agent in pool if agent["user_id"] not in used_source_ids]
        weights = [score_agent(agent, preferred) for agent in candidates]
        chosen = dict(rng.choices(candidates, weights=weights, k=1)[0])
        chosen["source_user_id"] = chosen.pop("user_id")
        chosen["agent_id"] = slot["agent_id"]
        chosen["gender"] = slot["gender"]
        chosen["age"] = slot["age"]
        chosen["age_group"] = slot["age_group"]
        chosen["ini_cash"] = slot["ini_cash"]
        chosen["location"] = assign_location(rng)
        chosen["news_depth"] = 1  # replaced below by assign_news_depths
        chosen["momentum_contrarian"] = "neutral"
        chosen["segment_key"] = segment_key(slot["age_group"], slot["gender"], slot["ini_cash"])
        chosen["match_score"] = score_agent(chosen, preferred)
        chosen["persona_prompt"] = generate_persona_prompt(chosen)
        used_source_ids.add(chosen["source_user_id"])
        selected.append(chosen)

    cash_assignments = (
        [config.INI_CASH_SMALL] * 73
        + [config.INI_CASH_LARGE] * (len(selected) - 73)
    )
    rng.shuffle(cash_assignments)
    for agent, initial_cash in zip(selected, cash_assignments):
        agent["ini_cash"] = initial_cash

    assign_news_depths(selected, rng)
    for agent in selected:
        agent["persona_prompt"] = generate_persona_prompt(agent)

    return selected


def save_sys_100(agents: Iterable[dict], output_db: Path = config.SYS_100_DB) -> None:
    init_agents_db(output_db)
    columns = [
        "agent_id",
        "source_user_id",
        "user_type",
        "gender",
        "age",
        "age_group",
        "location",
        "bh_disposition_effect_category",
        "bh_lottery_preference_category",
        "bh_total_return_category",
        "bh_underdiversification_category",
        "strategy",
        "trad_pro",
        "fol_ind",
        "ini_cash",
        "news_depth",
        "segment_key",
        "match_score",
        "persona_prompt",
        "momentum_contrarian",
    ]
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO agents ({', '.join(columns)}) VALUES ({placeholders})"
    with connect(output_db) as conn:
        conn.executemany(
            sql,
            [
                [
                    agent[col]
                    for col in columns
                ]
                for agent in agents
            ],
        )
        conn.commit()


# Cohort design split. Cash follows TwinMarket's own 73/27 wealth split (the amounts
# are ver3.0's 1억/10억); depth is the experiment's treatment split.
EXPECTED_CASH_COUNTS = {config.INI_CASH_SMALL: 73, config.INI_CASH_LARGE: 27}
EXPECTED_DEPTH_COUNTS = {0: 30, 1: 55, 2: 15}


def verify_distribution(agents: list[dict]) -> dict:
    cash = Counter(agent["ini_cash"] for agent in agents)
    depth = Counter(agent["news_depth"] for agent in agents)
    post_permissions = Counter(
        int(int(agent["news_depth"]) in {1, 2})
        for agent in agents
    )
    prompt_errors = [
        agent["agent_id"]
        for agent in agents
        if not agent.get("persona_prompt")
        or agent["persona_prompt"] != generate_persona_prompt(agent)
    ]

    segment_scores: dict[str, list[int]] = defaultdict(list)
    for agent in agents:
        segment_scores[agent["segment_key"]].append(agent["match_score"])

    # gender/age_group are no longer part of the cohort design and are not checked.
    return {
        "count": len(agents),
        "cash": dict(cash),
        "news_depth": dict(depth),
        "community_post_permission": dict(post_permissions),
        "distribution_pass": dict(cash) == EXPECTED_CASH_COUNTS
        and dict(depth) == EXPECTED_DEPTH_COUNTS
        and not prompt_errors,
        "prompt_errors": prompt_errors,
        "invalid_community_post_permission_agents": [],
        "segment_avg_scores": {
            key: round(sum(values) / len(values), 3) for key, values in sorted(segment_scores.items())
        },
    }
