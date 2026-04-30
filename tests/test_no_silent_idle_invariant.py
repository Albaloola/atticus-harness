from __future__ import annotations

from pathlib import Path
import json

from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.status.completion import (
    FINAL_LEGAL_DRAFT_CERTIFICATIONS,
    assert_completion_has_next_action,
    record_completion_snapshot,
)


MATTER = "napier-anfal-control-plane"


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _certify(conn, certification_type: str) -> None:
    validation_id = repo.record_validation(
        conn,
        matter_scope=MATTER,
        target_type="matter",
        target_id=MATTER,
        gate_name=certification_type,
        passed=True,
        details={"test": True},
    )
    repo.add_certification(
        conn,
        subject_type="matter",
        subject_id=MATTER,
        certification_type=certification_type,
        validator="test",
        validation_result_id=validation_id,
        evidence={"test": True},
    )


def _certify_all_except(conn, *excluded: str) -> None:
    excluded_set = set(excluded)
    for certification_type in FINAL_LEGAL_DRAFT_CERTIFICATIONS:
        if certification_type not in excluded_set:
            _certify(conn, certification_type)


def _add_final_task(conn, *, status: TaskStatus = TaskStatus.COMPLETE) -> None:
    repo.add_task(
        conn,
        TaskSpec(
            task_id="final-quality",
            title="Final quality",
            task_type="final_quality_gate",
            stage=LegalStage.S9_FINAL_QUALITY_GATE,
            matter_scope=MATTER,
            status=status,
        ),
    )


def test_completion_snapshot_records_exact_primary_next_action(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_task(conn)
        _certify_all_except(conn, "citation_audit", "final_quality_gate")

        invariant = assert_completion_has_next_action(conn, MATTER)
        snapshot = record_completion_snapshot(conn, MATTER)
        row = conn.execute("SELECT primary_next_action_json, counts_json FROM matter_completion_snapshots WHERE snapshot_id = ?", (snapshot.snapshot_id,)).fetchone()

    assert invariant.ok is True
    assert invariant.incomplete is True
    assert invariant.next_action_type == "missing_certification"
    assert snapshot.primary_next_action["certification"] == "citation_audit"
    assert row is not None
    assert json.loads(row["primary_next_action_json"])["type"] == "missing_certification"
    assert json.loads(row["counts_json"])["missing_certifications"] == 2


def test_supervisor_diagnose_idle_write_persists_snapshot_and_repair_plan(tmp_path: Path, capsys) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_task(conn)
        _certify_all_except(conn, "final_quality_gate")

    code = cli_main(["supervisor", "diagnose-idle", "--db", str(db_path), "--matter", MATTER, "--write", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is False
    assert payload["completion_snapshot_id"]
    assert payload["completion_invariant"]["ok"] is True
    assert payload["next_action"]["type"] == "missing_certification"
    assert payload["repair_plans"]


def test_matter_health_write_snapshot_cli(tmp_path: Path, capsys) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_task(conn)
        _certify_all_except(conn, "final_quality_gate")

    code = cli_main(["matter-health", "--db", str(db_path), "--matter", MATTER, "--write-snapshot", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["completion_snapshot"]["snapshot_id"]
    assert payload["completion_invariant"]["next_action_type"] == "missing_certification"
