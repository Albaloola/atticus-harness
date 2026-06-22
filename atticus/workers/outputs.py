"""Candidate output recording with lease quarantine."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
import sqlite3

from typing import cast
from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.scheduler.lease import LeaseError, complete_lease, require_active_lease
from atticus.workers.citation_context import allowed_citation_targets_for_task, proof_citation_targets_for_task
from atticus.workers.result_parser import ResultPacketError, packet_as_dict, parse_result


def record_worker_result(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str,
    worker_id: str,
    payload: dict[str, object],
) -> str:
    status = "candidate"
    quarantine_reason = ""
    try:
        lease = require_active_lease(conn, lease_id=lease_id)
        if lease["task_id"] != task_id:
            raise LeaseError(f"lease {lease_id} belongs to task {lease['task_id']}, not {task_id}")
        if lease["worker_id"] != worker_id:
            raise LeaseError(f"lease {lease_id} belongs to worker {lease['worker_id']}, not {worker_id}")
        _record_blocked_external_action_requests(conn, task_id=task_id, worker_id=worker_id, payload=payload)
        packet = parse_result(
            payload,
            allowed_citation_targets=allowed_citation_targets_for_task(conn, task_id=task_id),
            proof_citation_targets=proof_citation_targets_for_task(conn, task_id=task_id),
        )
        if packet.task_id != task_id:
            raise ResultPacketError(f"worker result task_id {packet.task_id!r} does not match leased task {task_id!r}")
        payload = packet_as_dict(packet)
    except (LeaseError, ResultPacketError) as exc:
        status = "quarantined"
        quarantine_reason = _augment_quarantine_reason(conn, task_id=task_id, reason=str(exc))
        _fail_active_lease(conn, lease_id=lease_id, reason=quarantine_reason)
        _ = repo.record_human_attention(
            conn,
            target_type="task",
            target_id=task_id,
            severity="blocker",
            reason=f"worker output quarantined: {quarantine_reason}",
        )
        _report_quarantine_to_orchestrator(conn, task_id=task_id, reason=quarantine_reason)

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


def _record_blocked_external_action_requests(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    worker_id: str,
    payload: Mapping[str, object],
) -> None:
    requests = payload.get("external_action_requests")
    if not isinstance(requests, list):
        return
    for request in cast(list[object], requests):
        if isinstance(request, Mapping):
            request_map = cast(Mapping[object, object], request)
            action_type = str(request_map.get("action_type") or request_map.get("type") or "external_action")
            request_payload = {str(key): value for key, value in request_map.items()}
        else:
            action_type = "external_action"
            request_payload = {"request": str(request)}
        _ = repo.record_external_action_block(
            conn,
            action_type=action_type,
            requested_by=worker_id,
            reason="worker result external action requests are blocked",
            payload={"task_id": task_id, "request": request_payload},
            matter_scope=_task_matter_scope(conn, task_id=task_id),
        )


def reject_candidate_output(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    reason: str,
    dry_run: bool = True,
) -> dict[str, object]:
    """Operator-review path for valid packets that should not be reduced."""

    reason = reason.strip()
    if not reason:
        raise ValueError("candidate rejection reason is required")
    row = cast(Mapping[str, object] | None, conn.execute(
        "SELECT candidate_id, task_id, status FROM candidate_outputs WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone())
    if row is None:
        raise ValueError(f"unknown candidate: {candidate_id}")
    if row["status"] != "candidate":
        raise ValueError(f"candidate {candidate_id} has status {row['status']}")
    task_id = str(row["task_id"])
    active_lease = cast(object | None, conn.execute(
        "SELECT 1 FROM leases WHERE task_id = ? AND status = 'active' LIMIT 1",
        (task_id,),
    ).fetchone())
    if active_lease is not None:
        raise ValueError(f"task {task_id} has an active lease; cannot reject candidate concurrently")
    other_candidate = cast(object | None, conn.execute(
        "SELECT 1 FROM candidate_outputs WHERE task_id = ? AND candidate_id != ? AND status = 'candidate' LIMIT 1",
        (task_id, candidate_id),
    ).fetchone())
    next_status = TaskStatus.REDUCER_PENDING if other_candidate is not None else TaskStatus.QUEUED
    result: dict[str, object] = {
        "dry_run": dry_run,
        "candidate_id": candidate_id,
        "task_id": task_id,
        "new_candidate_status": "quarantined",
        "new_task_status": str(next_status),
        "reason": reason,
    }
    if dry_run:
        return result
    now = utc_now()
    _ = conn.execute(
        "UPDATE candidate_outputs SET status = 'quarantined', quarantined_reason = ? WHERE candidate_id = ?",
        (reason, candidate_id),
    )
    _ = conn.execute(
        "UPDATE tasks SET status = ?, blocked_reasons_json = '[]', updated_at = ? WHERE task_id = ?",
        (next_status, now, task_id),
    )
    _ = repo.record_human_attention(
        conn,
        target_type="candidate",
        target_id=candidate_id,
        severity="warning",
        reason=f"candidate rejected by operator: {reason}",
    )
    _ = repo.emit_event(
        conn,
        "candidate.rejected",
        matter_scope=_task_matter_scope(conn, task_id=task_id),
        payload={"candidate_id": candidate_id, "task_id": task_id, "reason": reason, "next_task_status": str(next_status)},
    )
    return result


def _fail_active_lease(conn: sqlite3.Connection, *, lease_id: str, reason: str) -> None:
    row = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT task_id, status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()))
    if row is None or row["status"] != "active":
        return
    cur = conn.execute(
        "UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ? AND status = 'active'",
        (utc_now(), lease_id),
    )
    if cur.rowcount:
        _restore_task_after_failed_output_lease(conn, task_id=str(row["task_id"]))
        _ = repo.emit_event(conn, "lease.failed", matter_scope=_task_matter_scope(conn, task_id=str(row["task_id"])), payload={"lease_id": lease_id, "task_id": row["task_id"], "reason": reason})


def _augment_quarantine_reason(conn: sqlite3.Connection, *, task_id: str, reason: str) -> str:
    match = re.search(r"target artifact:([^ ]+) is outside work order context", reason)
    if match is None:
        return reason
    artifact_id = match.group(1)
    task = conn.execute("SELECT source_dependencies_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        return reason
    try:
        source_deps_raw = json.loads(str(task["source_dependencies_json"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        return reason
    if not isinstance(source_deps_raw, list):
        return reason
    source_deps = [str(item) for item in source_deps_raw if isinstance(item, str)]
    if not source_deps:
        return reason
    rows = conn.execute(
        "SELECT source_id FROM artifact_sources WHERE artifact_id = ? AND source_id IN (%s) ORDER BY source_id"
        % ",".join("?" for _ in source_deps),
        (artifact_id, *source_deps),
    ).fetchall()
    source_ids = [str(row["source_id"]) for row in rows]
    if not source_ids:
        return reason
    return (
        f"{reason}; extracted source-material artifacts are provenance, not primary evidence targets. "
        f"Cite source:{source_ids[0]} instead unless the artifact is explicitly listed in artifact_dependencies."
    )


def _report_quarantine_to_orchestrator(conn: sqlite3.Connection, *, task_id: str, reason: str) -> None:
    try:
        _ = repo.record_orchestrator_worker_failure(
            conn,
            task_id=task_id,
            failure_reason=f"worker output quarantined: {reason}",
            source="worker_result_quarantine",
        )
    except Exception as exc:
        _ = repo.emit_event(
            conn,
            "orchestrator.failure_signal_failed",
            matter_scope=_task_matter_scope(conn, task_id=task_id),
            payload={"task_id": task_id, "reason": reason, "signal_error": str(exc)},
        )


def _restore_task_after_failed_output_lease(conn: sqlite3.Connection, *, task_id: str) -> None:
    pending_candidate = cast(object | None, conn.execute(
        "SELECT 1 FROM candidate_outputs WHERE task_id = ? AND status = 'candidate' LIMIT 1",
        (task_id,),
    ).fetchone())
    next_status = TaskStatus.REDUCER_PENDING if pending_candidate is not None else TaskStatus.QUEUED
    _ = conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status IN (?, ?)",
        (next_status, utc_now(), task_id, TaskStatus.LEASED, TaskStatus.RUNNING),
    )


def _task_matter_scope(conn: sqlite3.Connection, *, task_id: str) -> str:
    row = cast(Mapping[str, object] | None, conn.execute("SELECT matter_scope FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    return str(row["matter_scope"]) if row is not None else "unknown"
