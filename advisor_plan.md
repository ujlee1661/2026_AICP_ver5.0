# Advisor 실험 설계안 v3

> 갱신일: 2026-09-19  
> 상태: 연구 설계 문서. 유료 API 호출이나 본실험 승인을 뜻하지 않는다.

## 1. 핵심 연구 아이디어

삼성전자 단일 종목 시뮬레이션에서 동일한 100명 에이전트를 먼저
2026-02-27부터 2026-05-04까지 실행한다. 그때까지 생성된 각 에이전트의
persona, belief, 거래 및 포트폴리오 기록만 사용해 전체 100명 모두에게
개인화된 Advisor 메모를 한 번 전달한다.

이후 2026-05-06부터 2026-05-29까지 시뮬레이션을 이어서 실행하고, Advisor
메모를 받은 세계와 받지 않은 세계를 비교한다. 핵심 질문은 다음 두 가지다.

1. Advisor 메모를 받은 100명 전원의 투자 행동이 달라지는가?
2. 그 행동 변화가 포트폴리오 성과 차이로 이어지는가?

Advisor는 매수·매도 방향이나 미래 수익률을 예측하지 않는다. 현재 시장 정보,
과거 행동 원장, portfolio와 실행 제약을 바탕으로 가장 합리적인 판단 절차를
통보식으로 제안한다. persona는 조언의 적합성·실행 가능성과 내용의 강조점을 조정하는
핵심 개인화 자료이며, persona 준수 자체가 조언의 최우선 목표는 아니다. 같은
시장·행동 기록이라도 persona의 목표·분석 방식·위험 성향에 따라 강조점, 확인
순서와 위험 대응을 다르게 구성한다.

개인화는 조언 내용에만 적용한다. persona의 말투·어휘·캐릭터는 모방하지 않고,
모든 Advisor 메모는 명료하고 중립적인 일반적 LLM 어드바이저 문체를 사용한다.

## 2. 기간과 대상

| 항목 | 확정값 |
|---|---|
| 종목 | 삼성전자(005930) |
| 전체 cohort | 봉인된 동일 에이전트 100명 |
| 사전 관찰 구간 | 2026-02-27 ~ 2026-05-04, 45거래일·90 events |
| Advisor 최초 노출 | 2026-05-06 AM |
| 사후 관찰 구간 | 2026-05-06 ~ 2026-05-29, 17거래일·34 events |
| 전체 실행 구간 | 2026-02-27 ~ 2026-05-29, 62거래일·124 events |
| Advisor 대상 | 전체 100명 전원 |
| Advisor 형식 | 대화 없는 1회성 통보 |

2026-05-05는 휴장일이다. 여기서 “약 3주”는 봉인 거래일 기준으로 5월 6일부터
5월 29일까지를 뜻한다. 기간 입력은 이미 생성된 다음 profile을 사용한다.

```text
preparation/rn_ab_sealed_to_20260529_v1/
```

이 profile은 기존 5월 4일까지의 90 events·760 news slots를 그대로 보존하고,
5월 6일부터 5월 29일까지를 이어 붙인 정본이다.

## 3. 실험 구조

### 3.1 필수 비교

Advisor 효과를 단순 전후 변화로 판단하지 않는다. 같은 5월 4일 상태에서
Advisor 유무만 다른 두 continuation을 만든다.

```text
공통 사전 상태: 2/27 ~ 5/4
             ├─ Advisor ON  : 100명 전원에게 메모 전달 → 5/6 ~ 5/29
             └─ Advisor OFF : 누구에게도 메모 없음     → 5/6 ~ 5/29
```

Advisor ON과 OFF는 다음 항목이 같아야 한다.

- 100명 agent ID와 persona
- 5월 4일 종료 시점 LTB·포트폴리오·거래·outcome 상태
- 뉴스, 가격, 캘린더와 Community 설정
- 모델, provider, temperature, seed와 거래 정책
- Advisor 대상 100명의 ID(전체 cohort)

허용되는 차이는 Advisor artifact와 그 노출 여부뿐이다.

### 3.2 Community 조건

1차 핵심 질문은 Advisor의 주효과다. 먼저 하나의 고정된 Community 조건에서
Advisor ON/OFF를 검증한다. Community 조건은 실행 전에 `on` 또는 `off`로
명시하고 두 continuation 사이에서는 바꾸지 않는다.

### 3.3 사용자 제공 general 조언 비교

동일한 5월 4일 parent에서 `Advisor OFF`, `personalized Advisor ON`,
`general Advisor ON`을 각각 fork한다. ON 두 조건은 모두 5월 6일 AM에만
조언을 한 번 전달하고, 이후에는 갱신된 LTB를 사용한다. cohort 100명,
뉴스·가격·Community 조건·메인 모델·seed·기간은 같게 둔다. 두 ON 조건에서
달라지는 처치는 조언 본문뿐이다.

- Personalized 정본: `outputs/advisor/advisor_comm_on_20260919/advisor_messages_100_final.json`
- General 정본: `outputs/advisor/advisor_comm_on_20260919/advisor_messages_100_general.json`
- General은 사용자 제공 동일 본문 419자를 100명 전원에게 전달한다. 개인별
  평가나 생성 모델 호출을 주장하지 않으며, 두 JSON은 같은 9/23 parent의
  상태 해시를 사용한다.
- 5월 6일~7월 10일 비교에는 `rn_ab_sealed_to_20260722_v1`의 봉인 입력을
  사용한다. 이 확장 기간의 결과는 기존 5월 29일 종료 설계와 구분한다.

Community가 Advisor 효과를 증폭하거나 약화하는지도 연구하려면 동일 실험을
Community OFF와 ON에 각각 적용해 다음 2×2를 구성한다.

| 셀 | Community | Advisor |
|---|---|---|
| 1 | OFF | OFF |
| 2 | OFF | ON |
| 3 | ON | OFF |
| 4 | ON | ON |

이 4셀은 Community 상호작용을 검정하는 확장 설계다. 실행한다면 Community별
사전 상태를 각각 같은 조건에서 현재 통합 엔진으로 생성해야 한다.

## 4. Advisor 대상 전체 cohort 확정

Advisor 대상은 행동 점수나 수익률로 선별하지 않는다. 봉인 cohort 100명
전원을 대상으로 한다.

- 대상 계약 namespace는 `advisor-full-cohort-v1`이다.
- 정렬된 전체 agent ID 목록을 JSON artifact로 봉인한다.
- Advisor ON/OFF 및 선택적 후속 반복에서도 동일한 100명을 사용한다.

이는 “행동이 나쁜 사람에게만 Advisor를 붙였을 때”가 아니라 “전체 개인투자자에게
개인화 Advisor를 제공했을 때”의 평균 처치 효과를 묻는 설계다. 과거
문서의 하위 20명, 규율 결여 점수 및 matched-control 선별 절차는 사용하지 않는다.

직접 처치 비교는 전체 100명의 Advisor ON 대 Advisor OFF 차이다. live LLM
출력이 byte 단위로 완전히 같아야 한다고 가정하지 않고, 행동 차이는 Advisor
없는 동일 포크에서 측정한 노이즈 바닥과 비교한다.

## 5. Advisor의 역할과 입력

### 5.1 역할

Advisor는 결과가 아니라 과정을 조언한다.

허용 예:

> 최근 짧은 기간에 매수와 매도를 반복하며 포지션을 크게 바꾼 기록이 있습니다.
> 다음 판단에서는 새 정보와 기존 기준을 구분하고 거래 규모를 한 번 더 점검하세요.

금지 예:

- “삼성전자를 매수하라/매도하라”
- “주가가 오를/내릴 것이다”
- 사전 구간 이후의 가격·뉴스·성과를 근거로 한 조언
- 입력 기록에 없는 숫자나 행동을 만들어 낸 조언

### 5.2 입력 원칙과 cutoff

Advisor 생성에 사용할 수 있는 정보는 2026-05-04 PM 종료 시점까지다.

입력의 최우선 기준은 **persona 원문**이다. Advisor는 모든 에이전트를 같은
보수적·저회전 투자자로 바꾸려 하지 않고, 해당 persona가 선언한 투자 방식
안에서 실제 행동의 일관성과 위험관리를 개선해야 한다. 예를 들어 단기 기술적
투자자의 높은 거래 빈도와 장기 기본적 투자자의 같은 거래 빈도를 동일하게
평가하지 않는다. 집중투자 성향도 그 자체를 오류로 보지 않고 실제 규모와
감당 가능성을 함께 본다.

| 입력 | 내용 |
|---|---|
| Persona | 재생성된 `persona_prompt` 원문 전체와 구조화 persona 필드 |
| LTB | 5월 4일까지의 최신 6차원 LTB 원문 |
| 거래 기록 | decision과 실제 fill, reason, risk_control을 구분한 과거 거래 이력 |
| 행동 요약 | 회전율, 거래 빈도, 주문 크기, 방향 전환, 포지션 집중도 등 결정론적 통계 |
| 성과 요약 | 누적 수익률, 실현·미실현손익, 변동성, MDD와 총자산 경로 |
| 포트폴리오 | 5월 4일 PM 종료 시점의 현금·보유량·평가액·총자산 |
| 실행 제약 | 부모 run signature와 공통 runtime에서 확정한 buy/sell-only, hold 불가, 최소 주문 수량 1주, 수수료 0 |

사용하지 않는 정보:

- 5월 6일 이후의 뉴스·가격·수익률
- 실제 개인투자자 수급의 미래값
- Community OFF 에이전트가 보지 못한 Community 정보
- 검증되지 않은 LTB 방향성 분류 결과
- 기사 본문 등 원래 에이전트 입력 계약에 없는 외부 정보

수익률은 행동을 해석하는 보조 근거다. 낮은 수익률만으로 행동이 나쁘다고
판정하거나 높은 수익률만으로 행동이 좋다고 판정하지 않는다. 좋은 과정이 짧은
기간의 손실로, 나쁜 과정이 우연한 수익으로 이어질 수 있음을 prompt에 명시한다.
실행 제약은 거래 이유 문장에서 추론하지 않고 `runtime_constraints` 구조화 필드로
제공한다. 부모 run signature와 현재 공통 runtime의 제약이 다르면 case 생성을
실패시킨다.

### 5.3 행동·성과 요약 계산식

행동 통계는 대상 선정이나 사전 점수화에 사용하지 않는다. Advisor가 45거래일
기록을 정확하고 일관되게 읽도록 돕는 사실 요약으로만 사용한다. 모든 거래
지표는 intended decision보다 canonical fill을 우선한다.

| 항목 | 계산식 |
|---|---|
| 체결 횟수 | `status='filled'`인 fill 수 |
| 매수·매도 횟수 | filled action별 건수 |
| 회전율 | `sum(abs(filled_trade_value)) / mean(event_total_value)` |
| 평균 거래 규모 | `mean(abs(filled_trade_value) / pre_fill_total_value)` |
| 최대 거래 규모 | `max(abs(filled_trade_value) / pre_fill_total_value)` |
| 방향 전환 횟수 | 시간순 연속 filled action에서 buy↔sell이 바뀐 횟수 |
| 평균·종료 현금 비중 | event별 `cash / total_value`의 평균과 5/4 PM 값 |
| 평균·최대 집중도 | `삼성전자 평가액 / total_value`의 평균과 최댓값 |
| 누적 수익률 | `(5/4 PM total_value - initial_value) / initial_value` |
| 실현손익 | 5/4 PM `realized_pnl` |
| 미실현손익 | `sum((current_price - avg_cost) * quantity)`; 저장된 position별 `unrealized_pnl` 합과 일치해야 함 |
| MDD | 총자산 경로의 직전 고점 대비 최대 하락률 |
| 자산 변동성 | event별 총자산 수익률의 표준편차 |

보유주식 원가는 `portfolio_state.positions`에 저장된 이동평균 단가 `avg_cost`를
정본으로 사용한다. `pre_fill_total_value`는 해당 fill 직전 turn의
`portfolio_state.total_value`다. 새 원가 계산법이나 별도 ledger를 만들지 않는다.
저장된 `unrealized_pnl`과 재계산값이 다르면 추정으로 메우지 않고 검증 실패로
처리한다. 분모가 0이면 비율은 `null`과 사유를 기록한다.

### 5.4 전체 기록과 대표 사례

Advisor에게 90 events의 원시 JSON을 무정형으로 전부 붙이지 않는다. 전체 기록은
누락 없이 결정론적으로 집계하되, prompt에는 다음 두 층을 함께 제공한다.

1. 전체 사전 구간의 행동·성과 요약
2. 원장 ID와 연결된 대표 실제 사례

대표 사례는 동일 규칙으로 자동 추출한다.

- 절대 체결금액이 큰 상위 5건
- 연속 fill의 방향 전환 사례 최대 5건
- 최대 drawdown 구간의 시작·저점·회복 상태
- 마지막 실제 fill 10건
- 각 사례의 decision, fill, reason, risk_control과 전후 포트폴리오
- 5월 4일 현재 최신 6차원 LTB

동일 거래가 여러 기준에 걸리면 한 번만 직렬화하고 해당 선정 사유를 모두
기록한다. 대표 사례가 없거나 5건보다 적으면 존재하는 사례만 제공한다.

### 5.5 Advisor 구조화 출력과 메모 형식

Advisor 모델은 검증 가능한 다음 내부 JSON을 출력한다.

```json
{
  "persona_basis": ["참고한 persona 근거와 현재 상황에서의 적합성 평가"],
  "observed_behavior": ["원장 통계 또는 사례에 근거한 관찰"],
  "persona_behavior_assessment": "persona의 상황 적합성과 실제 행동의 최적 판단 부합 여부",
  "performance_context": "성과를 보조 근거로 해석한 내용",
  "advice_body": "에이전트에게 전달할 통보형 메모 본문"
}
```

`persona_basis`와 `observed_behavior`는 각각 최소 1개가 있어야 한다.
`observed_behavior`의 각 항목은 입력에 포함된 cutoff 이전 대표 `fill_id`를 적어도
하나 인용해야 하며, 존재하지 않는 ID나 cutoff 이후 날짜를 인용하면 전체 출력을
거부하고 bounded retry를 수행한다. 에이전트에게 노출하는 값은 검증을 통과한
`advice_body`뿐이고 나머지는 연구·감사 artifact로 보존한다.

- 통보형이며 응답이나 재질문은 받지 않는다.
- 한 에이전트당 한 번 생성하고 5월 6일 AM에만 불변 메모를 노출한다.
- 본문은 1~500자다. 자동으로 자르지 않고 501자 이상이면 거부한다.
- 관찰과 행동 지침을 포함한다.
- 가장 중요한 판단 문제 하나를 적용 순간·확인 방법·정당한 예외와 함께 다룬다.
- 명확한 오류가 없으면 결함을 만들지 않고, 확인된 일관된 절차의 유지 조건과
  예외를 제시한다.
- persona의 방식이 현재 상황에 적합하면 행동 이탈을 교정하되, 부적합하면
  persona와 상충하더라도 시장·원장·제약에 비춰 더 나은 절차를 제시한다.
- 최적 판단을 persona에 맞게 전달하지만 persona 준수 자체를 목표로 삼지 않는다.
- persona의 목표·분석 방식·위험 성향을 조언의 강조점·확인 순서·위험 대응에
  구체적으로 반영하며, 누구에게나 같은 일반론은 허용하지 않는다.
- 말투는 persona에 맞추지 않고 모든 대상에게 동일한 명료하고 중립적인
  어드바이저 문체를 사용한다.
- 매수·매도 방향을 지시하지 않는다.
- 고정 접두사는 `당신의 담당 투자 어드바이저가 보낸 메모:`다.
- 접두사는 본문 500자 상한에서 제외한다.

날짜와 최초 노출 event는 artifact metadata로 관리하며 본문에 반복하지 않는다.

## 6. Reasoning 정책

### 6.1 메인 시뮬레이션 에이전트

100명 에이전트의 전체 시뮬레이션 호출은 2026-02-27부터 2026-05-29까지
기존 봉인 정책대로 reasoning OFF를 유지한다.

```json
{"effort": "none", "exclude": true}
```

Advisor ON이라고 해서 market analysis, decision, STB, LTB 또는 Community
호출의 reasoning을 켜지 않는다.

### 6.2 Advisor 메모 생성

Advisor가 100명 전원의 사전 기록을 검토해 각자 메모를 생성하는 별도 호출만
reasoning ON을 사용한다.

Advisor는 메인 에이전트와 동일하게 OpenRouter를 통해 다음 모델과 provider를
사용한다.

| 항목 | 확정값 |
|---|---|
| API | OpenRouter |
| model | `qwen/qwen3.5-flash-02-23` |
| provider | `alibaba` |
| 인증 | 프로젝트 `.env`의 기존 `OPENROUTER_API_KEY` |
| provider fallback | 허용하지 않음 |
| parameter requirement | 활성화 |

```json
{"effort": "high", "exclude": false}
```

두 호출은 서로 다른 call policy다. Advisor 생성 호출은 다음 항목을 별도로
기록하고 봉인한다.

- model과 provider
- reasoning 설정
- prompt hash
- 입력 cutoff와 입력 artifact hash
- 대상 agent ID와 생성 seed
- 원본 응답과 검증 결과
- 최종 메모 hash

`.env`의 키 값은 Advisor artifact, prompt, audit log, console 출력, 예외 메시지에
기록하지 않는다. 키 존재 여부만 preflight에서 확인하며, 별도 Advisor 전용 키를
새로 만들거나 소스 코드에 키를 하드코딩하지 않는다.

Advisor 생성은 유료 API 호출이므로 사용자 명시적 승인 전에는 실행하지 않는다.
Advisor의 reasoning ON이 메인 에이전트의 reasoning-off 계약을 바꾸거나 우회하면
안 된다.

## 7. 주입 위치와 인과 순서

Advisor 메모는 LTB나 STB에 직접 덮어쓰지 않는다. 별도의 `advisor_note`
컨텍스트로 관리하고 5월 6일 AM의 다음 단계에만 같은 원문을 제공한다.

1. `market_analysis`
2. `make_decision`
3. `post-fill LTB`

실행 순서는 유지한다.

```text
현재 news/community evidence
  → STB
  → 이전 LTB + 현재 STB + advisor_note로 market analysis
  → market analysis + advisor_note로 decision
  → 실제 fill
  → 이전 LTB + 현재 STB + 실제 fill + advisor_note로 post-fill LTB
  → PM community
```

따라서 5월 6일 AM의 STB는 아직 Advisor 메모를 보지 않는다. 같은 turn의 반영
여부는 market analysis와 decision, post-fill LTB에서 검사한다. 이후 event에는
Advisor 원문을 다시 제공하지 않고, 갱신된 LTB를 통한 지속 효과를 측정한다.

Advisor OFF에서는 빈 안내문을 대신 넣지 않는다. Advisor block 자체가 렌더링되지
않아야 한다.

## 8. Advisor artifact와 provenance

단순 `.md`만 정본으로 삼지 않는다. 사람이 읽는 `.md`를 만들 수는 있지만
canonical artifact에는 최소한 다음 필드가 필요하다.

```text
advisor_id, agent_id, selection_seed, source_run_id,
source_cutoff_event_id, source_state_sha256, advisor_prompt_sha256,
model, provider, reasoning_policy, generation_seed,
persona_basis, observed_behavior, persona_behavior_assessment,
performance_context, message_body, message_body_sha256,
valid_from_event_id, created_at
```

`agent_system_messages`를 저장소로 사용할 수 있지만 다음 조건을 만족해야 한다.

- `message_type='advisor'`
- 대상 100명에게 정확히 한 행
- 5월 6일 AM에만 조회 가능
- Advisor OFF에는 advisor 행 0개
- 다른 system message가 생겨도 Advisor가 가려지지 않는 전용 조회
- prompt trace에서 agent·event·advisor ID 연결 가능

## 9. Warm fork 요구사항

과거 `outputs/`의 legacy/ver6 결과 DB를 새 runtime 입력으로 재사용하지 않는다.
과거 결과는 참고 분석에는 쓸 수 있지만 새 실험의 canonical state가 될 수 없다.

사전 상태와 continuation은 현재 기본 경로에서 생성한다.

```text
scripts/05_run_simulation.py
  → twinmarket_kr/simulation.py
```

warm fork는 clean-base validator를 단순 우회해서 만들지 않는다. 다음을
fail-closed로 검증한다.

- parent run이 현재 코드·prompt·profile로 5월 4일 PM까지 정상 완료됨
- 100명 전원의 최신 LTB와 portfolio 상태가 경계 turn에 존재함
- 마지막 fill turn과 5월 6일 AM turn이 연속됨
- pending fill이 없음
- 미성숙 H1/H5 outcome이 이월되고 미래 가격을 미리 읽지 않음
- Community ON이면 5월 4일 PM Best 전달 상태와 동결 profile이 보존됨
- foreign-key check 통과
- parent run ID·상태 hash·fork ID가 provenance에 기록됨
- Advisor ON/OFF의 초기 DB hash가 동일함
- Advisor artifact 주입 이후에만 두 run의 hash가 갈라짐

첫 구현 전에는 Advisor 없는 동일 포크 두 개를 실행한다. 두 continuation의
차이로 live 모델의 노이즈 바닥을 측정하고, 경계 첫 LLM 호출 전 상태가 같은지
확인한다.

## 10. 평가 계획

### 10.1 1층: 조작 검증

다음은 기계적으로 전부 통과해야 한다.

- Advisor artifact에 고유한 전체 100명 존재
- Advisor ON DB에 대상별 메시지 1건, 총 100건
- Advisor OFF에는 advisor 메시지 0건
- 5월 6일 AM에만 대상자의 analysis·decision·post-fill LTB prompt에 동일 메모 포함
- 본문 길이·금지 표현·cutoff·hash 검증 통과
- 구조화 출력의 persona 근거와 행동 관찰이 실제 입력에 의해 뒷받침됨

각 turn의 market analysis, decision 또는 LTB 문구를 별도 규칙이나 LLM으로
분류해 “이번 turn에 조언을 따랐다/따르지 않았다”고 판정하지 않는다. 조언은
행동을 즉시 복제시키는 명령이 아니라 17거래일 동안 판단 기준에 영향을 줄 수
있는 통보이므로, 매 turn의 텍스트 유사도보다 사후 전체 행동 변화가 연구 질문에
더 가깝다.

따라서 1층은 메시지가 정확한 대상에게 정확한 기간 동안 전달됐는지 확인하는
기계적 조작 검증으로 끝낸다. Advisor의 실질적 반영 여부는 5월 29일 종료 후
§10.2 행동 지표와 §10.3 성과 지표의 Advisor ON/OFF 차이로 평가한다. 저장되지
않는 숨은 사고과정은 평가하지 않는다.

### 10.2 2층: 행동 변화 — 주 평가

Advisor 대상 100명 전원의 ON/OFF paired 차이를 계산한다.

- 거래 회전율과 거래 빈도
- 체결 수량·체결 금액과 그 분산
- 연속 subturn 방향 전환 빈도
- 포지션 집중도와 현금 비중
- 뉴스 및 Community 노출 후 거래 크기 변화

지표는 decision이 아니라 canonical fill을 우선 사용한다. 계산식·분모·결측
처리·집계 단위는 본실험 전에 고정한다. subturn을 독립 표본처럼 취급하지 않고
agent 단위의 사후 기간 변화로 집계한다.

### 10.3 3층: 성과 변화 — 보조 평가

- 누적 수익률
- 포트폴리오 변동성
- 최대 낙폭(MDD)
- 실현손익과 평가손익
- 최종 총자산

성과 차이는 보고하지만 짧은 사후 기간을 고려해 주된 성공 기준으로
삼지 않는다. “Advisor가 수익을 예측했다”는 주장도 하지 않는다.

### 10.4 Spillover

Community ON에서는 모두 Advisor를 받은 상태에서 게시글을 통한 조언 영향의
상호 전파가 생길 수 있다. 비처치 집단이 없으므로 이를 별도 spillover 효과로
식별하지 않는다.

## 11. 통계와 선택적 반복 실행

Advisor 주효과는 같은 agent·같은 5월 4일 상태의 ON/OFF paired 차이로 본다.
n=100이라고 충분한 검정력이 있다고 미리 가정하지 않는다.

여기서 “반복 seed”는 Advisor 대상을 다시 정한다는 뜻이 아니다. 같은
100명·같은 Advisor 메모·같은 입력을 유지한 채, live LLM 생성의 우연한 변동에
결과가 좌우되는지 확인하기 위해 시뮬레이션 생성 seed만 바꿔 같은 ON/OFF 쌍을
추가 실행하는 것을 뜻한다.

1차 실험은 사전 등록된 하나의 메인 시뮬레이션 seed로 Advisor ON/OFF 한 쌍을
완주해 기술적·탐색적 결과를 본다. 반복 실행은 필수 선행조건으로 두지 않는다.
다만 결과를 일반화하거나 통계적 유의성을 강하게 주장하려면 후속 단계에서
복수 생성 seed로 재현성을 확인한다.

Advisor 없는 동일 continuation 두 개를 먼저 돌리는 dummy fork는 반복 본실험이
아니라 배선과 비결정성 규모를 확인하는 엔지니어링 검증이다.

여러 subturn을 독립 표본으로 세어 표본 수를 부풀리지 않는다.

## 12. 주장 범위와 한계

1차 실험에는 sham Advisor가 없다. 따라서 다음 정도까지만 주장한다.

> 과거 행동 기록을 바탕으로 생성된 통보형 Advisor 메모를 제공했을 때,
> 제공하지 않은 동일 상태의 반사실 실행과 비교해 행동 또는 성과가 달라졌다.

이 설계만으로 개인화 내용, Advisor 권위 라벨, 추가 텍스트, 분석/결정 노출
경로의 효과를 서로 분리할 수는 없다. 필요하면 후속 실험에서 sham 메모,
비개인화 메모, 권위 라벨과 노출 위치를 별도 요인으로 둔다.

## 13. 구현 순서

### 단계 1 — 설계와 artifact 계약

- [x] 무작위 선정 namespace·seed와 추출 원칙 확정
- [x] Advisor 입력 통계의 역할과 기본 계산식 확정
- [x] 원가·pre-fill 총자산의 기존 DB ledger 기준 확정
- [x] Advisor prompt·출력 schema·500자 validator 확정
- [ ] 행동·성과 지표 계산식 사전등록
- [ ] Community 조건과 1차 실험 셀 수 확정

### 단계 2 — canonical fork 인프라

- [ ] 현재 통합 엔진으로 2/27~5/4 사전 run 생성
- [x] warm-state validator와 parent/fork provenance 구현
- [ ] 5/4 PM → 5/6 AM 경계, outcome, Community 이월 테스트
- [ ] Advisor 없는 동일 포크 E2E 및 노이즈 바닥 측정

### 단계 3 — Advisor 생성과 주입

- [x] 별도 Advisor 생성 call policy 구현
- [x] reasoning ON 요청과 응답 audit 구현
- [x] Advisor artifact sealer와 validator 구현
- [x] `advisor_note` 전용 조회 및 주입 훅 구현
- [x] analysis와 decision의 조건부 prompt block 구현
- [x] Advisor OFF에서 해당 block이 완전히 사라지는지 테스트

### 단계 4 — 검증 gate

- [x] 전체 무과금 테스트
- [x] D2가 포함된 7명 이상 오프라인 E2E
- [ ] 100명 warm-fork deterministic stub 회귀 검증
- [x] reasoning OFF/ON call-policy 분리 audit
- [ ] 유료 Advisor 생성 canary — 사용자 명시적 승인 후
- [ ] 본실험 — 사용자 명시적 승인 후

## 14. 확정 사항

| 항목 | 확정 내용 |
|---|---|
| 연구 질문 | Advisor 통보가 대상자의 행동과 성과를 바꾸는가 |
| 대상 | 삼성전자 에이전트 100명 |
| Advisor 대상 | 전체 100명 전원 |
| Advisor 대상 계약 | namespace `advisor-full-cohort-v1`, agent ID 결정론적 정렬 |
| 메인 시뮬레이션 seed | 기존 StudySpec과 동일한 `2` |
| 사전 구간 | 2026-02-27 ~ 2026-05-04 |
| 사후 구간 | 2026-05-06 ~ 2026-05-29 |
| 형식 | 1회성 통보형 개인화 메모 |
| 입력 cutoff | 2026-05-04 PM까지 |
| 목표 | 시장·원장·제약에 근거한 최적 판단 절차, 수익률 예측 아님 |
| 맞춤화 원칙 | 조언 내용은 persona의 목표·분석 방식·위험 성향에 맞게 개인화하되, 말투는 일반적이고 중립적인 LLM 문체를 사용하며 persona 일치를 결론보다 우선하지 않음 |
| 행동 통계 역할 | 대상 선정 점수가 아닌 Advisor 상담용 사실 요약 |
| 수익률 역할 | 행동의 좋고 나쁨을 단독 판정하지 않는 보조 근거 |
| 미실현손익 원가 | `portfolio_state.positions[].avg_cost` 이동평균 단가 |
| 거래 직전 총자산 | fill 직전 turn의 `portfolio_state.total_value` |
| 메인 에이전트 reasoning | 전체 기간 OFF |
| Advisor 생성 reasoning | 별도 1회성 호출만 ON |
| Advisor API/model | OpenRouter `qwen/qwen3.5-flash-02-23` |
| Advisor provider | `alibaba`, fallback 금지 |
| Advisor 인증 | 프로젝트 `.env`의 기존 `OPENROUTER_API_KEY` |
| 주입 위치 | analysis + decision, LTB/STB 직접 덮어쓰기 금지 |
| 핵심 비교 | 동일 5월 4일 상태의 Advisor ON 대 OFF |
| 데이터 | `rn_ab_sealed_to_20260529_v1` profile |
| legacy 결과 DB | 분석 참고만 가능, 새 runtime 입력으로 사용 금지 |

## 15. 구현 전 남은 결정

1. 1차 실험의 Community 조건을 OFF, ON 또는 4셀 중 무엇으로 할지
2. 1차 결과 뒤 선택적 반복 실행을 할지 여부

이 항목과 코드·artifact 계약이 확정되기 전에는 유료 Advisor 생성이나 본실험을
시작하지 않는다.
