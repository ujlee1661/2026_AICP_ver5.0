from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs/experiments/ADVISOR_PERSONALIZED_V5_20261003_TO_20260710"
OUT = ROOT / "outputs/experiments/derived/ADVISOR_PERSONALIZED_V5_ASOF_20260708_AM"
DB = RUN / ".runtime/committed.db"
CUTOFF_TURN = 177
CUTOFF_EVENT = "2026-07-08/AM"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export(connection: sqlite3.Connection, query: str, path: Path) -> int:
    cursor = connection.execute(query)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([item[0] for item in cursor.description])
        count = 0
        for row in cursor:
            writer.writerow(row)
            count += 1
    return count


def main() -> None:
    checkpoint = json.loads((RUN / ".runtime/checkpoint.json").read_text())
    assert checkpoint["status"] == "paused"
    assert checkpoint["completed_events"][-2:] == [CUTOFF_EVENT, "2026-07-08/PM"]
    assert checkpoint["inflight_event"] is None
    assert (RUN / "phase_complete_2026-07-08_am.json").is_file()
    OUT.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        counts = {}
        counts["portfolio_asof"] = export(
            connection,
            "SELECT * FROM portfolio_state WHERE turn=177 ORDER BY agent_id",
            OUT / "portfolio_asof.csv",
        )
        counts["decisions_post_advisor"] = export(
            connection,
            "SELECT * FROM simulation_decisions WHERE turn BETWEEN 91 AND 177 ORDER BY turn,agent_id",
            OUT / "decisions_post_advisor.csv",
        )
        counts["fills_post_advisor"] = export(
            connection,
            "SELECT * FROM simulation_fills WHERE turn BETWEEN 91 AND 177 ORDER BY turn,agent_id",
            OUT / "fills_post_advisor.csv",
        )
        counts["am_lineage"] = export(
            connection,
            """SELECT p.agent_id,p.turn,p.state_id,p.total_value,p.cash,
                      s.stb_id,s.scientific_sha256 AS stb_sha256,
                      d.decision_id,d.scientific_sha256 AS decision_sha256,
                      f.fill_id,f.scientific_sha256 AS fill_sha256,
                      l.ltb_id,l.scientific_sha256 AS ltb_sha256
               FROM portfolio_state p
               JOIN simulation_stb_states s ON s.agent_id=p.agent_id AND s.turn=p.turn
               JOIN simulation_decisions d ON d.agent_id=p.agent_id AND d.turn=p.turn
               JOIN simulation_fills f ON f.agent_id=p.agent_id AND f.turn=p.turn
               JOIN simulation_ltb_states l ON l.agent_id=p.agent_id AND l.turn=p.turn
               WHERE p.turn=177 ORDER BY p.agent_id""",
            OUT / "am_lineage.csv",
        )
        assert counts == {
            "portfolio_asof": 100,
            "decisions_post_advisor": 8700,
            "fills_post_advisor": 8700,
            "am_lineage": 100,
        }, counts
        values = [
            float(row[0]) for row in connection.execute(
                "SELECT total_value FROM portfolio_state WHERE turn=177 ORDER BY agent_id"
            )
        ]
        initial = [
            float(row[0]) for row in connection.execute(
                "SELECT total_value FROM portfolio_state WHERE turn=0 ORDER BY agent_id"
            )
        ]
        assert len(initial) == 100
        returns = [value / base - 1 for value, base in zip(values, initial)]
    manifest = {
        "artifact_type": "advisor_analysis_cutoff_v1",
        "status": "derived_as_of_cutoff",
        "source_run_id": RUN.name,
        "source_run_status": "paused",
        "cutoff_event_id": CUTOFF_EVENT,
        "cutoff_turn": CUTOFF_TURN,
        "excluded_committed_event_id": "2026-07-08/PM",
        "source_committed_db_sha256": sha256(DB),
        "source_run_signature_sha256": sha256(RUN / "run_signature.json"),
        "cutoff_event_state_sha256": checkpoint["event_state_sha256"][CUTOFF_EVENT],
        "row_counts": counts,
        "portfolio_mean_total_value": sum(values) / len(values),
        "portfolio_mean_return_from_turn_zero": sum(returns) / len(returns),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest["files_sha256"] = {
        path.name: sha256(path) for path in sorted(OUT.glob("*.csv"))
    }
    (OUT / "cutoff_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (OUT / "README.md").write_text(
        "# 개인화 조언 실험: 2026-07-08 AM cutoff\n\n"
        "원본 run은 2026-07-08 PM까지 커밋되었으며 2026-07-09 AM에서 중단되었습니다. "
        "이 산출물은 원본 DB의 `turn <= 177` 기록만 읽어 만든 AM 시점 분석 cutoff입니다. "
        "PM 기록과 미커밋 7월 9일 응답은 포함하지 않습니다. "
        "원본 run의 완료 마커나 정본 DB를 변경하지 않았습니다.\n\n"
        "`portfolio_asof.csv`는 100명의 AM 종료 포트폴리오, "
        "`decisions_post_advisor.csv`와 `fills_post_advisor.csv`는 "
        "5월 6일 AM부터 cutoff까지의 판단과 체결, "
        "`am_lineage.csv`는 cutoff event의 STB·decision·fill·LTB 연결입니다. "
        "`cutoff_manifest.json`에 원본 및 파일 해시와 행 수가 있습니다.\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
