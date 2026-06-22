from __future__ import annotations

from pathlib import Path
import json

from atticus.agents.repair_planner import (
    ensure_repair_plan_for_blocker,
    ensure_repair_plans_for_matter,
    list_repair_plans,
    record_repair_attempt,
)
from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.status.completion import FINAL_LEGAL_DRAFT_CERTIFICATIONS


MATTER = "napier-accommodation-arrears"


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _add_task(
    conn,
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.QUEUED,
    task_type: str = "verification",
    stage: LegalStage = LegalStage.S6_AUTHORITY_LAW_MAP,
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


def test_missing_citation_audit_creates_repair_plan(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "final-gate", task_type="final_quality_gate", stage=LegalStage.S9_FINAL_QUALITY_GATE, status=TaskStatus.COMPLETE)
        _certify_all_except(conn, "citation_audit", "final_quality_gate")

        plans = ensure_repair_plans_for_matter(conn, matter_scope=MATTER)

    citation_plans = [plan for plan in plans if plan.blocker_type == "missing_certification" and "citation_audit" in plan.blocker_signature or any(action.get("action_type") == "create_or_run_citation_audit" for action in plan.actions)]
    assert citation_plans
    assert citation_plans[0].actions[0]["action_type"] == "create_or_run_citation_audit"


def test_incomplete_dependency_reducer_pending_creates_manual_review_action(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "dependency", status=TaskStatus.REDUCER_PENDING)
        _add_task(conn, "blocked-final", task_type="final_quality_gate", stage=LegalStage.S9_FINAL_QUALITY_GATE)

        repo.update_task_blocked(conn, "blocked-final", ["incomplete task dependency: dependency"])
        plans = list_repair_plans(conn, matter_scope=MATTER)

    assert len(plans) == 1
    assert plans[0].blocker_type == "incomplete_dependency"
    assert plans[0].actions[0]["action_type"] == "manual_reducer_review"
    assert plans[0].actions[0]["dependency_task_id"] == "dependency"


def test_provider_401_creates_control_plane_repair_not_worker_retry(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "provider-task")

        plan = ensure_repair_plan_for_blocker(
            conn,
            matter_scope=MATTER,
            target_type="task",
            target_id="provider-task",
            reason="OpenRouter HTTP 401 unauthorized",
        )

    assert plan.blocker_type == "provider_control_plane"
    assert plan.status == "requires_human"
    assert plan.actions[0]["action_type"] == "provider_control_plane_attention"
    assert plan.actions[0]["retry_worker"] is False


def test_external_human_only_blocker_creates_operator_repair_not_retry(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "external-task")

        plan = ensure_repair_plan_for_blocker(
            conn,
            matter_scope=MATTER,
            target_type="task",
            target_id="external-task",
            reason="external/human-only action blocked: task requests outside evidence acquisition",
        )

    assert plan.blocker_type == "external_or_human_action"
    assert plan.status == "requires_human"
    assert plan.actions[0]["action_type"] == "operator_review_external_or_human_only_blocker"
    assert plan.actions[0]["retry_worker"] is False


def test_repair_plan_dedupes_same_blocker_signature(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "blocked-task")
        first = ensure_repair_plan_for_blocker(
            conn,
            matter_scope=MATTER,
            target_type="task",
            target_id="blocked-task",
            reason="missing certification: matter:napier-accommodation-arrears:citation_audit",
        )
        second = ensure_repair_plan_for_blocker(
            conn,
            matter_scope=MATTER,
            target_type="task",
            target_id="blocked-task",
            reason="missing certification: matter:napier-accommodation-arrears:citation_audit",
        )
        count = conn.execute("SELECT COUNT(*) AS n FROM repair_plans").fetchone()

    assert first.repair_plan_id == second.repair_plan_id
    assert int(count["n"]) == 1


def test_same_blocker_text_on_different_targets_gets_distinct_repair_ids(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    reason = "proposed task rejected: proposed source/evidence search has no source, artifact, or task scope"
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "blocked-task-a")
        _add_task(conn, "blocked-task-b")

        first = ensure_repair_plan_for_blocker(
            conn,
            matter_scope=MATTER,
            target_type="task",
            target_id="blocked-task-a",
            reason=reason,
        )
        second = ensure_repair_plan_for_blocker(
            conn,
            matter_scope=MATTER,
            target_type="task",
            target_id="blocked-task-b",
            reason=reason,
        )
        count = conn.execute("SELECT COUNT(*) AS n FROM repair_plans").fetchone()

    assert first.blocker_signature == second.blocker_signature
    assert first.repair_plan_id != second.repair_plan_id
    assert int(count["n"]) == 2


def test_repair_plan_reaches_requires_human_after_max_attempts(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "blocked-task")
        plan = ensure_repair_plan_for_blocker(
            conn,
            matter_scope=MATTER,
            target_type="task",
            target_id="blocked-task",
            reason="worker output quarantined: malformed result packet",
            max_attempts=2,
        )

        _ = record_repair_attempt(conn, repair_plan_id=plan.repair_plan_id, action_type="repair_worker_contract_or_prompt", status="attempted")
        final = record_repair_attempt(conn, repair_plan_id=plan.repair_plan_id, action_type="repair_worker_contract_or_prompt", status="attempted")
        attention = conn.execute(
            "SELECT reason FROM human_attention WHERE matter_scope = ? AND target_id = ? AND status = 'open'",
            (MATTER, "blocked-task"),
        ).fetchall()

    assert final.status == "requires_human"
    assert final.attempts_so_far == 2
    assert any("repair plan attempt limit reached" in str(row["reason"]) for row in attention)


def test_repairs_next_cli_creates_completion_repair_plans_with_write(tmp_path: Path, capsys) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "final-gate", task_type="final_quality_gate", stage=LegalStage.S9_FINAL_QUALITY_GATE, status=TaskStatus.COMPLETE)
        _certify_all_except(conn, "final_quality_gate")

    exit_code = cli_main(["repairs", "next", "--db", str(db_path), "--matter", MATTER, "--write", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["repair_plan"]["blocker_type"] == "missing_certification"
    assert f"--db {db_path}" in payload["repair_plan"]["actions"][0]["resume_command"]
