#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.memory_agent import load_agents_from_sys100
from twinmarket_kr.persona.select import (
    PERSONA_RENDERER_ID,
    load_pool,
    match_agents,
    persona_renderer_sha256,
    save_sys_100,
    structured_persona_sha256,
    verify_distribution,
)
from twinmarket_kr.persona.slots import load_fixed_slots


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"manifest must be a JSON object: {path}")
    return value


def validate_existing_baseline(
    db_path: Path = config.SYS_100_DB,
    sealed_root: Path | None = None,
) -> dict:
    """Validate the frozen Samsung cohort without changing any file."""

    sealed_root = sealed_root or config.SEALED_REAL_NEWS_BUNDLE.parent
    agents = load_agents_from_sys100(db_path)
    distribution = verify_distribution(agents)
    actual_projection = [
        {
            "ordinal": ordinal,
            "agent_id": str(agent["agent_id"]),
            "news_depth": int(agent["news_depth"]),
            "can_post_community": int(agent["can_post_community"]),
            "initial_cash": int(agent["ini_cash"]),
            "structured_persona_sha256": structured_persona_sha256(agent),
            "persona_sha256": str(agent["persona_prompt_sha256"]),
        }
        for ordinal, agent in enumerate(agents, start=1)
    ]
    projection = _load_manifest(sealed_root / "persona_projection.json")
    cohort = _load_manifest(sealed_root / "cohort.json")
    expected_cohort_projection = [
        {
            "ordinal": int(row["ordinal"]),
            "agent_id": str(row["agent_id"]),
            "news_depth": int(row["news_depth"]),
            "can_post_community": int(row["can_post_community"]),
            "initial_cash": int(row["initial_cash"]),
            "persona_sha256": str(row["persona_sha256"]),
        }
        for row in cohort.get("agents", [])
    ]
    actual_cohort_projection = [
        {
            key: row[key]
            for key in (
                "ordinal",
                "agent_id",
                "news_depth",
                "can_post_community",
                "initial_cash",
                "persona_sha256",
            )
        }
        for row in actual_projection
    ]
    errors: list[str] = []
    if not distribution.get("distribution_pass"):
        errors.append("cohort distribution or canonical persona prompt mismatch")
    if projection.get("agents") != actual_projection:
        errors.append("structured persona projection differs from sealed baseline")
    if projection.get("source_db_sha256") != _file_sha256(db_path):
        errors.append("sys_100.db file hash differs from sealed baseline")
    renderer = projection.get("renderer") or {}
    if renderer.get("id") != PERSONA_RENDERER_ID:
        errors.append("persona renderer ID differs from sealed baseline")
    if renderer.get("sha256") != persona_renderer_sha256():
        errors.append("persona renderer implementation differs from sealed baseline")
    if expected_cohort_projection != actual_cohort_projection:
        errors.append("cohort registry differs from the runtime persona projection")
    if errors:
        raise RuntimeError("baseline persona validation failed: " + "; ".join(errors))
    return {
        "mode": "validate_existing_baseline",
        "database": str(db_path),
        "sealed_root": str(sealed_root),
        "validation_pass": True,
        "distribution": distribution,
    }


def build_new_cohort(output_db: Path) -> dict:
    """Create a deliberately separate cohort after an explicit CLI request."""

    output_db = output_db.resolve()
    if output_db == config.SYS_100_DB.resolve():
        raise ValueError(
            "--create-new-cohort must not target the frozen baseline sys_100.db"
        )
    report_path = output_db.with_suffix(".validation.json")
    if output_db.exists() or report_path.exists():
        raise FileExistsError(
            f"new cohort output already exists: {output_db} or {report_path}"
        )
    slots = load_fixed_slots(config.FIXED_SLOTS_CSV)
    pool = load_pool(config.SYS_1000_CSV)
    agents = match_agents(pool, slots)
    report = verify_distribution(agents)
    if not report.get("distribution_pass"):
        raise RuntimeError("new cohort failed distribution/persona validation")
    save_sys_100(agents, output_db)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "mode": "create_new_cohort",
        "database": str(output_db),
        "report": str(report_path),
        "validation_pass": True,
        "distribution": report,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "기본 동작은 봉인된 삼성전자 100명 persona cohort의 읽기 전용 검증입니다."
        )
    )
    parser.add_argument(
        "--create-new-cohort",
        type=Path,
        metavar="OUTPUT_DB",
        help=(
            "명시적으로 새 cohort를 별도 DB에 생성합니다. "
            "기존 outputs/sys_100.db는 지정할 수 없습니다."
        ),
    )
    args = parser.parse_args(argv)
    result = (
        build_new_cohort(args.create_new_cohort)
        if args.create_new_cohort is not None
        else validate_existing_baseline()
    )
    report = result["distribution"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if result["mode"] == "create_new_cohort":
        print(f"saved: {result['database']}")
        print(f"report: {result['report']}")
    else:
        print(f"validated without modification: {result['database']}")


if __name__ == "__main__":
    main()
