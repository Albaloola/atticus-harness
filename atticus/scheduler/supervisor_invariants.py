"""Supervisor invariants that prevent quiet incomplete ticks."""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import cast

from atticus.agents.repair_planner import ensure_repair_plans_for_matter
from atticus.agents.repair_executor import execute_next_repair_plan, execute_repair_tick
from atticus.db import repo
from atticus.status.completion import (
    assert_completion_has_next_action,
    build_matter_completion_report,
    next_resume_action,
    record_completion_snapshot,
)


PROGRESS_KEYS = (
    "reduced_candidates",
    "imported_tasks",
    "leased_tasks",
    "executed_tasks",
    "applied_actions",
    "routed_operator_signals",
    "reducer_review_ids",
    "created_repair_task_ids",
    "unblocked_repair_task_ids",
    "repair_progress",
)

EXPLANATION_KEYS = (
    "reduction_errors",
    "skipped_reductions",
    "worker_errors",
    "preflight_groups",
    "blocked_repairs",
    "terminal_blocks",
)


def evaluate_no_silent_idle(
    conn: sqlite3.Connection,
    matter_scope: str | None,
    tick_result: Mapping[str, object],
    *,
    write: bool = True,
    auto_execute: bool = False,
) -> dict[str, object]:
    """Return ok=False when a zero-progress tick leaves unresolved matter work."""

    resolved_matter = matter_scope or _single_active_matter(conn)
    if not resolved_matter:
        return {"ok": True, "reason": "matter_scope_not_resolved"}
    if _has_any_items(tick_result, PROGRESS_KEYS):
        return {"ok": True, "matter_scope": resolved_matter, "reason": "progress_made"}
    if _has_any_items(tick_result, EXPLANATION_KEYS):
        report = build_matter_completion_report(conn, resolved_matter)
        if report.done:
            return {
                "ok": True,
                "matter_scope": resolved_matter,
                "reason": "tick_reported_blocker_or_error_matter_complete",
                "next_action": {"type": "complete", "resume_command": ""},
            }
        next_action = next_resume_action(conn, resolved_matter)
        repair_plans = [plan.as_dict() for plan in ensure_repair_plans_for_matter(conn, matter_scope=resolved_matter)] if write else []
        invariant = assert_completion_has_next_action(conn, resolved_matter)
        snapshot = record_completion_snapshot(conn, resolved_matter).as_dict() if write else {}
        if auto_execute and write:
            tick_execution = execute_repair_tick(conn, matter_scope=resolved_matter, max_repairs=10, write=True)
            execution = tick_execution.as_dict()
            if tick_execution.made_progress:
                invariant = assert_completion_has_next_action(conn, resolved_matter)
        else:
            execution = None
        decision = _blocker_decision(next_action)
        if write:
            _ = repo.emit_event(
                conn,
                "supervisor.zero_progress_explained",
                matter_scope=resolved_matter,
                payload={
                    "reason": "tick_reported_blocker_or_error",
                    "next_action": next_action,
                    "blocker_decision": decision,
                    "completion_invariant": invariant.as_dict(),
                    "completion_snapshot_id": snapshot.get("snapshot_id", ""),
                    "repair_plan_ids": [str(plan["repair_plan_id"]) for plan in repair_plans],
                    "repair_execution": execution,
                },
            )
        return {
            "ok": invariant.ok,
            "matter_scope": resolved_matter,
            "reason": "tick_reported_blocker_or_error" if invariant.ok else invariant.reason,
            "next_action": next_action,
            "blocker_decision": decision,
            "completion_invariant": invariant.as_dict(),
            "completion_snapshot_id": snapshot.get("snapshot_id", ""),
            "repair_plans": repair_plans,
            "repair_execution": execution,
        }

    report = build_matter_completion_report(conn, resolved_matter)
    if report.done:
        snapshot = record_completion_snapshot(conn, resolved_matter).as_dict() if write else {}
        return {
            "ok": True,
            "matter_scope": resolved_matter,
            "reason": "matter_complete",
            "next_action": {"type": "complete", "resume_command": ""},
            "completion_snapshot_id": snapshot.get("snapshot_id", ""),
        }

    next_action = next_resume_action(conn, resolved_matter)
    repair_plans: list[dict[str, object]] = []
    attention_id: int | None = None
    execution: dict[str, object] | None = None
    if write:
        repair_plans = [plan.as_dict() for plan in ensure_repair_plans_for_matter(conn, matter_scope=resolved_matter)]
        invariant = assert_completion_has_next_action(conn, resolved_matter)
        if auto_execute:
            tick_execution = execute_repair_tick(conn, matter_scope=resolved_matter, max_repairs=10, write=True)
            execution = tick_execution.as_dict()
            if tick_execution.made_progress:
                invariant = assert_completion_has_next_action(conn, resolved_matter)
        else:
            execution = None
        snapshot = record_completion_snapshot(conn, resolved_matter).as_dict()
        reason = f"supervisor made no progress while matter remains incomplete: {next_action.get('reason') or next_action.get('type')}"
        escalation = repo.record_loop_guard_failure(
            conn,
            matter_scope=resolved_matter,
            target_type="matter",
            target_id=resolved_matter,
            error_type="supervisor_no_progress",
            message=reason,
            source="supervisor.no_progress_detected",
            payload={"next_action": next_action},
        )
        if bool(escalation.get("terminal")):
            reason = f"supervisor no-progress repair limit reached; user intervention required: {next_action.get('reason') or next_action.get('type')}"
        attention_id = repo.record_human_attention_once(
            conn,
            matter_scope=resolved_matter,
            target_type="matter",
            target_id=resolved_matter,
            severity="blocker",
            reason=reason,
        )
        _ = repo.emit_event(
            conn,
            "supervisor.no_progress_detected",
            matter_scope=resolved_matter,
            payload={
                "next_action": next_action,
                "completion_invariant": invariant.as_dict(),
                "completion_snapshot_id": snapshot.get("snapshot_id", ""),
                "missing_certifications": list(report.missing_certifications),
                "runnable_count": report.runnable_count,
                "reducer_pending_count": report.reducer_pending_count,
                "failed_count": report.failed_count,
                "blocked_count": report.blocked_count,
                "repair_plan_ids": [str(plan["repair_plan_id"]) for plan in repair_plans],
                "repair_execution": execution,
                "attention_id": attention_id or "",
                "blocker_decision": _blocker_decision(next_action),
                "escalation": escalation,
            },
        )
    else:
        invariant = assert_completion_has_next_action(conn, resolved_matter)
        snapshot = {}

    return {
        "ok": False,
        "matter_scope": resolved_matter,
        "reason": "no_progress_with_incomplete_matter",
        "next_action": next_action,
        "completion_invariant": invariant.as_dict(),
        "completion_snapshot_id": snapshot.get("snapshot_id", ""),
        "blocker_decision": _blocker_decision(next_action),
        "missing_certifications": report.missing_certifications,
        "runnable_count": report.runnable_count,
        "reducer_pending_count": report.reducer_pending_count,
        "failed_count": report.failed_count,
        "blocked_count": report.blocked_count,
        "repair_plans": repair_plans,
        "attention_id": attention_id or "",
        "repair_execution": execution,
    }


def _has_any_items(tick_result: Mapping[str, object], keys: tuple[str, ...]) -> bool:
    for key in keys:
        value = tick_result.get(key)
        if isinstance(value, list | tuple | set | dict) and len(value) > 0:
            return True
        if value and not isinstance(value, list | tuple | set | dict):
            return True
    return False


def _single_active_matter(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT matter_scope
        FROM matters
        WHERE status = 'active'
        ORDER BY matter_scope
        LIMIT 2
        """
    ).fetchall()
    if len(rows) != 1:
        return ""
    return str(cast(Mapping[str, object], rows[0])["matter_scope"])


def _blocker_decision(next_action: Mapping[str, object]) -> dict[str, object]:
    action_type = str(next_action.get("type") or "unknown")
    owner = str(next_action.get("owner") or "maintenance")
    if owner in {"provider", "operator"}:
        decision = "human_decision"
    elif action_type in {"blocked_task", "failed_task", "unknown_incomplete"}:
        decision = "repair"
    elif action_type in {"manual_reducer_review", "missing_certification", "supervisor_tick"}:
        decision = "next_action"
    else:
        decision = "blocker"
    return {
        "decision": decision,
        "owner": owner,
        "type": action_type,
        "resume_command": str(next_action.get("resume_command") or ""),
    }
