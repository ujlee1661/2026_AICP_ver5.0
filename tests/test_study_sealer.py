from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import config
from twinmarket_kr.agents.news_agent import SealedNewsBundle
from twinmarket_kr.experiment_runtime import canonical_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_sealer():
    path = PROJECT_ROOT / "scripts" / "15_seal_study.py"
    spec = importlib.util.spec_from_file_location(
        "integrated_study_sealer",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import sealer: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IntegratedStudySealerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sealer = _load_sealer()

    def test_common_sealer_has_no_legacy_rn_import(self) -> None:
        source = (
            PROJECT_ROOT / "scripts" / "15_seal_study.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("twinmarket_kr.rn_ab", source)
        self.assertNotIn("persona_snapshot", source)

    def test_sealer_refuses_to_overwrite_an_existing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "sealed"
            existing.mkdir()
            with self.assertRaisesRegex(
                FileExistsError,
                "refusing to overwrite",
            ):
                self.sealer.main(["--out", str(existing)])

    def test_projection_pins_every_agent_and_matching_metadata(self) -> None:
        projection = self.sealer.build_persona_projection(
            config.SYS_100_DB
        )
        rows = projection["agents"]
        self.assertEqual(len(rows), 100)
        self.assertEqual(
            Counter(int(row["news_depth"]) for row in rows),
            Counter({1: 55, 0: 30, 2: 15}),
        )
        self.assertEqual(
            Counter(int(row["can_post_community"]) for row in rows),
            Counter({1: 70, 0: 30}),
        )
        self.assertTrue(
            all(
                len(str(row["structured_persona_sha256"])) == 64
                and len(str(row["persona_sha256"])) == 64
                for row in rows
            )
        )

    def test_explicit_agent_file_creates_an_ordered_subset_profile(self) -> None:
        selected = ["A002", "A001", "A100"]
        projection = self.sealer.build_persona_projection(
            config.SYS_100_DB,
            agent_ids=selected,
        )
        cohort = self.sealer.build_cohort(
            projection,
            config.FIXED_SLOTS_CSV,
        )

        self.assertEqual(
            [row["agent_id"] for row in projection["agents"]],
            selected,
        )
        self.assertEqual(
            [row["agent_id"] for row in cohort["agents"]],
            selected,
        )
        self.assertEqual(
            [row["ordinal"] for row in cohort["agents"]],
            [1, 2, 3],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text(
                json.dumps(selected),
                encoding="utf-8",
            )
            self.assertEqual(
                self.sealer.load_agent_ids(path),
                selected,
            )

    def test_checked_in_projection_is_current(self) -> None:
        expected = self.sealer.build_persona_projection(
            config.SYS_100_DB
        )
        path = config.SEALED_PROFILE_ROOT / "persona_projection.json"
        actual = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            canonical_sha256(actual),
            canonical_sha256(expected),
        )

    def test_review_records_actual_deterministic_validation_only(self) -> None:
        root = config.SEALED_PROFILE_ROOT
        bundle = SealedNewsBundle.load(root / "news.json")
        calendar = json.loads(
            (root / "calendar.json").read_text(encoding="utf-8")
        )
        stage_inputs = json.loads(
            (root / "stage_inputs.json").read_text(encoding="utf-8")
        )

        news_bytes = (root / "news.json").read_bytes()
        review = self.sealer.build_review(
            news_bundle=bundle,
            calendar=calendar,
            stage_inputs=stage_inputs,
            news_file_sha256=hashlib.sha256(news_bytes).hexdigest(),
        )

        # The recorded file digest must describe the sealed news.json shipped
        # beside the review, not the source candidate manifest the sealer read.
        self.assertEqual(
            review["real_news_bundle_file_sha256"],
            hashlib.sha256(news_bytes).hexdigest(),
        )
        self.assertEqual(
            review["real_news_bundle_manifest_sha256"],
            bundle.bundle_sha256,
        )
        self.assertTrue(review["validation_pass"])
        self.assertEqual(review["article_count"], len(bundle.articles))
        self.assertEqual(
            review["shortage_event_count"],
            len(bundle.accepted_shortages),
        )
        serialized = json.dumps(review, ensure_ascii=False)
        self.assertNotIn("reviewer_credential", serialized)
        self.assertNotIn("reviewed_at", serialized)
        self.assertNotIn("candidate_reviews", review)

    def test_calendar_queries_are_parameterized(self) -> None:
        dates = self.sealer.trading_dates(
            config.SIM_DB,
            stock_code=config.STOCK_CODE,
            start_date="2026-02-27",
            end_date="2026-02-27",
        )
        self.assertEqual(dates, ["2026-02-27"])

    def test_price_registry_query_reads_the_same_single_date(self) -> None:
        prices = self.sealer.stock_prices(
            config.SIM_DB,
            stock_code=config.STOCK_CODE,
            start_date="2026-02-27",
            end_date="2026-02-27",
        )
        self.assertEqual(list(prices), ["2026-02-27"])
        self.assertGreater(float(prices["2026-02-27"]["open"]), 0)
        self.assertGreater(float(prices["2026-02-27"]["close"]), 0)

if __name__ == "__main__":
    unittest.main()
