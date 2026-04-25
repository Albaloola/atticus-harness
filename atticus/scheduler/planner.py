"""Capacity-aware scheduler planning."""

from __future__ import annotations

import sqlite3

from atticus.db import repo
from atticus.providers.budget import check_budget
from atticus.scheduler.gates import evaluate_task_gates


def select_runnable_tasks(conn: sqlite3.Connection, *, capacity: int) -> list[sqlite3.Row]:
    runnable: list[sqlite3.Row] = []
    for task in conn.execute(
        """
        SELECT * FROM tasks
        WHERE status IN ('queued', 'ready')
        ORDER BY expected_value DESC, created_at ASC
        """
    ):
        result = evaluate_task_gates(conn, task)
        budget_reasons = _budget_blockers(conn, task)
        if result.allowed and not budget_reasons:
            runnable.append(task)
            if len(runnable) >= capacity:
                break
        else:
            repo.update_task_blocked(conn, task["task_id"], result.reasons + budget_reasons)
    return runnable


def _budget_blockers(conn: sqlite3.Connection, task: sqlite3.Row) -> list[str]:
    reasons: list[str] = []
    estimated = 0.0
    try:
        import json

        policy = json.loads(task["provider_policy_json"])
        estimated = float(policy.get("estimated_cost_usd") or 0)
    except (KeyError, TypeError, ValueError):
        estimated = 0.0

    if task["cost_limit_usd"] is not None and estimated > float(task["cost_limit_usd"]):
        reasons.append(
            f"task estimated cost {estimated:.4f} exceeds task cost limit {float(task['cost_limit_usd']):.4f}"
        )

    for scope_type, scope_id in (
        ("task", task["task_id"]),
        ("stage", task["stage"]),
        ("matter", task["matter_scope"]),
    ):
        decision = check_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated)
        if not decision.allowed:
            reasons.append(f"budget blocked for {scope_type}:{scope_id}: {decision.reason}")
    return reasons
