# AICP 3.0 작업 지침

이 저장소는 `sujin809/2026_AICP_ver3.0`의 `sujin_0727` 브랜치를
기준으로 작업한다. V2 저장소의 코드를 복사해 별도 실행 경로를 만들지 않는다.

## 최종 목표

과거 RN 전용 runtime에서 검증한 기능을
`scripts/05_run_simulation.py -> twinmarket_kr/simulation.py` 흐름 안에
통합했다. 이 기존 이름의 실행 경로가 프로젝트의 유일한 기본 실험 엔진이다.

완료된 저장소는 다음을 만족해야 한다.

- 기존 번호형 파이프라인 `00/01/02/.../05_run_simulation.py`를 사용자-facing
  기본 컨벤션으로 유지한다.
- 실제뉴스 Community OFF/ON 실험이 하나의 공통 엔진에서 실행된다.
- STB/LTB, decision/fill 분리, post-fill LTB, reasoning-off, journal/resume,
  provenance 및 community artifact가 기본 동작이다.
- 기존 결과 파일을 읽는 데 필요한 호환 reader/exporter는 유지할 수 있지만,
  잘못된 과거 실행 흐름 자체를 재현하기 위한 runtime은 유지하지 않는다.
- 최신 `sujin_0727` 코드와 봉인 입력을 유일한 메인으로 삼고, 새 실험이
  과거 legacy와 RN 두 경로로 갈라지지 않는다.
- 같은 엔진에서 조건, 종목, 기간, 에이전트 수를 설정으로 바꿀 수 있다.
- 이후 `real + bullish fake`, `real + bearish fake` 조건을 코드 복사 없이
  추가할 수 있다.
- 문서, 실행 명령, 로그, validator, report가 같은 실험 정의를 가리킨다.
- 다른 팀원이 GitHub를 clone한 뒤 README와 runbook만으로 실행 구조와 결과
  위치를 이해할 수 있다.

## 작업 원칙

- 오버엔지니어링하지 않는다.
- `rn_simulation.py`, `simulation_v3.py`, `new_runner.py`처럼 별도의 새 실행기를
  만들지 않는다.
- 새 프레임워크나 병렬 구현을 만들지 말고 RN의 검증된 동작을 기존
  `simulation.py`, `core`, `agents`, `community` 구조에 옮긴다.
- 제거된 RN 전용 09/12 실행기와 같은 compatibility entrypoint를 다시 만들지
  않는다. 과거 구현 확인이 필요하면 Git history를 사용한다.
- 커뮤니티의 행동 규칙은 RN 이전 레거시를 baseline으로 삼는다. RN의
  STB/LTB·journal·provenance 안전장치는 이식하되, RN의 균일 공개 프로필을
  커뮤니티 기본 설정으로 유지하지 않는다.
- 코드 수정 전 사용자에게 변경 파일, 로직, 실험 의미, 테스트 방법을 한국어로
  먼저 보고한다.
- 사용자가 요청하지 않은 리팩터링, 파일명 변경, DB 전면 재설계는 하지 않는다.
- 큰 파일을 한 번에 전부 이동하거나 이름을 바꾸지 않는다. 테스트가 있는 작은
  단위로 공통화하고 각 단계가 동작한 뒤 다음 단계로 간다.
- 기존 로그와 결과 파일은 삭제하지 않는다. 필요한 필드는 호환성을 유지하며
  추가한다.
- 유료 API 호출, canary, 본실험, commit, push는 사용자의 명시적 허가 없이
  실행하지 않는다.
- 코드·프롬프트 변경 후 게이트 순서는 **전체 테스트 → 오프라인 E2E → 유료**다.
  오프라인 E2E는 depth 2 agent가 포함되도록 `--max-agents 7` 이상으로 돌린다
  (A001~A004에는 D2가 없어 4명 코호트는 D2 검색 경로를 실행하지 못한다).
- 프롬프트에 구현 상세를 넣지 않는다. 근거 방향(support/contradict) 판단에
  연구자 편향을 주입하지 않는다. event 재시도와 resume은 동일 시드를 쓴다.

## 최신 흐름을 기존 번호형 본류에 통합하는 방식

두 구현을 서로 붙여서 세 번째 런타임을 만들지 않는다.

1. 최신 `sujin_0727`의 봉인 입력, StudySpec, STB/LTB, journal, community 중
   검증된 동작을 회귀 기준으로 고정한다.
2. 그 동작을 기존 `twinmarket_kr/simulation.py`와 기존 패키지 구조로
   단계적으로 이식한다. 단순히 `simulation.py`에서 `rn_ab.runner`를 호출하고
   끝내는 것은 완료가 아니다.
3. `scripts/05_run_simulation.py`가 통합된 기본 실행기를 호출하게 한다.
4. 과거 잘못된 runtime을 호출하는 compatibility entrypoint는 남기지 않고
   Git history로만 보존한다.
5. 레거시 DB·CSV·PDF 형식이 필요하면 canonical 원장에서 생성하는 호환
   exporter/reader로 유지한다. 같은 사실을 두 저장소에 따로 쓰지 않는다.
6. 새 실험은 반드시 통합된 StudySpec과 `simulation.py` 흐름을 사용한다. 과거
   `config.py` 전역값과 통합 StudySpec을 동시에 정본으로 두지 않는다.

삭제된 `rn_ab` 디렉터리를 rename한 runtime이나 `simulation.py`의 얇은
compatibility wrapper로 되살리지 않는다. 기능은 기존 모듈의 책임에 맞게
유지하며 신규 실험 경로에서 `twinmarket_kr.rn_ab` import는 항상 0이어야 한다.

### 격리·삭제 기준

- 활성 코드와 입력은 앞으로 실행할 통합 실험에서 실제로 참조하는 것만 둔다.
- 과거 잘못된 runtime은 `archive`에서 다시 실행할 수 있게 복제하지 않는다.
  필요한 설명만 history 문서에 남기고 소스 복구는 Git history를 사용한다.
- 과거 실험 결과는 분석 보존 대상이므로 runtime 입력 경로와 분리한다. 새 실행이
  `outputs/`의 과거 결과를 입력으로 읽어서는 안 된다.
- `archive/legacy_inputs/rn_ab_source_candidate_v1/input_candidates`는 현재
  입력이 아닌 격리된 NO-GO/history 자료다.
- 파일을 격리·제거하기 전 production import, 스크립트 호출, 테스트 fixture,
  문서 링크를 검색하고, 이식된 대체 경로의 테스트가 통과해야 한다.
- 제거한 기능을 이름만 바꾼 compatibility shim으로 되살리지 않는다.

목표 호출 구조는 다음과 같다.

```text
scripts/05_run_simulation.py
  -> twinmarket_kr/simulation.py
     -> 기존 core/community/agents 안에 통합된
        StudySpec + STB/LTB + decision/fill + journal/resume + report 흐름
```

## 지속 가능한 실험 설정

현재 `StudySpec`을 확장해 다음 축을 한곳에서 관리한다. 별도의 설정 프레임워크를
새로 만들지 않는다.

- instrument: 종목코드, 종목명, 거래소, 가격·개인수급 데이터, 달력
- news treatment: `real_only`, 향후 `real_plus_bullish_fake`,
  `real_plus_bearish_fake`
- community mode: `off`, `on`
- cohort: 에이전트 수, persona snapshot, depth 분포
- schedule: 시작·종료일, AM/PM event, burn-in
- memory/trade/model/evaluation policy

`005930`, `삼성전자`, 100명, 45일, 90 event 같은 값은 공통 엔진에 새로
하드코딩하지 않는다. 현재 삼성전자 baseline은 해시가 봉인된 하나의 study
profile로 유지한다.

현재 real-news baseline의 뉴스 정본은 새로 구축·봉인된
`preparation/rn_ab_sealed_v1/news.json`이다. 과거
`archive/legacy_inputs/rn_ab_source_candidate_v1/input_candidates`와 legacy selected-news CSV를
현재 실행 입력으로 되돌리지 않는다. 과거 candidate는 no-go/history artifact로
분리하고, 현재 실행·검증·보고서는 새 sealed news의 ID와 hash를 사용한다.

가짜뉴스 확장은 지금 본실험에 섞지 않는다. 다만 treatment schema와 입력
경계는 향후 실제뉴스 10개에 bullish 또는 bearish fake 1개를 추가할 수 있게
유지한다. fake 여부와 polarity label은 에이전트 입력에 노출하지 않는다.

## 단일 결과 원장과 보고서

- canonical DB/journal을 먼저 확정하고 CSV·PDF는 그 원장에서 생성한다.
- STB, decision, fill, LTB, community exposure와 claim의 ID 연결을 유지한다.
- 보고서가 `outputs/current`나 특정 과거 run 경로를 하드코딩하지 않게 한다.
- 기존 보고서의 유용한 표와 지표는 재사용하되 공통 artifact reader를 통해
  새 원장을 읽게 한다.
- 종목을 바꾸면 개인수급 비교 target과 보고서 종목명도 같은 Instrument 설정을
  따라야 한다.

## 문서 정리

기존 문서를 바로 삭제하지 않는다. 먼저 현재 구현과 역사적 설계를 구분한다.

최종적으로 팀원이 보는 정본은 다음 네 역할로 줄이는 것을 목표로 한다.

- `README.md`: 설치, 빠른 시작, 문서 안내, 결과 시작점
- `ARCHITECTURE.md`: 공통 엔진, STB/LTB, 거래, 커뮤니티, artifact 구조
- 실험 설계 문서: 연구질문, 조건, 종목·fake 확장 규칙, 지표
- runbook: prepare, preflight, run, resume, finalize, validate, report

오래된 문서는 삭제하지 말고 archive/history로 보존하며, 현재형으로 읽히지
않도록 상태를 표시한다. 동일한 정책 숫자와 실행 순서를 여러 문서에 복제하지
않는다.

Production prompt의 정본 디렉터리는 최상위 `prompts/` 하나다.
RN prompt의 top-level 통합은 완료되었으므로 별도 하위 production 경로나 같은
이름의 중복 파일을 다시 만들지 않는다. 봉인된 과거 run bundle 안의 prompt
복사본은 재현 artifact이므로 수정하지 않는다.

## 유지해야 하는 RN 실행 순서

다음 순서를 바꾸지 않는다.

1. 사용 가능한 과거 거래 성과를 확정한다.
2. 다음 AM에 전달할 수 있는 커뮤니티 노출을 준비한다.
3. 현재 event의 STB를 한 번 생성한다.
4. 이전 LTB와 현재 STB를 종합해 분석과 거래 결정을 만든다.
5. 잔고·보유량 제약을 적용한 실제 `fill`을 기록한다.
6. 이전 LTB, 현재 STB, 실제 fill, 사용 가능한 과거 성과를 반영해
   post-fill LTB를 한 번 생성한다.
7. PM이면 post-fill LTB 이후 커뮤니티 단계를 실행한다.

`decision`은 거래 의도이고 `fill`은 실제 체결 결과다. 두 값을 합치거나 같은
이름으로 기록하지 않는다.

## 이번 커뮤니티 수정 범위

RN 이전 레거시 커뮤니티의 게시 여부 판단, post type, 후보 선택 prompt,
익명 닉네임, D1/D2의 서로 다른 프로필 권한, 반응 기준, 동결 배치
방식, 기존 score와 Best 타이밍을 baseline으로 보존한다. 아래에 적힌 항목과
Best 원문/프로필 보강만 이번 통합에서 바꾼다. 명시적 사용자 결정 없이
커뮤니티를 새 실험 처치처럼 재설계하지 않는다.

레거시 동적 뱃지 3종(`상위 수익자`, `자산가`, `커뮤니티 인플루언서`)은
**사용자 결정으로 제거했다.** 계산·저장·노출과 선택 프롬프트의 평판 참고
문구를 모두 삭제했으므로, 뱃지를 baseline 보존 대상으로 되살리지 않는다.
제거 근거는 `ARCHITECTURE.md` §12.9에 있다. 저자 쪽 신호는 익명 닉네임과
D2 동결 프로필뿐이다.

### 1. Depth별 선택 상한

- D0: 선택 읽기 0개
- D1: 최대 5개
- D2: 최대 5개

봉인된 StudySpec에 값이 들어 있다는 사실만 믿지 않는다. 잘못된 값의 spec이
통과하지 않도록 spec 검증, 런타임 경계, 사후 artifact 검증과 테스트가 모두
같은 정책을 확인해야 한다.

### 2. 노출 수준 구분

분석 결과에서 다음 두 상태를 직접 구분할 수 있어야 한다.

- `full_body`: 해당 에이전트가 게시글 본문 전체를 실제로 읽은 경우
- `title_only`: 후보 화면의 익명 닉네임, 제목, 글 유형,
  반응 count/score는 봤지만 본문은 읽지 않은 경우

기존 `community_interactions.csv`와
`traces/community_exposure_trace.jsonl`을 우선 확장한다. 같은 정보를 담는
새 원장을 하나 더 만들지 않는다.

분석용 출력에는 적어도 agent, event, post, exposure level, 선택 여부,
반응, Best 여부와 provenance ID가 연결되어야 한다. 제목만 본 글을 본문까지
읽은 글로 집계해서는 안 된다. 미선택 title-only 후보는 레거시와 같이 분석용
로그로만 남기고 다음 AM claim이나 STB 근거에는 넣지 않는다.

### 3. 게시글 길이

- 커뮤니티 게시글 본문은 글당 최대 500자로 제한한다.
- 1,000자나 기존 8,000자 제한을 사용하지 않는다.
- posting prompt에도 500자 제한을 명시한다.
- 서버 검증에서도 500자를 초과하면 거부한다.
- 의미가 달라질 수 있는 자동·무음 잘라내기는 하지 않는다.
- 경계값 500자는 통과하고 501자는 실패하는 테스트를 둔다.

### 4. Best 자기 글 제외

- Best 게시글을 다음 AM에 전달할 때 작성자 본인에게 자기 글을 전달하지 않는다.
- 전역 Best 순위 계산 자체는 기존 반응 점수 규칙을 유지한다.
- 최소 변경 기본안은 전역 Best 최대 5개를 먼저 확정한 뒤 수신자별로 자기 글만
  제외하고, 6위 글로 채우지 않는 것이다. 따라서 작성자는 최대 4개를 받을 수
  있다.
- backfill이 필요하다는 사용자 결정이 나오기 전에는 수신자별 Best 순위를 새로
  만들지 않는다.
- `audience_count`, 실제 delivery 수, self-exclusion 여부를 artifact와
  validator가 재현할 수 있어야 한다.

## 기존 Best 규칙

사용자가 별도로 변경하지 않는 한 다음 규칙을 유지한다.

- 반응은 `like`, `unlike`, `none`이다.
- `score = like_count - unlike_count`이다.
- 점수 내림차순, like 수 내림차순, 기존의 결정론적 tie-break 순으로 정렬한다.
- 게시글이 5개보다 적으면 있는 글만 사용한다.
- 양수 점수 필터와 게시 강제는 추가하지 않는다.
- 같은 PM의 모든 독자는 동결된 후보 보드를 보고 반응한다. 실시간 인기
  피드백을 새로 만들지 않는다.

## 확정 Community lifecycle

이 순서는 구현과 테스트에서 그대로 보장해야 한다.

```text
PM의 실제 fill 확정
  -> post-fill LTB 생성
  -> `can_post_community=1`로 봉인된 D1/D2만 게시 여부를 결정
     (게시 강제 없음, 최대 1개)
  -> 당일 게시글 전체를 동결한 후보 보드 생성
  -> D1/D2가 자기 글을 제외한 제목 목록에서 읽을 글을 선택
     (D1 최대 5개, D2 최대 5개)
  -> 선택한 글만 본문 전체를 읽고 like/unlike/none 반응
  -> 모든 반응을 합산한 기존 score로 전역 Best 최대 5개 확정
  -> 다음 거래일 AM에 Best 원문을 수신자별로 전달
  -> 선택한 원문과 Best 원문으로 커뮤니티 해석 claim과 STB 근거 생성
```

### 후보 선택과 본문 읽기

- 후보 선택은 점수 상위 자동 선택이나 랜덤 선택이 아니라, 각 에이전트의
  persona prompt를 받은 LLM이 `selected_post_ids`를 반환하는 방식이다.
- 후보 화면은 `title_only`다. 본문을 절대 포함하지 않는다.
  익명 닉네임, 제목, 글 유형, 현재 반응 count/score를 제공한다.
  D1/D2 모두 이 화면에서 글을 고르되, 포트폴리오·최근 거래·작성자 평판
  신호는 후보 화면에 넣지 않는다.
- 후보 선택 때의 score는 해당 동결 보드의 score snapshot이다. 새 당일 게시판은
  반응 전이므로 대체로 0이며, 이 화면을 실시간 인기 순위처럼 만들지 않는다.
- 선택 결과가 비어 있어도 허용한다. D1과 D2에게 5개를 억지로 읽히지
  않는다.
- D1이 선택한 `full_body`에는 원문 전체와 작성자 익명 닉네임이 포함된다.
- D2가 선택한 `full_body`에는 원문 전체, 익명 닉네임과 함께 작성자의
  포트폴리오 요약 및 PM 시점 이전 최근 3건 거래가 포함된다.
- `full_body`는 선택된 글 또는 Best로 전달된 글의 전체 본문을 실제로 읽은
  경우에만 기록한다. 제목만 본 글을 full-body로 승격하지 않는다.

### Best의 다음 AM 전달

- D0와 D1은 Best 원문 전체, 작성자 익명 닉네임을 받는다.
- D2는 Best 원문 전체, 익명 닉네임, 작성자의 동결된 포트폴리오
  요약 및 최근 3건 거래를 받는다.
- D1/D2는 전날 직접 선택해 읽은 원문과 Best 원문을 받을 수 있다.
- 같은 글이 직접 선택과 Best에 모두 해당하면 본문은 한 번만 전달하고,
  `selected_full_body_replay`와 `best_full_body` 두 노출 관계를 남긴다.
  두 관계는 원장 라벨로만 남기지 않고 claim이 인용할 수 있는 근거 ID로
  모두 등록한다. 본문을 한 번만 직렬화한다는 이유로 인용 가능한 관계를
  하나로 줄이지 않는다.
- Best 작성자는 자기 글을 받지 않는다. 전역 Top 5에 들어간 자기 글을 6위 글로
  대체하지 않는다.
- 제목만 본 미선택 후보는 분석용 `title_only` 로그로만 남기며 다음 AM 해석,
  claim, STB 근거로 사용하지 않는다.
- D2에 제공하는 포트폴리오·거래 프로필은 다음 AM에 새로 조회하지 않는다.
  해당 PM 후보 보드 생성 시점의 snapshot을 동결해 재전달한다.

## STB/LTB와 커뮤니티 경계

- STB와 LTB는 기존 6차원을 유지한다.
- `belief_summary`는 사람과 레거시 로그용이며, 6차원 대신 다음 거래 입력으로
  사용하지 않는다.
- 선택한 글과 Best 글의 full body는 실제 노출 사실과 연결되어야 한다.
- title-only 노출은 분석에는 남기되 본문을 읽은 근거로 취급하지 않는다.
- 커뮤니티 글은 검증되지 않은 투자자 발언이다. 실제 거래는 canonical fill
  ledger만 신뢰한다.
- 게시글 작성은 post-fill LTB, 그 LTB와 연결된 view change, 현재 PM fill을
  활용하되 이를 그대로 복사해 공개하는 방식으로 강제하지 않는다.

## 구현할 때 우선 확인할 파일

- 최종 기본 진입점: `scripts/05_run_simulation.py`,
  `twinmarket_kr/simulation.py`
- 기존 턴 흐름: `twinmarket_kr/core/daily_cycle.py`,
  `twinmarket_kr/core/collect_context.py`
- 통합 정책 정본: `twinmarket_kr/study_spec.py`
- 통합 커뮤니티 정본: `twinmarket_kr/community/`
- DB 스키마: `twinmarket_kr/db/schema.py`
- STB/LTB 및 게시 prompt: `prompts/update_short_term_belief.txt`,
  `prompts/update_long_term_belief.txt`, `prompts/posting_decision.txt`
- 봉인 입력 생성: `scripts/15_seal_study.py`
- 통합 테스트: `tests/test_integrated_study_spec.py`,
  `tests/test_hierarchical_memory.py`, `tests/test_community_policy.py`,
  `tests/test_legacy_community_phase.py`,
  `tests/test_experiment_checkpoint_runtime.py`

과거 RN 전용 구현과 테스트는 Git history 근거일 뿐 production 정본이 아니다.
현재 동작은 위 통합 경로와 통합 테스트에서만 유지한다.

## 구현 순서

### 1단계: 현재 baseline 안정화

1. 현재 동작을 관련 테스트로 고정한다.
2. D1=5, D2=5 정책을 fail-closed로 고정한다.
3. 게시 prompt와 본문 검증 한도를 500자로 맞춘다.
4. Best delivery에서 self-authored post를 제외한다.
5. 기존 artifact에 노출 수준과 self-exclusion을 분석 가능한 형태로 반영한다.
6. validator와 report 계산을 같은 정의로 맞춘다.
7. 관련 단위·통합 테스트를 실행한다.
8. 코드 변경으로 기존 봉인 해시가 달라졌음을 보고하고 입력을 재봉인한다.

### 2단계: 실행 경로 통합

1. RN runner·StudySpec과 레거시 runner의 호출·상태·출력 차이를 표로 확정한다.
2. RN event state machine을 기존 `simulation.py`와 `core` 흐름에 이식한다.
3. `scripts/05_run_simulation.py`를 기본 실행 진입점으로 전환한다.
4. 신규 run의 canonical 쓰기 경로를 하나로 제한한다.
5. 필요한 레거시 출력만 호환 export로 재생성한다.
6. 실제뉴스 OFF/ON 회귀 테스트가 통합 전과 같은 의미를 보장하게 한다.
7. 신규 실행 경로에서 `twinmarket_kr.rn_ab` production import가 남지 않았는지
   검사한다.
8. 기존 `00/01/02/.../05` 번호형 실행 순서와 파일명을 유지하고 README/runbook의
   유일한 기본 명령으로 사용한다.

### 3단계: 설정 일반화

1. 삼성전자·조건명·기간 하드코딩을 StudySpec/profile로 이동한다.
2. 두 번째 synthetic instrument로 경로 일반성을 무과금 검증한다.
3. bullish/bearish fake treatment는 입력 schema와 leakage test까지만 준비하고,
   사용자의 승인 전에는 실제 run을 실행하지 않는다.

### 4단계: 문서·리포트 정리

1. 정본 문서 역할을 네 가지로 정리한다.
2. validator와 report를 canonical artifact에 연결한다.
3. 오래된 문서를 history로 표시한다.
4. clone 후 무과금 smoke test가 가능한 GitHub 인수인계 상태를 확인한다.

## 검증 원칙

- 먼저 외부 API를 부르지 않는 관련 테스트만 실행한다.
- D1 6개·D2 11개 선택은 실패해야 한다.
- D1 5개·D2 5개 선택은 통과해야 한다.
- 게시글 500자는 통과하고 501자는 실패해야 한다.
- Best 작성자는 자기 글 exposure가 없어야 한다.
- 다른 에이전트는 같은 Best 글을 정상적으로 받아야 한다.
- full-body와 title-only 집계가 서로 섞이지 않아야 한다.
- Community OFF에는 게시·선택·반응·Best 노출이 생기지 않아야 한다.
- 코드가 바뀌면 기존 sealed StudySpec의 `baseline_commit`과 해시가 낡는다.
  canary 전에 `scripts/15_seal_study.py`와 preflight를 다시 실행해야 한다.

---

## 다음 세션 즉시 재개용 인수인계 (2026-07-29)

이 절은 현재 리팩터링 작업의 중단 지점을 기록한다. 다음 세션은 저장소를 새로
설계하거나 과거 대화를 다시 요약하지 말고, 이 절의 `P0`부터 바로 이어서
작업한다.

### 저장소와 작업 상태

- 로컬 경로:
  `/Users/kwon_junyoung/Documents/AICP/2026_AICP_ver3.0`
- 기준 원격/브랜치:
  `sujin809/2026_AICP_ver3.0`, `sujin_0727`
- 기준 HEAD: `f4e1795`
- 작업 트리는 의도적으로 큰 미커밋 변경 상태다. 기존 수정과 삭제를
  `git reset`, `git checkout --`, `git clean`으로 되돌리지 않는다.
- 유료 API 호출, live reasoning-off canary, 본실험, commit, push는 하지
  않았다.
- 사용자가 토큰 절약을 위해 작업 중단을 요청했고, 진행 중이던 하위 에이전트도
  모두 중단했다. 특히 아래의 duplicate DB schema 제거는 시작 전에 멈췄으므로
  아직 완료된 것으로 간주하면 안 된다.

### 현재까지 통합된 것

- 신규 기본 실행은
  `scripts/05_run_simulation.py -> twinmarket_kr/simulation.py` 하나다.
- 별도 `twinmarket_kr/rn_ab/` runtime과 RN 전용 09/10/11/12/15 실행기,
  별도 checkpoint runner, 날짜 고정 복구 runner는 제거했다.
- STB/LTB 정본 prompt는 최상위 `prompts/` 하나이며 실제 호출부는
  `twinmarket_kr/llm/belief.py`다.
- 현재 인과 순서는 아래와 같이 통합돼 있다.

```text
현재 news/community evidence
  -> STB_t 6차원
  -> LTB_(t-1)와 STB_t를 서로 구분해 analysis/decision 생성
  -> intended decision
  -> 잔고·보유량 제약을 적용한 actual fill_t
  -> LTB_(t-1) + STB_t + actual fill_t + 도래한 outcome으로 post-fill LTB_t
  -> PM community
```

- `belief_summary`와 `view_change`는 사람·로그·게시글용 파생값이며 다음 거래
  입력은 6차원 STB/LTB다.
- Community baseline은 D0/D1/D2 선택 읽기 상한 0/5/5,
  `can_post_community=1`인 D1/D2만 게시,
  D0도 다음 AM Best 원문 열람, 글 500자, Best 자기 글 제외·무보충,
  title-only/full-body 분리 로그로 통합했다.
- strict OpenRouter 요청은 `reasoning={"effort":"none","exclude":true}`,
  고정 model/provider, fallback 금지, 유료 API 명시 승인, zero-reasoning
  canary gate로 이식했다.
- event journal/resume, STB/decision/fill/LTB lineage, H1/H5 outcome,
  canonical run validator와 명시적 run-dir report gate를 공통 본류로 옮겼다.
- `scripts/02_prepare_news.py`는 봉인 뉴스의 읽기 전용 validator다.
- `scripts/03_load_stock_data.py`는 기본 읽기 전용 validator이며
  `--write --source ... --target ... --profile-root ...`를 모두 명시해야만
  쓴다.
- 오래된 `scripts/06_run_community_smoke_test.py`와
  `scripts/07_prepare_fake_news_injection.py`는 제거했다.
- 팀원이 보는 정본 문서는 `AGENTS.md`를 제외하고 다음 네 개로 정리했다.
  `README.md`, `ARCHITECTURE.md`, `EXPERIMENT_DESIGN.md`,
  `RUNBOOK_AND_PREFLIGHT.md`.

### 절대 바꾸지 않는 확정값

- 현재 baseline 뉴스는 봉인된 Sujin real-news bundle이다.
- event별 category target은 회사/삼성 5, 반도체/산업 3, 거시 2다.
- 특정 category가 부족해도 다른 category 기사로 빈자리를 채우지 않는다.
- shortage를 기록하고 run은 계속한다. shortage 때문에 fail하지 않는다.
- 아래 뉴스 bundle 내용과 선택 정책은 이번 잔여 리팩터링에서 수정하지 않는다.
  - bundle hash:
    `a6fb61900c27071b2a79781478592d99d914482fbba0f4ecaafa73edcb8ab707`
  - file hash:
    `cf3561dbe9f9fa360b716970e8352022fa8cbcd4d824c1ef249880d1ee7e5f55`
  - 90 events, 760 slots/articles, 59 shortage events
- 부족한 기사를 새로 채우지 않기로 확정했으므로 `outputs/*_split` 5개 폴더가
  뉴스 소스의 최종본이다. 위 두 hash가 그 최종본에서 나온 정본이다.
- **에이전트에게 노출되는 뉴스 텍스트는 제목과 요약뿐이다. 기사 본문은
  노출하지 않는다.** 봉인 bundle에는 본문이 아예 없고 `raw_body_sha256`,
  `version_sha256`, `cutoff_version_sha256` 해시만 있다. 본문을 다시
  노출하도록 바꾸지 않는다.
  - `news_agent._public_content_item`의 `content` 필드는 기사 본문이 아니라
    `summary`다. 이름이 `content`라서 본문으로 오해하지 않는다.
  - depth별 노출은 다음과 같다. D0는 제목만 보며 요약을 받지 않는다.
    - D0: 해당 event의 제목 목록만 (`read_contents`는 항상 빈 배열,
      `limits.daily_read_max = 0`)
    - D1: 제목 목록 + 그 event 기사들의 요약
    - D2: D1과 같고, 추가로 최근 7일 검색 결과 최대 5건의 요약
- 삼성 baseline cohort는 정확히 100명, D0/D1/D2=30/55/15다.
- persona의 기존 demographic·투자성향 표현은 유지하며, 전원 동일한
  `trad_pro`, `fol_ind`를 새 persona 문장에 노출하지 않는다.
- 수수료는 0, hold는 사용하지 않으며 첫 3거래일은 분석 burn-in이다.
- H1/H5는 둘 다 사용하며 미래 결과는 도래 event 이후에만 LTB에 들어간다.

#### 봉인만 하고 절대 호출하지 않는 prompt 2종

`prompts/initial_belief.txt`와 `prompts/news_interpretation.txt`는
`prompt_bundle_sha256` 재현을 위해 봉인 묶음에 남겨두는 파일이며, **어떤
production 경로에서도 LLM 호출로 연결하지 않는다.** 근거는
`twinmarket_kr/study_spec.py`의 `_SUPPORT_PROMPTS`에 문자열로 박혀 있다.

| 파일 | 봉인 사유 문자열 | 실제 담당 |
| --- | --- | --- |
| `initial_belief.txt` | `sealed_compatibility_only_deterministic_ltb0_no_model_call` | `experiment_runtime.deterministic_initial_belief()` |
| `news_interpretation.txt` | `sealed_compatibility_only_current_stb_owns_interpretation` | `update_short_term_belief.txt` 단계 |

- `twinmarket_kr/llm/analysis.py`의 `interpret_news()`는 호출부가 없는 것이
  정상이다. "죽은 코드"로 보고 호출을 새로 만들거나 삭제하지 않는다.
- 두 파일을 실제 호출로 연결하면 설계 계약 위반이다. 특히
  `initial_belief.txt`는 `belief_summary`와 `view_change`를 LLM 출력으로
  요구하는데, `EXPERIMENT_DESIGN.md` 7.3절은 이 둘이 저장된 6차원에서
  결정론적으로 렌더링되는 사람용 projection이며 모델 출력이 아니라고 고정한다.
- 두 파일의 내용을 고쳐도 실험 동작은 바뀌지 않고 `prompt_bundle_sha256`만
  바뀐다. 수정했다면 재봉인이 필요하다는 뜻일 뿐, 동작 변경으로 보고하지 않는다.
- `belief_summary` renderer는 `twinmarket_kr/belief_projection.py` 한 곳뿐이다.
  무호출 LTB₀(`deterministic_initial_belief`)과 post-fill LTB
  (`llm/belief.render_ltb_human_log`)가 같은 함수를 쓴다. 어느 한쪽에 별도
  문장을 새로 쓰지 않는다. 회귀 테스트는
  `tests/test_clean_experiment_base.py::…::test_initial_belief_summary_is_the_six_dimension_projection`이다.
  LTB₀의 `view_change`는 parent가 없으므로 before/after dict가 아니라
  `"initial"` 문자열을 유지한다.
- 이 renderer를 바꾸면 base DB의 turn-zero 행이 바뀌므로
  `scripts/04_build_experiment_base.py --force` 재실행이 필요하다. 다만
  `belief_summary`는 LTB scientific digest에서 제외돼 있어 `human_log_sha256`만
  달라지고 StudySpec 재봉인 대상은 아니다.

#### community select/react 출력 예시는 비어 있지 않게 유지한다

`twinmarket_kr/community/reading.py`의 `SELECT_OUTPUT_CONTRACT`와
`REACT_OUTPUT_CONTRACT`가 프롬프트 끝에 주입하는 JSON 예시는 **항상 실제 값이
들어 있는 형태**여야 한다. 빈 배열을 유일한 예시로 보여주지 않는다.

- 0720 원본 `prompts/community_reading.txt`는 `[post_id, ...]`와
  `[{"post_id": int, "reaction": ...}, ...]`처럼 비어 있지 않은 예시였다.
  v2 `prompts/common/` 단계에서 양쪽 다 `[]`로 바뀌었고, 그 변경은
  `PROMPT_LINEAGE.md`에 기록되지 않은 드리프트였다. 2026-07-30에 원래 방향으로
  되돌렸다.
- `reactions: []`는 편향 이전에 **항상 오답**이다. `_validate_reactions`가
  `set(candidate_ids) != available`을 `reactions:must_cover_every_read_post`로
  거부하고, react 단계는 본문이 있을 때만 호출된다.
- `selected_post_ids: []`는 `EXPERIMENT_DESIGN.md` 8.2절상 유효한 결과다.
  따라서 "빈 배열도 유효"라는 문장은 산문으로 유지하되, 예시는 비우지 않는다.
  선택을 강제하는 문구도 넣지 않는다.
- 선택이 0으로 쏠리면 반응이 0이 되고, Best 정렬이
  `ORDER BY score DESC, like_count DESC, post_id ASC`에서 사실상 `post_id` 순으로
  붕괴한다. ON arm이 오류 없이 조용히 약해지므로 로그로는 잡히지 않는다.
- 실제 모델의 선택률은 offline stub으로 측정할 수 없다. stub은
  `post_ids[:limit]`을 결정론적으로 반환한다. 이 항목은 live canary에서 실제
  `selected_post_ids` 분포로 확인한다.
- 회귀 테스트는
  `tests/test_community_policy.py::CommunityReadingOutputContractTests`다.
  프롬프트 파일이 모드별 스키마를 직접 갖지 않는지, 두 계약의 예시가 비어 있지
  않은지, 렌더된 프롬프트에 다른 모드의 키나 미치환 슬롯이 남지 않는지를 본다.

### 다음 세션의 P0 작업 순서

다음 순서를 건너뛰지 않는다.

1. 먼저 이 파일 전체와 `git status --short`, `git diff --stat`을 읽는다.
2. production/test 참조를 `rg`로 증명한 뒤 아래의 남은 중복·우회 경로를
   제거한다.
   - `twinmarket_kr/agents/news_agent.py`에 남은 구 pkl/CSV/JSON-split
     재샘플러와 `NewsAgent.__init__`의 봉인 입력 우회 branch
   - 참조되지 않는 `scripts/prepare_samsung_news.py`,
     `scripts/reprocess_samsung_summaries.py`,
     `scripts/refresh_event_schedule.py`
   - `twinmarket_kr/db/schema.py`의 죽은 `paper_*`,
     `PAPER_SIM_DDLS`, `PAPER_RUNTIME_TABLES`
   - `twinmarket_kr/db/connection.py`의 `init_paper_sim_db` 및 RN 전용
     migration
3. 위 코드를 이름만 바꾼 compatibility shim으로 남기지 않는다. 다만 기존
   결과를 읽는 데 실제로 쓰이는 reader/exporter는 production write path와
   분리된 경우에만 유지한다.
4. `analysis/current_experiment_review/`, `validation/outputs/`,
   `validation/tmp/`처럼 과거 결과인 디렉터리는 삭제하지 말고 현재 runtime
   입력과 분리된 `archive/` 아래 history로 격리할지 참조를 확인한 뒤
   결정한다.
5. 네 정본 문서가 최종 CLI, 파일명, artifact schema와 일치하는지 다시
   대조한다. 삭제한 06/07/RN runner를 현재 실행 명령으로 되살리지 않는다.
6. 코드와 prompt가 완전히 고정된 뒤에만 StudySpec을 재봉인한다. 먼저 임시
   output으로 sealer와 validator를 검증하고, 그 다음 공식
   `preparation/rn_ab_sealed_v1/`을 갱신한다. 위 뉴스 두 hash가 그대로인지
   반드시 재확인한다.
7. 전체 무과금 테스트, 정적 검사, OFF/ON 중단·재개 smoke, pair evaluator,
   PDF report 생성·열람 검증을 마친 뒤 최종 적대적 감사를 한다.

### 현재 알려진 미완료/NO-GO

- 공식 `preparation/rn_ab_sealed_v1/study_spec.json`은 최신 instrument/schema,
  prompt/persona hash 변경 전 봉인이어서 최종 재봉인이 필요하다.
- 따라서 현재 상태는 live canary와 본실험에 대해 `NO-GO`다.
- 관련 표적 테스트는 여러 묶음에서 통과했지만, 마지막 파일 정리와 공식
  재봉인 이후 전체 test suite는 아직 실행하지 않았다.
- `twinmarket_kr/db/schema.py`와 `connection.py`에는 canonical SIM schema와
  별개인 RN `paper_*` 중복 경로가 아직 남아 있다.
- `news_agent.py`의 구 입력 loader는 canonical 05가 호출하지 않지만 잘못
  호출하면 봉인 입력을 우회할 수 있다.
- 뉴스 기사 본문의 역사적 수정 시점 provenance 문제와 정확히 15:30인 기사
  경계는 데이터 담당자와 후속 확인할 항목이다. 현재 뉴스 bundle이나
  5/3/2 선택 정책을 이 문제 때문에 변경하지 않는다.

### 검증 시 최소 명령

프로젝트 루트에서 실행한다. 먼저 각 CLI의 `--help`로 현재 인자를 확인한다.

```bash
python scripts/02_prepare_news.py
python scripts/03_load_stock_data.py
python scripts/04_build_experiment_base.py --help
python scripts/05_run_simulation.py --help
python scripts/08_run_six_conditions.py --help
python scripts/15_seal_study.py --help
python scripts/99_validate.py --help
python -m pytest -q
git diff --check
python -m compileall -q twinmarket_kr scripts tests
```

테스트용 LLM은 반드시 offline mode를 사용한다. `--allow-paid-api`와 live
canary는 사용자 승인 없이는 실행하지 않는다. smoke 결과는 명시적 임시
run-dir에 만들고 `outputs/current` 또는 latest glob으로 찾지 않는다.

### 다음 세션에서 사용자가 보낼 한 줄

다음 문장만 받아도 즉시 이어갈 수 있어야 한다.

> `2026_AICP_ver3.0/AGENTS.md를 처음부터 끝까지 읽고, "다음 세션 즉시 재개용 인수인계"의 P0 1번부터 시작해 전체 리팩터링·재봉인·무과금 검증을 끝까지 완료해. 뉴스 5/3/2 무보충 정책과 봉인 bundle은 건드리지 말고, 유료 API·commit·push는 하지 마.`
