"""Capacity-aware scheduler planning."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import sqlite3

from typing import cast
from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.providers.budget import check_budget
from atticus.scheduler.gates import evaluate_task_gates


def select_runnable_tasks(conn: sqlite3.Connection, *, capacity: int) -> list[Mapping[str, object]]:
    capacity_requested = max(0, capacity)
    if capacity_requested == 0:
        return []

    runnable: list[Mapping[str, object]] = []
    for task in conn.execute(
        """
        SELECT * FROM tasks
        WHERE status IN ('queued', 'ready', 'blocked')
        ORDER BY expected_value DESC, created_at ASC
        """
    ):
        result = evaluate_task_gates(conn, task)
        budget_reasons = budget_blockers(conn, task)
        if result.allowed and not budget_reasons:
            if str(task["status"]) == str(TaskStatus.BLOCKED):
                _requeue_previously_blocked_task(conn, task_id=str(task["task_id"]))
                task = cast(Mapping[str, object], conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone())
            runnable.append(task)
            if len(runnable) >= capacity_requested:
                break
        else:
            repo.update_task_blocked(conn, str(task["task_id"]), result.reasons + budget_reasons)
    return runnable


def budget_blockers(conn: sqlite3.Connection, task: sqlite3.Row) -> list[str]:
    reasons: list[str] = []
    estimated = _estimated_cost_usd(task, reasons)

    if reasons:
        return reasons

    cost_limit = cast(object, task["cost_limit_usd"])
    if cost_limit is not None and cost_limit != "" and estimated > float(str(cost_limit)):
        reasons.append(
            f"task estimated cost {estimated:.4f} exceeds task cost limit {float(str(cost_limit)):.4f}"
        )

    for scope_type, scope_id in (
        ("task", str(task["task_id"])),
        ("stage", str(task["stage"])),
        ("matter", str(task["matter_scope"])),
    ):
        decision = check_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated)
        if not decision.allowed:
            reasons.append(f"budget blocked for {scope_type}:{scope_id}: {decision.reason}")
    return reasons


def _estimated_cost_usd(task: sqlite3.Row, reasons: list[str]) -> float:
    try:
        policy = json.loads(str(task["provider_policy_json"] or "{}"))
    except (json.JSONDecodeError, TypeError) as exc:
        reasons.append(f"malformed provider policy for task {task['task_id']}: {exc}")
        return 0.0
    if not isinstance(policy, dict):
        reasons.append(f"malformed provider policy for task {task['task_id']}: policy must be a JSON object")
        return 0.0
    policy_map = cast(dict[str, object], policy)
    raw = policy_map.get("estimated_cost_usd")
    if raw is None:
        return 0.0
    if isinstance(raw, bool):
        reasons.append(f"provider policy for task {task['task_id']} has invalid estimated_cost_usd: boolean is not allowed")
        return 0.0
    try:
        if not isinstance(raw, int | float | str):
            raise TypeError(f"unsupported value type: {type(raw).__name__}")
        estimated = float(str(raw))
    except (TypeError, ValueError) as exc:
        reasons.append(f"provider policy for task {task['task_id']} has invalid estimated_cost_usd: {raw!r}: {exc}")
        return 0.0
    if not math.isfinite(estimated) or estimated < 0:
        reasons.append(f"provider policy for task {task['task_id']} has invalid estimated_cost_usd: must be finite and non-negative")
        return 0.0
    return estimated


def _requeue_previously_blocked_task(conn: sqlite3.Connection, *, task_id: str) -> None:
    _ = conn.execute(
        """
        UPDATE tasks
        SET status = ?, blocked_reasons_json = '[]', updated_at = ?
        WHERE task_id = ? AND status = ?
        """,
        (TaskStatus.QUEUED, utc_now(), task_id, TaskStatus.BLOCKED),
    )
    _ = repo.emit_event(
        conn,
        "task.unblocked",
        matter_scope=repo.matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown",
        payload={"task_id": task_id, "reason": "scheduler gates passed"},
    )
