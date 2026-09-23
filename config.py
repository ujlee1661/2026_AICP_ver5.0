from __future__ import annotations

import os
from pathlib import Path

from twinmarket_kr.rn_model_pin import RN_PAPER_MODEL

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs) -> bool:  # type: ignore[no-redef]
        return False


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "outputs"
PREPARATION_DIR = PROJECT_ROOT / "preparation"
PROMPT_DIR = PROJECT_ROOT / "prompts"
LOG_DIR = OUTPUT_DIR / "logs"

SYS_1000_DB = DATA_DIR / "sys_1000.db"
SYS_1000_CSV = DATA_DIR / "sys_1000.csv"
FIXED_SLOTS_CSV = DATA_DIR / "fixed_slots.csv"
STOCK_DATA_CSV = DATA_DIR / "stock_data.csv"
TRADING_DAYS_CSV = DATA_DIR / "trading_days.csv"
# Persona cohort DB. The default is the current frozen baseline; set
# TWINMARKET_SYS_100_DB to run a different sealed cohort without editing this file
# (05_run_simulation.py and experiment_runtime.py both read config.SYS_100_DB
# directly, so an env override is the only place that reaches both).
SYS_100_DB = Path(
    os.getenv("TWINMARKET_SYS_100_DB", str(DATA_DIR / "sys_100_ko_ver5.db"))
)
SIM_DB = OUTPUT_DIR / "sim.db"
EXPERIMENT_BASE_DB = OUTPUT_DIR / "experiment_base_to_20260722.db"

# 현재 실제뉴스 baseline의 유일한 production 입력이다. ``outputs/*_split``과
# legacy selected-news CSV는 이 봉인 파일을 만드는 source/history artifact일
# 뿐이며, 시뮬레이션이 런타임에 다시 표본을 뽑는 입력으로 사용하지 않는다.
# Sealed profile directory. Default is the approved 2026-02-27..2026-07-22
# experiment profile; set
# TWINMARKET_SEALED_PROFILE to point the regression suite at another sealed profile
# without editing checked-in tests.
SEALED_PROFILE_ROOT = PREPARATION_DIR / os.getenv(
    "TWINMARKET_SEALED_PROFILE", "rn_ab_sealed_to_20260722_v1"
)
SEALED_REAL_NEWS_BUNDLE = SEALED_PROFILE_ROOT / "news.json"
SEALED_EVENT_CALENDAR = SEALED_PROFILE_ROOT / "calendar.json"
SEALED_EVENT_PRICES = SEALED_PROFILE_ROOT / "prices.json"

STOCK_CODE = "005930"
COUNTERSIDE_USER_ID = "COUNTERSIDE"

# ----- 수수료 기준과 레거시 상수 -----
# COMMISSION_RATE는 05 실행기의 immutable signature에 기록된다. StudySpec,
# exchange/fill 경계와 DB CHECK도 모두 0만 허용하므로 값을 바꾸면 preflight 또는
# runtime 검증이 실패한다. 아래 나머지 세 상수만 현재 실행 경로에서 사용하지
# 않는 레거시 문서 호환 이름이다.
#
# N_WARMUP: RN burn-in(3거래일)은 봉인된 registry/study_spec 에서 온다.
# N_TRANSITION, CIRCUIT_BREAKER: 현재 어느 경로에서도 쓰지 않는다.
COMMISSION_RATE = 0.0
CIRCUIT_BREAKER = 0.30
N_WARMUP = 3
N_TRANSITION = 4

INI_CASH_SMALL = 100_000_000
INI_CASH_LARGE = 1_000_000_000
MIN_ORDER_UNIT = 1
RANDOM_SEED = 2
# 100명 기준 depth 분포는 D2 15 / D1 55 / D0 30 이다.
# 추적되는 structured cohort ``outputs/sys_100.db``와 봉인된 cohort/projection이
# 이 분포의 정본이며, 과거 별도 persona snapshot은 실행 입력으로 쓰지 않는다.
NEWS_DEPTH2_RATIO = 0.15
NEWS_DEPTH0_COUNT = 30
MAX_SINGLE_TRADE_CASH_RATIO = 0.50
MARKET_CLOSE_TIME = "15:30"
ORDER_CUTOFF_TIME = "15:30"

# 100자 한도는 라이브에서 재시도의 대부분을 만들었다. 1거래일 유료 실행에서
# 검증 재시도 33건 중 32건이 길이 초과였고 실제 초과 폭은 101~117자였다.
# 전 차원을 150으로 통일해 그 재시도를 제거한다. 이 값은 봉인 StudySpec의
# ``belief_limits``와 exact match여야 하므로 변경 시 재봉인이 필요하다.
BELIEF_LIMITS = {
    "dim_1": 150,
    "dim_2": 150,
    "dim_3": 150,
    "dim_4": 150,
    "dim_5": 150,
    "dim_6": 150,
}

# 미사용(dead). 실험 구간은 CLI 인자(--start-date/--end-date) 또는 RN 봉인
# calendar registry 에서 결정된다. 여기에 날짜를 적어도 반영되지 않는다.
EXPERIMENT_START_DATE = ""
EXPERIMENT_END_DATE = ""

load_dotenv(PROJECT_ROOT / ".env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# RN paper execution is deliberately pinned to this one model.  The strict RN
# call path rejects any other model before constructing an HTTP request.
PAPER_OPENROUTER_MODEL = RN_PAPER_MODEL
PAPER_REASONING_DISABLED_MODEL = PAPER_OPENROUTER_MODEL
PAPER_OPENROUTER_PROVIDER = "alibaba"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", PAPER_OPENROUTER_MODEL)
OPENROUTER_EMBED_MODEL = os.getenv("OPENROUTER_EMBED_MODEL", "")
OPENROUTER_MAX_RETRIES = int(os.getenv("OPENROUTER_MAX_RETRIES", "6"))
OPENROUTER_RETRY_MAX_DELAY = float(os.getenv("OPENROUTER_RETRY_MAX_DELAY", "30"))
OPENROUTER_GLOBAL_CONCURRENCY = int(os.getenv("OPENROUTER_GLOBAL_CONCURRENCY", "16"))
OPENROUTER_REQUIRE_PARAMETERS = os.getenv("OPENROUTER_REQUIRE_PARAMETERS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
OPENROUTER_ALLOW_FALLBACKS = os.getenv("OPENROUTER_ALLOW_FALLBACKS", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}
OPENROUTER_PROVIDER_ORDER = [
    item.strip()
    for item in os.getenv(
        "OPENROUTER_PROVIDER_ORDER",
        PAPER_OPENROUTER_PROVIDER,
    ).split(",")
    if item.strip()
]
OPENROUTER_SLOT_DIR = OUTPUT_DIR / ".openrouter_slots"
OPENROUTER_AUDIT_LOG = Path(
    os.getenv("OPENROUTER_AUDIT_LOG", str(OUTPUT_DIR / "openrouter_calls.jsonl"))
)
SIMULATION_CONCURRENCY = int(os.getenv("SIMULATION_CONCURRENCY", "30"))


# ===== Community Settings =====
ENABLE_COMMUNITY: bool = True
ENABLE_COMMUNITY_POSTING: bool = True
ENABLE_COMMUNITY_READING: bool = True

COMMUNITY_DEPTH1_READ_LIMIT: int = 5
COMMUNITY_DEPTH2_READ_LIMIT: int = 5
COMMUNITY_BEST_POST_COUNT: int = 5

# If no separate community model is configured, keep every LLM call on the
# primary model rather than silently falling back to a billable OpenAI model.
OPENROUTER_COMMUNITY_MODEL: str = os.getenv("OPENROUTER_COMMUNITY_MODEL", OPENROUTER_MODEL)


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
