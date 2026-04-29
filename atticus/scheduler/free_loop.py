"""Autonomous safe free-model loop for Atticus.

This module is deliberately small and conservative. Workers only create
candidate packets; reducer code remains the single canonical writer. The loop is
bounded by caller-provided ticks so tests and operators can run it safely.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sqlite3
from typing import cast
from uuid import uuid4

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.reducer.reducer import ReductionBlocked, reduce_candidate
from atticus.scheduler.lease import LeaseError, acquire_lease
from atticus.scheduler.planner import select_runnable_tasks
from atticus.workers.proposed_tasks import import_proposed_tasks_from_candidate
from atticus.workers.runtime import execute_codex_work_order, execute_local_work_order, execute_openrouter_work_order

def run_free_loop_once(
    conn: sqlite3.Connection,
    *,
    output_dir: str | Path,
    capacity: int = 15,
    execute_workers: bool = True,
    runtime: str = "openrouter",
    allow_live: bool = False,
    env: Mapping[str, str] | None = None,
    codex_timeout_seconds: float = 180.0,
    codex_reasoning_effort: str = "low",
) -> dict[str, object]:
    """Run one safe supervisor tick.

    Order matters: reduce already-completed worker candidates first, import their
    approved follow-up tasks, then fill free capacity with currently unblocked
    runnable tasks. This prevents a completed candidate from stranding the queue.
    """

    reduced_candidates: list[str] = []
    imported_tasks: list[str] = []
    reduction_errors: list[dict[str, str]] = []

    for candidate in _pending_candidates(conn):
        candidate_id = str(candidate["candidate_id"])
        task_id = str(candidate["task_id"])
        try:
            reducer_lease_id = acquire_lease(
                conn,
                task_id=task_id,
                worker_id=f"atticus-reducer-{_short_id()}",
                seconds=900,
                dry_run=False,
                lease_role="reducer",
            )
            reduction = reduce_candidate(
                conn,
                candidate_id=candidate_id,
                reducer_lease_id=reducer_lease_id,
                dry_run=False,
            )
            reduced_candidates.append(candidate_id)
            reducer_imported = reduction.get("imported_tasks", [])
            if isinstance(reducer_imported, list):
                imported_tasks.extend(str(imported_task_id) for imported_task_id in cast(list[object], reducer_imported))
            else:
                imported_tasks.extend(import_proposed_tasks_from_candidate(conn, candidate))
        except (LeaseError, ReductionBlocked, ValueError, KeyError) as exc:
            reduction_errors.append({"candidate_id": candidate_id, "task_id": task_id, "error": str(exc)})
            _ = repo.record_human_attention(
                conn,
                target_type="candidate",
                target_id=candidate_id,
                severity="blocker",
                reason=f"free loop reduction failed: {exc}",
            )

    runnable = select_runnable_tasks(conn, capacity=max(0, capacity))
    leased_tasks: list[str] = []
    executed_tasks: list[str] = []
    worker_errors: list[dict[str, str]] = []

    for index, task in enumerate(runnable, start=1):
        task_id = str(task["task_id"])
        worker_id = f"atticus-free-{index:02d}-{_short_id()}"
        lease_id: str | None = None
        try:
            lease_id = acquire_lease(conn, task_id=task_id, worker_id=worker_id, seconds=900, dry_run=False)
            leased_tasks.append(task_id)
            if not execute_workers:
                continue
            if runtime == "local":
                _ = execute_local_work_order(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, output_dir=output_dir)
            elif runtime == "openrouter":
                _ = execute_openrouter_work_order(
                    conn,
                    task_id=task_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    output_dir=output_dir,
                    env=env,
                    allow_live=allow_live,
                )
            elif runtime == "codex":
                _ = execute_codex_work_order(
                    conn,
                    task_id=task_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    output_dir=output_dir,
                    env=env,
                    allow_live=allow_live,
                    timeout_seconds=codex_timeout_seconds,
                    reasoning_effort=codex_reasoning_effort,
                )
            else:
                raise ValueError(f"unsupported free loop runtime: {runtime}")
            executed_tasks.append(task_id)
        except Exception as exc:
            if lease_id is not None:
                _fail_active_lease_after_worker_exception(conn, lease_id=lease_id, task_id=task_id, reason=str(exc))
            worker_errors.append({"task_id": task_id, "error": str(exc)})
            _ = repo.record_human_attention(
                conn,
                target_type="task",
                target_id=task_id,
                severity="blocker",
                reason=f"free loop worker failed: {exc}",
            )

    ok = not reduction_errors and not worker_errors
    _ = repo.emit_event(
        conn,
        "free_loop.tick",
        matter_scope=_tick_matter_scope(conn, leased_tasks=leased_tasks, executed_tasks=executed_tasks, reduction_errors=reduction_errors, worker_errors=worker_errors),
        payload={
            "ok": ok,
            "reduced_candidates": reduced_candidates,
            "imported_tasks": imported_tasks,
            "leased_tasks": leased_tasks,
            "executed_tasks": executed_tasks,
            "reduction_errors": reduction_errors,
            "worker_errors": worker_errors,
        },
    )
    return {
        "ok": ok,
        "reduced_candidates": reduced_candidates,
        "imported_tasks": imported_tasks,
        "leased_tasks": leased_tasks,
        "executed_tasks": executed_tasks,
        "reduction_errors": reduction_errors,
        "worker_errors": worker_errors,
    }


def run_free_loop(
    conn: sqlite3.Connection,
    *,
    output_dir: str | Path,
    capacity: int = 15,
    max_ticks: int = 1,
    runtime: str = "openrouter",
    allow_live: bool = False,
    env: Mapping[str, str] | None = None,
    codex_timeout_seconds: float = 180.0,
    codex_reasoning_effort: str = "low",
) -> dict[str, object]:
    """Run a bounded autonomous free loop and return per-tick summaries."""

    ticks: list[dict[str, object]] = []
    for _ in range(max(0, max_ticks)):
        tick = run_free_loop_once(
            conn,
            output_dir=output_dir,
            capacity=capacity,
            execute_workers=True,
            runtime=runtime,
            allow_live=allow_live,
            env=env,
            codex_timeout_seconds=codex_timeout_seconds,
            codex_reasoning_effort=codex_reasoning_effort,
        )
        ticks.append(tick)
        if not tick["reduced_candidates"] and not tick["leased_tasks"]:
            break
    ok = all(bool(tick.get("ok")) for tick in ticks)
    return {"ok": ok, "ticks": ticks, "tick_count": len(ticks)}


def _fail_active_lease_after_worker_exception(conn: sqlite3.Connection, *, lease_id: str, task_id: str, reason: str) -> None:
    """Defensively close capacity if a worker crashes before its runtime cleanup."""

    row = cast(Mapping[str, object] | None, conn.execute("SELECT status FROM leases WHERE lease_id = ? AND task_id = ?", (lease_id, task_id)).fetchone())
    if row is None or row["status"] != "active":
        return
    now = utc_now()
    _ = conn.execute("UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ?", (now, lease_id))
    repo.update_task_blocked(conn, task_id, [reason])
    _ = repo.emit_event(
        conn,
        "lease.failed",
        matter_scope=repo.matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown",
        payload={"lease_id": lease_id, "task_id": task_id, "reason": reason},
    )


def _pending_candidates(conn: sqlite3.Connection) -> list[Mapping[str, object]]:
    return [
        cast(Mapping[str, object], row)
        for row in conn.execute(
            """
            SELECT co.* FROM candidate_outputs co
            JOIN tasks t ON t.task_id = co.task_id
            WHERE co.status = 'candidate' AND t.status = ?
            ORDER BY co.created_at ASC
            """,
            (TaskStatus.REDUCER_PENDING,),
        )
    ]


def _short_id() -> str:
    return uuid4().hex[:8]


def _tick_matter_scope(
    conn: sqlite3.Connection,
    *,
    leased_tasks: list[str],
    executed_tasks: list[str],
    reduction_errors: list[dict[str, str]],
    worker_errors: list[dict[str, str]],
) -> str:
    task_ids = set(leased_tasks) | set(executed_tasks)
    task_ids.update(item["task_id"] for item in reduction_errors if item.get("task_id"))
    task_ids.update(item["task_id"] for item in worker_errors if item.get("task_id"))
    scopes = {
        scope
        for task_id in task_ids
        if (scope := repo.matter_scope_for_target(conn, target_type="task", target_id=task_id))
    }
    if not scopes:
        return "atticus"
    if len(scopes) == 1:
        return scopes.pop()
    return "multi"
