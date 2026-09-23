from __future__ import annotations

import hashlib
import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script(filename: str):
    path = PROJECT_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(
        filename.replace(".", "_"),
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NumberedPrepareStepTests(unittest.TestCase):
    def test_news_step_is_read_only_sealed_validation(self) -> None:
        module = _load_script("02_prepare_news.py")
        report = module.validate_sealed_news(
            config.SEALED_REAL_NEWS_BUNDLE.parent
        )
        self.assertTrue(report["validation_pass"])
        self.assertEqual(report["mode"], "read_only_sealed_news_validation")
        self.assertEqual(report["stock_code"], "005930")
        self.assertEqual(report["event_count"], 196)

    def test_market_step_default_validation_does_not_modify_database(self) -> None:
        module = _load_script("03_load_stock_data.py")
        before = _sha256(config.SIM_DB)
        with redirect_stdout(io.StringIO()):
            module.main([])
        self.assertEqual(_sha256(config.SIM_DB), before)

    def test_retired_side_runners_are_absent(self) -> None:
        self.assertFalse(
            (PROJECT_ROOT / "scripts" / "06_run_community_smoke_test.py").exists()
        )
        self.assertFalse(
            (PROJECT_ROOT / "scripts" / "07_prepare_fake_news_injection.py").exists()
        )


if __name__ == "__main__":
    unittest.main()
