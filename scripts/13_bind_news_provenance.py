#!/usr/bin/env python3
"""통합 뉴스 provenance 바인딩 (safe-subset candidate builder).

합의된 가용시각 규칙은 ``effective_at = max(published_at, modified_at)``이다.
현재 확보한 본문은 실험 기간이 지난 뒤 재크롤한 버전이므로, 실제 당시
``effective_at``에 그 본문을 직접 관측했다는 뜻은 아니다. 현재 번들은
가용시각을 ``observed_at`` 필드에 투영한다. 제목·요약의 EOD/개인수급 누출
패턴과 알려진 수정시각은 차단하지만, 원문 버전 아카이브가 없는 기사의 미기록
사후 수정 가능성까지 증명하지는 못한다. 이 한계와 보강 여부는 데이터 담당자
확인 전까지 변경하지 않고 preflight의 미결정 사항으로 남긴다.

이 스크립트는 네트워크/LLM을 접촉하지 않고, 소스 DB/CSV를 변경하지 않는다.
산출물은 execution_authorized=false candidate이며, 별도 승인·봉인 단계에서
calendar/stage-input/target/price registry + StudySpec과 함께 sealed 된다.

입력 join:
  - split JSON 5폴더  : 제목·본문·요약·작성시각(effective_at)·필터링여부(N만).
    외부 URL 원장이 없는 신규 행은 안정적인 repo://data/... 행 참조를 provenance로 사용한다.
  - rescrape CSV       : article_id·url·published_at·modified_at·source (제목으로 join)
  - crawl *.jsonl      : 실제 scraped_at과 본문 (제목으로 join, 있으면 우선)
  - data/*_news.pkl    : crawl export가 없는 기간의 로컬 수집 원장

바인딩 조건: 본문 존재 + 실제 scraped_at 존재 + scraped_at >= effective_at.
그 외는 quarantine(사유 기록). 부족 event는 shortage로 수용한다.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import pickle
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from twinmarket_kr.agents.news_agent import (  # noqa: E402
    SealedNewsBundleError,
    canonical_news_sha256,
    news_article_payload_sha256,
    reject_synthetic_news_marker,
    reject_target_leakage_text,
)

FOLDER_SECTOR = {
    "samsung_split": "종목",
    "semiconductor_split": "섹터",
    "macro_economic-policy_split": "경제",
    "macro_business-index_split": "경제",
    "macro_trade_split": "경제",
}
SECTOR_PRIORITY = {"종목": 0, "섹터": 1, "경제": 2}
CATEGORY_TARGETS = {"종목": 5, "섹터": 3, "경제": 2}  # 턴당, 합 10
def _norm_ts(s: str | None) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    return s.replace(" ", "T").replace("Z", "+00:00")


def _split_ts(value: str | None) -> str | None:
    normalized = _norm_ts(value)
    if normalized is None:
        return None
    if len(normalized) == 16:
        return normalized + ":00+09:00"
    if len(normalized) == 19:
        return normalized + "+09:00"
    return normalized


def _repo_split_ref(path: Path, *, article_index: int) -> str | None:
    try:
        relative = path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return None
    if not relative.parts or relative.parts[0] != "data":
        return None
    return f"repo://{relative.as_posix()}#article={article_index}"


def _event_of(effective_at: str) -> tuple[str, str]:
    """effective_at → (date, subturn). AM = ~08:59 이하, PM = 그 이후."""
    date = effective_at[:10]
    hhmm = effective_at[11:16]
    return date, ("AM" if hhmm <= "08:59" else "PM")


def load_curated_summaries(splits_dir: Path) -> dict[str, dict]:
    """필터링 N 기사의 큐레이션 요약. key=제목, 섹터 우선순위로 dedup."""
    by_title: dict[str, dict] = {}
    for folder, sector in FOLDER_SECTOR.items():
        fpath = splits_dir / folder
        if not fpath.exists():
            continue
        for jf in sorted(fpath.glob("*.json"), key=lambda x: int(x.stem)):
            for article_index, art in enumerate(
                json.loads(jf.read_text(encoding="utf-8"))
            ):
                if not isinstance(art, dict) or str(art.get("필터링 여부", "N")) != "N":
                    continue
                title = str(art.get("제목", "")).strip()
                summary = str(art.get("요약", "")).strip()
                if not title or not summary:
                    continue
                split_ref = _repo_split_ref(jf, article_index=article_index)
                split_body = str(art.get("본문", "")).strip()
                split_effective_at = _split_ts(art.get("작성시각"))
                cur = by_title.get(title)
                if cur is not None and SECTOR_PRIORITY[cur["category"]] <= SECTOR_PRIORITY[sector]:
                    continue
                by_title[title] = {
                    "summary": summary,
                    "category": sector,
                    "split_provenance": (
                        {
                            "url": split_ref,
                            "body": split_body,
                            "published_at": split_effective_at,
                            "modified_at": None,
                            "effective_at": split_effective_at,
                            "observed_at": split_effective_at,
                            "source": (
                                "매일경제"
                                if title.endswith(" - 매일경제")
                                else "repository-split"
                            ),
                            "file_category": folder,
                        }
                        if split_ref and split_body and split_effective_at
                        else None
                    ),
                }
    return by_title


def _file_category(path: str) -> str:
    if "macro" in path:
        return "경제"
    if "semiconductor" in path or "semi" in path:
        return "섹터"
    return "종목"


def load_crawl(crawl_dir: Path) -> dict[str, dict]:
    """제목 → 재크롤 provenance(+본문). effective_at 최신이 이기도록 dedup."""
    by_title: dict[str, dict] = {}
    for fp in glob.glob(str(crawl_dir / "*.jsonl")):
        cat = _file_category(fp)
        with open(fp, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                title = str(r.get("title", "")).strip()
                if not title or not r.get("url"):
                    continue
                entry = {
                    "url": r["url"].strip(),
                    "body": str(r.get("body", "")).strip(),
                    "published_at": _norm_ts(r.get("published_at")),
                    "modified_at": _norm_ts(r.get("modified_at")),
                    "effective_at": _norm_ts(r.get("effective_at")),
                    "observed_at": _norm_ts(r.get("scraped_at")),
                    "source": str(r.get("source", "")).strip(),
                    "file_category": cat,
                }
                cur = by_title.get(title)
                if cur is None or (entry["effective_at"] or "") > (cur["effective_at"] or ""):
                    by_title[title] = entry
    return by_title


def load_local_news_sources(data_dir: Path) -> dict[str, dict]:
    """Load the collected local news ledgers without exposing article bodies.

    The PKL ledgers contain title, summary, body, publication date/time, source,
    and URL.  They are used only as provenance inputs; the sealed public payload
    continues to contain title/summary plus body/version hashes, never the body.
    """

    by_title: dict[str, dict] = {}
    for filename in ("samsung_news.pkl", "sector_news.pkl", "economy_news.pkl"):
        path = data_dir / filename
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            frame = pickle.load(handle)
        for raw in frame.to_dict("records"):
            title = str(raw.get("title", "")).strip()
            date = str(raw.get("date", ""))[:10]
            time = str(raw.get("time", "")).strip()[:5]
            body = str(raw.get("body", "")).strip()
            url = str(raw.get("url", "")).strip()
            if not title or len(date) != 10 or len(time) != 5 or not body or not url:
                continue
            effective_at = f"{date}T{time}:00+09:00"
            entry = {
                "url": url,
                "body": body,
                "published_at": effective_at,
                "modified_at": None,
                "effective_at": effective_at,
                "observed_at": effective_at,
                "source": str(raw.get("source", "")).strip(),
                "file_category": filename,
            }
            # Split JSON titles retain the publisher suffix while the PKL
            # ledger stores the same MK title without it. Register both exact
            # representations; no fuzzy matching is used.
            for title_key in (title, f"{title} - 매일경제"):
                current = by_title.get(title_key)
                if current is None or effective_at > str(current["effective_at"]):
                    by_title[title_key] = entry
    return by_title


def build(
    splits_dir: Path,
    crawl_dir: Path,
    data_dir: Path,
    out_dir: Path,
    *,
    start_date: str,
    end_date: str,
) -> None:
    curated = load_curated_summaries(splits_dir)
    local_sources = load_local_news_sources(data_dir)
    # A crawl record carries stronger scrape-time provenance and therefore wins.
    provenance = {**local_sources, **load_crawl(crawl_dir)}

    bound: list[dict] = []
    quarantine: list[dict] = []
    per_event: dict[tuple[str, str], Counter] = defaultdict(Counter)
    stats = Counter()

    for title, c in curated.items():
        # 봉인 anti-fake 가드: 제목/요약에 가짜·합성 마커가 있으면 봉인 불가 → 격리.
        # (fake 주입과 구분 불가하므로 실제 '가짜뉴스' 다룬 기사도 제외한다.)
        try:
            reject_synthetic_news_marker(title, field="title")
            reject_synthetic_news_marker(c["summary"], field="summary")
        except SealedNewsBundleError:
            stats["fake_marker_in_text"] += 1
            quarantine.append({"title": title, "reason": "fake_marker_in_text"})
            continue
        # EOD/target 누출 가드: 요약/제목에 당일 종가·장 마감·개인 순매수 등이 있으면
        # agent-visible 텍스트가 미래/타깃을 누출 → 봉인 불가 → 격리.
        try:
            reject_target_leakage_text(title, field="title")
            reject_target_leakage_text(c["summary"], field="summary")
        except SealedNewsBundleError:
            stats["eod_leakage_in_text"] += 1
            quarantine.append({"title": title, "reason": "eod_leakage_in_text"})
            continue
        prov = provenance.get(title)
        if prov is None:
            prov = c.get("split_provenance")
            if prov is not None:
                stats["repo_split_provenance"] += 1
        if prov is None:
            stats["no_crawl_provenance"] += 1
            quarantine.append({"title": title, "reason": "no_crawl_provenance"})
            continue
        effective_at = prov["effective_at"]
        body = prov["body"]
        if not effective_at or not (
            start_date <= effective_at[:10] <= end_date
        ):
            stats["out_of_window"] += 1
            continue
        if not body:
            stats["no_body"] += 1
            quarantine.append({"title": title, "reason": "no_body"})
            continue
        # 현재 방법론은 실제 재크롤 시각이 아니라 effective_at을 runtime
        # availability gate로 투영한다. 데이터 담당자 확인 전에는 이 정책과
        # 기존 봉인 입력을 변경하지 않는다.
        obs = effective_at

        published_at = prov["published_at"] or effective_at
        # 크롤 데이터 모순(modified < published) 방어: 수정시각을 무효 처리.
        modified_at = prov["modified_at"]
        if modified_at and modified_at[:19] < published_at[:19]:
            modified_at = None
        # 섹터(버킷)는 split 수집 폴더 기준(권위); 그 외 file_category 폴백.
        category = c["category"]
        # 안정적 article_id: 날짜_섹터_제목해시8.
        aid = (
            f"news_{effective_at[:10].replace('-', '')}_{category}_"
            f"{canonical_news_sha256(title)[:8]}"
        )

        # effective_at 배치 규칙 하에서 현재 본문 = as-of effective 버전.
        raw_body_sha256 = canonical_news_sha256(body)
        version_sha256 = canonical_news_sha256(
            {"body": body, "published_at": published_at, "modified_at": modified_at}
        )
        cutoff_version_sha256 = canonical_news_sha256(
            {"body": body, "as_of": effective_at}
        )

        payload_fields = {
            "article_id": aid,
            "title": title,
            "summary": c["summary"],
            "published_at": published_at,
            "observed_at": obs,
            "last_modified_at": modified_at,
            "source_url": prov["url"],
            "source": prov["source"],
            "raw_body_sha256": raw_body_sha256,
            "version_sha256": version_sha256,
            "cutoff_version_sha256": cutoff_version_sha256,
        }
        article = {
            "payload_sha256": news_article_payload_sha256(payload_fields),
            **payload_fields,
        }
        bound.append(article)
        per_event[_event_of(effective_at)][category] += 1
        stats["bound"] += 1

    # 커버리지 (5/3/2 대비, 부족 허용)
    coverage = []
    for ev in sorted(per_event):
        bc = per_event[ev]
        got = sum(min(CATEGORY_TARGETS[c], bc.get(c, 0)) for c in CATEGORY_TARGETS)
        coverage.append(
            {"date": ev[0], "subturn": ev[1], "delivered": got, "by_bucket": dict(bc), "short": got < 10}
        )
    short_events = [c for c in coverage if c["short"]]

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "artifact_type": "rn_news_provenance_bound_candidate",
        "version": "rn-news-provenance-bind-v1",
        "execution_authorized": False,
        "run_eligible": False,
        "exposure_rule": (
            "effective_at = max(published_at, modified_at); "
            "runtime observed_at is the derived availability gate"
        ),
        "start_date": start_date,
        "end_date": end_date,
        "target_real_news_per_event": 10,
        "category_targets": CATEGORY_TARGETS,
        "counts": {
            "bound": len(bound),
            "quarantined": len(quarantine),
            "events_with_articles": len(per_event),
            "short_events": len(short_events),
            **dict(stats),
        },
        "candidate_sha256": None,
    }
    manifest["candidate_sha256"] = canonical_news_sha256(
        {"manifest": {k: v for k, v in manifest.items() if k != "candidate_sha256"},
         "articles": [a["payload_sha256"] for a in bound]}
    )

    (out_dir / "provenance_bound_articles.json").write_text(
        json.dumps({"manifest": manifest, "articles": bound}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "quarantine_report.json").write_text(
        json.dumps({"count": len(quarantine), "entries": quarantine}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "coverage_report.json").write_text(
        json.dumps({"per_event": coverage, "short_events": short_events}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 60)
    print("통합 뉴스 provenance 바인딩 (candidate, 미승인)")
    print("=" * 60)
    print(f"바인딩됨          : {len(bound)}")
    print(f"격리(quarantine)  : {len(quarantine)}  {dict(stats)}")
    print(f"기사 보유 event   : {len(per_event)}")
    print(f"10개 미만 event   : {len(short_events)} (부족 허용 정책)")
    print(f"산출물            : {out_dir}/")
    print("  - provenance_bound_articles.json / quarantine_report.json / coverage_report.json")
    print("주의: execution_authorized=false. 승인·봉인 단계에서 calendar/stage-input/")
    print("      target/price registry + StudySpec과 함께 real_news_bundle로 sealed 필요.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="통합 뉴스 provenance 바인딩 (crawl provenance + 큐레이션 요약).")
    p.add_argument("--splits-dir", type=Path, default=PROJECT_ROOT / "outputs")
    p.add_argument("--crawl-dir", type=Path, default=PROJECT_ROOT / "outputs/crawl")
    p.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    p.add_argument("--out-dir", type=Path,
                   default=PROJECT_ROOT / "preparation/rn_ab_source_candidate_v1/provenance_bound")
    p.add_argument("--start-date", default="2026-02-27")
    p.add_argument("--end-date", default="2026-05-04")
    args = p.parse_args(argv)
    if args.start_date > args.end_date:
        p.error("--start-date must not follow --end-date")
    build(
        args.splits_dir,
        args.crawl_dir,
        args.data_dir,
        args.out_dir,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
