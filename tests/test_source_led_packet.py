from __future__ import annotations

from pathlib import Path
from typing import cast
from collections.abc import Mapping

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.cli import main as cli_main
from atticus.db import repo
from atticus.workflows.source_led_packet import create_source_led_candidate_for_task


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _certify(conn, matter_scope: str, certification_type: str) -> None:
    validation_id = repo.record_validation(
        conn,
        matter_scope=matter_scope,
        target_type="matter",
        target_id=matter_scope,
        gate_name=certification_type,
        passed=True,
        details={"test": True},
    )
    repo.add_certification(
        conn,
        subject_type="matter",
        subject_id=matter_scope,
        certification_type=certification_type,
        validator="test",
        validation_result_id=validation_id,
        evidence={"test": True},
    )


def test_source_led_packet_records_quote_supported_candidate_without_local_stub(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    matter = "alpha"
    quote = "The student has rent arrears of £5,526.66 and requested an urgent pause to enforcement."
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope=matter, path="/alpha/arrears.txt", sha256="a" * 64)
        artifact_id = repo.add_artifact(
            conn,
            matter_scope=matter,
            path="/alpha/extracted/arrears.txt",
            artifact_type="extracted_text",
            content=f"Background.\n\n{quote}\n\nFurther background.",
            source_ids=[source_id],
        )
        _ = conn.execute(
            """
            INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
              coverage_status, confidence, metadata_json, created_at)
            VALUES ('extract-source-led', ?, ?, 'plain_text', 'complete', 0.95, '{}', '2026-04-30T00:00:00+00:00')
            """,
            (source_id, artifact_id),
        )
        _certify(conn, matter, "source_inventory")
        _certify(conn, matter, "extraction_coverage")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="urgent-hardship-evidence-triage",
                matter_scope=matter,
                title="Urgent hardship Notice to Quit pause evidence triage",
                task_type="evidence_triage",
                stage=LegalStage.S2_EVIDENCE_REGISTRY,
                source_dependencies=[source_id],
                validation_gates=["citation_support_integrity"],
                status=TaskStatus.QUEUED,
            ),
        )

        result = create_source_led_candidate_for_task(
            conn,
            matter_scope=matter,
            task_id="urgent-hardship-evidence-triage",
            write=True,
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, payload_json FROM candidate_outputs WHERE candidate_id = ?", (result.candidate_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'urgent-hardship-evidence-triage'").fetchone())
        chunk_count = conn.execute("SELECT COUNT(*) AS n FROM source_chunks WHERE source_id = ?", (source_id,)).fetchone()

    assert result.candidate_id.startswith("cand-")
    assert result.support_summary["ok"] is True
    assert result.selected_source_ids == (source_id,)
    assert candidate["status"] == "candidate"
    assert task["status"] == TaskStatus.REDUCER_PENDING
    assert chunk_count is not None and int(str(chunk_count["n"])) >= 1
    assert "rent arrears" in str(candidate["payload_json"])


def test_citation_support_cli_verify_read_only_does_not_write_or_crash(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    matter = "alpha"
    quote = "The student has rent arrears of £5,526.66 and requested an urgent pause to enforcement."
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope=matter, path="/alpha/arrears.txt", sha256="b" * 64)
        artifact_id = repo.add_artifact(
            conn,
            matter_scope=matter,
            path="/alpha/extracted/arrears.txt",
            artifact_type="extracted_text",
            content=f"Background.\n\n{quote}\n\nFurther background.",
            source_ids=[source_id],
        )
        _ = conn.execute(
            """
            INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
              coverage_status, confidence, metadata_json, created_at)
            VALUES ('extract-source-led-cli', ?, ?, 'plain_text', 'complete', 0.95, '{}', '2026-04-30T00:00:00+00:00')
            """,
            (source_id, artifact_id),
        )
        _certify(conn, matter, "source_inventory")
        _certify(conn, matter, "extraction_coverage")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="urgent-hardship-evidence-triage-cli",
                matter_scope=matter,
                title="Urgent hardship Notice to Quit pause evidence triage",
                task_type="evidence_triage",
                stage=LegalStage.S2_EVIDENCE_REGISTRY,
                source_dependencies=[source_id],
                validation_gates=["citation_support_integrity"],
                status=TaskStatus.QUEUED,
            ),
        )
        result = create_source_led_candidate_for_task(
            conn,
            matter_scope=matter,
            task_id="urgent-hardship-evidence-triage-cli",
            write=True,
        )
        before = int(conn.execute(
            "SELECT COUNT(*) AS n FROM citation_support_results WHERE candidate_id = ?",
            (result.candidate_id,),
        ).fetchone()["n"])

    assert cli_main([
        "citation-support",
        "verify",
        "--db",
        str(db_path),
        "--candidate-id",
        result.candidate_id,
        "--json",
    ]) == 0

    with repo.db_connection(db_path, read_only=True) as conn:
        after = int(conn.execute(
            "SELECT COUNT(*) AS n FROM citation_support_results WHERE candidate_id = ?",
            (result.candidate_id,),
        ).fetchone()["n"])
    assert after == before
