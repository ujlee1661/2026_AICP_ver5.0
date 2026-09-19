from __future__ import annotations

import importlib.util
import pickle
import tempfile
import unittest
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bind_news_provenance",
    PROJECT_ROOT / "scripts" / "13_bind_news_provenance.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalNewsProvenanceTests(unittest.TestCase):
    def test_local_pkl_source_provides_deterministic_publication_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = pd.DataFrame(
                [
                    {
                        "date": "2026-05-29",
                        "time": "14:25",
                        "title": "테스트 기사",
                        "category": "종목",
                        "summary": "요약",
                        "body": "원문 본문",
                        "source": "mk",
                        "url": "https://example.test/article",
                    }
                ]
            )
            with (root / "samsung_news.pkl").open("wb") as handle:
                pickle.dump(frame, handle)

            loaded = MODULE.load_local_news_sources(root)

        self.assertEqual(
            set(loaded),
            {"테스트 기사", "테스트 기사 - 매일경제"},
        )
        article = loaded["테스트 기사"]
        self.assertIs(article, loaded["테스트 기사 - 매일경제"])
        self.assertEqual(article["effective_at"], "2026-05-29T14:25:00+09:00")
        self.assertEqual(article["published_at"], article["effective_at"])
        self.assertEqual(article["body"], "원문 본문")
        self.assertNotIn("summary", article)

    def test_missing_or_incomplete_rows_are_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = pd.DataFrame(
                [
                    {
                        "date": "2026-05-29",
                        "time": "14:25",
                        "title": "본문 없음",
                        "body": "",
                        "source": "mk",
                        "url": "https://example.test/missing",
                    }
                ]
            )
            with (root / "economy_news.pkl").open("wb") as handle:
                pickle.dump(frame, handle)
            self.assertEqual(MODULE.load_local_news_sources(root), {})


if __name__ == "__main__":
    unittest.main()
