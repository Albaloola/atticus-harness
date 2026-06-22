"""Capacity-aware scheduler planning."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import sqlite3

from typing import cast
from atticus.agents.decomposition import decompose_broad_task_if_needed
from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.providers.budget import check_budget
from atticus.scheduler.capacity import agent_capacity
from atticus.scheduler.gates import blocked_task_auto_requeue_allowed, evaluate_task_gates


def select_runnable_tasks(
    conn: sqlite3.Connection,
    *,
    capacity: int,
    dry_run: bool = False,
    matter_scope: str | None = None,
    allow_decomposition: bool = True,
    resolved_transient_blocker_prefixes: tuple[str, ...] = (),
) -> list[Mapping[str, object]]:
    capacity_requested = agent_capacity(capacity)
    if capacity_requested == 0:
        return []

    runnable: list[Mapping[str, object]] = []
    matter_clause = "AND matter_scope = ?" if matter_scope else ""
    params: tuple[object, ...] = (matter_scope,) if matter_scope else ()
    for task in conn.execute(
        f"""
        SELECT * FROM tasks
        WHERE status IN ('queued', 'ready', 'blocked')
        {matter_clause}
        ORDER BY expected_value DESC, created_at ASC
        """,
        params,
    ):
        result = evaluate_task_gates(conn, task)
        budget_reasons = budget_blockers(conn, task)
        if result.allowed and not budget_reasons:
            if str(task["status"]) == str(TaskStatus.BLOCKED):
                if not blocked_task_auto_requeue_allowed(
                    task,
                    resolved_transient_blocker_prefixes=resolved_transient_blocker_prefixes,
                ):
                    continue
                if not dry_run:
                    _requeue_previously_blocked_task(conn, task_id=str(task["task_id"]))
                    task = cast(Mapping[str, object], conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone())
            if allow_decomposition and not dry_run:
                decomposition = decompose_broad_task_if_needed(
                    conn,
                    task_id=str(task["task_id"]),
                    reason="pre_dispatch_token_budget",
                    write=True,
                )
                if decomposition.get("applied"):
                    continue
            runnable.append(task)
            if len(runnable) >= capacity_requested:
                break
        else:
            if not dry_run:
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
    row = conn.execute("SELECT matter_scope, blocked_reasons_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    matter_scope = str(row["matter_scope"]) if row is not None else repo.matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown"
    try:
        reasons = json.loads(str(row["blocked_reasons_json"] or "[]")) if row is not None else []
    except (json.JSONDecodeError, TypeError):
        reasons = []
    clean_reasons = [str(reason) for reason in reasons] if isinstance(reasons, list) else []
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
        matter_scope=matter_scope,
        payload={"task_id": task_id, "reason": "scheduler gates passed"},
    )
    _ = repo.resolve_system_task_attention(
        conn,
        task_id=task_id,
        matter_scope=matter_scope,
        reasons=clean_reasons,
        resolution_source="scheduler.gates_passed",
    )
