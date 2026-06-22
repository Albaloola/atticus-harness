from __future__ import annotations

from pathlib import Path
import json

from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.status.completion import (
    FINAL_LEGAL_DRAFT_CERTIFICATIONS,
    build_matter_completion_report,
    next_resume_action,
)


MATTER = "napier-accommodation-arrears"


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _add_final_work_task(
    conn,
    task_id: str = "napier-final-quality",
    *,
    status: TaskStatus = TaskStatus.COMPLETE,
    task_type: str = "final_quality_gate",
    stage: LegalStage = LegalStage.S9_FINAL_QUALITY_GATE,
) -> None:
    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            matter_scope=MATTER,
            title=task_id,
            task_type=task_type,
            stage=stage,
            status=status,
        ),
    )


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


def test_matter_health_not_done_when_final_gate_missing_even_if_most_tasks_complete(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(conn)
        _certify_all_except(conn, "final_quality_gate")

        report = build_matter_completion_report(conn, MATTER)

    assert not report.done
    assert report.blocked
    assert report.runnable_count == 0
    assert report.missing_certifications == ("final_quality_gate",)
    assert report.reducer_pending == ()


def test_matter_health_reports_reducer_pending_as_next_action(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(
            conn,
            "napier-accommodation-arrears-repair-draft-citations-v1",
            status=TaskStatus.REDUCER_PENDING,
            task_type="citation_repair",
            stage=LegalStage.S7_HOSTILE_REVIEW,
        )
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="napier-accommodation-arrears-repair-draft-citations-v1",
            lease_id=None,
            worker_id="test-worker",
            output_type="result_packet",
            payload={"summary": "citation repair candidate"},
            status="candidate",
        )

        action = next_resume_action(conn, MATTER)

    assert action["type"] == "manual_reducer_review"
    assert action["task_id"] == "napier-accommodation-arrears-repair-draft-citations-v1"
    assert action["candidate_id"] == candidate_id
    assert action["after"] == "run citation audit, then final quality gate"


def test_matter_health_reports_missing_citation_audit_before_final_quality_gate(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(conn)
        _certify_all_except(conn, "citation_audit", "final_quality_gate")

        report = build_matter_completion_report(conn, MATTER)
        action = next_resume_action(conn, MATTER)

    assert report.missing_certifications[:2] == ("citation_audit", "final_quality_gate")
    assert report.reducer_pending == ()
    assert action["type"] == "missing_certification"
    assert action["certification"] == "citation_audit"
    assert action["after"] == "run citation audit, then final quality gate"


def test_matter_health_prioritizes_runnable_user_directed_work_before_final_gate_drift(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    task_id = "napier-accommodation-arrears-coord-prepare-urgent-evidence-led-hardship-and-notice-to-quit-pause-position-p-ctx-1-evidence-triage"
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(conn)
        _certify_all_except(conn, "final_quality_gate")
        repo.add_task(
            conn,
            TaskSpec(
                task_id=task_id,
                matter_scope=MATTER,
                title="Prepare urgent hardship and Notice to Quit pause position pack",
                task_type="evidence_triage",
                stage=LegalStage.S5_ISSUE_ROUTE_MAP,
                status=TaskStatus.QUEUED,
                expected_value=100.0,
            ),
        )

        action = next_resume_action(conn, MATTER)

    assert action["type"] == "supervisor_tick"
    assert action["owner"] == "scheduler"
    assert action["task_id"] == task_id
    assert action["task_type"] == "evidence_triage"
    assert "run-free-loop" in str(action["resume_command"])


def test_matter_health_surfaces_gate_resolved_blocked_task_before_final_gate_drift(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    parent_id = "urgent-hardship-evidence-triage"
    audit_id = "urgent-hardship-citation-audit"
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(conn)
        _certify_all_except(conn, "final_quality_gate")
        repo.add_task(
            conn,
            TaskSpec(
                task_id=parent_id,
                matter_scope=MATTER,
                title="Urgent hardship evidence triage",
                task_type="evidence_triage",
                stage=LegalStage.S2_EVIDENCE_REGISTRY,
                status=TaskStatus.COMPLETE,
            ),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id=audit_id,
                matter_scope=MATTER,
                title="Audit urgent hardship citations",
                task_type="citation_audit",
                stage=LegalStage.S7_HOSTILE_REVIEW,
                status=TaskStatus.BLOCKED,
                task_dependencies=[parent_id],
            ),
        )
        repo.update_task_blocked(conn, audit_id, [f"incomplete task dependency: {parent_id}"])

        action = next_resume_action(conn, MATTER)

    assert action["type"] == "supervisor_tick"
    assert action["task_id"] == audit_id
    assert "safely requeueable blocked" in str(action["reason"])


def test_matter_health_excludes_closed_human_attention(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(conn)
        _certify_all_except(conn)
        resolved_id = repo.record_human_attention(
            conn,
            matter_scope=MATTER,
            target_type="matter",
            target_id=MATTER,
            severity="warning",
            reason="old warning",
        )
        _ = repo.record_human_attention(
            conn,
            matter_scope=MATTER,
            target_type="matter",
            target_id=MATTER,
            severity="blocker",
            reason="open blocker",
        )
        conn.execute("UPDATE human_attention SET status = 'resolved' WHERE attention_id = ?", (resolved_id,))

        report = build_matter_completion_report(conn, MATTER)

    assert not report.done
    assert [item["reason"] for item in report.unresolved_human_attention] == ["open blocker"]


def test_matter_health_reports_stale_dependencies_as_not_done(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(conn)
        _certify_all_except(conn)
        artifact_id = repo.add_artifact(
            conn,
            matter_scope=MATTER,
            path="/tmp/stale-draft.json",
            artifact_type="draft",
            stale=True,
        )

        report = build_matter_completion_report(conn, MATTER)

    assert not report.done
    assert artifact_id in report.stale_artifacts
    assert any(requirement.requirement_id == f"artifact:{artifact_id}" for requirement in report.requirements)


def test_matter_health_cli_why_not_done_reports_next_action(tmp_path: Path, capsys) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(conn)
        _certify_all_except(conn, "citation_audit", "final_quality_gate")

    exit_code = cli_main(["matter-health", "--db", str(db_path), "--matter", MATTER, "--why-not-done", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["done"] is False
    assert payload["next_action"]["certification"] == "citation_audit"
    assert f"--db {db_path}" in payload["next_action"]["resume_command"]


def test_matter_health_cli_includes_reducer_pending_and_next_action(tmp_path: Path, capsys) -> None:
    db_path = init_db(tmp_path)
    task_id = "napier-accommodation-arrears-repair-draft-citations-v1"
    with repo.db_connection(db_path) as conn:
        _add_final_work_task(
            conn,
            task_id,
            status=TaskStatus.REDUCER_PENDING,
            task_type="citation_repair",
            stage=LegalStage.S7_HOSTILE_REVIEW,
        )
        _ = repo.record_candidate_output(
            conn,
            task_id=task_id,
            lease_id=None,
            worker_id="test-worker",
            output_type="result_packet",
            payload={"summary": "citation repair candidate"},
            status="candidate",
        )

    exit_code = cli_main(["matter-health", "--db", str(db_path), "--matter", MATTER, "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["reducer_pending"][0]["task_id"] == task_id
    assert payload["next_action"]["type"] == "manual_reducer_review"
    assert f"--db {db_path}" in payload["next_action"]["resume_command"]
