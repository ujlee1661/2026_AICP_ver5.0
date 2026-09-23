# Trading Direction Validation

삼성전자 실제 투자자별 순거래 데이터와 TwinMarket Korea의 canonical 완료 run을
일별 순매수/순매도 방향으로 비교한다. 이 검증기는 publication-ready run만
받으며, run 내부에는 어떤 파생 파일도 쓰지 않는다.

## 입력

| 파일 | 설명 |
| --- | --- |
| `validation/data_trading_value.csv` | 실제 투자자별 순거래대금 |
| `validation/data_trading_volume.csv` | 실제 투자자별 순거래량 |
| `<run-dir>/exchange_fills.csv` | canonical 시뮬레이션 체결 내역 |
| `<run-dir>/daily_exchange_summary.csv` | canonical 일별 가격/거래 요약 |
| `<run-dir>/run_metadata.json` | 봉인된 거래일·조건 메타데이터 |

2026-02-27~2026-07-22 구간은 KRX 정보데이터시스템의 삼성전자(`005930`)
`투자자별 거래실적(개별종목) > 일별추이 > 순매수`에서 내려받은 원본
`data/data_5238_20260923_value.csv`와
`data/data_5307_20260923_volume.csv`로 검증한다. 원본은 EUC-KR이며, 기존 원장과
겹치는 69거래일의 값이 모든 투자자 열에서 정확히 일치한 상태로 병합했다.

`--run-dir`와 `--output-dir`는 모두 명시해야 한다. `outputs/current`, latest
glob, 전역 DB, archive는 입력으로 사용하지 않는다.

## 실행

아래처럼 pair root 바깥의 파생물 위치를 지정한다.

```bash
python validation/validate_trading_direction.py \
  --run-dir outputs/experiments/<pair_id>/RN_COMM_OFF \
  --output-dir outputs/experiments/<pair_id>/derived/RN_COMM_OFF/direction_validation \
  --skip-initial-days 3
```

초기 3거래일을 포함하는 민감도 검증은 `--skip-initial-days 0`을 명시한다.

## 산출물

지정한 `<output-dir>/`에 다음 파생물이 생성된다.

| 파일 | 설명 |
| --- | --- |
| `daily_comparison_value.csv` | 거래대금 기준 일별 비교 |
| `daily_comparison_volume.csv` | 거래량 기준 일별 비교 |
| `normalized_comparison_value.csv` | 거래대금 정규화 비교 |
| `normalized_comparison_volume.csv` | 거래량 정규화 비교 |
| `summary_metrics.json` | 방향 일치율, balanced accuracy, 상관계수, baseline 비교 |
| `validation_report.pdf` | PDF 보고서 |

이 경로가 `--run-dir` 내부이거나 symlink를 통해 내부로 해석되면 실행은
중단한다. 그래야 signed artifact tree와 canonical run의 재검증 가능성이
보존된다. 과거 결과는 `archive/legacy_results/validation/`에 보관한다.

## 기준

- 시뮬레이션 체결에서 `buy`는 양수, `sell`은 음수로 환산한다.
- AM+PM gross signed fill을 일별 합산해 실제 `Individuals` 방향과 1차 비교한다.
- run metadata의 승인된 거래일 집합과 체결 ledger가 정확히 일치해야 하며,
  단순 날짜 교집합으로 조용히 축소하지 않는다.
- 삼성전자 baseline 기본 설정은 봉인된 burn-in과 같은 초기 3거래일 제외다.
- 상관계수와 코사인 유사도는 보조 지표이며, 방향 지표가 1차 해석 기준이다.
