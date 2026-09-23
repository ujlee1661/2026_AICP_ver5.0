# 실행·Preflight·복구 Runbook

> 현재 판정: **2026-02-27~2026-07-22 본실험 입력·무과금 E2E 검증 완료
> (유료 승인 및 clean freeze 대기)**
>
> `05 -> simulation.py`에는 sealed StudySpec, atomic checkpoint/resume,
> response journal, canonical validator와 유료 호출 gate가 연결되어 있다.
> 완료된 검증:
> - reasoning-off live canary (Qwen3.5 Flash, provider=alibaba 고정,
>   전 호출 `reasoning_tokens=0`)
> - 유료 2일 검증 v8 완주 (100 agent, 400/400 turn, 소진 0, 최대 재시도
>   3/10, $1.87) 및 `99_validate` `segment_valid_not_publication_ready` 통과.
>   보존본: `outputs/logs/live_2day_v8_20260731/`
> - 무과금 98거래일 전 구간 E2E 완주 (D2 agent 포함 `--max-agents 7`,
>   196/196 event, outcome ledger finalize, h5·right-censoring 검증)
> - `kill -9` 중단 후 동일 인자 resume 복구와 journal 재생(재과금 없음) 실증
>
> 봉인 기준: `baseline_commit 6ecd2c9`, `prompt_bundle_sha256 9cb9c07a…`.
> 98거래일 본실험은 조건·run ID·비용 상한을 명시해 사용자 승인 후에만 시작한다.

이 문서는 앞으로 사용할 단일 번호형 파이프라인의 운영 절차다. 정책과 상세
아키텍처는 각각 [`EXPERIMENT_DESIGN.md`](EXPERIMENT_DESIGN.md)와
[`ARCHITECTURE.md`](ARCHITECTURE.md)를 정본으로 삼고, 여기에는
준비·판정·실행·재개·완료 순서만 둔다.

## 1. 운영 권한

다음 작업은 별도 승인 없이 할 수 있다.

- 파일·Git 상태·hash를 읽는 검사
- 외부 API를 부르지 않는 단위·통합 테스트
- 임시 디렉터리에서의 synthetic fixture와 offline failpoint test
- validator와 report의 정적 검사

다음 작업은 사용자 명시적 승인 전 금지한다.

- 유료 API 호출
- live canary와 45일 본실험
- 봉인 입력·실험 결과를 덮어쓰는 작업
- commit, push, PR
- model/provider 변경

승인 문장에는 최소 run ID, 조건, 기간, agent 수, 예상 비용 상한이 있어야 한다.
“테스트해 봐”를 유료 실행 승인으로 해석하지 않는다.

### 게이트 순서 (필수)

코드·프롬프트 변경 후에는 반드시 이 순서로 통과한 뒤에만 유료 실행한다.

1. 전체 테스트 (`.venv/bin/python -m unittest discover -s tests`)
2. 오프라인 E2E — **`--max-agents 7` 이상**으로 실행할 것. A001~A004에는
   depth 2 agent가 없어(첫 D2는 A007) 4명 코호트로는 D2 검색 경로가 실행조차
   되지 않는다. D2 검색이 죽어 있던 결함을 4명 E2E가 잡지 못한 원인이다.
3. 유료 실행 (승인 필요)

순서를 건너뛰어 유료 실행이 죽은 사례가 2건 있다(NameError 미검출,
저장 경계 미러 미수정). compileall과 단위 테스트만으로는 잡히지 않았다.

## 2. 현재 하드 스톱

현재 `05 --help`에는 명시적 `--run-dir`, `--resume`, sealed
news/calendar/price, Community ON/OFF, 유료 승인, reasoning-off canary
옵션이 노출된다.

```bash
python scripts/05_run_simulation.py --help
```

production `05`는 legacy CSV나 JSON split을 자동 선택하지 않고 sealed bundle이
없거나 hash가 다르면 중단한다. RN `09/12`와 별도 checkpoint runner는
제거됐다.

무과금 P0에서 현 profile 재봉인, 전체 회귀, profile validator와 과거 45거래일
OFF/ON 중단·재개 검증을 완료했고, 7월 22일 확장 profile은 7 agent Community ON
전 구간 offline E2E와 canonical validator를 통과했다.
live reasoning-off canary와 유료 2일 검증(v8)도 승인 하에 완료했다
(문서 상단 판정 참조). 현재 실제 하드 스톱은 다음뿐이다.

1. 98거래일 본실험은 별도 비용·조건·run ID 승인 없이는 실행할 수 없다.
2. live 실행 전에는 의도한 diff를 freeze하고 clean code/prompt/input 기록을
   승인 run record에 남겨야 한다. 현재 작업의 무과금 검증은 이를 대체하지 않는다.

profile hash 검증을 우회하거나 `study_spec.json`의 hash 문자열만 손으로
바꾸지 않는다.

## 3. 환경 준비

작업 루트에서 Python 3.12를 사용한다.

```bash
python3.12 --version
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest --version
```

Python 3.9나 임의의 global package 조합으로 paper 결과를 만들지 않는다.
최종 freeze 전에는 Python, dependency, OS 정보를 run signature와 별도
환경 기록에 남기고 dependency lock을 고정한다.

`requirements.txt`에는 runtime의 `pandas`, `openai`, PDF용 `reportlab`과
회귀 실행용 `pytest`가 포함되어 있다. 설치가 덜 된 전역 Python에서 일부
스크립트만 실행해 나온 결과를 최종 회귀로 기록하지 않는다.

API key가 없는 상태가 offline 검사에는 정상이다. paper mode가 offline stub,
fallback model 또는 키 누락을 조용히 허용하면 실패다.

## 4. Git·기준 입력 확인

### 4.1 branch와 작성자

```bash
git status --short --branch
git remote -v
git log -1 --format='%H%n%an <%ae>%n%ad%n%s' --date=iso-strict
git ls-files preparation/rn_ab_sealed_v1/news.json
```

현재 확인 기대값은 다음과 같다.

- branch: `sujin_0727`
- remote: `sujin809/2026_AICP_ver3.0`
- base HEAD: `f4e17956f39e0cb0d94974cb03684d68f5e53ce7`
- author: `sujinjung <e62974347@gmail.com>`
- `preparation/rn_ab_sealed_v1/news.json`이 Git tracked

개발 중 dirty working tree는 허용되지만 canary와 본실험에는 허용되지 않는다.
freeze 시점에는 의도한 diff, code tree hash, prompt tree hash를 run record에
남긴다.

### 4.2 sealed bundle 구조

아래 검사는 파일을 수정하거나 API를 호출하지 않는다.

```bash
python - <<'PY'
import json
from pathlib import Path

root = Path("preparation/rn_ab_sealed_v1")
required = {
    "study_spec.json",
    "calendar.json",
    "cohort.json",
    "news.json",
    "prices.json",
    "stage_inputs.json",
    "known_injection.json",
    "review.json",
}
missing = sorted(name for name in required if not (root / name).is_file())
assert not missing, f"missing sealed files: {missing}"

spec = json.loads((root / "study_spec.json").read_text())
calendar = json.loads((root / "calendar.json").read_text())
cohort = json.loads((root / "cohort.json").read_text())
news = json.loads((root / "news.json").read_text())

assert len(calendar["dates"]) == 45
assert len(cohort["agents"]) == 100
assert len(news["slots"]) == 760
assert len(news["accepted_shortages"]) == 59
assert news["stock_code"] == "005930"
assert news["target_real_news_per_event"] == 10
assert news["fake_news_per_event"] == 0
assert spec["trade_policy"]["commission_rate"] == 0
assert spec["trade_policy"]["sell_tax_rate"] == 0
assert spec["model_policy"]["reasoning"] == {
    "effort": "none",
    "exclude": True,
}
print("sealed structure: PASS")
print("bundle:", news["bundle_sha256"])
print("spec baseline_commit:", spec["baseline_commit"])
PY
```

이 검사는 내부 canonical hash 전체를 재계산하는 validator의 대체가 아니다.
구조가 맞는지 빠르게 확인하는 첫 단계일 뿐이다.

현재 로컬 `study_spec.json`은 D1=5, D2=5, 뉴스 D2=5, fee=0,
strict reasoning-off 같은 통합 정책과 현 production prompt hash를 담는다.
candidate와 official profile 모두 `02`·`03`(read-only)·`04` 검증을 통과했고,
재봉인 전후 `news.json`은 byte-identical이다. 이 빠른 구조 검사는 live provider
telemetry 또는 canary 승인 증거가 아니므로, 기존 묶음을 유료 실행 승인으로
간주하지 않는다.

## 5. 00→04 준비 절차

최종 pipeline의 책임 순서는 다음과 같다.

1. `00`: instrument별 시장 원천과 target source를 확보한다.
2. `01`: 현재 baseline에서는 frozen cohort·persona·depth를 읽기 전용
   검증한다. 새 cohort는 명시적 별도 출력 경로에만 생성한다.
3. `02`: 현재 sealed news·달력·가격·StudySpec 연결을 읽기 전용으로
   검증한다. 이 스크립트에는 write 경로가 없다. 새 news source의
   provenance 결합과 봉인은 `13`과 `14`의 책임이다.
4. `03`: 기본 동작으로 현재 `StockData`를 읽기 전용 검증한다. 적재는
   명시적 `--write`와 source/target 경로가 있을 때만 한다.
5. `04`: clean base DB, 초기 portfolio, 결정론적 LTB₀과 base digest를 만든다.

현재 본실험에서는 `preparation/rn_ab_sealed_to_20260722_v1/`을 입력 정본으로
사용하며 cohort와 news를 재선발하지 않는다. source를 바꾸지 않는 일반
재현·실행 준비에서 `00`, `02`, `03`, `13`, `14`를 다시 실행하지 않는다.

7월 22일 본실험은 아래처럼 기간과 versioned 입력을 모두 명시한다. 설정 기본값도
같은 profile을 가리키지만, 승인 run record에는 해석의 여지가 없도록 경로를
생략하지 않는다.

```bash
TWINMARKET_OFFLINE_LLM=1 python scripts/05_run_simulation.py \
  --start-date 2026-02-27 \
  --end-date 2026-07-22 \
  --news-bundle preparation/rn_ab_sealed_to_20260722_v1/news.json \
  --calendar-registry preparation/rn_ab_sealed_to_20260722_v1/calendar.json \
  --price-registry preparation/rn_ab_sealed_to_20260722_v1/prices.json \
  --base-db outputs/experiment_base_to_20260722.db \
  --run-dir outputs/experiments/<run_id>
```

최종 freeze 전 `02 --help`와 `03 --help`를 다시 확인해 `02`에는 write
경로가 없고, `03`의 write는 `--write`와 명시적 source/target/profile 없이는
시작되지 않는지 회귀로 고정한다. bare 실행이 기존 sealed input이나
`sim.db`를 바꾸면 NO-GO다.

뉴스 provenance나 기간을 새로 만드는 경우에만
`13_bind_news_provenance.py -> 14_seal_news_bundle.py`를 별도 versioned
경로에 실행한다. code·prompt·persona 정책만 바뀐 현재 작업은 기존
`news.json`을 그대로 참조해 `15_seal_study.py`가 새 profile을 만들면 된다.

준비 단계 공통 gate:

- 모든 경로가 명시적이며 symlink·`outputs/current`·latest glob이 아님
- 종목, 날짜, event, agent ID가 registry와 exact equality
- input hash를 읽는 동안 파일이 변하지 않음
- run DB는 새 run-scoped 경로이며 0-byte·stale DB가 아님
- 초기 portfolio와 LTB₀이 agent별 정확히 하나
- 과거 result directory를 runtime input으로 사용하지 않음

## 6. 무과금 테스트

통합 도중에는 영향 범위를 좁혀 실행하고, freeze 전에는 전체 suite를 실행한다.

### 6.1 빠른 회귀

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_simulation_cohort.py \
  tests/test_hierarchical_memory.py \
  tests/test_integrated_memory_prompt_wiring.py \
  tests/test_community_policy.py \
  tests/test_legacy_community_phase.py
```

2026-07-29 Python 3.12 가상환경에서 전체 `pytest -q`는 **159 passed,
11 subtests passed**였다. 이에는 STB/LTB wiring, sealed news, checkpoint/
resume, provider JSON canonical-order replay, canonical validation과 파생 output
guard 회귀가 포함된다. 이는
무과금 검증 기록이며, live canary나 본실험 결과를 대체하지 않는다.

### 6.2 main sealed-news 회귀

먼저 변경 파일의 구문을 검사한다.

```bash
PYTHONPYCACHEPREFIX=/tmp/aicp_pycache python -m py_compile \
  config.py \
  twinmarket_kr/agents/news_agent.py \
  twinmarket_kr/core/collect_context.py \
  scripts/05_run_simulation.py \
  tests/test_sealed_news_agent.py
```

그다음 bundle과 미래뉴스 차단 회귀를 실행한다.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest -v \
  tests.test_sealed_news_agent \
  tests.test_experiment_safety
```

결과에는 실행 시각, tree hash와 실제 test count를 함께 남긴다. 이 문서의
과거 PASS 숫자를 새 tree의 증거로 재사용하지 않는다.

### 6.3 sealed input·StudySpec·누수

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_integrated_study_spec.py \
  tests/test_study_sealer.py \
  tests/test_sealed_news_agent.py \
  tests/test_experiment_safety.py
```

### 6.4 journal·resume·finalize

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_response_journal.py \
  tests/test_experiment_checkpoint_runtime.py \
  tests/test_canonical_run_validation.py \
  tests/test_pair_evaluation.py
```

### 6.5 community와 strict call

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_community_policy.py \
  tests/test_legacy_community_phase.py \
  tests/test_integrated_memory_prompt_wiring.py \
  tests/test_lineage_logging.py \
  tests/test_runner_paid_gate.py
```

### 6.6 전체 suite

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
```

과거 RN 전용 runtime 테스트는 공통 모듈 회귀 테스트로 대체하고 제거했다.

필수 경계 fixture:

- D1 선택 5 통과, 6 실패
- D2 선택 5 통과, 6 실패
- 뉴스 D2 추가 검색 5건 통과, 6건 거부
- 전역 Best 최대 5개와 결정론적 순위
- post 본문 500자 통과, 501자 실패
- D0 post/select/react 0, next-AM Best full body 정상
- Best 작성자 자기 글 0, 6위 backfill 0
- 다른 독자는 같은 Best를 정상 수신
- `title_only`와 `full_body` 집계 분리
- Community OFF의 community artifact 0
- fee/tax 0과 portfolio reconciliation
- future news/price/target/fake label leakage 거부
- STB, analysis, decision, fill, post-fill LTB, community, WAL failpoint의
  resume digest equality

테스트 통과 수만 보고하지 않는다. 실행한 명령, Python 버전, test 목록, 실패와
skip을 함께 기록한다.

## 7. production import와 단일 writer 검사

통합 완료 후보에서 다음 검색은 신규 실행 경로의 RN runtime import가 0이어야
한다.

```bash
rg -n 'twinmarket_kr\.rn_ab' \
  scripts/05_run_simulation.py \
  twinmarket_kr/simulation.py \
  twinmarket_kr/core \
  twinmarket_kr/agents \
  twinmarket_kr/community
```

다음도 함께 검사한다.

```bash
rg -n 'outputs/current|glob\(.+latest|resume_202|run_full_restart|09_run_realnews|12_operate_realnews' \
  scripts/05_run_simulation.py \
  twinmarket_kr \
  validation
```

검색 결과는 무조건 삭제하지 말고 production 호출인지, read-only compatibility
reader인지 구분한다. 신규 run writer가 legacy와 canonical 양쪽에 같은 사실을
쓰면 NO-GO다.

## 8. 통합 preflight

현재 `05`에는 별도 `--preflight-only` 옵션이 없다. 대신 실제 simulation
경로는 model client 생성과 run DB mutation 전에 다음을 순서대로 검증하고,
하나라도 다르면 중단한다.

- call policy와 Community mode
- StudySpec, cohort/persona projection, prompt bundle 의미·hash
- sealed news, calendar, price의 canonical hash와 ordered event ID
- date range, 100명 cohort, depth 30/55/15
- clean base DB와 runtime-table absence
- model/provider/fallback/reasoning-off 정책
- live이면 `--allow-paid-api`와 이전 canary audit

무과금 preflight는 `05`를 억지로 중간 종료시키는 방식이 아니라 이 문서의
정적 검사와 아래 전용 회귀로 수행한다.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_integrated_study_spec.py \
  tests/test_study_sealer.py \
  tests/test_clean_experiment_base.py \
  tests/test_runner_paid_gate.py \
  tests/test_sealed_news_agent.py \
  tests/test_news_quota_policy.py
```

실제 `05`가 시작되면 새 run은 `run_signature.json`, `run_metadata.json`,
`.runtime/checkpoint.json`, `.runtime/committed.db`,
`.runtime/response_journal.sqlite`를 만든다. 이것은 preflight report만
만드는 동작이 아니라 simulation state 생성이므로 dry-run과 혼동하지 않는다.

## 9. 재봉인과 freeze

코드나 production prompt가 바뀌면 기존 StudySpec의 `baseline_commit`,
code hash, prompt bundle hash가 낡는다. 다음 순서로 새 버전을 만든다.

1. 통합 테스트를 모두 통과시킨다.
2. active code, prompt, dependency, input path를 확정한다.
3. 존재하지 않는 새로운 versioned output directory에 bundle을 생성한다.
4. 원본 봉인을 덮어쓰지 않고 old/new hash diff를 검토한다.
5. 새 StudySpec이 현재 code commit/tree와 prompt hash를 가리키는지 검사한다.
6. 두 조건 resolved manifest의 허용 diff만 확인한다.
7. reviewer와 승인 시각을 run record에 남긴다.

`15_seal_study.py`는 지정한 디렉터리에 파일을 쓰므로 기존
`preparation/rn_ab_sealed_v1/`을 대상으로 바로 실행하지 않는다. 예시는
다음과 같다.

```bash
python scripts/15_seal_study.py \
  --out preparation/rn_ab_sealed_candidate_20260729 \
  --sys-db data/sys_100_ko_ver5.db \
  --sim-db outputs/sim.db \
  --prompt-dir prompts \
  --news preparation/rn_ab_sealed_v1/news.json \
  --stock-code 005930 \
  --instrument-name 삼성전자 \
  --start-date 2026-02-27 \
  --end-date 2026-05-04
```

생성 뒤 old/new 파일 목록과 hash, 45거래일·90 event, 100명·30/55/15,
뉴스 760 slot·59 shortage, prompt bundle hash를 검토한다. 리뷰가 끝난
candidate만 새 production profile 이름으로 승인한다. 재봉인 자체는 외부 API를
호출하지 않지만 기존 봉인을 덮어쓰는 명령은 별도 승인 없이 실행하지 않는다.

## 10. live canary

canary는 다음 P0가 모두 PASS한 뒤 사용자 승인을 받아 수행한다.

- 공통 05 경로와 canonical one-writer
- 새 sealed manifest
- 전체 무과금 suite
- resume failpoint
- reasoning-off request 정적 검사
- budget와 pause threshold

canary는 본 cohort 100명을 유지하고 전체 run과 같은 input format/hash 정책을
쓴다. 기간만 승인된 짧은 event 구간으로 제한한다. OFF/ON을 모두 실행해
다음을 확인한다.

- same input / community-only diff
- STB→analysis→decision→fill→post-fill LTB→PM community 순서와 row count
- D0 passive Best, D1/D2 선택 상한, 500자, self-exclusion
- strict reasoning-off 실제 telemetry
- provider/model 일치, fallback 0
- fee 0
- token·비용·latency·retry·WAL·disk
- 중단 후 exact resume

canary 때문에 prompt, schema, policy를 바꾸면 기존 canary는 무효다. 새
manifest로 양 조건 canary를 다시 실행한다.

reasoning-off telemetry canary는 simulation을 시작하지 않는 별도 1회
유료 호출이다. 사용자 승인 뒤에만 다음 형태로 실행한다.

```bash
python scripts/05_run_simulation.py \
  --allow-paid-api \
  --capture-reasoning-off-canary outputs/canary/reasoning_off_audit.jsonl
```

생성된 audit에서 request의 `reasoning.effort=none`,
`reasoning.exclude=true`, pinned model/provider, fallback 0, 응답 reasoning
비어 있음, reasoning token 0을 모두 확인한다. 이후 live simulation에는 같은
파일을 `--reasoning-off-canary-audit`으로 명시한다.

## 11. 본실험

본실험 승인 시에도 정확한 실행 명령은 통합된 `05 --help`와 freeze record에서
복사한다. 수동으로 날짜·agent·condition을 다시 입력해 manifest와 다른 run을
만들지 않는다.

시작 전 운영자가 확인할 값:

```text
Study ID:
Run pair ID:
Resolved manifest SHA-256:
Code/prompt/dependency SHA-256:
Cohort/calendar/news/price/target SHA-256:
Community-only diff: PASS/FAIL
P0: PASS/FAIL
Canary: PASS/FAIL
Budget cap:
Time/disk cap:
Paid-call approval and timestamp:
```

현재 baseline 두 arm의 canonical launcher는 `08`이며 내부적으로 각 arm을
공통 `05`로 실행한다. 재봉인 profile은 준비됐고, 아래 명령은 clean/frozen
record와 승인된 canary가 갖춰진 뒤에만 실행한다.

```bash
python scripts/08_run_six_conditions.py \
  --start-date 2026-02-27 \
  --end-date 2026-05-04 \
  --seed 2 \
  --conditions RN_COMM_OFF RN_COMM_ON \
  --output-root outputs/experiments/<pair_id> \
  --experiment-base-db outputs/experiment_base_sim.db \
  --real-profile-root preparation/<approved_profile> \
  --max-parallel-runs 2 \
  --global-api-concurrency 16 \
  --allow-paid-api \
  --reasoning-off-canary-audit outputs/canary/reasoning_off_audit.jsonl \
  --evaluation-targets validation/data_trading_value.csv
```

`08`이라는 파일명과 달리 baseline 기본 조건은 여섯 개가 아니라
`RN_COMM_OFF`, `RN_COMM_ON` 두 개다. fake profile 경로가 승인되기 전에는
fake 조건 ID를 추가하지 않는다. 한 arm만 진단할 때도 별도 runner를 만들지
않고 `05 --community-mode off|on`과 explicit sealed input/run-dir을 사용한다.

실행 중에는 immutable input을 수정하지 않는다. status는 checkpoint와
canonical DB를 읽어 확인하며 CSV 행 수만으로 완료를 판단하지 않는다.

## 12. 모니터링과 pause

다음 상황에서는 자동 대체나 데이터 보정 없이 pause한다.

- model/provider/reasoning telemetry 불일치
- manifest 또는 input hash drift
- 동일 logical ID의 다른 request
- schema validation 반복 실패
- condition별 event key 차이
- fee nonzero 또는 portfolio reconciliation 실패
- article payload/slot mismatch
- community 권한·length·self-exclusion 위반
- checkpoint commit 상태 불명
- budget/time/disk threshold 초과

뉴스 목표 미달은 이미 봉인된 `shortage_accepted`이면 pause 사유가 아니다.
실제 전달 수를 그대로 기록하고 두 조건이 같은 payload를 썼는지 계속
검증한다. 런타임에서 새 shortage를 만들거나 기존 부족 수를 기본값으로
덮으면 pause한다.

## 13. resume

resume 전에는 원 run root를 복사하거나 새 run ID로 가장하지 않는다.

1. 실행 프로세스가 완전히 종료됐는지 확인한다.
2. run lock, checkpoint state, completed phase를 읽는다.
3. manifest와 code/prompt/input hash가 원 run과 같은지 검증한다.
4. 해당 arm의 committed DB, pending snapshot과 journal 상태를 검사한다.
5. `running`이면 committed DB와 artifact snapshot으로 해당 event 전 상태를
   복구한다.
6. `commit_decided`이면 DB를 되돌리지 않고 journal commit을 마무리한다.
7. 동일 request의 accepted response는 replay하고 physical API를 재호출하지
   않는다.
8. 첫 미완료 event에서 재개한다.

resume은 `05_run_simulation.py --resume`의 같은 run directory·signature
경로만 사용한다. 날짜별 복구 entrypoint와 과거 별도 checkpoint/RN 실행기는
제거됐으며 Git history에서만 확인한다.

예를 들어 OFF arm을 재개할 때는 처음 실행과 같은 인자를 모두 유지하고
`--resume`만 추가한다.

```bash
python scripts/05_run_simulation.py \
  --start-date 2026-02-27 \
  --end-date 2026-05-04 \
  --seed 2 \
  --community-mode off \
  --news-bundle preparation/<approved_profile>/news.json \
  --calendar-registry preparation/<approved_profile>/calendar.json \
  --price-registry preparation/<approved_profile>/prices.json \
  --base-db outputs/experiment_base_sim.db \
  --run-dir outputs/experiments/<pair_id>/RN_COMM_OFF \
  --allow-paid-api \
  --reasoning-off-canary-audit outputs/canary/reasoning_off_audit.jsonl \
  --resume
```

`--start-date`, `--end-date`, seed, condition, input path/hash, model policy나
production prompt가 바뀌면 같은 run을 재개하지 말고 별도 profile/run으로
시작한다.

복구 후 즉시 검사할 것:

- duplicate logical call, decision, fill, LTB가 0
- completed event key가 줄거나 바뀌지 않음
- replayed response hash가 원 journal과 동일
- OFF/ON pair 운영 중이면 두 arm의 completed-prefix와 next event를 별도로 비교
- uninterrupted fixture와 digest equality

## 14. finalize

마지막 event 뒤에는 별도 finalization transaction을 수행한다.

1. due가 오지 않은 outcome을 `right_censored`로 확정한다.
2. 마지막 PM Best의 다음 AM 전달을 `right_censored`로 확정한다.
3. 예정·실제 community delivery 수를 reconcile한다.
4. journal pending/accepted/committed 상태를 검사한다.
5. DB FK, unique key, row count, hash, portfolio, fee를 검사한다.
6. 조건별 canonical fill export index를 만든다.
7. 완료 marker를 원자적으로 기록한다.

validator가 PASS하기 전에는 `run_complete`라는 이름만으로 성공을 주장하지
않는다.

## 15. validate와 report

실행 완료 뒤 순서는 고정한다.

```text
read-only integrity -> external derivative validation -> report
```

validator와 report는 명시한 signed run root만 읽고, 결과는 run 밖의
`derived/<condition>/`에 쓴다. legacy `outputs/current`, global DB, 과거
PDF/CSV fallback은 hard fail이어야 한다.

필수 검증:

- expected vs actual agent-event-stage key
- STB/LTB parent와 evidence lineage
- STB→analysis→decision→fill→post-fill LTB 계보, decision/fill one-to-one,
  portfolio
- `next_turn`·`H1`·`H5`별 maturity, 1회 consumption, terminal censoring
- news coverage·shortage·depth projection
- community title/full-body, reaction, Best, self-exclusion, delivery
- strict reasoning-off와 call counts
- fee/tax 0
- burn-in/evaluation mask
- frozen actual target

필수 report:

- integrity와 run provenance
- real-news coverage와 shortage sensitivity
- daily direction primary metric과 baseline
- memory lineage와 decision/fill 차이
- community mechanism
- fee-free wealth/turnover 진단
- retry, missingness, right censoring

명시한 완료 run을 먼저 검증하고 방향성 분석을 실행한다.

```bash
python scripts/99_validate.py \
  --run-dir outputs/experiments/<pair_id>/RN_COMM_OFF \
  --output outputs/experiments/<pair_id>/derived/RN_COMM_OFF/run_validation.json

python validation/validate_trading_direction.py \
  --run-dir outputs/experiments/<pair_id>/RN_COMM_OFF \
  --output-dir outputs/experiments/<pair_id>/derived/RN_COMM_OFF/direction_validation \
  --skip-initial-days 3

python scripts/generate_run_report_pdf.py \
  --run-dir outputs/experiments/<pair_id>/RN_COMM_OFF \
  --output outputs/experiments/<pair_id>/derived/RN_COMM_OFF/run_report.pdf

python scripts/generate_community_report_pdf.py \
  --run-dir outputs/experiments/<pair_id>/RN_COMM_ON \
  --output outputs/experiments/<pair_id>/derived/RN_COMM_ON/community_report.pdf
```

OFF/ON 두 arm이 모두 완료되면 `scripts/08_run_six_conditions.py`가 같은 cohort,
calendar, news, prompt, model, seed와 exact fill grid를 검증한
`pair_evaluation.json`을 생성한다. run별 PDF는 explicit `--run-dir`을 받는
`generate_run_report_pdf.py`와 `generate_community_report_pdf.py`만 현재
정본으로 사용한다. 과거 run ID·과거 fake schema를 고정한
condition-comparison/deep/fake report는 제거했다. 향후 fake 조건 보고서는
승인된 fake StudySpec과 공통 pair artifact reader에 연결한 뒤 추가한다.

방향성 검증은 agent의 AM+PM gross signed fill value를 일별 합산해 실제
삼성전자 `Individuals` 최종 일별 순거래대금 방향과 비교한다. AM-only/PM-only는
시간대별 실제 개인수급 target이 없으므로 primary metric으로 쓰지 않는다.
Community 분석에서는 `community_interactions.csv`의 `title_only`와
`full_body`를 반드시 분리하고, `community_best_posts.csv`의 예정/실제
delivery와 self-exclusion을 함께 확인한다.

## 16. Advisor warm-fork 실험

Advisor 실험은 `rn_ab_sealed_to_20260529_v1` profile을 사용한다. 먼저 현재 통합
엔진으로 2026-05-04 PM까지 100명 parent segment를 만든다. 과거 legacy/ver6
DB는 warm base로 사용할 수 없다.

```bash
python scripts/16_prepare_advisor_inputs.py \
  --parent-run-dir outputs/experiments/<parent_run> \
  --output outputs/advisor/<experiment_id>/advisor_cases.json
```

아래 단계만 reasoning ON이며 유료 호출이므로 사용자 승인을 받은 뒤 실행한다.
메인 시뮬레이션 호출은 Advisor ON/OFF 모두 reasoning OFF다.

```bash
python scripts/17_generate_advisor_artifact.py \
  --cases outputs/advisor/<experiment_id>/advisor_cases.json \
  --output outputs/advisor/<experiment_id>/advisor_messages.json \
  --audit outputs/advisor/<experiment_id>/advisor_openrouter_calls.jsonl \
  --allow-paid-api
```

동일한 parent `.runtime/runtime_sim.db`에서 5월 6일~29일 ON/OFF를 각각 새
`--run-dir`로 fork한다. ON은 `--advisor-artifact`가 필수이고 OFF에서는 금지된다.
`--warm-base`는 parent completion, turn 90 경계, pending trade 부재와 parent
DB 일치를 fail-closed로 검사한다. run signature에는 parent 경로·signature·terminal
hash와 복제 직후 DB hash가 함께 기록된다.

```bash
python scripts/05_run_simulation.py \
  --start-date 2026-05-06 --end-date 2026-05-29 --seed 2 \
  --community-mode off --advisor-mode on \
  --advisor-artifact outputs/advisor/<experiment_id>/advisor_messages.json \
  --warm-base --parent-run-dir outputs/experiments/<parent_run> \
  --base-db outputs/experiments/<parent_run>/.runtime/runtime_sim.db \
  --news-bundle preparation/rn_ab_sealed_to_20260529_v1/news.json \
  --calendar-registry preparation/rn_ab_sealed_to_20260529_v1/calendar.json \
  --price-registry preparation/rn_ab_sealed_to_20260529_v1/prices.json \
  --run-dir outputs/experiments/<advisor_on_run> \
  --allow-paid-api --reasoning-off-canary-audit <approved_canary.jsonl>
```

현재 확정되지 않은 Community 조건은 실행 직전에 `off`, `on`, 또는 4셀 중
선택한다. Advisor 대상은 namespace `advisor-full-cohort-v1`로 고정한 전체
100명이며 agent ID 결정론적 정렬 순서로 artifact에 기록한다.

## 17. 재현 패키지

팀원이 clone 후 같은 run을 검증할 수 있도록 다음을 보존한다.

- commit/tree hash와 dirty diff 여부
- Python·dependency·OS 정보
- `run_signature.json`, `run_metadata.json`, StudySpec와 모든 source hash
- cohort, calendar, price, news, prompt registry
- 조건별 DB와 response journal
- checkpoint/recovery history
- raw telemetry와 validator 결과
- deterministic exports와 report index
- 실행·resume·finalize 명령과 승인 record

secret, API key, 직접 개인식별정보는 패키지에 넣지 않는다.

## 18. 최종 Go/No-Go

| Gate | GO 기준 |
| --- | --- |
| G0 Git | 기준 branch/commit/tree와 diff 기록 |
| G1 input | 최신 수진 sealed news를 새 code/prompt에 맞춰 재봉인 |
| G2 architecture | 05 단일 entrypoint, RN production import 0, canonical writer 1 |
| G3 science | StudySpec·event·depth·STB/LTB·community·fee 정책 검증 |
| G4 safety | strict reasoning-off, no fallback, no leakage |
| G5 recovery | event failpoint와 resumed digest equality |
| G6 canary | 양 조건 live telemetry와 budget 승인 |
| G7 finalization | integrity, exporter, validator, report PASS |

하나라도 FAIL 또는 NOT RUN이면 본실험 GO가 아니다. 허용된 뉴스 shortage는
`PASS_WITH_NEWS_SHORTAGE`로 별도 기록하되 실험을 중단하지 않는다.
