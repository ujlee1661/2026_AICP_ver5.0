from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.advisor.artifact import canonical_sha256


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "17_generate_advisor_artifact.py"
SPEC = importlib.util.spec_from_file_location("generate_advisor_artifact", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AdvisorGenerationCheckpointTests(unittest.TestCase):
    @staticmethod
    def _source() -> dict:
        payload = {
            "cases": [
                {"agent_id": "A001"},
                {"agent_id": "A002"},
                {"agent_id": "A003"},
            ]
        }
        payload["artifact_sha256"] = canonical_sha256(payload)
        return payload

    def test_checkpoint_round_trip_preserves_completed_prefix(self) -> None:
        source = self._source()
        messages = [{"agent_id": "A001"}, {"agent_id": "A002"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advisor.partial.json"
            MODULE._write_checkpoint(path, source=source, messages=messages)
            self.assertEqual(
                MODULE._load_checkpoint(path, source=source),
                messages,
            )

    def test_checkpoint_rejects_non_prefix_agents(self) -> None:
        source = self._source()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advisor.partial.json"
            MODULE._write_checkpoint(
                path,
                source=source,
                messages=[{"agent_id": "A002"}],
            )
            with self.assertRaisesRegex(ValueError, "not a cohort prefix"):
                MODULE._load_checkpoint(path, source=source)

    def test_checkpoint_rejects_different_cases(self) -> None:
        source = self._source()
        changed = self._source()
        changed["artifact_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advisor.partial.json"
            MODULE._write_checkpoint(
                path,
                source=source,
                messages=[{"agent_id": "A001"}],
            )
            with self.assertRaisesRegex(ValueError, "cases differ"):
                MODULE._load_checkpoint(path, source=changed)


if __name__ == "__main__":
    unittest.main()
