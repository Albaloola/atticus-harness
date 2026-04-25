"""Safe live resume orchestration without launching workers."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.providers.live_readiness import live_readiness_report
from atticus.scheduler.lease import acquire_lease, expire_leases


def prepare_live_resume(
    conn: sqlite3.Connection,
    *,
    capacity: int = 15,
    env: Mapping[str, str] | None = None,
    probe_result: Mapping[str, Any] | None = None,
    write_leases: bool = False,
    worker_prefix: str = "atticus-openrouter",
    lease_seconds: int = 900,
) -> dict[str, Any]:
    """Return a live resume plan and optionally acquire leases for safe slots.

    This is intentionally only an orchestration gate. It never starts OpenClaw,
    shells, provider workers, or external legal actions. A successful provider
    probe is required before any lease is written. Stale active leases are
    expired globally before readiness so old capacity cannot strand tasks.
    """

    capacity_requested = max(0, capacity)
    expired_leases = expire_leases(conn)
    readiness = live_readiness_report(conn, capacity=capacity_requested, env=env)
    plan_readiness = dict(readiness)
    reasons = list(plan_readiness.get("reasons") or [])
    if probe_result is not None and not isinstance(probe_result, Mapping):
        probe = {"ok": False, "reason": "probe_result_json must decode to a JSON object"}
    else:
        probe = dict(probe_result or {})
    if probe.get("ok") is not True:
        reasons.append(str(probe.get("reason") or "successful OpenRouter probe with literal ok=true is required before live leasing"))
    else:
        probe_provider = str(probe.get("provider") or "")
        probe_model = str(probe.get("model") or "")
        matching_tasks: list[dict[str, Any]] = []
        mismatched_tasks: list[dict[str, Any]] = []
        for task in plan_readiness.get("runnable_tasks", []):
            task_provider = str(task.get("provider") or "")
            task_model = str(task.get("model") or "")
            if probe_provider != task_provider or probe_model != task_model:
                mismatched_tasks.append(
                    {
                        "task_id": task.get("task_id"),
                        "title": task.get("title"),
                        "reasons": [
                            "OpenRouter probe does not match runnable task provider policy: "
                            f"probe={probe_provider or 'unset'}/{probe_model or 'unset'} "
                            f"task={task.get('task_id')}/{task_provider or 'unset'}/{task_model or 'unset'}"
                        ],
                    }
                )
            else:
                matching_tasks.append(task)
        if mismatched_tasks:
            plan_readiness["blocked_tasks"] = list(plan_readiness.get("blocked_tasks") or []) + mismatched_tasks
            plan_readiness["runnable_tasks"] = matching_tasks
            plan_readiness["runnable_task_ids"] = [task["task_id"] for task in matching_tasks]
            plan_readiness["capacity_safe"] = len(matching_tasks)
            if not matching_tasks:
                reasons.append(mismatched_tasks[0]["reasons"][0])
        plan_readiness["ready"] = bool(plan_readiness.get("ready")) and bool(plan_readiness.get("runnable_task_ids"))

    leases: list[dict[str, Any]] = []
    can_lease = not reasons and bool(plan_readiness.get("ready"))
    if can_lease and write_leases:
        try:
            for index, task_id in enumerate(plan_readiness["runnable_task_ids"], start=1):
                worker_id = f"{worker_prefix}-{index:02d}"
                lease_id = acquire_lease(conn, task_id=task_id, worker_id=worker_id, seconds=lease_seconds, dry_run=False)
                leases.append({"task_id": task_id, "worker_id": worker_id, "lease_id": lease_id})
        except Exception as exc:
            _rollback_live_resume_leases(conn, leases=leases, reason=str(exc))
            reasons.append(f"live lease acquisition failed before launch; rolled back {len(leases)} leases: {exc}")
            leases = []
            can_lease = False
    elif can_lease:
        for index, task_id in enumerate(plan_readiness["runnable_task_ids"], start=1):
            leases.append({"task_id": task_id, "worker_id": f"{worker_prefix}-{index:02d}", "lease_id": f"dry-run-lease-{task_id}"})

    return {
        **plan_readiness,
        "ready": can_lease,
        "reasons": reasons,
        "probe": probe,
        "write_leases": write_leases,
        "leases": leases,
        "expired_leases": expired_leases,
    }


def _rollback_live_resume_leases(conn: sqlite3.Connection, *, leases: list[dict[str, Any]], reason: str) -> None:
    """Fail leases written by a partially failed live-resume planning pass."""

    now = utc_now()
    for lease in leases:
        conn.execute(
            "UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ? AND status = 'active'",
            (now, lease["lease_id"]),
        )
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
            (TaskStatus.QUEUED, now, lease["task_id"], TaskStatus.LEASED),
        )
    if leases:
        repo.emit_event(
            conn,
            "live_resume.rollback_leases",
            payload={"reason": reason, "leases": leases},
        )
