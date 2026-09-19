# Primary simulation runs

이 파일은 현재 분석에서 우선 사용하는 두 실행 결과와 보조 실행 결과의 위치를
구분한다. 실행 결과의 내용이나 메타데이터는 수정하지 않았다.

## 주요 자료

### 1. 현재 Community ON 본실험

- 경로: `outputs/experiments/RN_COMM_ON_20260918/`
- 조건: `RN_COMM_ON`
- 에이전트: 100명
- 기간: 45거래일, 90 events
- 개인투자자 방향 일치율: 71.4% (burn-in 3일 제외, 30/42일)
- 방향성 보고서:
  `output/pdf/RN_COMM_ON_20260918_direction_validation/validation_report.pdf`

### 2. 과거 OFF/ON 비교 실험

- 경로: `outputs/logs/rn_ab_ver6_45day_20260826/`
- 포함 조건: `RN_COMM_OFF/`, `RN_COMM_ON/`
- 에이전트: 각 100명
- 기간: 각 45거래일, 90 events
- 개인투자자 방향 일치율:
  - OFF: 76.2% (burn-in 3일 제외, 32/42일)
  - ON: 83.3% (burn-in 3일 제외, 35/42일)
- 상태: 보존된 과거 비교 결과이며 현재 본실험의 입력으로 사용하지 않는다.

## 보조·과거 실행

주요 자료와 혼동하지 않도록 다음 위치로 분리했다.

- offline E2E:
  `outputs/archive/supporting_runs/offline_e2e/`
- 완료되지 않은 live 실행 시도:
  `outputs/archive/supporting_runs/incomplete_live_attempts/`
- 별도 보조 비교 실험:
  `outputs/archive/supporting_runs/auxiliary_experiments/`

이 디렉터리들은 기록 보존용이다. 공식 결과를 인용할 때는 위의 주요 자료 두
경로를 먼저 사용한다.
