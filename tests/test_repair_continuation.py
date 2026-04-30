from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import cast

from atticus.agents.repair_planner import list_repair_plans
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.scheduler.free_loop import run_free_loop_once
from atticus.scheduler.planner import select_runnable_tasks
from atticus.status.completion import FINAL_LEGAL_DRAFT_CERTIFICATIONS, build_matter_completion_report
from atticus.workers.proposed_tasks import import_proposed_tasks_from_candidate
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION
from atticus.scheduler.lease import acquire_lease


MATTER = "napier-repair-continuation"


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _add_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.QUEUED,
    task_type: str = "source_inventory",
    stage: LegalStage = LegalStage.S0_SOURCE_INVENTORY,
    matter_scope: str = MATTER,
    task_dependencies: list[str] | None = None,
    provider_policy: dict[str, object] | None = None,
    instructions: str = "",
) -> None:
    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            title=task_id,
            task_type=task_type,
            instructions=instructions,
            matter_scope=matter_scope,
            stage=stage,
            status=status,
            task_dependencies=task_dependencies or [],
            provider_policy=provider_policy or {},
        ),
    )


def _certify(conn: sqlite3.Connection, certification_type: str) -> None:
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


def _certify_all_except(conn: sqlite3.Connection, *excluded: str) -> None:
    excluded_set = set(excluded)
    for certification_type in FINAL_LEGAL_DRAFT_CERTIFICATIONS:
        if certification_type not in excluded_set:
            _certify(conn, certification_type)


def _packet(task_id: str, *, proposed_tasks: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "repair continuation regression packet",
        "findings": [
            {
                "finding_id": "finding-1",
                "finding_type": "fact",
                "text": "bounded repair finding",
                "citation_ids": [],
                "confidence": 0.5,
                "reasoning_status": "uncertain",
            }
        ],
        "citations": [],
        "proposed_artifacts": [
            {
                "artifact_type": "foundation_note",
                "title": "Repair note",
                "path": f"candidate/{task_id}.json",
                "stage": str(LegalStage.S0_SOURCE_INVENTORY),
                "content": "{}",
            }
        ],
        "proposed_tasks": proposed_tasks or [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


def test_missing_certifications_keep_final_quality_unfinished_and_create_repair_actions(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(
            conn,
            "final-quality",
            status=TaskStatus.COMPLETE,
            task_type="final_quality_gate",
            stage=LegalStage.S9_FINAL_QUALITY_GATE,
        )
        _certify_all_except(conn, "citation_audit", "final_quality_gate")

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False, matter_scope=MATTER)
        report = build_matter_completion_report(conn, MATTER)
        plans = [plan.as_dict() for plan in list_repair_plans(conn, matter_scope=MATTER)]

    invariant = cast(Mapping[str, object], result["no_silent_idle"])
    assert invariant["ok"] is True
    assert invariant["reason"] == "progress_made"
    assert result["created_repair_task_ids"]
    assert report.done is False
    assert report.safe_to_finalize is False
    assert {"citation_audit", "final_quality_gate"}.issubset(set(report.missing_certifications))
    action_types = {str(action["action_type"]) for plan in plans for action in cast(tuple[dict[str, object], ...], plan["actions"])}
    assert "create_or_run_citation_audit" in action_types
    assert "create_certification_work" in action_types


def test_dependency_repair_requeues_only_after_dependency_complete_and_routes_reducer_review(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "ready-dependency", status=TaskStatus.COMPLETE)
        _add_task(conn, "blocked-by-complete-dependency", task_dependencies=["ready-dependency"])
        repo.update_task_blocked(conn, "blocked-by-complete-dependency", ["incomplete task dependency: ready-dependency"])

        runnable = select_runnable_tasks(conn, capacity=1, matter_scope=MATTER, dry_run=False)
        unblocked = cast(
            Mapping[str, object],
            conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'blocked-by-complete-dependency'").fetchone(),
        )

        _add_task(conn, "reducer-dependency", status=TaskStatus.REDUCER_PENDING)
        _add_task(conn, "blocked-by-reducer", task_dependencies=["reducer-dependency"])
        repo.update_task_blocked(conn, "blocked-by-reducer", ["incomplete task dependency: reducer-dependency"])
        plans = list_repair_plans(conn, matter_scope=MATTER)

    assert [str(task["task_id"]) for task in runnable] == ["blocked-by-complete-dependency"]
    assert unblocked["status"] == str(TaskStatus.QUEUED)
    assert json.loads(str(unblocked["blocked_reasons_json"])) == []
    reducer_plans = [plan for plan in plans if plan.target_id == "blocked-by-reducer"]
    assert reducer_plans
    assert reducer_plans[0].actions[0]["action_type"] == "manual_reducer_review"
    assert reducer_plans[0].actions[0]["dependency_task_id"] == "reducer-dependency"


def test_provider_terminal_blocker_is_not_retried_as_repair_work(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    reason = "OpenRouter provider call failed after dispatch: OpenRouter HTTP 401: unauthorized"
    with repo.db_connection(db_path) as conn:
        _add_task(
            conn,
            "provider-auth-terminal",
            provider_policy={
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-flash",
                "allow_fallback": False,
                "estimated_cost_usd": 0.01,
            },
        )
        repo.update_task_blocked(conn, "provider-auth-terminal", [reason])

        result = run_free_loop_once(
            conn,
            output_dir=tmp_path / "out",
            capacity=1,
            execute_workers=False,
            runtime="openrouter",
            allow_live=True,
            env={"ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            matter_scope=MATTER,
        )
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'provider-auth-terminal'").fetchone())
        plan = [plan for plan in list_repair_plans(conn, matter_scope=MATTER) if plan.target_id == "provider-auth-terminal"][0]

    assert result["leased_tasks"] == []
    assert task["status"] == str(TaskStatus.BLOCKED)
    assert json.loads(str(task["blocked_reasons_json"])) == [reason]
    assert plan.blocker_type == "provider_control_plane"
    assert plan.status == "requires_human"
    assert plan.actions[0]["retry_worker"] is False


def test_external_action_task_becomes_operator_terminal_block_not_runnable_work(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(
            conn,
            "external-email",
            task_type="external_request",
            instructions="Send email to the university asking for a certified notice.",
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=1, execute_workers=False, matter_scope=MATTER)
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'external-email'").fetchone())
        plan = [plan for plan in list_repair_plans(conn, matter_scope=MATTER) if plan.target_id == "external-email"][0]

    assert result["leased_tasks"] == []
    assert task["status"] == str(TaskStatus.BLOCKED)
    assert "external/human-only action blocked" in str(task["blocked_reasons_json"])
    assert plan.blocker_type == "external_or_human_action"
    assert plan.status == "requires_human"
    assert plan.actions[0]["action_type"] == "operator_review_external_or_human_only_blocker"
    assert plan.actions[0]["retry_worker"] is False


def test_free_loop_progress_from_reduction_suppresses_no_silent_idle(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "repair-parent")
        lease_id = acquire_lease(conn, task_id="repair-parent", worker_id="worker-01", dry_run=False)
        candidate_id = record_worker_result(
            conn,
            task_id="repair-parent",
            lease_id=lease_id,
            worker_id="worker-01",
            payload=_packet(
                "repair-parent",
                proposed_tasks=[
                    {
                        "task_id": "repair-followup",
                        "title": "Repair follow-up",
                        "task_type": "source_inventory",
                        "matter_scope": MATTER,
                        "stage": str(LegalStage.S0_SOURCE_INVENTORY),
                        "instructions": "Continue bounded internal source inventory repair.",
                        "provider_policy": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.0},
                    }
                ],
            ),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False, matter_scope=MATTER)
        event_count = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type = 'supervisor.no_progress_detected'").fetchone()

    invariant = cast(Mapping[str, object], result["no_silent_idle"])
    assert result["reduced_candidates"] == [candidate_id]
    assert result["imported_tasks"] == ["repair-followup"]
    assert invariant["ok"] is True
    assert invariant["reason"] == "progress_made"
    assert event_count is not None and int(str(event_count["n"])) == 0


def test_high_risk_reducer_candidate_is_queued_for_manual_review_not_auto_reduced(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(
            conn,
            "draft-candidate",
            status=TaskStatus.REDUCER_PENDING,
            task_type="draft_preparation",
            stage=LegalStage.S8_DRAFT_PREPARATION,
        )
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="draft-candidate",
            lease_id=None,
            worker_id="worker-01",
            output_type="worker_result_packet",
            payload=_packet("draft-candidate"),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False, matter_scope=MATTER)
        review = conn.execute("SELECT reason FROM reducer_review_queue WHERE candidate_id = ?", (candidate_id,)).fetchone()
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'draft-candidate'").fetchone())

    assert result["reduced_candidates"] == []
    assert cast(list[object], result["skipped_reductions"])
    assert review is not None
    assert "manual reducer review" in str(review["reason"])
    assert task["status"] == str(TaskStatus.REDUCER_PENDING)


def test_proposed_task_collision_is_deterministic_and_does_not_mutate_existing_task(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_task(conn, "parent-collision")
        source_id = repo.add_source(conn, source_id="NAP-SRC-0001", matter_scope=MATTER, path="/nap/source.pdf", sha256="1" * 64)
        _add_task(conn, "existing-followup", task_type="source_inventory")
        _ = conn.execute(
            "UPDATE tasks SET source_dependencies_json = ? WHERE task_id = 'existing-followup'",
            (json.dumps([source_id]),),
        )
        proposed = [
            {
                "task_id": "existing-followup",
                "title": "Collision must not overwrite",
                "task_type": "source_inventory",
                "matter_scope": MATTER,
                "stage": str(LegalStage.S0_SOURCE_INVENTORY),
                "instructions": "Attempt to collide with an existing task.",
                "source_dependencies": [],
                "provider_policy": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.0},
            }
        ]
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="parent-collision",
            lease_id=None,
            worker_id="worker-01",
            output_type="worker_result_packet",
            payload=_packet("parent-collision", proposed_tasks=proposed),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT * FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

        first = import_proposed_tasks_from_candidate(conn, candidate)
        second = import_proposed_tasks_from_candidate(conn, candidate)
        existing = cast(Mapping[str, object], conn.execute("SELECT source_dependencies_json FROM tasks WHERE task_id = 'existing-followup'").fetchone())
        attention_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM human_attention
            WHERE target_type = 'proposed_task'
              AND target_id = 'existing-followup'
              AND status = 'open'
              AND reason LIKE '%collides with an existing task%'
            """
        ).fetchone()
        duplicate_events = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type = 'proposed_task.rejection_duplicate_seen'").fetchone()

    assert first == []
    assert second == []
    assert json.loads(str(existing["source_dependencies_json"])) == [source_id]
    assert attention_count is not None and int(str(attention_count["n"])) == 1
    assert duplicate_events is not None and int(str(duplicate_events["n"])) == 1
