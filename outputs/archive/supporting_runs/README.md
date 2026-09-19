# Supporting and historical runs

이 디렉터리는 현재 주요 분석 대상이 아닌 실행 결과를 보존한다. 파일은 삭제하거나
내용을 수정하지 않고 원래 run 디렉터리 단위로 이동했다.

- `offline_e2e/`: offline stub으로 실행한 구조 검증용 run
- `incomplete_live_attempts/`: `run_complete.json`이 없는 중간 또는 중단 run
- `auxiliary_experiments/`: 현재 주요 결과와 별도로 수행한 비교·보조 실험

내부 metadata에 기록된 절대 경로는 실행 당시의 원래 위치를 나타낼 수 있다.
따라서 이곳의 run은 현재 실험 결과 인용이나 `--resume` 대상으로 사용하지 않는다.
현재 주요 자료는 `outputs/PRIMARY_RUNS.md`를 따른다.
