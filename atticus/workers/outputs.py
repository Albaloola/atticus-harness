"""Candidate output recording with lease quarantine."""

from __future__ import annotations

import sqlite3
from typing import Any

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.scheduler.lease import LeaseError, complete_lease, require_active_lease
from atticus.workers.result_parser import ResultPacketError, parse_result


def record_worker_result(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str,
    worker_id: str,
    payload: dict[str, Any],
) -> str:
    status = "candidate"
    quarantine_reason = ""
    try:
        lease = require_active_lease(conn, lease_id=lease_id)
        if lease["task_id"] != task_id:
            raise LeaseError(f"lease {lease_id} belongs to task {lease['task_id']}, not {task_id}")
        if lease["worker_id"] != worker_id:
            raise LeaseError(f"lease {lease_id} belongs to worker {lease['worker_id']}, not {worker_id}")
        packet = parse_result(payload)
        if packet.task_id != task_id:
            raise ResultPacketError(f"worker result task_id {packet.task_id!r} does not match leased task {task_id!r}")
    except (LeaseError, ResultPacketError) as exc:
        status = "quarantined"
        quarantine_reason = str(exc)
        _fail_active_lease(conn, lease_id=lease_id, reason=quarantine_reason)
        repo.record_human_attention(
            conn,
            target_type="task",
            target_id=task_id,
            severity="blocker",
            reason=f"worker output quarantined: {quarantine_reason}",
        )

    candidate_id = repo.record_candidate_output(
        conn,
        task_id=task_id,
        lease_id=lease_id,
        worker_id=worker_id,
        output_type="worker_result_packet",
        payload=payload,
        status=status,
        quarantined_reason=quarantine_reason,
    )
    if status == "candidate":
        complete_lease(conn, lease_id=lease_id, task_status=TaskStatus.REDUCER_PENDING)
    else:
        repo.update_task_status(conn, task_id, TaskStatus.QUARANTINED, quarantine_reason)
    return candidate_id


def _fail_active_lease(conn: sqlite3.Connection, *, lease_id: str, reason: str) -> None:
    row = conn.execute("SELECT task_id, status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
    if row is None or row["status"] != "active":
        return
    cur = conn.execute(
        "UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ? AND status = 'active'",
        (utc_now(), lease_id),
    )
    if cur.rowcount:
        _restore_task_after_failed_output_lease(conn, task_id=row["task_id"])
        repo.emit_event(conn, "lease.failed", payload={"lease_id": lease_id, "task_id": row["task_id"], "reason": reason})


def _restore_task_after_failed_output_lease(conn: sqlite3.Connection, *, task_id: str) -> None:
    pending_candidate = conn.execute(
        "SELECT 1 FROM candidate_outputs WHERE task_id = ? AND status = 'candidate' LIMIT 1",
        (task_id,),
    ).fetchone()
    next_status = TaskStatus.REDUCER_PENDING if pending_candidate is not None else TaskStatus.QUEUED
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status IN (?, ?)",
        (next_status, utc_now(), task_id, TaskStatus.LEASED, TaskStatus.RUNNING),
    )
