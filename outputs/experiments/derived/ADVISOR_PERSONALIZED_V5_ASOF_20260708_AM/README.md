# 개인화 조언 실험: 2026-07-08 AM cutoff

원본 run은 2026-07-08 PM까지 커밋되었으며 2026-07-09 AM에서 중단되었습니다. 이 산출물은 원본 DB의 `turn <= 177` 기록만 읽어 만든 AM 시점 분석 cutoff입니다. PM 기록과 미커밋 7월 9일 응답은 포함하지 않습니다. 원본 run의 완료 마커나 정본 DB를 변경하지 않았습니다.

`portfolio_asof.csv`는 100명의 AM 종료 포트폴리오, `decisions_post_advisor.csv`와 `fills_post_advisor.csv`는 5월 6일 AM부터 cutoff까지의 판단과 체결, `am_lineage.csv`는 cutoff event의 STB·decision·fill·LTB 연결입니다. `cutoff_manifest.json`에 원본 및 파일 해시와 행 수가 있습니다.
