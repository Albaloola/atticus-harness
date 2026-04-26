"""Fenced task lease helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3
from uuid import uuid4

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.scheduler.gates import evaluate_task_gates


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
) -> str:
    task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        raise LeaseError(f"unknown task: {task_id}")
    if not dry_run:
        expire_leases(conn, task_id=task_id)
        task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
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

    gate_result = evaluate_task_gates(conn, task)
    if not gate_result.allowed:
        if not dry_run:
            repo.update_task_blocked(conn, task_id, gate_result.reasons)
        raise LeaseError(f"task {task_id} is blocked by gates: {'; '.join(gate_result.reasons)}")
    if task["status"] == TaskStatus.BLOCKED and not dry_run:
        conn.execute(
            "UPDATE tasks SET status = ?, blocked_reasons_json = '[]', updated_at = ? WHERE task_id = ? AND status = ?",
            (TaskStatus.QUEUED, utc_now(), task_id, TaskStatus.BLOCKED),
        )
        repo.emit_event(conn, "task.unblocked", payload={"task_id": task_id, "reason": "lease gates passed"})
        task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()

    existing = conn.execute(
        "SELECT lease_id FROM leases WHERE task_id = ? AND status = 'active'",
        (task_id,),
    ).fetchone()
    if existing is not None:
        raise LeaseError(f"task {task_id} already has active lease {existing['lease_id']}")

    if dry_run:
        return f"dry-run-lease-{task_id}"

    lease_id = f"lease-{uuid4().hex}"
    current = conn.execute(
        "SELECT COALESCE(MAX(fencing_token), 0) AS token FROM leases WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    fencing_token = int(current["token"]) + 1
    now = utc_now()
    expires_at = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    try:
        conn.execute(
            """
            INSERT INTO leases(lease_id, task_id, worker_id, status, fencing_token, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (lease_id, task_id, worker_id, fencing_token, expires_at, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise LeaseError(f"task {task_id} already has an active lease") from exc
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
        (TaskStatus.LEASED, now, task_id),
    )
    repo.emit_event(
        conn,
        "lease.acquired",
        payload={"lease_id": lease_id, "task_id": task_id, "worker_id": worker_id, "fencing_token": fencing_token},
    )
    return lease_id


def lease_is_active(conn: sqlite3.Connection, *, lease_id: str, task_id: str | None = None) -> bool:
    params: tuple[str, ...]
    if task_id is None:
        params = (lease_id,)
        row = conn.execute("SELECT * FROM leases WHERE lease_id = ?", params).fetchone()
    else:
        params = (lease_id, task_id)
        row = conn.execute("SELECT * FROM leases WHERE lease_id = ? AND task_id = ?", params).fetchone()
    if row is None or row["status"] != "active":
        return False
    return _parse_time(row["expires_at"]) > datetime.now(UTC)


def require_active_lease(conn: sqlite3.Connection, *, lease_id: str, task_id: str | None = None) -> sqlite3.Row:
    if task_id is None:
        row = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM leases WHERE lease_id = ? AND task_id = ?", (lease_id, task_id)).fetchone()
    if row is None:
        raise LeaseError(f"unknown lease: {lease_id}")
    if row["status"] != "active":
        raise LeaseError(f"lease {lease_id} is not active")
    if _parse_time(row["expires_at"]) <= datetime.now(UTC):
        raise LeaseError(f"lease {lease_id} is expired")
    return row


def complete_lease(conn: sqlite3.Connection, *, lease_id: str, task_status: str = TaskStatus.REDUCER_PENDING) -> None:
    lease = require_active_lease(conn, lease_id=lease_id)
    now = utc_now()
    conn.execute(
        "UPDATE leases SET status = 'completed', updated_at = ? WHERE lease_id = ?",
        (now, lease_id),
    )
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
        (str(task_status), now, lease["task_id"]),
    )
    repo.emit_event(conn, "lease.completed", payload={"lease_id": lease_id, "task_id": lease["task_id"]})


def expire_leases(conn: sqlite3.Connection, *, task_id: str | None = None) -> list[str]:
    now_dt = datetime.now(UTC)
    expired: list[str] = []
    if task_id is None:
        rows = conn.execute("SELECT * FROM leases WHERE status = 'active'")
    else:
        rows = conn.execute("SELECT * FROM leases WHERE status = 'active' AND task_id = ?", (task_id,))
    for row in rows:
        if _parse_time(row["expires_at"]) <= now_dt:
            next_status = _status_after_expired_lease(conn, task_id=row["task_id"])
            expired.append(row["lease_id"])
            conn.execute(
                "UPDATE leases SET status = 'expired', updated_at = ? WHERE lease_id = ?",
                (utc_now(), row["lease_id"]),
            )
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status IN (?, ?)",
                (next_status, utc_now(), row["task_id"], TaskStatus.LEASED, TaskStatus.RUNNING),
            )
            repo.record_human_attention(
                conn,
                target_type="task",
                target_id=row["task_id"],
                severity="warning",
                reason=f"lease expired: {row['lease_id']}",
            )
            repo.emit_event(
                conn,
                "lease.expired",
                payload={"lease_id": row["lease_id"], "task_id": row["task_id"], "worker_id": row["worker_id"]},
            )
    return expired


def _status_after_expired_lease(conn: sqlite3.Connection, *, task_id: str) -> TaskStatus:
    pending_candidate = conn.execute(
        "SELECT 1 FROM candidate_outputs WHERE task_id = ? AND status = 'candidate' LIMIT 1",
        (task_id,),
    ).fetchone()
    return TaskStatus.REDUCER_PENDING if pending_candidate is not None else TaskStatus.QUEUED
