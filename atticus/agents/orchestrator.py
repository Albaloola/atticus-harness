"""Matter-scoped orchestrator state and repair proposals."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.providers.model_policy import default_smart_model_policy, load_model_routing_policy, smart_provider_policy_for_route
from atticus.scheduler.gates import evaluate_task_gates
from atticus.scheduler.lease import LeaseError, acquire_lease, expire_leases


def ensure_matter_orchestrator(conn: sqlite3.Connection, matter_scope: str) -> str:
    current = repo.get_matter_orchestrator(conn, matter_scope=matter_scope)
    if current is not None:
        return str(current["orchestrator_id"])
    return repo.upsert_matter_orchestrator(conn, matter_scope=matter_scope, status="idle")


def orchestrator_tick(conn: sqlite3.Connection, matter_scope: str, capacity: int, *, dry_run: bool = True) -> dict[str, object]:
    current = repo.get_matter_orchestrator(conn, matter_scope=matter_scope)
    orchestrator_id = str(current["orchestrator_id"]) if current is not None else ""
    if not dry_run and not orchestrator_id:
        orchestrator_id = ensure_matter_orchestrator(conn, matter_scope)
    if not dry_run:
        _ = expire_leases(conn)
    candidates = _runnable_matter_tasks(conn, matter_scope=matter_scope, capacity=max(0, capacity))
    leased: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    if not dry_run:
        for task in candidates:
            task_id = str(task["task_id"])
            try:
                lease_id = acquire_lease(
                    conn,
                    task_id=task_id,
                    worker_id=f"orchestrator-{orchestrator_id}",
                    seconds=900,
                    dry_run=False,
                )
            except LeaseError as exc:
                skipped.append({"task_id": task_id, "reason": str(exc)})
                continue
            leased.append({"task_id": task_id, "lease_id": lease_id})
        _ = repo.record_orchestrator_event(
            conn,
            orchestrator_id=orchestrator_id,
            event_type="orchestrator.tick",
            payload={"dry_run": False, "capacity": capacity, "leased": leased, "skipped": skipped},
        )
    return {
        "dry_run": dry_run,
        "matter_scope": matter_scope,
        "orchestrator_id": orchestrator_id,
        "would_create_orchestrator": dry_run and not orchestrator_id,
        "capacity": capacity,
        "runnable_task_ids": [str(task["task_id"]) for task in candidates],
        "leased": leased,
        "skipped": skipped,
        "external_actions": "blocked",
    }


def report_worker_failure_to_orchestrator(
    conn: sqlite3.Connection,
    task_id: str,
    failure_reason: str,
    *,
    matter_scope: str | None = None,
) -> str:
    task = conn.execute("SELECT matter_scope FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    task_matter_scope = str(task["matter_scope"])
    if matter_scope is not None and task_matter_scope != matter_scope:
        raise ValueError(f"task {task_id} belongs to matter {task_matter_scope}, not {matter_scope}")
    orchestrator_id = ensure_matter_orchestrator(conn, task_matter_scope)
    _ = repo.record_human_attention(
        conn,
        target_type="task",
        target_id=task_id,
        severity="warning",
        reason=f"worker failure reported to orchestrator: {failure_reason}",
    )
    _ = conn.execute(
        """
        UPDATE matter_orchestrators
        SET failure_count = failure_count + 1, status = 'repair_required', updated_at = ?
        WHERE orchestrator_id = ?
        """,
        (utc_now(), orchestrator_id),
    )
    return repo.record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.worker_failed",
        payload={"task_id": task_id, "failure_reason": failure_reason, "retry_policy": "no silent infinite retry"},
    )


def orchestrator_plan_repair(conn: sqlite3.Connection, matter_scope: str, failure_event_id: str) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT oe.*, mo.failure_count
        FROM orchestrator_events oe
        JOIN matter_orchestrators mo ON mo.orchestrator_id = oe.orchestrator_id
        WHERE oe.orchestrator_event_id = ? AND oe.matter_scope = ?
        """,
        (failure_event_id, matter_scope),
    ).fetchone()
    if row is None:
        raise ValueError(f"failure event not found in matter {matter_scope}: {failure_event_id}")
    payload = _json_object(str(row["payload_json"] or "{}"))
    reason = str(payload.get("failure_reason") or "").lower()
    actions: list[dict[str, object]] = []
    if any(term in reason for term in ("citation", "unsupported", "fabricated")):
        actions.append({"type": "verifier_task", "task_type": "citation_audit", "reason": "failure mentions citation/support"})
    if any(term in reason for term in ("context", "token", "missing source", "stale")):
        actions.append({"type": "context_rebuild", "reason": "failure suggests context/source mismatch"})
    if int(row["failure_count"]) >= 2 or any(term in reason for term in ("contradiction", "complex", "uncertain")):
        actions.append({"type": "model_upgrade", "decision_tier": "pro_orchestrator", "reason": "repeated or complex failure requires Pro review"})
    if not actions:
        actions.append({"type": "human_intervention", "reason": "failure reason does not map to a safe automatic repair"})
    return {
        "matter_scope": matter_scope,
        "failure_event_id": failure_event_id,
        "proposed_actions": actions,
        "retry_limit": 1,
        "external_actions": "blocked",
        "canonical_writes": "reducer_only",
    }


def orchestrator_select_model(conn: sqlite3.Connection, matter_scope: str, task_id: str) -> dict[str, object]:
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ? AND matter_scope = ?", (task_id, matter_scope)).fetchone())
    if task is None:
        raise ValueError(f"task not found in matter {matter_scope}: {task_id}")
    provider_policy = _provider_policy(task)
    policy_raw = provider_policy.get("model_routing")
    policy = load_model_routing_policy(cast(Mapping[str, object], policy_raw)) if isinstance(policy_raw, Mapping) else default_smart_model_policy()
    decision_policy = smart_provider_policy_for_route(
        policy,
        layer=_layer_for_task(task),
        stage=str(task["stage"]),
        task_type=str(task["task_type"]),
        task_id=task_id,
        matter_scope=matter_scope,
        expected_value=float(str(task["expected_value"] or 0.0)),
    )
    orchestrator_id = ensure_matter_orchestrator(conn, matter_scope)
    _ = conn.execute(
        "UPDATE matter_orchestrators SET model_decision_json = ?, updated_at = ? WHERE orchestrator_id = ?",
        (json.dumps(decision_policy.get("model_decision") or {}, sort_keys=True), utc_now(), orchestrator_id),
    )
    _ = repo.record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.model_selected",
        payload={"task_id": task_id, "provider_policy": decision_policy},
    )
    return decision_policy


def _runnable_matter_tasks(conn: sqlite3.Connection, *, matter_scope: str, capacity: int) -> list[Mapping[str, object]]:
    if capacity <= 0:
        return []
    rows = cast(list[Mapping[str, object]], conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE matter_scope = ? AND status IN (?, ?, ?)
        ORDER BY expected_value DESC, created_at ASC
        """,
        (matter_scope, str(TaskStatus.QUEUED), str(TaskStatus.READY), str(TaskStatus.BLOCKED)),
    ).fetchall())
    runnable: list[Mapping[str, object]] = []
    for task in rows:
        if conn.execute("SELECT 1 FROM leases WHERE task_id = ? AND status = 'active'", (task["task_id"],)).fetchone() is not None:
            continue
        gate_result = evaluate_task_gates(conn, task)
        if gate_result.allowed:
            runnable.append(task)
        if len(runnable) >= capacity:
            break
    return runnable


def _provider_policy(task: Mapping[str, object]) -> dict[str, object]:
    try:
        loaded = json.loads(str(task["provider_policy_json"] or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(cast(Mapping[str, object], loaded)) if isinstance(loaded, Mapping) else {}


def _json_object(text: str) -> dict[str, object]:
    loaded = json.loads(text)
    return dict(cast(Mapping[str, object], loaded)) if isinstance(loaded, Mapping) else {}


def _layer_for_task(task: Mapping[str, object]) -> str:
    task_type = str(task["task_type"])
    if "hostile" in task_type:
        return "hostile_review"
    if "final_quality" in task_type:
        return "verifier"
    if "reducer" in task_type:
        return "reducer"
    return "worker"
