"""Fenced task lease helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from datetime import UTC, datetime, timedelta
import json
import sqlite3
from uuid import uuid4

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.scheduler.capacity import MAX_PARALLEL_AGENT_CAPACITY
from atticus.scheduler.gates import blocked_task_auto_requeue_allowed, evaluate_task_gates


class LeaseError(RuntimeError):
    """Raised when a lease cannot be acquired or used."""


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def acquire_lease(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    worker_id: str,
    seconds: int = 900,
    dry_run: bool = False,
    lease_role: str = "worker",
) -> str:
    if lease_role not in {"worker", "reducer"}:
        raise LeaseError(f"unsupported lease role: {lease_role}")
    _savepoint_used = False
    if not dry_run:
        if conn.in_transaction:
            _ = conn.execute("SAVEPOINT lease_acquire")
            _savepoint_used = True
        else:
            _ = conn.execute("BEGIN IMMEDIATE")
    task = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()))
    if task is None:
        raise LeaseError(f"unknown task: {task_id}")
    if not dry_run:
        _ = expire_leases(conn, task_id=task_id)
        task = cast(Mapping[str, object], conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    leaseable = {
        TaskStatus.QUEUED,
        TaskStatus.READY,
        TaskStatus.REDUCER_PENDING,
        TaskStatus.BLOCKED,
        str(TaskStatus.QUEUED),
        str(TaskStatus.READY),
        str(TaskStatus.REDUCER_PENDING),
        str(TaskStatus.BLOCKED),
    }
    if task["status"] not in leaseable:
        raise LeaseError(f"task {task_id} is not leaseable from status {task['status']}")
    if lease_role == "reducer" and task["status"] != TaskStatus.REDUCER_PENDING:
        raise LeaseError(f"reducer lease requires task {task_id} to be in status {TaskStatus.REDUCER_PENDING}")
    if not dry_run and seconds >= 0:
        _ = expire_leases(conn)
    if lease_role == "worker" and not dry_run:
        active_worker_count = _active_worker_lease_count(conn)
        if active_worker_count >= MAX_PARALLEL_AGENT_CAPACITY:
            raise LeaseError(f"global worker capacity reached: {active_worker_count}/{MAX_PARALLEL_AGENT_CAPACITY}")

    gate_result = evaluate_task_gates(conn, task)
    if not gate_result.allowed:
        if not dry_run:
            repo.update_task_blocked(conn, task_id, gate_result.reasons)
        raise LeaseError(f"task {task_id} is blocked by gates: {'; '.join(gate_result.reasons)}")
    if task["status"] == TaskStatus.BLOCKED and not dry_run:
        if not blocked_task_auto_requeue_allowed(task):
            raise LeaseError(f"task {task_id} is blocked by terminal runtime failure")
        reasons = _blocked_reasons(task)
        _ = conn.execute(
            "UPDATE tasks SET status = ?, blocked_reasons_json = '[]', updated_at = ? WHERE task_id = ? AND status = ?",
            (TaskStatus.QUEUED, utc_now(), task_id, TaskStatus.BLOCKED),
        )
        _ = repo.emit_event(conn, "task.unblocked", matter_scope=str(task["matter_scope"]), payload={"task_id": task_id, "reason": "lease gates passed"})
        _ = repo.resolve_system_task_attention(
            conn,
            task_id=task_id,
            matter_scope=str(task["matter_scope"]),
            reasons=reasons,
            resolution_source="lease.gates_passed",
        )
        task = cast(Mapping[str, object], conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())

    existing = cast(Mapping[str, object] | None, conn.execute(
        "SELECT lease_id FROM leases WHERE task_id = ? AND status = 'active'",
        (task_id,),
    ).fetchone())
    if existing is not None:
        raise LeaseError(f"task {task_id} already has active lease {existing['lease_id']}")

    if dry_run:
        return f"dry-run-lease-{task_id}"

    lease_id = f"lease-{uuid4().hex}"
    current = conn.execute(
        "SELECT COALESCE(MAX(fencing_token), 0) AS token FROM leases WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    fencing_token = int(float(str(current["token"] if current is not None else 0))) + 1
    now = utc_now()
    expires_at = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    try:
        _ = conn.execute(
            """
            INSERT INTO leases(lease_id, task_id, worker_id, lease_role, status, fencing_token, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (lease_id, task_id, worker_id, lease_role, fencing_token, expires_at, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise LeaseError(f"task {task_id} already has an active lease") from exc
    _ = conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
        (TaskStatus.LEASED, now, task_id),
    )
    _ = repo.resolve_system_task_attention(
        conn,
        task_id=task_id,
        matter_scope=str(task["matter_scope"]),
        resolution_source="lease.acquired",
    )
    _ = repo.emit_event(
        conn,
        "lease.acquired",
        matter_scope=str(task["matter_scope"]),
        payload={"lease_id": lease_id, "task_id": task_id, "worker_id": worker_id, "fencing_token": fencing_token},
    )
    if _savepoint_used:
        _ = conn.execute("RELEASE SAVEPOINT lease_acquire")
    return lease_id


def _active_worker_lease_count(conn: sqlite3.Connection) -> int:
    now = datetime.now(UTC)
    count = 0
    for row in conn.execute("SELECT expires_at FROM leases WHERE status = 'active' AND lease_role = 'worker'"):
        if _parse_time(str(row["expires_at"])) > now:
            count += 1
    return count


def _blocked_reasons(task: Mapping[str, object]) -> list[str]:
    try:
        raw = task["blocked_reasons_json"]
    except KeyError:
        return []
    try:
        loaded = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def lease_is_active(conn: sqlite3.Connection, *, lease_id: str, task_id: str | None = None) -> bool:
    params: tuple[str, ...]
    if task_id is None:
        params = (lease_id,)
        row = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT * FROM leases WHERE lease_id = ?", params).fetchone()))
    else:
        params = (lease_id, task_id)
        row = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT * FROM leases WHERE lease_id = ? AND task_id = ?", params).fetchone()))
    if row is None or row["status"] != "active":
        return False
    return _parse_time(str(row["expires_at"])) > datetime.now(UTC)


def require_active_lease(conn: sqlite3.Connection, *, lease_id: str, task_id: str | None = None) -> Mapping[str, object]:
    if task_id is None:
        row = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()))
    else:
        row = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT * FROM leases WHERE lease_id = ? AND task_id = ?", (lease_id, task_id)).fetchone()))
    if row is None:
        raise LeaseError(f"unknown lease: {lease_id}")
    if row["status"] != "active":
        raise LeaseError(f"lease {lease_id} is not active")
    if _parse_time(str(row["expires_at"])) <= datetime.now(UTC):
        raise LeaseError(f"lease {lease_id} is expired")
    return row


def complete_lease(conn: sqlite3.Connection, *, lease_id: str, task_status: str = TaskStatus.REDUCER_PENDING) -> None:
    lease = require_active_lease(conn, lease_id=lease_id)
    now = utc_now()
    _ = conn.execute(
        "UPDATE leases SET status = 'completed', updated_at = ? WHERE lease_id = ?",
        (now, lease_id),
    )
    _ = conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
        (str(task_status), now, lease["task_id"]),
    )
    matter_scope = repo.matter_scope_for_target(conn, target_type="task", target_id=str(lease["task_id"])) or "unknown"
    _ = repo.resolve_system_task_attention(
        conn,
        task_id=str(lease["task_id"]),
        matter_scope=matter_scope,
        resolution_source="lease.completed",
    )
    _ = repo.emit_event(conn, "lease.completed", matter_scope=matter_scope, payload={"lease_id": lease_id, "task_id": lease["task_id"]})


def expire_leases(conn: sqlite3.Connection, *, task_id: str | None = None) -> list[str]:
    now_dt = datetime.now(UTC)
    expired: list[str] = []
    if task_id is None:
        rows = conn.execute("SELECT * FROM leases WHERE status = 'active'")
    else:
        rows = conn.execute("SELECT * FROM leases WHERE status = 'active' AND task_id = ?", (task_id,))
    for row in rows:
        if _parse_time(str(row["expires_at"])) <= now_dt:
            lease_task_id = str(row["task_id"])
            lease_id = str(row["lease_id"])
            next_status = _status_after_expired_lease(conn, task_id=lease_task_id)
            expired.append(lease_id)
            _ = conn.execute(
                "UPDATE leases SET status = 'expired', updated_at = ? WHERE lease_id = ?",
                (utc_now(), lease_id),
            )
            _ = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status IN (?, ?)",
                (next_status, utc_now(), lease_task_id, TaskStatus.LEASED, TaskStatus.RUNNING),
            )
            _ = repo.record_human_attention(
                conn,
                matter_scope=repo.matter_scope_for_target(conn, target_type="task", target_id=lease_task_id) or "unknown",
                target_type="task",
                target_id=lease_task_id,
                severity="warning",
                reason=f"stale_transient_network: lease expired: {lease_id}",
            )
            _ = repo.emit_event(
                conn,
                "lease.expired",
                matter_scope=repo.matter_scope_for_target(conn, target_type="task", target_id=lease_task_id) or "unknown",
                payload={"lease_id": row["lease_id"], "task_id": row["task_id"], "worker_id": row["worker_id"]},
            )
    return expired


def _status_after_expired_lease(conn: sqlite3.Connection, *, task_id: str) -> TaskStatus:
    pending_candidate = cast(object | None, conn.execute(
        "SELECT 1 FROM candidate_outputs WHERE task_id = ? AND status = 'candidate' LIMIT 1",
        (task_id,),
    ).fetchone())
    return TaskStatus.REDUCER_PENDING if pending_candidate is not None else TaskStatus.QUEUED
