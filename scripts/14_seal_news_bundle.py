#!/usr/bin/env python3
"""통합 실뉴스 번들 조립·검증 (real_news_bundle_manifest candidate).

script 13의 provenance 바인딩 결과를 요청한 거래일×2 event 슬롯에 매핑하고,
공통 ``SealedNewsBundle`` 스키마로 묶어 검증한다. 네트워크/LLM 미접촉.

배치 규칙: 기사 노출시각 = effective_at(=observed_at, script 13에서 확정).
각 기사를 effective_at <= cutoff 를 만족하는 가장 이른 event에 배치(gapless).
event당 카테고리 쿼터 5/3/2(합 10), 부족하면 shortage 기록(부족 허용 정책).

산출: real_news_bundle_manifest.json  (execution_authorized=false candidate).
검증: 임시 파일을 공통 ``SealedNewsBundle.load``로 다시 읽어 검증한다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.agents.news_agent import (  # noqa: E402
    SealedNewsBundle,
    news_bundle_content_sha256,
    news_fake_registry_sha256,
)

TARGET = 10
CATEGORY_TARGETS = {"종목": 5, "섹터": 3, "경제": 2}
CATEGORY_ORDER = ["종목", "섹터", "경제"]
TZ = "+09:00"


def trading_dates(
    sim_db: Path,
    *,
    stock_code: str,
    start_date: str,
    end_date: str,
) -> list[str]:
    with sqlite3.connect(sim_db) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM StockData WHERE stock_id=? AND date BETWEEN ? AND ? ORDER BY date",
            (stock_code, start_date, end_date),
        ).fetchall()
    return [r[0] for r in rows]


def build_events(dates: list[str]) -> list[dict]:
    """명시적 거래일 event + 뉴스 윈도(사용자 확정 의도).

    - AM(D): 전 거래일 15:30 ~ 당일 08:59  (cutoff 08:59)
    - PM(D): 당일 09:00 ~ 당일 15:29        (cutoff 15:30; 15:30은 다음 AM)
    윈도우 경계는 분(HH:MM) 단위로 비교한다.
    """
    # 캘린더 규약과 동일한 event_id/윈도우 (calendar_event_registry decision_event_id):
    #   event_id = "{date}/AM" | "{date}/PM"
    #   AM(D): (전 거래일 15:30, D 08:59]   PM(D): (D 08:59, D 15:30]
    # 경계 비교는 분(HH:MM) 단위. AM은 start 배타, PM은 start 배타.
    events = []
    for i, d in enumerate(dates):
        prev = dates[i - 1] if i > 0 else None
        am_start = f"{prev}T15:30" if prev else "0000-01-01T00:00"
        events.append({
            "event_id": f"{d}/AM", "date": d, "subturn": "am",
            "cutoff": f"{d}T08:59:00{TZ}",
            "win_start": am_start, "win_end": f"{d}T08:59",
        })
        events.append({
            "event_id": f"{d}/PM", "date": d, "subturn": "pm",
            "cutoff": f"{d}T15:30:00{TZ}",
            "win_start": f"{d}T08:59", "win_end": f"{d}T15:30",
        })
    return events


def _cat_of(article_id: str) -> str:
    # news_{YYYYMMDD}_{category}_{hash8}
    parts = article_id.split("_")
    return parts[2] if len(parts) >= 4 else "경제"


def assign(articles: list[dict], events: list[dict]) -> tuple[dict[str, list[dict]], int]:
    """effective_at(=observed_at)이 event 뉴스 윈도우 [win_start, win_end]에 속하면 배치.

    분(HH:MM) 단위 비교. 어느 윈도우에도 안 들면 drop(마지막 PM 이후 등).
    """
    by_event: dict[str, list[dict]] = defaultdict(list)
    dropped = 0
    for a in articles:
        eff = a["observed_at"][:16]  # YYYY-MM-DDTHH:MM (= effective_at, 분 단위)
        placed = None
        for e in events:
            if e["win_start"] <= eff <= e["win_end"]:
                placed = e["event_id"]
                break
        if placed is None:
            dropped += 1
            continue
        by_event[placed].append(a)
    return by_event, dropped


def select_slots(event_articles: list[dict]) -> list[dict]:
    """원 설계의 카테고리 5/3/2를 적용하고 부족하면 있는 만큼 반환한다."""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for a in event_articles:
        by_cat[_cat_of(a["article_id"])].append(a)
    chosen: list[dict] = []
    for cat in CATEGORY_ORDER:
        pool = sorted(by_cat.get(cat, []), key=lambda x: (x["observed_at"], x["article_id"]))
        chosen.extend(pool[: CATEGORY_TARGETS[cat]])
    return chosen


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="통합 실뉴스 번들 조립·검증.")
    p.add_argument("--bound", type=Path,
                   default=PROJECT_ROOT / "preparation/rn_ab_source_candidate_v1/provenance_bound/provenance_bound_articles.json")
    p.add_argument("--sim-db", type=Path, default=PROJECT_ROOT / "outputs/sim.db")
    p.add_argument("--out", type=Path,
                   default=PROJECT_ROOT / "preparation/rn_ab_source_candidate_v1/real_news_bundle_manifest.json")
    p.add_argument(
        "--base-bundle",
        type=Path,
        default=None,
        help=(
            "Optional sealed bundle whose existing event slots are preserved "
            "exactly; only later calendar events are selected from --bound."
        ),
    )
    p.add_argument("--stock-code", default="005930")
    p.add_argument("--start-date", default="2026-02-27")
    p.add_argument("--end-date", default="2026-05-04")
    args = p.parse_args(argv)
    if args.start_date > args.end_date:
        p.error("--start-date must not follow --end-date")

    bound = json.loads(args.bound.read_text(encoding="utf-8"))["articles"]
    base = (
        json.loads(args.base_bundle.read_text(encoding="utf-8"))
        if args.base_bundle is not None
        else None
    )
    base_slots_by_event: dict[str, list[dict]] = defaultdict(list)
    base_articles: dict[str, dict] = {}
    base_shortages: dict[str, dict] = {}
    if base is not None:
        SealedNewsBundle.load(
            args.base_bundle,
            expected_stock_code=args.stock_code,
        )
        base_articles = {
            str(article["article_id"]): article
            for article in base["articles"]
        }
        for slot in base["slots"]:
            base_slots_by_event[str(slot["event_id"])].append(slot)
        base_shortages = dict(base.get("accepted_shortages") or {})
    dates = trading_dates(
        args.sim_db,
        stock_code=args.stock_code,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    events = build_events(dates)
    by_event, dropped = assign(bound, events)

    used_articles: dict[str, dict] = {}
    slots: list[dict] = []
    accepted_shortages: dict[str, dict] = {}
    empty_events: list[str] = []

    for e in events:
        eid = e["event_id"]
        if eid in base_slots_by_event:
            preserved = sorted(
                base_slots_by_event[eid],
                key=lambda row: int(row["slot_ordinal"]),
            )
            for slot in preserved:
                article_id = str(slot["article_id"])
                used_articles[article_id] = base_articles[article_id]
                slots.append(dict(slot))
            if eid in base_shortages:
                accepted_shortages[eid] = base_shortages[eid]
            continue
        chosen = select_slots(by_event.get(eid, []))
        if not chosen:
            empty_events.append(eid)
            continue
        article_ids, hashes = [], []
        for ordinal, a in enumerate(chosen, start=1):
            used_articles[a["article_id"]] = a
            slots.append({
                "event_id": eid,
                "slot_ordinal": ordinal,
                "article_id": a["article_id"],
                "payload_sha256": a["payload_sha256"],
            })
            article_ids.append(a["article_id"])
            hashes.append(a["payload_sha256"])
        if len(chosen) != TARGET:
            accepted_shortages[eid] = {
                "target_real_count": TARGET,
                "selected_safe_count": len(chosen),
                "serialized_count": len(chosen),
                "delivered_real_count": len(chosen),
                "actual_real_count": len(chosen),
                "missing_real_count": TARGET - len(chosen),
                "coverage_status": "shortage_accepted",
                "ordered_article_ids": article_ids,
                "ordered_payload_sha256": hashes,
            }

    # Article payload는 스키마 정확히 12필드(article_id + 11). payload_sha256 별도.
    article_list = []
    for a in used_articles.values():
        article_list.append({k: v for k, v in a.items()})

    bundle = {
        "artifact_type": "real_news_bundle_manifest",
        "bundle_sha256": "",  # 아래에서 채움
        "stock_code": args.stock_code,
        "target_real_news_per_event": TARGET,
        "fake_news_per_event": 0,
        "articles": article_list,
        "slots": slots,
        "accepted_shortages": accepted_shortages,
        "known_fake_ids": [],
        "known_fake_payload_hashes": [],
        "fake_registry_sha256": news_fake_registry_sha256(
            known_fake_ids=[],
            known_fake_payload_hashes=[],
        ),
    }
    bundle["bundle_sha256"] = news_bundle_content_sha256(bundle)

    # 공통 runtime loader가 실제로 수용하는 파일만 최종 경로로 승격한다.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        validated = SealedNewsBundle.load(
            temporary,
            expected_stock_code=args.stock_code,
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(args.out)

    covered = len({s["event_id"] for s in slots})
    full = sum(1 for e in events if e["event_id"] not in accepted_shortages and e["event_id"] not in empty_events)
    print("=" * 60)
    print("통합 실뉴스 번들 조립·검증 (candidate)")
    print("=" * 60)
    print(f"거래일 {len(dates)} → event {len(events)}")
    print(f"슬롯 배치 event : {covered}/{len(events)}")
    print(f"  10개 완비    : {full}")
    print(f"  shortage     : {len(accepted_shortages)}")
    print(f"  빈 event(기사0): {len(empty_events)}  {empty_events[:5]}")
    print(f"사용 기사      : {len(article_list)} / 슬롯 {len(slots)} / 윈도우밖 drop {dropped}")
    print(
        "SealedNewsBundle.load          : ✅ 통과 "
        f"({validated.bundle_sha256[:12]}…)"
    )
    print(f"산출물: {args.out}")
    if empty_events:
        print("⚠️ 기사 0개 event는 현재 bundle schema로 실행할 수 없습니다.")
        return 1
    print("✅ 번들 스키마·슬롯 검증 통과 (execution_authorized=false candidate)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
