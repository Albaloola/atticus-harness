"""Safe live resume orchestration without launching workers."""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.providers.live_readiness import live_readiness_report
from atticus.scheduler.capacity import agent_capacity
from atticus.scheduler.lease import acquire_lease, expire_leases


def prepare_live_resume(
    conn: sqlite3.Connection,
    *,
    capacity: int = 15,
    env: Mapping[str, str] | None = None,
    matter_scope: str | None = None,
    probe_result: object | None = None,
    write_leases: bool = False,
    worker_prefix: str = "atticus-openrouter",
    lease_seconds: int = 900,
) -> dict[str, object]:
    """Return a live resume plan and optionally acquire leases for safe slots.

    This is intentionally only an orchestration gate. It never starts OpenClaw,
    shells, provider workers, or external legal actions. A successful provider
    probe is required before any lease is written. Stale active leases are
    expired globally before readiness so old capacity cannot strand tasks.
    """

    capacity_requested = max(0, capacity)
    capacity_effective = agent_capacity(capacity_requested)
    expired_leases = expire_leases(conn) if write_leases else []
    readiness = live_readiness_report(conn, capacity=capacity_requested, env=env, matter_scope=matter_scope)
    plan_readiness = dict(readiness)
    reasons_raw = plan_readiness.get("reasons")
    reasons = [str(reason) for reason in cast(list[object], reasons_raw)] if isinstance(reasons_raw, list) else []
    if probe_result is not None and not isinstance(probe_result, Mapping):
        probe = {"ok": False, "reason": "probe_result_json must decode to a JSON object"}
    else:
        probe = {str(key): value for key, value in cast(Mapping[object, object], probe_result).items()} if isinstance(probe_result, Mapping) else {}
    if probe.get("ok") is not True:
        reasons.append(str(probe.get("reason") or "successful OpenRouter probe with literal ok=true is required before live leasing"))
    else:
        probe_provider = str(probe.get("provider") or "")
        probe_model = str(probe.get("requested_model") or probe.get("model") or "")
        matching_tasks: list[dict[str, object]] = []
        mismatched_tasks: list[dict[str, object]] = []
        runnable_tasks = cast(list[dict[str, object]], plan_readiness.get("runnable_tasks") if isinstance(plan_readiness.get("runnable_tasks"), list) else [])
        for task in runnable_tasks:
            task_provider = str(task.get("provider") or "")
            task_model = str(task.get("model") or "")
            models_raw = task.get("models")
            task_models = [str(model) for model in cast(list[object], models_raw) if str(model)] if isinstance(models_raw, list) else ([task_model] if task_model else [])
            if not _probe_matches_task_policy(probe=probe, probe_provider=probe_provider, probe_model=probe_model, task_provider=task_provider, task_models=task_models):
                reason = f"OpenRouter probe does not match runnable task provider policy: probe={probe_provider or 'unset'}/{probe_model or 'unset'} task={task.get('task_id')}/{task_provider or 'unset'}/{','.join(task_models) or 'unset'}"
                mismatched_tasks.append(
                    {
                        "task_id": task.get("task_id"),
                        "title": task.get("title"),
                        "reasons": [reason],
                    }
                )
            else:
                matching_tasks.append(task)
        if mismatched_tasks:
            blocked_raw = plan_readiness.get("blocked_tasks")
            blocked_tasks = cast(list[dict[str, object]], blocked_raw if isinstance(blocked_raw, list) else [])
            plan_readiness["blocked_tasks"] = blocked_tasks + mismatched_tasks
            plan_readiness["runnable_tasks"] = matching_tasks
            plan_readiness["runnable_task_ids"] = [task["task_id"] for task in matching_tasks]
            plan_readiness["capacity_safe"] = len(matching_tasks)
            if not matching_tasks:
                reasons.append(str(cast(list[str], mismatched_tasks[0]["reasons"])[0]))
        plan_readiness["ready"] = bool(plan_readiness.get("ready")) and bool(plan_readiness.get("runnable_task_ids"))
        if matter_scope is not None:
            _ = repo.resolve_local_stub_blockers_after_live_approval(conn, matter_scope=matter_scope)

    leases: list[dict[str, object]] = []
    can_lease = not reasons and bool(plan_readiness.get("ready"))
    if can_lease and write_leases:
        try:
            runnable_task_ids = [str(task_id) for task_id in cast(list[object], plan_readiness["runnable_task_ids"])]
            for index, task_id in enumerate(runnable_task_ids, start=1):
                worker_id = f"{worker_prefix}-{index:02d}"
                lease_id = acquire_lease(conn, task_id=task_id, worker_id=worker_id, seconds=lease_seconds, dry_run=False)
                leases.append({"task_id": task_id, "worker_id": worker_id, "lease_id": lease_id})
        except Exception as exc:
            _rollback_live_resume_leases(conn, leases=leases, reason=str(exc))
            reasons.append(f"live lease acquisition failed before launch; rolled back {len(leases)} leases: {exc}")
            leases = []
            can_lease = False
    elif can_lease:
        runnable_task_ids = [str(task_id) for task_id in cast(list[object], plan_readiness["runnable_task_ids"])]
        for index, task_id in enumerate(runnable_task_ids, start=1):
            leases.append({"task_id": task_id, "worker_id": f"{worker_prefix}-{index:02d}", "lease_id": f"dry-run-lease-{task_id}"})

    return {
        **plan_readiness,
        "capacity_effective": capacity_effective,
        "ready": can_lease,
        "reasons": reasons,
        "probe": probe,
        "write_leases": write_leases,
        "leases": leases,
        "expired_leases": expired_leases,
    }


def _probe_matches_task_policy(
    *,
    probe: Mapping[str, object],
    probe_provider: str,
    probe_model: str,
    task_provider: str,
    task_models: list[str],
) -> bool:
    """Return whether a preverified provider probe is usable for a task policy."""

    if probe_model not in task_models:
        return False
    if probe_provider == task_provider:
        return True
    provenance_result = str(probe.get("provider_policy_result") or "")
    return task_provider == "openrouter" and provenance_result == "openrouter_endpoint_provenance"


def _rollback_live_resume_leases(conn: sqlite3.Connection, *, leases: list[dict[str, object]], reason: str) -> None:
    """Fail leases written by a partially failed live-resume planning pass."""

    now = utc_now()
    for lease in leases:
        _ = conn.execute(
            "UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ? AND status = 'active'",
            (now, lease["lease_id"]),
        )
        _ = conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status = ?",
            (TaskStatus.QUEUED, now, lease["task_id"], TaskStatus.LEASED),
        )
    if leases:
        _ = repo.emit_event(
            conn,
            "live_resume.rollback_leases",
            matter_scope=_rollback_matter_scope(conn, leases=leases),
            payload={"reason": reason, "leases": leases},
        )


def _rollback_matter_scope(conn: sqlite3.Connection, *, leases: list[dict[str, object]]) -> str:
    scopes = {
        scope
        for lease in leases
        if (scope := repo.matter_scope_for_target(conn, target_type="task", target_id=str(lease.get("task_id") or "")))
    }
    if not scopes:
        return "unknown"
    if len(scopes) == 1:
        return scopes.pop()
    return "multi"
