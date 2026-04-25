"""Candidate output recording with lease quarantine."""

from __future__ import annotations

import sqlite3

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
    payload: dict,
) -> str:
    status = "candidate"
    quarantine_reason = ""
    try:
        require_active_lease(conn, lease_id=lease_id, task_id=task_id)
        parse_result(payload)
    except (LeaseError, ResultPacketError) as exc:
        status = "quarantined"
        quarantine_reason = str(exc)
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
