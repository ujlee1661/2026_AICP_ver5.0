from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import config
from twinmarket_kr.agents.news_agent import (
    NewsAgent,
    SealedNewsBundle,
    SealedNewsBundleError,
)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _article(index: int, *, event_date: str = "2026-02-27") -> dict[str, Any]:
    payload = {
        "article_id": f"news_20260227_종목_{index:08x}",
        "title": f"검증 기사 {index}",
        "summary": f"검증 가능한 기사 요약 {index}",
        "published_at": f"{event_date}T08:{index:02d}:00+09:00",
        "observed_at": f"{event_date}T08:{index:02d}:00+09:00",
        "last_modified_at": None,
        "source_url": f"https://example.com/articles/{index}",
        "source": "test-source",
        "raw_body_sha256": f"{index + 1:064x}",
        "version_sha256": f"{index + 11:064x}",
        "cutoff_version_sha256": f"{index + 21:064x}",
    }
    return {
        **payload,
        "payload_sha256": _canonical_sha256(payload),
    }


def _replace_source_url(article: dict[str, Any], source_url: str) -> None:
    article["source_url"] = source_url
    payload = dict(article)
    payload.pop("payload_sha256", None)
    article["payload_sha256"] = _canonical_sha256(payload)


def _bundle_payload(*, shortage: bool = False) -> dict[str, Any]:
    articles = [_article(1)] if shortage else [_article(1), _article(2)]
    slots = [
        {
            "event_id": "2026-02-27/AM",
            "slot_ordinal": index,
            "article_id": article["article_id"],
            "payload_sha256": article["payload_sha256"],
        }
        for index, article in enumerate(articles, start=1)
    ]
    accepted_shortages: dict[str, Any] = {}
    if shortage:
        accepted_shortages["2026-02-27/AM"] = {
            "target_real_count": 2,
            "selected_safe_count": 1,
            "serialized_count": 1,
            "delivered_real_count": 1,
            "actual_real_count": 1,
            "missing_real_count": 1,
            "coverage_status": "shortage_accepted",
            "ordered_article_ids": [articles[0]["article_id"]],
            "ordered_payload_sha256": [articles[0]["payload_sha256"]],
        }
    body = {
        "artifact_type": "real_news_bundle_manifest",
        "stock_code": "005930",
        "target_real_news_per_event": 2,
        "fake_news_per_event": 0,
        "articles": articles,
        "slots": slots,
        "accepted_shortages": accepted_shortages,
        "known_fake_ids": [],
        "known_fake_payload_hashes": [],
        "fake_registry_sha256": _canonical_sha256(
            {
                "known_fake_ids": [],
                "known_fake_payload_hashes": [],
            }
        ),
    }
    return {
        **body,
        "bundle_sha256": _canonical_sha256(body),
    }


def _reseal(payload: dict[str, Any]) -> None:
    body = dict(payload)
    body.pop("bundle_sha256", None)
    payload["bundle_sha256"] = _canonical_sha256(body)


def _retime_article(
    article: dict[str, Any],
    *,
    published_at: str,
    observed_at: str,
    last_modified_at: str | None,
) -> None:
    article.update(
        {
            "published_at": published_at,
            "observed_at": observed_at,
            "last_modified_at": last_modified_at,
        }
    )
    payload = dict(article)
    payload.pop("payload_sha256", None)
    article["payload_sha256"] = _canonical_sha256(payload)


class SealedNewsBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "news.json"

    def _write(self, payload: dict[str, Any]) -> Path:
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return self.path

    def test_real_sujin_bundle_is_tracked_shape_and_loads_exactly(self) -> None:
        bundle = SealedNewsBundle.load(config.SEALED_REAL_NEWS_BUNDLE)
        self.assertEqual(bundle.stock_code, "005930")
        self.assertEqual(bundle.target_real_count, 10)
        self.assertEqual(len(bundle.slots_by_event), 196)
        self.assertEqual(
            sum(len(slots) for slots in bundle.slots_by_event.values()),
            1_602,
        )
        self.assertEqual(len(bundle.articles), 1_602)
        self.assertEqual(len(bundle.accepted_shortages), 138)
        self.assertEqual(
            bundle.bundle_sha256,
            "6ba52298c4d00029ee370c5f6d5d43d4860658e8c27dd9ed66482ee4f40dc11c",
        )

    def test_sealed_repo_split_source_is_accepted_but_arbitrary_repo_uri_is_not(self) -> None:
        payload = _bundle_payload()
        _replace_source_url(
            payload["articles"][0],
            "repo://data/samsung_split/048.json#article=3",
        )
        payload["slots"][0]["payload_sha256"] = payload["articles"][0][
            "payload_sha256"
        ]
        _reseal(payload)
        SealedNewsBundle.load(self._write(payload))

        _replace_source_url(
            payload["articles"][0],
            "repo://data/../private/secret.json#article=3",
        )
        payload["slots"][0]["payload_sha256"] = payload["articles"][0][
            "payload_sha256"
        ]
        _reseal(payload)
        with self.assertRaisesRegex(
            SealedNewsBundleError,
            "http\\(s\\) URL or a sealed repo",
        ):
            SealedNewsBundle.load(self._write(payload))

    def test_depth2_additional_results_never_repeat_current_event_base_news(
        self,
    ) -> None:
        bundle = SealedNewsBundle.load(config.SEALED_REAL_NEWS_BUNDLE)
        agent = NewsAgent(news_bundle_path=config.SEALED_REAL_NEWS_BUNDLE)
        for event_id, slots in bundle.slots_by_event.items():
            event_date, subturn = event_id.split("/", 1)
            base_ids = {slot.article_id for slot in slots}
            results = agent.search_news_flat(
                keywords=["삼성전자"],
                current_date=event_date,
                window_end_date=event_date,
                window_end_time="09:00" if subturn == "AM" else "15:30",
                lookback_days=7,
                top_n=5,
                exclude_article_ids=base_ids,
            )
            result_ids = {str(row["id"]) for row in results}
            self.assertLessEqual(len(results), 5, event_id)
            self.assertTrue(result_ids.isdisjoint(base_ids), event_id)

    def test_slot_order_and_depth_projection_are_exact(self) -> None:
        payload = _bundle_payload()
        payload["slots"].reverse()
        _reseal(payload)
        agent = NewsAgent(news_bundle_path=self._write(payload))

        depth0 = agent.build_event_context("2026-02-27/AM", 0)
        depth0 = agent.expand_context_from_selection(
            base_context=depth0,
            current_date="2026-02-27",
        )
        self.assertEqual(
            [row["slot_ordinal"] for row in depth0["daily_titles"]],
            ["1", "2"],
        )
        self.assertEqual(depth0["read_contents"], [])
        self.assertTrue(
            all(
                "summary" not in row and "content" not in row
                for row in depth0["daily_titles"]
            )
        )

        for depth in (1, 2):
            context = agent.build_event_context("2026-02-27/AM", depth)
            context = agent.expand_context_from_selection(
                base_context=context,
                current_date="2026-02-27",
            )
            self.assertEqual(len(context["daily_titles"]), 2)
            self.assertEqual(len(context["read_contents"]), 2)
            self.assertEqual(
                [row["slot_ordinal"] for row in context["read_contents"]],
                ["1", "2"],
            )
            self.assertTrue(
                all(row["content"].startswith("검증 가능한") for row in context["read_contents"])
            )

    def test_accepted_shortage_is_reported_without_failure(self) -> None:
        agent = NewsAgent(news_bundle_path=self._write(_bundle_payload(shortage=True)))
        context = agent.build_event_context("2026-02-27/AM", 1)
        self.assertEqual(
            context["coverage"],
            {
                "event_id": "2026-02-27/AM",
                "target_real_count": 2,
                "delivered_real_count": 1,
                "missing_real_count": 1,
                "coverage_status": "shortage_accepted",
            },
        )

    def test_depth2_search_requires_provenance_and_a_visible_slot_by_cutoff(
        self,
    ) -> None:
        visible_now = _article(1)
        observed_late = _article(2)
        future_slot_only = _article(3)
        _retime_article(
            observed_late,
            published_at="2026-02-27T08:30:00+09:00",
            observed_at="2026-02-27T09:20:00+09:00",
            last_modified_at="2026-02-27T09:10:00+09:00",
        )
        _retime_article(
            future_slot_only,
            published_at="2026-02-27T08:40:00+09:00",
            observed_at="2026-02-27T08:41:00+09:00",
            last_modified_at=None,
        )
        articles = [visible_now, observed_late, future_slot_only]
        slots = [
            {
                "event_id": "2026-02-27/AM",
                "slot_ordinal": 1,
                "article_id": visible_now["article_id"],
                "payload_sha256": visible_now["payload_sha256"],
            },
            {
                "event_id": "2026-02-27/PM",
                "slot_ordinal": 1,
                "article_id": observed_late["article_id"],
                "payload_sha256": observed_late["payload_sha256"],
            },
            {
                "event_id": "2026-02-27/PM",
                "slot_ordinal": 2,
                "article_id": future_slot_only["article_id"],
                "payload_sha256": future_slot_only["payload_sha256"],
            },
        ]
        shortage = {
            "target_real_count": 2,
            "selected_safe_count": 1,
            "serialized_count": 1,
            "delivered_real_count": 1,
            "actual_real_count": 1,
            "missing_real_count": 1,
            "coverage_status": "shortage_accepted",
            "ordered_article_ids": [visible_now["article_id"]],
            "ordered_payload_sha256": [visible_now["payload_sha256"]],
        }
        body = {
            "artifact_type": "real_news_bundle_manifest",
            "stock_code": "005930",
            "target_real_news_per_event": 2,
            "fake_news_per_event": 0,
            "articles": articles,
            "slots": slots,
            "accepted_shortages": {"2026-02-27/AM": shortage},
            "known_fake_ids": [],
            "known_fake_payload_hashes": [],
            "fake_registry_sha256": _canonical_sha256(
                {
                    "known_fake_ids": [],
                    "known_fake_payload_hashes": [],
                }
            ),
        }
        agent = NewsAgent(
            news_bundle_path=self._write(
                {
                    **body,
                    "bundle_sha256": _canonical_sha256(body),
                }
            )
        )

        am_results = agent.search_news_flat(
            keywords=["검증"],
            current_date="2026-02-27",
            window_end_date="2026-02-27",
            window_end_time="09:00",
        )
        self.assertEqual(
            [row["id"] for row in am_results],
            [visible_now["article_id"]],
        )

        pm_results = agent.search_news_flat(
            keywords=["검증"],
            current_date="2026-02-27",
            window_end_date="2026-02-27",
            window_end_time="15:30",
        )
        self.assertEqual(
            {row["id"] for row in pm_results},
            {
                visible_now["article_id"],
                observed_late["article_id"],
                future_slot_only["article_id"],
            },
        )

        additional_results = agent.search_news_flat(
            keywords=["검증"],
            current_date="2026-02-27",
            window_end_date="2026-02-27",
            window_end_time="15:30",
            exclude_article_ids={visible_now["article_id"]},
        )
        self.assertEqual(
            {row["id"] for row in additional_results},
            {
                observed_late["article_id"],
                future_slot_only["article_id"],
            },
        )
        self.assertNotIn(
            visible_now["article_id"],
            {row["id"] for row in additional_results},
        )

    def test_bundle_self_hash_rejects_unsealed_edit(self) -> None:
        payload = _bundle_payload()
        payload["articles"][0]["title"] = "몰래 바꾼 제목"
        with self.assertRaisesRegex(
            SealedNewsBundleError,
            "bundle_sha256 does not bind",
        ):
            SealedNewsBundle.load(self._write(payload))

    def test_article_payload_hash_rejects_resealed_text_edit(self) -> None:
        payload = _bundle_payload()
        payload["articles"][0]["title"] = "다시 봉인하려 한 제목"
        _reseal(payload)
        with self.assertRaisesRegex(
            SealedNewsBundleError,
            "article payload_sha256",
        ):
            SealedNewsBundle.load(self._write(payload))

    def test_slot_hash_rejects_resealed_article_swap(self) -> None:
        payload = _bundle_payload()
        payload["slots"][0]["payload_sha256"] = payload["articles"][1][
            "payload_sha256"
        ]
        _reseal(payload)
        with self.assertRaisesRegex(
            SealedNewsBundleError,
            "slot payload hash differs",
        ):
            SealedNewsBundle.load(self._write(payload))

    def test_unknown_event_and_implicit_legacy_source_fail_closed(self) -> None:
        bundle_path = self._write(_bundle_payload())
        agent = NewsAgent(news_bundle_path=bundle_path)
        with self.assertRaisesRegex(SealedNewsBundleError, "absent"):
            agent.build_event_context("2026-03-03/AM", 1)
        with self.assertRaises(TypeError):
            NewsAgent()
        for legacy_kwargs in (
            {"processed_csv_path": bundle_path},
            {"daily_csv_path": bundle_path},
            {"use_json_splits": True},
            {"splits_dir": self.path.parent},
            {"daily_seed": 2},
            {"include_fake_news": True},
        ):
            with self.assertRaises(TypeError):
                NewsAgent(news_bundle_path=bundle_path, **legacy_kwargs)

    def test_expected_study_hash_is_enforced(self) -> None:
        with self.assertRaisesRegex(
            SealedNewsBundleError,
            "study-pinned",
        ):
            SealedNewsBundle.load(
                self._write(_bundle_payload()),
                expected_bundle_sha256="f" * 64,
            )


class NumberedEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = config.PROJECT_ROOT / "scripts" / "05_run_simulation.py"
        spec = importlib.util.spec_from_file_location("aicp_numbered_05", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot import scripts/05_run_simulation.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.module = module

    def test_default_is_the_sealed_bundle_and_legacy_flags_are_removed(self) -> None:
        parser = self.module.build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.news_bundle, config.SEALED_REAL_NEWS_BUNDLE)
        for legacy_flag in (
            "--processed-news-csv",
            "--daily-news-csv",
            "--use-fake-news-injection",
            "--fake-news-mode",
            "--fake-news-variant",
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args([legacy_flag, "legacy"])

    def test_production_files_do_not_import_rn_ab(self) -> None:
        for relative in (
            "config.py",
            "scripts/05_run_simulation.py",
            "twinmarket_kr/agents/news_agent.py",
            "twinmarket_kr/core/collect_context.py",
        ):
            source = (config.PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("twinmarket_kr.rn_ab", source, relative)


if __name__ == "__main__":
    unittest.main()
