# AICP 3.0 실험 설계 정본

> 상태: **승인된 설계 계약, 무과금 P0 리팩터링·재봉인·검증 PASS · live 실행 전**
>
> 현재 번호형 엔진에 핵심 정책이 연결되어 있고, 현 code·prompt·persona projection
> 재봉인, 전체 무과금 회귀, 1 agent/45거래일 OFF/ON 실제 중단·재개 offline 검증,
> PDF fixture QA를 마쳤다. 아래
> 정책이 적혀 있다는 사실만으로 clean/frozen live 승인, live canary 또는 본실험
> 완료를 의미하지 않는다.

## 1. 연구 목적

이 연구는 시장가격을 생성하거나 투자 성과를 최적화하는 연구가 아니다. 실제
가격을 외생적으로 고정한 상태에서 LLM 개인투자자 집단의 매수·매도 행동이 실제
개인투자자 행동과 어느 정도 닮는지, 그리고 커뮤니티 정보환경이 그 행동과
신념에 어떤 차이를 만드는지 측정한다.

현재 baseline의 핵심 질문은 다음과 같다.

1. 실제 뉴스만 본 에이전트 집단의 일별 거래 방향은 실제 삼성전자 개인투자자
   순거래 방향과 얼마나 일치하는가?
2. 같은 cohort·가격·뉴스·모델·seed에서 커뮤니티 ON과 OFF의 거래, 신념,
   손익, 분산은 어떻게 달라지는가?
3. 커뮤니티 원문이 실제로 노출된 경로와 이후 STB·decision·fill의 변화가
   provenance로 연결되는가?

가격 경로 재현, 사람 개인의 주문 예측, 투자 권고는 연구 주장에 포함하지 않는다.

## 2. 현재 baseline study profile

아래 값은 현재 삼성전자 baseline profile의 승인값이다. 공통 엔진에 흩어진
상수로 복제하지 않고 StudySpec과 봉인 registry에서 관리한다.

| 축 | 현재 값 |
| --- | --- |
| instrument | 삼성전자 `005930`, 한국 거래소 가격·달력 |
| 기간 | 2026-02-27 ~ 2026-05-04, 45거래일 |
| decision event | 거래일별 AM·PM, 총 90 event |
| burn-in | 첫 3거래일 |
| 주 분석 | burn-in 이후 42거래일 |
| cohort | 동일한 고정 100명 |
| depth | D0=30, D1=55, D2=15 |
| community 선택·반응 가능 | D1+D2=70명 |
| community 게시 판단 가능 | 모든 D1/D2 70명 |
| 초기 현금 | 1억 원 90명, 10억 원 10명 |
| study seed | 2 |
| 거래 선택 | `buy` 또는 `sell`; `hold` 없음 |
| 체결 | AM은 시가, PM은 종가의 전량 체결 정책 |
| 1회 매수 상한 | 가용 현금의 50% |
| 수수료·거래세 | `commission_rate=0`, `sell_tax_rate=0`, 모든 `fee_amount=0` |
| 모델·provider | `qwen/qwen3.5-flash-02-23`, `alibaba`, fallback 없음 |
| 동시 호출 상한 | 조건별 8 |
| reasoning | strict off |

종목, 날짜, agent 수를 바꾸는 후속 study는 같은 실행 엔진에서 별도의 봉인
profile을 만든다. 삼성전자·45일·100명 값을 코드에 새로 하드코딩하거나 현재
baseline의 hash를 수정해 재사용하지 않는다.

## 3. 조건과 비교 원칙

현재 baseline은 두 조건만 실행한다.

| 조건 | 뉴스 | 커뮤니티 |
| --- | --- | --- |
| `RN_COMM_OFF` | `real_only` | off |
| `RN_COMM_ON` | `real_only` | on |

두 조건의 resolved manifest 차이는 `community_mode` 하나여야 한다. cohort,
날짜, 가격, 뉴스 ID·payload·부족 event, prompt, model, seed, 거래 규칙은
동일하게 고정한다. Community OFF에서는 게시, 선택, 본문 읽기, 반응, Best
전달, community claim이 모두 0이어야 한다.

향후 뉴스 처치는 같은 StudySpec 축으로만 추가한다.

- `real_only`
- `real_plus_bullish_fake`
- `real_plus_bearish_fake`

가짜뉴스 조건을 위해 코드를 복사하거나 새 runner를 만들지 않는다. 각 event의
실제뉴스는 목표 10개를 먼저 유지하고 fake 1개를 추가한다. 안전한 실제뉴스가
10개 미만인 event는 baseline과 동일한 실제 전달 묶음과 shortage 기록을
그대로 사용한다. fake 여부, synthetic ID, 생성 prompt, bullish/bearish
polarity는 에이전트가 보는 입력에 노출하지 않고 사후 분석 registry에만 둔다.

## 4. 최신 실제뉴스 정본

현재 실행 입력은 `preparation/rn_ab_sealed_v1/news.json` 하나다. 이 파일은
`sujin_0727`의 Git 기준 커밋에 포함되어 있고 다음 특성을 가진다.

- 종목: `005930`
- 등록 event: 90
- event별 실제뉴스 목표: 10
- event별 카테고리 목표: 종목 5·섹터 3·경제 2
- 봉인 article/slot: 760
- 목표 미달을 명시적으로 수락한 event: 59
- fake registry ID와 payload hash: 0
- canonical bundle hash:
  `a6fb61900c27071b2a79781478592d99d914482fbba0f4ecaafa73edcb8ab707`

shortage는 오류를 숨기는 기본값이 아니다. 안전한 고유 기사만 전달하고 아래를
event별로 기록한 뒤 실험은 계속한다.

- 목표 수, 선택 가능한 안전 기사 수, 직렬화 수, 실제 전달 수, 부족 수
- 순서가 고정된 article ID와 payload hash
- selection/review 사유와 coverage 상태
- 두 조건이 같은 shortage bundle을 썼다는 pair 검증

부족분을 미래 기사, 중복 기사, 합성 기사, 조건별 다른 기사로 채우지 않는다.
한 카테고리의 초과 기사로 다른 카테고리의 부족분도 채우지 않는다. 예를 들어
종목 4·섹터 4·경제 2가 가능해도 선택은 4·3·2, 총 9개이며 shortage로 남긴다.
전체 45일 분석과 함께 complete-news-only 민감도 분석, shortage event 목록,
고정 denominator를 보고한다.

`archive/legacy_inputs/rn_ab_source_candidate_v1/input_candidates/`, legacy 뉴스 CSV,
과거 `outputs/` 결과는 현재 입력이 아니다.

## 5. 시간과 정보 누수 경계

각 decision event는 그 시점에 이용 가능했던 정보만 사용한다.

- AM: 승인된 AM cutoff 이전 정보와 전일 확정 상태
- PM: 승인된 PM cutoff 이전 정보와 당일 허용 상태
- 현재 fill: 같은 event의 STB·analysis·decision에는 보이지 않고 체결 후
  LTB에만 한 번 반영
- 가격 성과: 관찰 시점이 지난 과거 episode만 사용
- 실제 개인수급 target: evaluator 전용이며 agent prompt와 memory에 노출 금지

preflight는 종목, 거래일, agent ID, 가격, 기사 `published_at`·`observed_at`,
payload version을 exact-match로 검사한다. 미래 종가·고가·저가·개인수급,
미래 뉴스, 미래 agent 상태, fake 정답·polarity가 보이면 fail-closed다.
종목·날짜·agent registry가 어긋났을 때 교집합만 사용해 조용히 계속하지 않는다.

현재 Sujin 뉴스 bundle의 노출 가능 시각은
`effective_at = max(published_at, last_modified_at)`이다. 준비 스크립트는 이
값을 `observed_at`에 저장하므로, 현재 필드명은 실제 재크롤 관측시각이 아니라
발행·수정 메타데이터로 정의한 노출 기준을 뜻한다. AM/PM slot과 D2 후보는
`published_at`, `observed_at`, `last_modified_at`, 최초 visible slot이 모두
cutoff를 넘지 않을 때만 허용한다. 논문에서는 이를 실제 과거 crawl log라고
표현하지 않는다.

## 6. event state machine

에이전트별 event 순서는 다음과 같다.

1. 관찰 시점이 지난 과거 거래 성과와 다음 AM community delivery를 확정한다.
2. 현재 뉴스와 실제 노출된 community 근거로 STB를 정확히 한 번 생성한다.
3. 이전 LTB와 현재 STB를 서로 분리된 입력 block으로 analysis에 제공한다.
4. 같은 두 belief와 현재 실행 상태로 `decision`을 생성한다.
5. 현금·보유량·가격 제약을 적용해 canonical `fill`을 확정한다.
6. 이전 LTB, 현재 STB, 실제 decision/fill episode, 사용 가능한 과거 성과로
   post-fill LTB를 정확히 한 번 생성한다.
7. PM event이면 post-fill LTB 이후 community phase를 실행한다.
8. 검증된 stage 결과를 하나의 원자적 commit으로 journal과 DB에 반영한다.

`decision`은 LLM이 선택한 요청 행동과 수량이고 `fill`은 제약 검사를 통과한 뒤
실제로 반영된 체결 사실이다. 이번 baseline에서는 실행 가능한 `buy` 또는
`sell`만 허용하고, 요청 수량 전부를 공시가격에 체결한다. 따라서 최종
scientific row에서는 `requested_quantity = filled_quantity`, `fee=0`이어야
한다. 다만 잘못된 action·수량을 낸 validation attempt와 재요청 이력은
`decision` 전 단계의 API/validation 로그에 남긴다. 이를 최종 fill의 부분체결
또는 미체결로 해석하지 않는다. 로그·분석·LTB 어디에서도 decision과 fill
객체를 같은 것으로 취급하지 않는다.

## 7. STB·LTB 계약

STB와 LTB는 기존 여섯 차원을 유지한다.

- STB: 현재 event의 뉴스와 실제 전달된 community claim에 대한 단기 관점
- LTB: 이전 LTB, 현재 STB, 실제 decision/fill, 관찰 가능한 과거 성과를
  재귀적으로 통합한 다음 event용 장기 관점

production prompt도 분리되어 있다.

| 단계 | prompt | 호출 위치 |
| --- | --- | --- |
| STB | `prompts/update_short_term_belief.txt` | 현재 evidence를 만든 뒤 analysis 전 |
| post-fill LTB | `prompts/update_long_term_belief.txt` | 실제 fill과 due outcome을 확정한 뒤 |

`twinmarket_kr/study_spec.py`는 두 파일을 서로 다른 core stage로 봉인하고,
`twinmarket_kr/llm/belief.py`가 각각 다른 typed payload를 구성한다.

analysis와 decision은 이전 LTB 6차원과 현재 STB 6차원을 모두 받되 서로
합쳐진 자유문장 summary를 정본으로 사용하지 않는다. `belief_summary`는 사람용
projection일 뿐 다음 거래 입력이 아니다. `view_change`는 post 작성의 제한된
private context에만 허용하며 analysis·decision의 숨은 입력으로 사용하지 않는다.

각 STB, analysis, decision, fill, LTB에는 agent ID, condition, event ID,
source ID, logical call ID, schema version, request/response hash, commit 상태가
연결되어야 한다. 동일 logical key의 다른 payload는 재실행하지 않고 중단한다.

### 7.1 기존 여섯 차원의 의미

STB와 LTB는 시간 역할만 다르고 아래 스키마와 글자 제한은 같다. FUSE의 여섯
평가 지표나 별도의 memory taxonomy로 바꾸지 않는다.

| 차원 | canonical 의미 | 현재 제한 | STB에서의 역할 | LTB에서의 역할 |
| --- | --- | ---: | --- | --- |
| `dim_1` | 향후 약 1개월 삼성전자 주가 방향 전망 | 150자 | 현재 event 정보가 중기 방향에 주는 단기 시사점 | 과거 관점과 현재 시사점을 재귀 통합한 다음 event용 전망 |
| `dim_2` | 현재 valuation이 싸다·비싸다·적정하다는 관점과 근거 | 150자 | 현재 정보가 valuation에 주는 시사점 | 누적 valuation 관점 |
| `dim_3` | 금리·환율·경기·반도체 업황 등 거시·산업 환경 판단 | 150자 | 현재 정보가 환경 판단에 주는 시사점 | 누적 거시·산업 판단 |
| `dim_4` | 삼성전자를 둘러싼 시장 심리와 투자자 분위기 | 150자 | 현재 뉴스·허용 community에서 감지한 심리 | 시간에 걸쳐 통합한 심리 판단 |
| `dim_5` | 뉴스·community를 접하고 얻은 개인적 해석과 깨달음 | 150자 | 이번 event 정보의 persona-conditioned 해석 | 누적 정보 해석 원칙과 관점 |
| `dim_6` | 자신의 최근 투자 판단·위험관리 능력에 대한 성찰 | 150자 | 과거 성과 없이 현재 정보 한계와 주의점만 표현 | 현재 fill과 도래한 next-turn/H1/H5 결과를 반영한 누적 자기평가 |

한도의 정본은 `config.BELIEF_LIMITS`(전 차원 150자)이고 봉인·검증이 모두 이를
읽는다. STB `dim_6`는 `정보 한계:`와 `주의점:` 두 표기를 반드시 포함해야 하며
(validator 강제), 이는 과거 성과 회상 없이 현재 정보의 한계만 쓰게 하는 장치다.
LTB 재서술 규칙은 "여섯 차원 전부가 직전 LTB와 verbatim 동일하면 거부"뿐이다.
차원별 문장 유지는 정당하다 — 관점이 안 변한 차원에 새 표현을 강제하면 임베딩
기반 deviation 측정에 억지 패러프레이즈 노이즈가 깔리기 때문이다. 이 규칙은
생성 경계(`llm/belief.py`)와 저장 경계(`memory_agent.save_post_fill_ltb`)에
동일하게 있고, 한쪽만 고치면 유료 실행이 저장 단계에서 죽는다.

`dim_1`은 STB에서도 당일 예측으로 축소하지 않는다. “short-term”은 belief의
입력 수명과 갱신 역할이 짧다는 뜻이며, 차원 자체의 전망 horizon은 약 1개월로
유지한다. `dim_6` 역시 STB가 볼 수 없는 과거 체결 성패를 만들어내지 않는다.
실제 거래 성찰은 가격 관찰 gate를 통과한 outcome이 post-fill LTB에 들어갈 때만
발생한다.

### 7.2 한 event에서의 생성·사용 순서

한 agent의 event `t`에서 causal 순서는 다음 식으로 고정한다.

```text
CurrentEvidence_t + frozen Persona
  -> STB_t (6D)

LTB_(t-1) (6D) + STB_t (6D) + Market/Portfolio constraints
  -> Analysis_t
  -> Decision_t
  -> deterministic full Fill_t

LTB_(t-1) (6D) + STB_t (6D) + Decision/Fill_t
  + outcomes whose available_from_event <= t
  -> LTB_t (6D, next-event-visible)
```

즉 `LTB_(t-1) + STB_t`를 문자열로 붙여 곧바로 LTB를 만드는 것이 아니다.
두 belief를 분리 입력으로 받은 decision-making process가 먼저 있고, 실제
`fill_t`가 확정된 뒤에야 다음 event용 `LTB_t`를 만든다. 같은 event의
analysis/decision이 방금 생성한 `LTB_t`를 다시 읽는 순환은 금지한다.

### 7.3 evidence와 사람용 projection

- STB의 `dimension_evidence`는 현재 event에 실제 노출된 news, D2 search,
  full-body community claim ID만 참조한다.
- LTB의 `integration_evidence`는 각 차원의 STB 근거 부분집합을 유지한다.
  과거 가격 outcome ID는 `dim_6`에만 추가할 수 있다.
- 이번 event의 decision/fill은 구조화된 필수 context이지만 support count를
  부풀리는 evidence ID로 만들지 않는다.
- `belief_summary`는 저장된 여섯 차원에서 결정론적으로 렌더링하는 사람용
  로그다. 다음 STB·analysis·decision 입력으로 넘기지 않는다.
- `view_change`는 parent LTB와 새 LTB의 차원별 before/after를 결정론적으로
  렌더링한다. post-fill community 글 생성의 제한된 private context와 사후
  분석에만 사용한다.

## 8. Community 정책

### 8.1 권한

| Depth | 쓰기 | 선택 읽기·반응 | 다음 AM Best |
| --- | --- | --- | --- |
| D0 | 불가 | 불가 | 자기 글 문제가 없는 Best 원문 전체, 익명 닉네임 |
| D1 | 모두 게시 판단, PM당 최대 1개 | 최대 5개 | Best 원문 전체, 익명 닉네임 |
| D2 | 모두 게시 판단, PM당 최대 1개 | 최대 5개 | D1 정보 + 작성자의 PM 시점 동결 profile |

D1+D2 70명은 모두 선택·반응 및 작성 후보이며, 게시 여부는 각자 판단
판단을 한다. 게시를 강제하지 않는다. D0는 게시, 후보 선택, 반응을 하지 않지만
다음 AM Best의 **본문 전체**를 받는다.

작성자 평판 badge는 두지 않는다. legacy 동적 badge 3종은 임계값 없는 상위
20% 배정과 초기자본 의존성 때문에 처치를 교란하는 미봉인 자유 변수였고,
계산·저장·노출을 모두 제거했다. 근거는 `ARCHITECTURE.md` §12.9에 있다.
따라서 저자 쪽 추가 정보는 D2 동결 profile 하나뿐이며 D1은 익명 닉네임만 본다.

D2의 동결 profile은 해당 PM의 체결까지 반영한 후보 보드 시점 snapshot이며
포트폴리오 요약과 최근 3건 체결까지만 포함한다. 글이 그날의 거래를 이야기하는
글이므로 같은 PM의 체결을 포함하는 것이 정본이다. private belief, stable
agent ID, 미래 상태는 노출하지 않으며 다음 AM에 다시 조회하지 않는다.

### 8.2 게시와 읽기

- 게시글 본문은 글당 최대 500자다.
- 500자는 통과하고 501자는 서버가 거부한다.
- 의미를 바꿀 수 있는 자동·무음 잘라내기는 하지 않는다.
- D1/D2는 별도 게시 권한 속성 없이 모두 게시 판단을 한다.
  artifact의 `can_post_community` 값은 과거 결과 reader 호환을 위해 남지만,
  별도 persona 속성이 아니라 depth에서 결정론적으로 파생된다(D0=0, D1/D2=1).
- 권한이 있는 agent는 한 PM에 최대 한 글만 쓴다. `community_posts`의
  unique index `(agent_id, date)`가 이를 DB에서 강제한다.
- 후보 보드에는 익명 닉네임·제목·post type·동결 반응 count/score만 보이고
  본문과 작성자 평판 신호는 넣지 않는다.
- 당일 게시글의 반응 count/score는 반응 전이라 대체로 0이므로, 후보 선택은
  실질적으로 제목과 post type으로 이루어진다. 이는 의도한 성질이다.
- 이 후보 노출은 `title_only`로 기록한다.
- 각 agent의 persona를 받은 LLM이 `selected_post_ids`를 반환한다.
- 선택 결과는 0개여도 유효하며 D1과 D2 모두 5개를 강제로 채우지 않는다.
- 선택한 글만 원문 전체를 전달하고 `full_body`로 기록한다.
- 반응은 `like`, `unlike`, `none` 중 하나다.
- 같은 PM의 모든 독자는 같은 동결 후보 보드를 본다. 읽는 동안 바뀌는
  실시간 인기 피드백은 없다.

### 8.3 Best

기존 Best 규칙을 유지한다.

```text
score = like_count - unlike_count
```

score 내림차순, like 수 내림차순, 기존 결정론적 tie-break 순으로 전역 순위를
정하고 최대 5개를 고른다. 글이 5개보다 적으면 있는 글만 사용하며 양수 점수
필터나 forced posting은 추가하지 않는다.

다음 거래일 AM에는 전역 Best의 제목뿐 아니라 동결된 원문 전체를 전달한다.
작성자에게 자기 글은 전달하지 않고 그 자리를 6위 글로 채우지 않는다. 따라서
전역 Top 5 작성자는 최대 4개만 받을 수 있다. 같은 글을 전날 선택해 읽었고
Best로도 받는 경우 본문은 한 번만 직렬화하되 `selected_full_body_replay`와
`best_full_body` 두 관계를 모두 남긴다. 두 관계는 원장 라벨에 그치지 않고
claim이 인용할 수 있는 근거 ID로 함께 등록한다. 본문 1회 직렬화 때문에 어느
한 관계로만 인용이 제한되면 안 된다.

마지막 PM의 Best는 다음 AM이 없으므로 `right_censored`로 기록한다.
예정 audience, 실제 delivery, self-exclusion, no-backfill을 validator가
reader별로 재현할 수 있어야 한다.

### 8.4 community provenance

분석 원장에는 최소한 다음 연결이 있어야 한다.

- agent, condition, source event, delivery event, post
- `title_only` 또는 `full_body`
- selected 여부, reaction, Best 여부와 rank
- body hash와 동결 profile hash
- self-exclusion과 예정·실제 delivery 수
- community interpretation claim과 실제 source ID

미선택 `title_only` 글은 분석용 노출로만 남기고 다음 AM claim이나 STB 근거로
사용하지 않는다. 이 규칙은 claim 화이트리스트뿐 아니라 단계 진입 조건에도
적용한다. full-body 노출이 하나도 없는 agent에게는 community interpretation
단계를 실행하지 않는다. 커뮤니티 글은 검증되지 않은 투자자 발언이며 실제 거래
사실은 canonical fill ledger만 신뢰한다.

### 8.5 claim 인용 계약 (번호 기반, 2026-07-30~)

claim의 `supporting_quote`는 모델의 자유 인용이 아니라 번호 참조로 만든다.

1. full-body로 노출된 글의 제목과 본문 문장에 `[인용 N]` 번호를 부여해
   quotable registry를 만든다. 문장 분리는 `.!?…` 종결부호와 줄바꿈 기준이다.
2. 모델은 `supporting_quote_ref`(정수 N)만 출력한다. 인용문 텍스트를 직접
   쓰지 않는다.
3. `materialize_claim_quotes`가 N을 registry의 원문 문장으로 치환해
   `supporting_quote`로 저장한다. 인용의 축자 일치가 **구조적으로** 보장되며,
   과거 자유 인용 방식에서 반복되던 공백 변형·짜깁기·오귀속 거부가 사라진다.
4. 해당 문장의 출처 노출 ID가 `source_exposure_ids`에 없으면 자동 보충한다.
5. validator 버전은 `community-thinking-validator-v4`다.

연구자는 저장된 `supporting_quote_ref`와 `supporting_quote`를 원문과 대조해
에이전트가 실제로 무엇을 읽고 인용했는지 사후 검증할 수 있다.

## 9. LLM 호출 정책

논문 경로의 모든 physical HTTP request는 최종 request body에 아래 정책을
가져야 한다.

```json
{
  "reasoning": {
    "effort": "none",
    "exclude": true
  }
}
```

봉인된 모델과 provider 하나만 허용하고 fallback을 끈다. `exclude=true`만
설정해 reasoning을 숨기는 것은 허용되지 않는다. canary는 요청 모델·provider,
빈 reasoning field, `reasoning_tokens=0`, schema 통과를 실제 telemetry로
증명해야 한다. 누락, nonzero reasoning token, provider/model drift, offline
stub은 paper run에서 fail-closed다.

logical call과 physical attempt를 분리해 retry 비용과 scientific row count를
혼동하지 않는다. schema 오류나 중단으로 rollback된 attempt는 최종 결과에
섞지 않고 journal에는 보존한다.

## 10. 평가와 보고

primary behavioral metric은 agent들의 일별 AM+PM gross signed fill value 방향과
실제 삼성전자 `Individuals` 일별 최종 순거래대금 방향의 비교다. AM-only와
PM-only 결과는 실제 intraday 개인수급 target이 없으므로 탐색 분석으로만
보고한다.

필수 분석 범위는 다음과 같다.

- 전체 45거래일과 burn-in 3거래일 제외 42거래일
- 방향 일치율과 class별 재현율, 항상 매수·항상 매도 baseline
- 거래 강도, turnover, 보유·현금·자산 변화
- Community ON/OFF의 paired contrast와 agent별 이질성
- STB/LTB 변화, decision/fill 차이, memory lineage
- `title_only`와 `full_body`를 분리한 community mechanism
- 전체 달력과 complete-news-only shortage 민감도
- 수수료·세금이 모두 0이라는 reconciliation
- 오류·retry·missingness·right censoring

게시글 수나 like 수만으로 community가 거래를 바꿨다고 인과적으로 단정하지
않는다. integrity 검증을 통과한 canonical 원장에서만 통계를 생성한다.

## 11. 설계 완료와 실행 완료의 구분

설계 완료는 이 문서와 봉인 StudySpec의 정책이 합의됐다는 뜻이다. 실행 완료는
별도로 다음 증거가 모두 있어야 한다.

1. `05 -> simulation.py` 단일 경로에서 두 baseline 조건이 실행된다.
2. 신규 production import에서 `twinmarket_kr.rn_ab`가 0이다.
3. stage, community, strict-off, fee, shortage, resume 테스트가 통과한다.
4. 변경된 코드·prompt에 맞춰 입력을 재봉인했다.
5. 양 조건 canary가 같은 입력과 community만 다른 manifest를 증명한다.
6. 본실험의 canonical journal, 완료 marker, validator, report가 모두 통과한다.

현재 1~4는 공통 번호형 경로에서 무과금으로 검증했다. 현 code·prompt·persona
projection으로 StudySpec을 재봉인했고, 전체 regression suite와 official profile의
1 agent/45거래일 OFF/ON 실제 중단·재개 offline 검증을 통과했다. 5~6의 live 단계는 유료 승인 없이
실행하지 않았으므로, canary와 본실험은 **NO-GO**이며 완료를 주장하지 않는다.

## 12. 아키텍처 원칙

최종 구조는 “두 엔진을 연결한 세 번째 엔진”이 아니라 기존 번호형 엔진 자체에
검증된 기능을 흡수한 하나의 구조다.

1. `StudySpec -> ResolvedStudyManifest -> RunContext`가 모든 과학적 설정의
   단일 경로다.
2. `scripts/05_run_simulation.py -> twinmarket_kr/simulation.py`만 production
   orchestration을 시작한다.
3. event 단계는 typed input과 typed result를 주고받으며 LLM task가 DB를
   임의로 부분 갱신하지 않는다.
4. DB/journal이 원장이고 CSV·JSONL·PDF는 검증된 파생물이다.
5. 날짜, 종목, cohort, 처치를 바꿔도 모듈을 복사하지 않고 profile만 바꾼다.
6. 과거 runtime을 호환 이름이나 archive 실행기로 되살리지 않는다.
7. 입력, prompt, 실행 상태, 결과는 run ID와 manifest hash로 묶고
   `outputs/current`나 최신 파일 glob으로 추측하지 않는다.

목표 의존 방향은 다음과 같다.

```text
Authored StudySpec + sealed registries
  -> resolver
     -> immutable ResolvedStudyManifest
        -> RunContext
           -> event scheduler
              -> evidence projection
                 -> STB -> analysis -> decision -> fill -> post-fill LTB
                    -> PM community
                       -> atomic commit + response journal
                          -> integrity validator
                             -> deterministic CSV/JSONL export
                                -> analysis/report
```

아래쪽 모듈이 위쪽 CLI나 전역 `config.py`를 역참조하지 않는다. `config.py`는
일반 개발 기본값을 둘 수 있지만 논문용 종목, 날짜, agent, news treatment,
community mode, model, 수수료를 결정하는 정본이 아니다.

## 13. 번호형 00→05 호출 그래프

사용자-facing 실행은 기존 번호형 흐름을 유지한다.

```text
00 market source
  -> 01 persona/cohort
     -> 13 provenance bind -> 14 news seal -> 15 study seal
        -> 02 sealed news linkage validation (read-only)
           -> 03 event price/calendar validation or explicit load
           -> 04 clean base + LTB₀
              -> 05 preflight/run/resume/finalize
```

| 번호 | 책임 | 현재 파일 |
| --- | --- | --- |
| `00` | instrument별 시장 원천 준비 | `scripts/00_fetch_market_data.py` |
| `01` | 현재 baseline cohort/persona/depth 읽기 전용 검증; 명시할 때만 별도 DB에 새 cohort 생성 | `scripts/01_build_persona.py` |
| `02` | sealed news·달력·가격·StudySpec 연결을 읽기 전용으로 검증한다. write 경로는 없다. | `scripts/02_prepare_news.py` |
| `03` | 기본은 현재 `StockData` 읽기 전용 검증; 적재는 명시적 `--write`와 source/target 경로 | `scripts/03_load_stock_data.py` |
| `04` | runtime row가 없는 clean base, 초기 portfolio, 결정론적 LTB₀ 생성 | `scripts/04_build_experiment_base.py` |
| `05` | StudySpec 검증, run signature, 실행, event checkpoint/resume, finalization | `scripts/05_run_simulation.py` |

`02a_init_memory.py`, 별도 초기 belief 생성기와 checkpoint runner의 유효한
동작은 `04`와 `05`에 흡수돼 제거됐다. 뉴스 입력을 새로 만들 때 쓰는
`13_bind_news_provenance.py`, `14_seal_news_bundle.py`,
`15_seal_study.py`는 simulation runner가 아니라 versioned input-build
도구다. 새 뉴스 원천을 만드는 write 작업은 `13`과 `14`의 책임이며 `02`의
책임이 아니다. 현재 baseline은 이미 봉인된 Sujin bundle을 다시
표본추출하지 않는다.

## 14. 모듈 책임

### 14.1 설정과 입력

| 구성요소 | 최종 책임 | 현재 위치 |
| --- | --- | --- |
| StudySpec | 사람이 승인한 instrument, treatment, cohort, schedule, memory, trade, model, evaluation 정책 | `twinmarket_kr/study_spec.py` |
| resolver | authored spec과 registry hash를 검증해 계산값을 포함한 불변 manifest 생성 | `twinmarket_kr/study_spec.py`, `scripts/15_seal_study.py` |
| RunContext | manifest, run ID, 조건, 경로, registry reader, 정책을 dependency로 전달 | `scripts/05_run_simulation.py`, `twinmarket_kr/experiment_runtime.py` |
| EventSchedule | 45일·90 event의 순서, AM/PM, cutoff, reference price ID 제공 | `calendar.json`, `stage_inputs.json`, `prices.json` |
| cohort registry | agent ID, 구조화 persona, depth, depth에서 파생된 게시 가능 상태, 초기 현금 exact map | `cohort.json`, `persona_projection.json`, `data/sys_100_ko_ver5.db` |
| sealed news registry | event별 순서가 고정된 실제뉴스, payload hash, shortage | `news.json` |
| prompt registry | production prompt hash와 stage schema를 고정 | 최상위 `prompts/`; run bundle의 사본은 재현 artifact |

### 14.2 실행

| 모듈 | 최종 책임 |
| --- | --- |
| `scripts/05_run_simulation.py` | 인자 파싱, 실행 전 StudySpec·입력·유료 승인 gate, run signature, event checkpoint/resume/finalize |
| `twinmarket_kr/simulation.py` | event 순회, agent barrier, fill, post-fill LTB, PM community, outcome finalization |
| `twinmarket_kr/core/daily_cycle.py` | 한 agent-event의 현재 evidence→STB→analysis→decision 조합 |
| `twinmarket_kr/core/collect_context.py` | 해당 event에 합법적으로 보이는 이전 LTB, 현재 execution state, 전달 완료 exposure만 수집 |
| `twinmarket_kr/agents/news_agent.py` | sealed event ID 직접 조회, depth projection, D2 검색 후보 경계, news exposure provenance |
| `twinmarket_kr/agents/memory_agent.py` | STB/LTB 상태, transition, outcome consumption, lineage read/write |
| `twinmarket_kr/agents/exchange_agent.py` | decision 검증, 현금·보유량 제약, reference price 체결, fee=0 보장 |
| `twinmarket_kr/community/` | post-fill 게시, 동결 보드, 선택, 본문 전달, 반응, Best, next-AM 전달 |
| `twinmarket_kr/llm/` | strict schema 호출, pinned provider/model, reasoning-off request와 telemetry |

### 14.3 저장·검증·보고

| 모듈 | 최종 책임 |
| --- | --- |
| `twinmarket_kr/db/schema.py` | 공통 scientific schema와 migration |
| `twinmarket_kr/llm/response_journal.py` | logical response와 physical attempt 분리, exact request replay, commit 상태 |
| checkpoint coordinator | event 전 snapshot, durable commit decision, rollback/recovery |
| `twinmarket_kr/run_logger.py` | DB 사실을 분석용 trace/export 형식으로 투영; 두 번째 원장 역할 금지 |
| `twinmarket_kr/run_integrity.py` | manifest, DB key set, hash, fee, exposure, delivery, resume equality 검증 |
| `validation/` | frozen target과 canonical fill ledger의 행동 방향 비교 |
| report scripts | integrity PASS 산출물만 읽어 CSV·PDF 생성 |

과거 RN 전용 runner, phase coordinator, journal, spec, validator에서 필요한
동작은 위 공통 책임으로 이식됐고 전용 runtime은 제거됐다.

## 15. sealed news와 depth projection 데이터 흐름

```text
news.json
  -> bundle/file/payload/slot hash 검증
  -> EventSchedule의 event_id exact lookup
  -> shortage coverage record 결합
  -> cohort.json의 agent.news_depth exact lookup
  -> agent-visible projection
       D0: article_id, payload hash, title, published_at
       D1: D0 필드 + sealed summary
       D2: D1 필드 + 허용된 최근 검색 결과
  -> exposure ID와 실제 직렬화 hash 기록
  -> STB evidence packet
```

D0 projection에는 빈 summary key조차 넣지 않는다. D1과 D2의 event feed는 같은
봉인 title+summary를 사용한다. D2 검색은 event feed를 바꾸는 것이 아니라
별도의 cutoff-safe 후보 registry에서 agent keyword로 선택한 고유 결과를
최대 5건 추가하는 권한이다. 뉴스 D2 검색 상한 5건은 커뮤니티 선택 읽기의
D1=5/D2=5와 별개다. 현재 event 기사와 중복되거나 7 calendar-day
lookback/cutoff를 넘는 결과는 거부한다.

caller가 date, depth, 기사 배열을 임의로 넘겨 registry를 우회할 수 없어야 한다.
`event_id -> sealed slot -> payload hash -> depth projection -> exposure ID`가
한 방향으로만 결정된다. fake 조건에서도 이 흐름은 같고 agent-visible
projection에서 fake metadata만 완전히 제거한다.

D2 검색의 후보 매칭은 agent keyword를 공백 단위 토큰으로 나눠 제목(2배
가중)·요약의 부분 일치로 채점하고, 0점 후보는 버린 뒤 상위 5건만 반환한다.
2026-07-31 이전 코드는 키워드 구 전체의 완전 일치를 요구해 라이브 검색이
항상 0건을 반환했다(에러 없는 조용한 오작동). belief에 유입되는 것은 검색으로
회수된 **기사**(evidence `kind=depth2_recent_search`)뿐이다. pre/post-search
사고 산출물(`depth2_flow`의 step2·step4)은 감사·분석용 로그 전용이며 어떤
프롬프트에도 재유입되지 않는다.

## 16. AM/PM state machine과 데이터 경계

한 거래일은 AM scientific turn, PM scientific turn, PM community로 구성된다.

```text
전일 PM에서 동결된 Best
  -> 당일 AM eligible reader에게 실제 delivery
  -> 과거 outcome mature
  -> AM current evidence
  -> AM STB
  -> AM analysis
  -> AM decision
  -> AM fill(open)
  -> AM post-fill LTB
  -> 과거 outcome mature
  -> PM current evidence
  -> PM STB
  -> PM analysis
  -> PM decision
  -> PM fill(close)
  -> PM post-fill LTB
  -> post/read/react/Best freeze
  -> 다음 거래일 AM delivery schedule
```

각 agent-event의 입력과 출력은 다음처럼 제한한다.

| 단계 | 읽을 수 있음 | 새로 생성 | 읽으면 안 됨 |
| --- | --- | --- | --- |
| STB | 현재 sealed news, 실제 전달된 current community claim | STB 6D와 evidence edge | 이전 LTB, portfolio, 현재 fill, 미래 가격·target |
| analysis | 이전 LTB 6D, 현재 STB 6D, 현재 시장·portfolio | typed analysis | raw community/news를 별도 우회 입력, 현재 fill |
| decision | 이전 LTB, 현재 STB, 검증된 analysis, 실행 가능 상태 | buy/sell, requested quantity | 미래 outcome, 현재 fill |
| fill | decision, reference price, pre-portfolio | actual fill, post-portfolio | LLM 재판단 |
| post-fill LTB | 이전 LTB, 현재 STB, actual decision/fill, due past outcomes | 새 LTB 6D와 transition | 아직 due가 아닌 outcome |
| PM community | 새 post-fill LTB, deterministic view change, PM fill | post/selection/reaction/Best | pre-fill belief로 쓰기, future portfolio |

한 agent 안에서는 `STB → analysis → decision`이 순서대로 진행되지만 모든
agent의 STB가 끝나기를 기다리는 전역 STB barrier나 전역 analysis barrier는
두지 않는다. agent별 chain을 조건별 concurrency 상한 안에서 병렬 실행한 뒤,
**모든 agent의 decision이 완성되는 지점에 하나의 exchange barrier**를 둔다.
그 뒤 봉인 가격으로 fill을 결정론적으로 적용하고, post-fill LTB를 병렬 생성한
뒤 agent ID 순으로 저장한다. 이 순서가 일부 agent의 fill이나 새 LTB가 다른
agent의 같은 event decision에 들어가는 것을 막는다.

## 17. canonical DB와 lineage

최종 main 엔진은 같은 사실을 두 테이블 계보에 동시에 쓰지 않는다. 아래는
통합 원장이 보존하는 논리 스키마다. 신규 writer의 core 테이블은
`simulation_*` namespace 하나이며, 과거 RN `paper_*` writer는 제거됐다.

### 17.0 reference와 run namespace

| 저장물 | PK·natural key | 경계 |
| --- | --- | --- |
| frozen agents snapshot | `agent_id` PK | 구조화 persona, depth, 초기 현금을 read-only로 제공; global DB fallback 금지 |
| `StockData` | `(date, stock_id)` 복합 PK | Instrument와 calendar에 맞는 reference price·market feature만 제공 |
| resolved manifest metadata | `manifest_sha256` | run ID, condition, schema, code/prompt/input hash를 모든 scientific row에 바인딩 |
| `portfolio_state` | `state_id` PK; agent-event unique | LTB와 별개인 실행 상태. fill의 pre/post portfolio와 exact equality |

실제 개인수급 target은 이 run DB의 agent-readable reference table에 적재하지
않는다. evaluator 전용 artifact로 격리하고 finalize 이후 validator만 읽는다.

### 17.1 핵심 scientific tables

| 테이블 | PK·자연키 | 주요 FK·책임 |
| --- | --- | --- |
| `simulation_stb_states` | `stb_id`; agent-event unique | 현재 evidence와 6D STB, input/scientific hash |
| `simulation_analyses` | `analysis_id`; agent-event unique | `source_ltb_id`, `source_stb_id`; typed analysis와 response hash |
| `simulation_decisions` | `decision_id`; agent-event unique | 이전 `ltb_id`, 현재 `stb_id`, 최종 `analysis_id` |
| `simulation_fills` | `fill_id`; agent-event unique, `decision_id` unique | `decision_id`, source LTB/STB, pre/post portfolio, actual quantity·price·fee |
| `simulation_ltb_states` | `ltb_id`; agent-event unique | `parent_ltb_id`, `source_stb_id`, `source_decision_id`, `source_fill_id`; 6D LTB |
| portfolio state | `state_id`; agent-event unique | fill의 post-portfolio와 exact reconciliation |

위 `simulation_*` 테이블은 현재 공통 writer가 실제로 사용한다.
`simulation_ltb_states` 자체가 parent/STB/decision/fill과
`integration_evidence_json`을 보존하므로 별도 transition table을 전제하지
않는다. DB는 arm별 run directory에 격리되며 `run_signature.json`,
`run_metadata.json`, checkpoint의 event 순서가 condition·manifest·event
namespace를 바인딩한다. 과거 RN `paper_*` DDL·initializer·migration은 현재
schema와 connection 경로에 남아 있지 않으며, 과거 결과는 archive와 Git 이력으로만
참조한다.

필수 lineage는 다음과 같다.

```text
current evidence -> STBₜ
(LTBₜ₋₁, STBₜ) -> analysisₜ -> decisionₜ -> fillₜ
(LTBₜ₋₁, STBₜ, fillₜ, due past outcomes) -> post-fill LTBₜ
post-fill LTBₜ -> PM community
```

LTB₀은 parent, STB, decision, fill이 모두 null이고 첫 event부터 보인다.
LTBₜ은 `visible_from_event`가 다음 event 이후여야 하며 같은 event의 analysis나
decision에 다시 들어가면 안 된다.

### 17.2 provenance와 outcome tables

| 저장물 | PK·unique | 역할 |
| --- | --- | --- |
| `simulation_trade_outcomes` | `outcome_id`; `(fill_id, horizon)` unique | next-turn, H1, H5의 matured/right-censored 상태와 due event |
| `simulation_outcome_consumptions` | `consumption_id`; `outcome_id` unique | due outcome을 어느 post-fill LTB가 한 번 소비했는지 기록 |
| STB/LTB evidence JSON | target belief와 차원별 구조 | 현재 news/community evidence와 outcome 관계 |
| `memory_lineage.jsonl` | agent-event lineage | previous LTB + STB→analysis→decision→fill→post-fill LTB 전체 연결 |
| `trade_outcomes.jsonl` | maturity/finalization event | outcome 성숙과 terminal censoring trace |

### 17.3 community tables와 trace

| 저장물 | 키 | 역할 |
| --- | --- | --- |
| `community_posts` | `post_id` PK; author-PM unique | author agent logical FK, PM event, 제목, 원문, 본문 hash, post type |
| `community_interactions` | reader-post unique reaction row | 선택한 원문에 대한 실제 reaction |
| `community_logs` | reader-source-turn unique | 다음 AM에 전달할 동결 Best·selected 관계 |
| `community_posts.csv` | run-local post projection | post 원문과 source LTB/fill/decision |
| `community_selection_inputs.csv` | reader-source-PM | 실제 title-only 후보 보드 |
| `community_interactions.csv` | provenance ID | title-only, selected full body, Best full body, reaction을 관계별 기록 |
| `community_best_posts.csv` 파생물 | source PM-post-rank | score, rank, 예정·실제 audience, self-exclusion, right censoring |
| `traces/community_exposure_trace.jsonl` 파생물 | provenance ID | reader별 실제 노출을 canonical relation에서 직렬화 |

현재 DB의 단순 reaction row만으로는 title 후보, selected full body,
next-AM Best 관계를 모두 표현할 수 없다. 따라서 run-local interaction CSV와
exposure trace가 실제 전달 provenance를 보존하며 `99_validate.py`가 post,
Best, DB reaction, 예정·실제 delivery와 교차 검증한다. title 후보를 본 것과
본문을 읽은 것을 한 row count로 합치지 않는다.

### 17.4 response journal

run/condition별 response journal은 scientific DB와 분리하되 같은 manifest
hash에 묶는다.

| 테이블 | PK·unique | 역할 |
| --- | --- | --- |
| `logical_responses` | `logical_call_id` | exact canonical request/response hash, validation 상태, commit 상태 |
| `physical_attempts` | `attempt_id`; logical-call/phase-attempt/attempt-number unique | 실제 HTTP 시도, success/error, retry와 비용 telemetry |

`logical_call_id`는 최소 run, condition, agent, event, stage, schema version을
포함한다. 동일 ID와 동일 request hash의 accepted response만 replay할 수 있다.
동일 ID에 다른 request가 오면 중단한다.

## 18. transaction, checkpoint, resume

원자성 단위는 한 AM 또는 PM scientific event다. 그 event 안의 community
준비, STB, analysis, decision, fill, post-fill LTB, PM이면 community-after가
모두 성공해야 commit한다.

1. 실행 전 run lock을 획득하고 manifest hash와 다음 event를 확인한다.
2. 해당 arm의 canonical DB와 run-local artifact tree를 event snapshot으로
   보존한다.
3. checkpoint를 `running` 상태로 내구성 있게 기록한다.
4. stage barrier를 순서대로 실행한다. LLM accepted response는 journal에서
   아직 `pending`이다.
5. 모든 DB write와 validator가 성공하면 logical call 목록을 포함한
   `commit_decided` checkpoint를 먼저 fsync한다.
6. 각 journal response를 `committed`로 표시하고 completed phase를 기록한다.
7. snapshot을 제거하고 다음 event로 이동한다.

`commit_decided` 이전 실패는 해당 arm의 DB와 sidecar를 snapshot으로 복원한다.
동일 request의 accepted journal response는 provider를 다시 호출하지 않고
replay할 수 있다. `commit_decided` 이후 중단은 DB를 되돌리지 않고 journal
commit과 completed marker를 idempotent하게 마무리한다. commit 여부를 증명할
수 없으면 자동으로 어느 쪽도 선택하지 않고 pause한다.

resume은 다음을 검증한 뒤에만 허용한다.

- run ID, manifest hash, code/prompt/input hash가 원 run과 동일
- completed event key와 DB row/key/hash가 일치
- checkpoint snapshot hash와 journal manifest가 일치
- 같은 logical key에 다른 payload가 없음
- 새 run처럼 base를 초기화하거나 이미 committed event를 재호출하지 않음

uninterrupted run과 failpoint-resumed run은 scientific DB digest, event key set,
canonical export hash가 같아야 한다. 날짜만 건너뛰는 수동 resume script,
임의 1주 보정, 최신 run 자동 선택은 금지한다.

## 19. logs → validator → CSV/PDF

```text
canonical run DB + response journal + resolved manifest
  -> read-only integrity validator
     -> external derivative JSON
        -> statistical validator
           -> external CSV/PDF report
```

런타임 중 CSV에 먼저 쓰고 DB와 나중에 맞추는 구조를 최종 원장으로 삼지 않는다.
필요한 trace를 streaming으로 남길 수는 있지만 finalize 때 DB key·row
count·hash와 one-to-one reconciliation을 통과해야 한다.

run root에는 최소 다음 종류가 있어야 한다.

- `run_signature.json`, `run_metadata.json`, StudySpec와 source/input/prompt/code hash
- arm별 canonical SQLite DB와 response journal
- phase checkpoint, pause/recovery history, 완료 marker
- STB/LTB/analysis/decision/fill/outcome lineage export
- `exchange_fills.csv`, portfolio export
- community posts, interactions, Best, delivery/exposure export
- reasoning-off, provider/model, token/cost/retry telemetry
- signed completion에 필요한 canonical log·projection·lineage export

validator는 아래 순서로 실행한다.

1. manifest와 입력 hash, condition diff를 검사한다.
2. 예상 agent-event key와 실제 STB/analysis/decision/fill/LTB key를 비교한다.
3. PK/FK, parent chain, input/scientific hash, fee=0, portfolio를 reconcile한다.
4. news slot·shortage·depth exposure와 community delivery 정책을 검사한다.
5. journal logical/physical attempt와 committed scientific row를 맞춘다.
6. resumed/uninterrupted digest 계약과 완료 marker를 검사한다.
7. 모두 PASS한 뒤에만 run 밖의 명시적 `derived/<condition>/`에 JSON,
   CSV, PDF와 실제 개인수급 평가 결과를 생성한다.

보고서는 특정 과거 run 경로나 `outputs/current`를 읽지 않고 명시한 canonical
run만 읽는다. validation/report output은 signed run tree 밖이어야 하며, CSV/PDF
생성 실패가 canonical run을 훼손하지 않아야 한다. 파생물은 같은 원장에서 다시
만들 수 있어야 한다.

## 20. profile 일반화

StudySpec은 다음 축을 한곳에서 관리한다.

| 축 | 필수 내용 |
| --- | --- |
| instrument | stock code/name, exchange, price source, target source, trading calendar |
| schedule | start/end, ordered AM/PM event registry, cutoff, burn-in/evaluation mask |
| cohort | agent registry, count, depth distribution, persona snapshot, initial asset |
| news treatment | real-only, real+bullish fake, real+bearish fake, registry hash |
| community | off/on, permission map, read cap, Best, length, timing |
| memory/trade | 6D schema, outcome horizon, price/fill policy, fee/tax |
| model/call | model, provider, reasoning-off, concurrency, retry, seed derivation |
| evaluation | frozen target, metrics, shortage sensitivity, report contract |

새 종목은 Instrument profile이 가격, 뉴스, target, report label을 함께 바꿔야
한다. 새 날짜는 calendar registry와 cutoff review를 새로 봉인한다. agent 수를
줄이거나 늘릴 때는 frozen cohort map을 명시적으로 만들며 첫 N명 뒤 임의
D2 교체 같은 보정은 하지 않는다. bullish/bearish fake는 같은 factual anchor와
event schedule을 쓰고 polarity만 다르게 봉인한다.

두 번째 synthetic instrument를 이용한 무과금 test로 `005930`, 날짜 범위,
100명, 조건명이 production path에 숨어 있지 않은지 검사한 뒤 실제 확장을
허용한다.

## 21. 제거되는 과거 흐름

다음 항목은 필요한 기능을 공통 엔진으로 이식하고 working tree에서 제거했다.

| 과거 흐름 | 제거 이유 |
| --- | --- |
| `scripts/09_run_realnews_community_ab.py` | RN 전용 preflight가 새 기본 진입점이 되면 번호형 본류와 분기됨 |
| `scripts/12_operate_realnews_community_ab.py` | RN 전용 run/resume/finalize가 두 번째 production engine을 유지함 |
| `twinmarket_kr/rn_ab/runner.py`, phase/stage adapter | 검증 기능은 이식하되 별도 orchestration을 남기면 상태·로그·수정이 양분됨 |
| `scripts/05_run_simulation_checkpointed.py` | checkpoint가 기본 05와 분리되면 정상 실행과 재개 실행의 의미가 달라짐 |
| 날짜가 박힌 `resume_*.py`, `run_full_restart.sh` | 특정 과거 DB·날짜·절대경로를 복구하는 비재현적 진입점 |
| legacy selected-news CSV runtime | 최신 sealed event bundle과 별개인 가변 입력 경로 |
| `archive/legacy_inputs/rn_ab_source_candidate_v1/input_candidates` | no-go/history candidate이며 현재 sealed input이 아님 |
| `outputs/current`·latest glob fallback | 어떤 run을 읽었는지 재현할 수 없고 과거 결과가 신규 입력으로 섞임 |

과거 DB·CSV를 읽어야 하면 canonical schema로의 read-only importer 또는
canonical 원장에서의 exporter만 둔다. 제거된 runtime과 같은 이름의 shim을
만들지 않는다.

현재 `scripts/08_run_six_conditions.py`는 과거 30명 실행기가 아니라 공통
`05_run_simulation.py`만 호출하는 RN_COMM_OFF/ON pair launcher다. 기본 조건은
실제뉴스 두 arm뿐이며, 완료 후 공통 `pair_evaluation.json`을 생성한다.

## 22. 구현 상태 판정의 정본

이 문서는 연구 설계와 정책을 고정한다. 변동하는 working tree의 구현 상태,
Git 근거, active·임시·격리 경계와 남은 실행 gate는
[`ARCHITECTURE.md`](ARCHITECTURE.md)의 20절을 정본으로 삼는다. 실행 명령과
검증 결과는 [`RUNBOOK_AND_PREFLIGHT.md`](RUNBOOK_AND_PREFLIGHT.md)를 따른다.

현재 공통 `05`에는 StudySpec 의미 검증, sealed news/depth, STB/LTB,
decision/fill, outcome, Community, journal/checkpoint/resume, paid-call gate와
explicit-run validator/report 경로가 연결되어 있다. 현 tree의 전체 무과금 회귀,
변경된 prompt/code/persona hash 재봉인, external derivative output guard와 PDF
fixture QA도 완료했다. 남은 핵심 gate는 clean/frozen tree의 승인 record, 승인된
strict reasoning-off live canary와 45일 OFF/ON 본실험이므로, canary와 본실험은
여전히 **NO-GO**다.
