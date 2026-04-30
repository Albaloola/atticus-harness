from __future__ import annotations

from pathlib import Path

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.status.completion import FINAL_LEGAL_DRAFT_CERTIFICATIONS
from atticus.workflows.final_gate import create_missing_final_gate_work, final_gate_readiness, plan_final_gate_repairs


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _certify(conn, certification_type: str) -> None:
    validation_id = repo.record_validation(
        conn,
        matter_scope="napier",
        target_type="matter",
        target_id="napier",
        gate_name=certification_type,
        passed=True,
    )
    repo.add_certification(
        conn,
        subject_type="matter",
        subject_id="napier",
        certification_type=certification_type,
        validator="test",
        validation_result_id=validation_id,
        evidence={"test": True},
    )


def _certify_all_except(conn, *excluded: str) -> None:
    excluded_set = set(excluded)
    for cert in FINAL_LEGAL_DRAFT_CERTIFICATIONS:
        if cert not in excluded_set:
            _certify(conn, cert)


def test_final_gate_blocked_by_missing_citation_audit_has_specific_repair(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _certify_all_except(conn, "citation_audit", "final_quality_gate")
        readiness = final_gate_readiness(conn, "napier")
        repairs = plan_final_gate_repairs(conn, "napier")

    assert not readiness["ready"]
    assert readiness["missing_certifications"] == ["citation_audit", "final_quality_gate"]
    assert readiness["next_action"]["certification"] == "citation_audit"
    assert repairs[0]["type"] == "create_missing_certification_work"
    assert repairs[0]["certification"] == "citation_audit"


def test_final_gate_blocked_by_reducer_pending_citation_repair_points_to_reducer_review(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _certify_all_except(conn, "citation_audit", "final_quality_gate")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-repair",
                title="Repair citations",
                task_type="citation_repair",
                stage=LegalStage.S7_HOSTILE_REVIEW,
                matter_scope="napier",
                status=TaskStatus.REDUCER_PENDING,
            ),
        )
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="citation-repair",
            lease_id=None,
            worker_id="worker",
            output_type="worker_result_packet",
            payload={
                "schema_version": "worker_result_packet.v2",
                "task_id": "citation-repair",
                "summary": "needs review",
                "findings": [],
                "citations": [],
                "proposed_artifacts": [],
                "proposed_tasks": [],
                "uncertainties": [],
                "contradictions": [],
                "risk_flags": [],
                "redaction_flags": [],
                "external_action_requests": [],
            },
        )
        from atticus.reducer.review_queue import enqueue_reducer_review

        enqueue_reducer_review(conn, candidate_id=candidate_id, reason="high-risk legal stage S7 requires manual reducer review")
        readiness = final_gate_readiness(conn, "napier")

    assert readiness["next_action"]["type"] == "manual_reducer_review"
    assert readiness["next_action"]["candidate_id"] == candidate_id


def test_final_gate_readiness_true_only_after_all_required_certifications(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _certify_all_except(conn)
        readiness = final_gate_readiness(conn, "napier")

    assert readiness["ready"]
    assert readiness["complete"]
    assert readiness["missing_certifications"] == []


def test_final_gate_create_missing_does_not_duplicate_existing_audit_tasks(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _certify_all_except(conn, "citation_audit", "final_quality_gate")
        first = create_missing_final_gate_work(conn, "napier")
        second = create_missing_final_gate_work(conn, "napier")
        tasks = conn.execute("SELECT task_id, task_type FROM tasks WHERE matter_scope = 'napier' AND task_type = 'citation_audit'").fetchall()

    assert first["created"] is True
    assert first["certification"] == "citation_audit"
    assert second["created"] is False
    assert second["task_id"] == first["task_id"]
    assert len(tasks) == 1


def test_final_gate_create_missing_creates_final_only_after_prerequisites(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _certify_all_except(conn, "final_quality_gate")
        result = create_missing_final_gate_work(conn, "napier")
        task = conn.execute("SELECT task_type, stage FROM tasks WHERE task_id = ?", (result["task_id"],)).fetchone()

    assert result["created"] is True
    assert result["certification"] == "final_quality_gate"
    assert task is not None
    assert task["task_type"] == "final_quality_gate"
    assert task["stage"] == "S9"


def test_final_gate_blockers_include_owner_signature_and_resume_command(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _certify_all_except(conn, "citation_audit", "final_quality_gate")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-repair-owned",
                title="Repair citations",
                task_type="citation_repair",
                stage=LegalStage.S7_HOSTILE_REVIEW,
                matter_scope="napier",
                status=TaskStatus.REDUCER_PENDING,
            ),
        )
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="citation-repair-owned",
            lease_id=None,
            worker_id="worker",
            output_type="worker_result_packet",
            payload={"summary": "needs review"},
        )
        from atticus.reducer.review_queue import enqueue_reducer_review

        enqueue_reducer_review(conn, candidate_id=candidate_id, reason="manual reducer review required")
        attention_id = repo.record_human_attention_once(
            conn,
            matter_scope="napier",
            target_type="matter",
            target_id="napier",
            severity="blocker",
            reason="operator decision required",
            owner="operator",
        )

        readiness = final_gate_readiness(conn, "napier")

    blockers = readiness["blocked_reasons"]
    assert blockers
    assert all(blocker.get("owner") for blocker in blockers)
    assert all(blocker.get("signature") for blocker in blockers)
    assert all(blocker.get("resume_command") for blocker in blockers)
    assert any(blocker["type"] == "reducer_review_required" and blocker["owner"] == "reducer" for blocker in blockers)
    assert any(blocker["type"] == "open_human_attention" and blocker["attention_id"] == attention_id for blocker in blockers)
    assert readiness["next_action"]["resume_command"]
