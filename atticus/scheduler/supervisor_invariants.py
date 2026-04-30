"""Supervisor invariants that prevent quiet incomplete ticks."""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import cast

from atticus.agents.repair_planner import ensure_repair_plans_for_matter
from atticus.db import repo
from atticus.status.completion import build_matter_completion_report, next_resume_action


PROGRESS_KEYS = (
    "reduced_candidates",
    "imported_tasks",
    "leased_tasks",
    "executed_tasks",
    "applied_actions",
    "routed_operator_signals",
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
) -> dict[str, object]:
    """Return ok=False when a zero-progress tick leaves unresolved matter work."""

    resolved_matter = matter_scope or _single_active_matter(conn)
    if not resolved_matter:
        return {"ok": True, "reason": "matter_scope_not_resolved"}
    if _has_any_items(tick_result, PROGRESS_KEYS):
        return {"ok": True, "matter_scope": resolved_matter, "reason": "progress_made"}
    if _has_any_items(tick_result, EXPLANATION_KEYS):
        return {"ok": True, "matter_scope": resolved_matter, "reason": "tick_reported_blocker_or_error"}

    report = build_matter_completion_report(conn, resolved_matter)
    if report.done:
        return {
            "ok": True,
            "matter_scope": resolved_matter,
            "reason": "matter_complete",
            "next_action": {"type": "complete", "resume_command": ""},
        }

    next_action = next_resume_action(conn, resolved_matter)
    repair_plans: list[dict[str, object]] = []
    attention_id: int | None = None
    if write:
        repair_plans = [plan.as_dict() for plan in ensure_repair_plans_for_matter(conn, matter_scope=resolved_matter)]
        reason = f"supervisor made no progress while matter remains incomplete: {next_action.get('reason') or next_action.get('type')}"
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
                "missing_certifications": list(report.missing_certifications),
                "runnable_count": report.runnable_count,
                "reducer_pending_count": report.reducer_pending_count,
                "failed_count": report.failed_count,
                "blocked_count": report.blocked_count,
                "repair_plan_ids": [str(plan["repair_plan_id"]) for plan in repair_plans],
                "attention_id": attention_id or "",
            },
        )

    return {
        "ok": False,
        "matter_scope": resolved_matter,
        "reason": "no_progress_with_incomplete_matter",
        "next_action": next_action,
        "missing_certifications": report.missing_certifications,
        "runnable_count": report.runnable_count,
        "reducer_pending_count": report.reducer_pending_count,
        "failed_count": report.failed_count,
        "blocked_count": report.blocked_count,
        "repair_plans": repair_plans,
        "attention_id": attention_id or "",
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
