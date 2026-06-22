from __future__ import annotations

from pathlib import Path
from typing import cast
import json

from atticus.core.matter_profiles import MANDATORY_S8_S9_GATES
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.validation.gates import run_validation


def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_profile_mandatory_s8_s9_gates_are_registered_validation_handlers(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        for gate in sorted(MANDATORY_S8_S9_GATES - {"claim_evidence_support"}):
            outcome = run_validation(conn, gate_name=gate, target_type="matter", target_id="alpha")
            assert "unknown validation gate" not in str(outcome.details)


def test_subagent_cross_matter_isolation_gate_passes_for_same_matter_dependencies(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="ALPHA-SRC-1", matter_scope="alpha", path="/alpha.pdf", sha256="a" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="same-matter-task",
                title="Same",
                task_type="extraction_qa",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        outcome = run_validation(conn, gate_name="cross_matter_isolation", target_type="task", target_id="same-matter-task")

    assert outcome.passed is True


def test_subagent_cross_matter_isolation_gate_fails_for_cross_matter_source(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="BETA-SRC-1", matter_scope="beta", path="/beta.pdf", sha256="b" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="cross-matter-task",
                title="Cross",
                task_type="extraction_qa",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        outcome = run_validation(conn, gate_name="cross_matter_isolation", target_type="task", target_id="cross-matter-task")

    assert outcome.passed is False
    assert "BETA-SRC-1" in str(cast(dict[str, object], outcome.details)["problems"])


def test_extraction_coverage_fails_for_low_confidence_ocr(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="ALPHA-OCR-LOW", matter_scope="alpha", path="/alpha/scan.pdf", sha256="c" * 64)
        artifact_id = repo.add_artifact(
            conn,
            artifact_id="ART-OCR-LOW",
            matter_scope="alpha",
            path="/alpha/ocr.txt",
            artifact_type="ocr_text",
            content="low confidence OCR text",
            source_ids=[source_id],
        )
        _ = conn.execute(
            """
            INSERT INTO ocr_records(ocr_id, source_id, artifact_id, engine, coverage_status, metadata_json, created_at)
            VALUES ('ocr-low-confidence', ?, ?, 'existing_text', 'complete', ?, '2026-04-30T00:00:00+00:00')
            """,
            (source_id, artifact_id, json.dumps({"source_sha256": "c" * 64, "confidence": 0.42})),
        )
        outcome = run_validation(conn, gate_name="extraction_coverage", target_type="matter", target_id="alpha")

    assert outcome.passed is False
    assert outcome.details["low_confidence_ocr"] == [source_id]
