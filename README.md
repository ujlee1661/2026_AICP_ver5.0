# TwinMarket Korea

삼성전자 실제 가격을 외생적으로 고정하고, 개인투자자 에이전트의 거래와
커뮤니티 정보 노출 효과를 비교하는 시뮬레이션입니다.

## 현재 상태

- 기본 실행기는 `scripts/05_run_simulation.py -> twinmarket_kr/simulation.py`
  한 경로입니다.
- 실제뉴스 baseline은 Community OFF/ON 두 조건입니다.
- 현재 persona는 `data/sys_100_ko_ver5.db`의 100명이며 depth 분포는
  D0/D1/D2 = 30/55/15입니다.
- 커뮤니티 글쓰기는 depth만으로 허용하지 않습니다.
  D1/D2 70명 모두 게시 여부를 자유롭게 판단합니다(D0는 게시 불가).
- 커뮤니티 읽기는 종전대로 D1/D2가 수행하며 각 event 최대 5개입니다.
- 로컬 `.env`는 기본적으로 `TWINMARKET_OFFLINE_LLM=1`이므로 외부 API를
  호출하지 않습니다.
- 유료 live 실행은 명시적 승인, API key, reasoning-off canary, 전체 테스트와
  offline E2E 통과 전에는 실행하지 않습니다.

최근 로컬 검증에서는 커뮤니티·persona 관련 테스트 52개가 통과했습니다.
전체 테스트는 207개 통과, 기존 회귀 2개 실패 상태이므로 현재 상태를 live
본실험 GO로 해석하면 안 됩니다. 실패 항목은 LTB prompt 기대값 1건과 퇴역한
cohort builder의 `momentum_contrarian` 기대값 1건입니다.

## 실행 구조

```text
scripts/00_* ... scripts/04_*
  -> scripts/05_run_simulation.py
     -> twinmarket_kr/simulation.py
        -> core / agents / community
           -> canonical DB + event journal
              -> validator + report
```

event 순서는 다음과 같습니다.

```text
도래한 과거 성과와 AM 커뮤니티 노출 확정
  -> 현재 STB
  -> 이전 LTB + 현재 STB로 analysis/decision
  -> 잔고·보유량 제약을 반영한 실제 fill
  -> post-fill LTB
  -> PM community
```

`decision`은 거래 의도이고 `fill`은 실제 체결입니다. 새 실험은 별도 RN
runtime이나 과거 compatibility runner를 사용하지 않습니다.

## 정본 입력

현재 기본 baseline profile은 `preparation/rn_ab_sealed_v1/`(…‑2026-05-04,
45거래일)입니다. 2026-05-29까지 실행할 때는 기존 90개 event를
그대로 보존하고 34개 event만 추가한
`preparation/rn_ab_sealed_to_20260529_v1/` profile을 명시합니다. 종료일은
일요일인 5월 31일이 아니라 마지막 거래일인 5월 29일입니다.

| 파일 | 역할 |
| --- | --- |
| `study_spec.json` | 실험 정책, 입력·prompt hash, 모델·거래 정책 |
| `cohort.json` | 고정 100명 cohort와 depth |
| `news.json` | event별 실제뉴스 제목·요약과 shortage 기록 |
| `calendar.json` | 거래일과 AM/PM event |
| `prices.json` | event별 체결 가격 |
| `stage_inputs.json` | 실행 단계별 봉인 입력 |

뉴스 정본 hash는 다음과 같습니다.

- bundle:
  `a6fb61900c27071b2a79781478592d99d914482fbba0f4ecaafa73edcb8ab707`
- file:
  `cf3561dbe9f9fa360b716970e8352022fa8cbcd4d824c1ef249880d1ee7e5f55`

에이전트는 기사 본문을 받지 않습니다. D0는 제목만 보고, D1은 event 기사
요약까지, D2는 여기에 최근 7일 cutoff-safe 검색 요약 최대 5건을 추가로
받습니다. 과거 `outputs/` 결과와 `archive/legacy_inputs/` 자료는 새 실행의
입력으로 사용하지 않습니다.

## 로컬 환경 준비

Python 3.12를 사용합니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

이 작업 폴더에는 다음 gitignored 로컬 파일이 준비되어 있습니다.

- `.env`: offline stub 기본값과 비어 있는 `OPENROUTER_API_KEY`
- `.venv/`: Python 3.12 가상환경
- `outputs/experiment_base_sim.db`: 새 run이 복사해 쓰는 turn-zero DB
- `outputs/experiment_base_sim.report.json`: base DB 검증 기록

다른 컴퓨터에서 clone했다면 `.env`를 직접 만들고 base DB를 생성합니다.

```bash
python scripts/02_prepare_news.py
python scripts/03_load_stock_data.py
python scripts/04_build_experiment_base.py --force
```

`03`은 기본적으로 읽기 전용 검증이며, source DB를 갱신하려면 문서화된
`--write` 인수를 모두 명시해야 합니다.

## 먼저 실행할 무과금 smoke test

현재 cohort에서 D2를 포함하려면 최소 22명을 사용합니다.

```bash
source .venv/bin/activate
python scripts/05_run_simulation.py \
  --max-agents 23 \
  --max-days 1 \
  --community-mode on \
  --run-dir outputs/logs/local_offline_smoke
```

같은 run을 중단 지점부터 이어갈 때는 다른 조건을 바꾸지 않고
`--resume`만 추가합니다.

```bash
python scripts/05_run_simulation.py \
  --max-agents 23 \
  --max-days 1 \
  --community-mode on \
  --run-dir outputs/logs/local_offline_smoke \
  --resume
```

## OFF/ON paired baseline

`.env`의 offline 설정을 유지한 아래 명령은 유료 API 없이 실행 구조와
artifact를 검증합니다.

```bash
python scripts/08_run_six_conditions.py \
  --conditions RN_COMM_OFF RN_COMM_ON \
  --output-root outputs/logs/rn_ab_local_offline
```

실제 유료 실험은 다음 조건을 모두 만족해야 합니다.

1. 전체 테스트와 OFF/ON offline E2E가 통과한다.
2. `.env`에서 `TWINMARKET_OFFLINE_LLM=0`으로 바꾸고
   `OPENROUTER_API_KEY`를 설정한다.
3. 현재 코드·prompt·persona에 대응하는 reasoning-off canary audit를 만든다.
4. 실행 명령에 `--allow-paid-api`와
   `--reasoning-off-canary-audit <audit.jsonl>`를 함께 지정한다.

`--allow-paid-api`만으로 canary 검증을 우회할 수 없습니다. 유료 canary와
본실험은 비용이 발생하므로 명시적 승인 없이 실행하지 않습니다. 상세 절차와
중단·재개 기준은 [RUNBOOK_AND_PREFLIGHT.md](RUNBOOK_AND_PREFLIGHT.md)를
따릅니다.

## 결과 검증과 보고서

run 완료 후 파생물은 run 디렉터리 밖에 생성합니다.

```bash
python scripts/99_validate.py \
  --run-dir <run-dir> \
  --output <pair-root>/derived/<condition>/run_validation.json

python validation/validate_trading_direction.py \
  --run-dir <run-dir> \
  --output-dir <pair-root>/derived/<condition>/direction_validation \
  --skip-initial-days 3

python scripts/generate_run_report_pdf.py \
  --run-dir <run-dir> \
  --output <pair-root>/derived/<condition>/run_report.pdf

python scripts/generate_community_report_pdf.py \
  --run-dir <community-on-run-dir> \
  --output <pair-root>/derived/RN_COMM_ON/community_report.pdf
```

주요 결과는 다음 순서로 확인합니다.

| 확인 목적 | artifact |
| --- | --- |
| 완료 상태 | `run_complete.json`, `run_metadata.json` |
| 재개 상태 | `.runtime/checkpoint.json` |
| 실제 체결 | `exchange_fills.csv` |
| 거래 의도 | `submitted_orders.csv` |
| STB/LTB 계보 | `memory_lineage.jsonl`, `agent_turns.jsonl` |
| 게시글 | `community_posts.csv` |
| title-only/full-body 노출 | `community_interactions.csv` |
| Best와 자기 글 제외 | `community_best_posts.csv` |
| canonical DB | `.runtime/committed.db` |

## 보존된 과거 로그

로컬 실행 결과의 우선순위와 보조 run 격리 위치는
[`outputs/PRIMARY_RUNS.md`](outputs/PRIMARY_RUNS.md)에 정리되어 있습니다.
현재 주요 결과는 `outputs/experiments/RN_COMM_ON_20260918/`과
`outputs/logs/rn_ab_ver6_45day_20260826/` 두 경로입니다.

`outputs/logs/`에는 현재 아래 폴더만 보존합니다.

```text
outputs/logs/rn_ab_ver6_45day_20260826/
  RN_COMM_OFF/
  RN_COMM_ON/
  RN_COMM_OFF.console.log
  RN_COMM_ON.console.log
  matrix_manifest.json
```

잘못 생성된 `RN_COMM_ON.corrupted_20260826_1314`와 대응 console log를 포함한
나머지 오래된 로그는 삭제했습니다. OFF/ON 각각에는 완료 marker가 있지만 상위
`matrix_manifest.json`은 `running` 상태로 남아 있으므로, 이 폴더는 보존된
과거 결과일 뿐 새 실험 입력이나 현재 GO 판정으로 사용하지 않습니다.

## 커뮤니티 핵심 정책

- 게시: D1/D2 모두 자유롭게 판단, 게시 강제 없음, agent-PM당 최대 1개
- 읽기: D0 0개, D1 최대 5개, D2 최대 5개
- 선택: persona를 받은 LLM이 제목 후보 화면에서 선택하며 빈 선택도 허용
- 본문: 최대 500자, 501자는 거부하며 자동으로 자르지 않음
- 노출: 미선택 후보는 `title_only`, 실제 읽은 글만 `full_body`
- 반응: `like`, `unlike`, `none`
- Best: `like_count - unlike_count`, 최대 5개
- Best 전달: 작성자 자기 글 제외, 6위 글로 보충하지 않음
- D2 저자 정보: 후보 보드 시점의 포트폴리오와 최근 거래 snapshot 사용
- 작성자 평판 badge: 사용하지 않음

## 문서

| 문서 | 역할 |
| --- | --- |
| [README.md](README.md) | 설치, 빠른 시작, 현재 상태와 결과 위치 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 엔진, STB/LTB, 거래, 커뮤니티, artifact 구조 |
| [EXPERIMENT_DESIGN.md](EXPERIMENT_DESIGN.md) | 연구 질문, 조건, 정책과 분석 계약 |
| [RUNBOOK_AND_PREFLIGHT.md](RUNBOOK_AND_PREFLIGHT.md) | preflight, 실행, resume, 검증, 보고 |
| [advisor_plan.md](advisor_plan.md) | 5월 4일 warm fork 기반 개인화 Advisor 실험 계약 |

`AGENTS.md`는 작업 지침입니다. 그 밖의 Markdown과 archive 자료는 역사·결과
sidecar일 수 있으며 현재 실행 명령의 정본으로 사용하지 않습니다.
