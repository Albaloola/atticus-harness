"""Safe local worker runtime.

This module deliberately supports only the local stub adapter. It proves the
harness execution path without enabling OpenClaw, shell agents, external legal
actions, or provider-backed spending.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from time import perf_counter
from uuid import uuid4
from typing import Any

from atticus.adapters.local_stub import LocalStubAdapter
from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.providers.budget import charge_budget, require_budget
from atticus.scheduler.lease import require_active_lease
from atticus.workers.contracts import safe_path_component
from atticus.workers.outputs import record_worker_result
from atticus.workers.work_order import build_work_order


class WorkerExecutionBlocked(RuntimeError):
    """Raised when a requested worker execution is outside the safe runtime."""


@dataclass(frozen=True)
class WorkerExecutionResult:
    candidate_id: str
    worker_attempt_id: str
    output_path: Path
    adapter: str
    provider_run_id: str | None


def execute_local_work_order(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str,
    worker_id: str,
    output_dir: str | Path,
    adapter_name: str = "local_stub",
) -> WorkerExecutionResult:
    """Execute one leased task through the local stub and record a candidate.

    The function is intentionally narrow:
    - only ``local_stub`` is accepted;
    - a valid active lease is required;
    - configured task/stage/matter budgets are checked before adapter execution;
    - output is written only to a task-local JSON file;
    - canonical artifacts are never written here.
    """

    if adapter_name != "local_stub":
        raise WorkerExecutionBlocked(f"adapter {adapter_name!r} is not enabled for safe local execution")

    lease = require_active_lease(conn, lease_id=lease_id, task_id=task_id)
    task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        raise WorkerExecutionBlocked(f"unknown task: {task_id}")
    if lease["worker_id"] != worker_id:
        raise WorkerExecutionBlocked(f"lease {lease_id} belongs to worker {lease['worker_id']}, not {worker_id}")

    provider_policy = json.loads(task["provider_policy_json"] or "{}")
    estimated_cost = float(provider_policy.get("estimated_cost_usd") or 0.0)
    if task["cost_limit_usd"] is not None and estimated_cost > float(task["cost_limit_usd"]):
        repo.record_human_attention(
            conn,
            target_type="task",
            target_id=task_id,
            severity="blocker",
            reason=f"task estimated cost {estimated_cost:.4f} exceeds task cost limit {float(task['cost_limit_usd']):.4f}",
        )
        raise WorkerExecutionBlocked("task cost limit would be exceeded")

    for scope_type, scope_id in (("task", task_id), ("stage", task["stage"]), ("matter", task["matter_scope"])):
        require_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated_cost)

    attempt_id = _record_attempt_started(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, adapter=adapter_name)
    started = perf_counter()
    task_component = safe_path_component(task_id)
    output_path = Path(output_dir).resolve() / task_component / f"{attempt_id}.json"
    provider_run_id: str | None = None
    try:
        order = build_work_order(conn, task_id=task_id, lease_id=lease_id, persist_context=True)
        payload = LocalStubAdapter().run(order.as_dict())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        latency_ms = int((perf_counter() - started) * 1000)
        provider_run_id = repo.record_provider_run(
            conn,
            task_id=task_id,
            stage=task["stage"],
            requested_provider=str(provider_policy.get("provider") or "local"),
            requested_model=str(provider_policy.get("model") or "stub"),
            actual_provider="local",
            actual_model="stub",
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=0.0,
            latency_ms=latency_ms,
            fallback_allowed=bool(provider_policy.get("allow_fallback") or False),
            fallback_policy_result="local_stub_not_provider_backed",
            raw_usage={"adapter": adapter_name, "output_path": str(output_path)},
        )
        for scope_type, scope_id in (("task", task_id), ("stage", task["stage"]), ("matter", task["matter_scope"])):
            charge_budget(conn, scope_type=scope_type, scope_id=scope_id, amount_usd=estimated_cost, provider_run_id=provider_run_id)
        candidate_id = record_worker_result(
            conn,
            task_id=task_id,
            lease_id=lease_id,
            worker_id=worker_id,
            payload=payload,
        )
        candidate = conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if candidate is None or candidate["status"] != "candidate":
            reason = candidate["quarantined_reason"] if candidate is not None else "candidate output was not recorded"
            _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            conn.commit()
            raise WorkerExecutionBlocked(f"local worker output quarantined: {reason}")
        _record_attempt_finished(conn, attempt_id=attempt_id, status="succeeded", output_path=output_path)
        return WorkerExecutionResult(
            candidate_id=candidate_id,
            worker_attempt_id=attempt_id,
            output_path=output_path,
            adapter=adapter_name,
            provider_run_id=provider_run_id,
        )
    except WorkerExecutionBlocked:
        raise
    except Exception as exc:
        _mark_lease_failed(conn, lease_id=lease_id, reason=str(exc))
        _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": str(exc)})
        repo.update_task_status(conn, task_id, TaskStatus.FAILED, str(exc))
        conn.commit()
        raise


def _mark_lease_failed(conn: sqlite3.Connection, *, lease_id: str, reason: str) -> None:
    """Mark a lease as failed so capacity accounting cannot leak active leases."""

    now = utc_now()
    row = conn.execute("SELECT task_id FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ?",
        (now, lease_id),
    )
    repo.emit_event(conn, "lease.failed", payload={"lease_id": lease_id, "task_id": row["task_id"], "reason": reason})


def _record_attempt_started(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str,
    worker_id: str,
    adapter: str,
) -> str:
    attempt_id = f"wattempt-{uuid4().hex}"
    now = utc_now()
    conn.execute(
        """
        INSERT INTO worker_attempts(worker_attempt_id, task_id, lease_id, worker_id, adapter, status, started_at)
        VALUES (?, ?, ?, ?, ?, 'running', ?)
        """,
        (attempt_id, task_id, lease_id, worker_id, adapter, now),
    )
    conn.execute("UPDATE tasks SET status = 'running', updated_at = ? WHERE task_id = ?", (now, task_id))
    repo.emit_event(conn, "worker_attempt.started", payload={"worker_attempt_id": attempt_id, "task_id": task_id, "adapter": adapter})
    return attempt_id


def _record_attempt_finished(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    status: str,
    output_path: Path,
    error: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE worker_attempts
        SET status = ?, finished_at = ?, output_path = ?, error_json = ?
        WHERE worker_attempt_id = ?
        """,
        (status, utc_now(), str(output_path), json.dumps(error or {}, sort_keys=True), attempt_id),
    )
    repo.emit_event(conn, "worker_attempt.finished", payload={"worker_attempt_id": attempt_id, "status": status})
