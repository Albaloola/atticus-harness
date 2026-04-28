"""Autonomous safe free-model loop for Atticus.

This module is deliberately small and conservative. Workers only create
candidate packets; reducer code remains the single canonical writer. The loop is
bounded by caller-provided ticks so tests and operators can run it safely.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import cast
from uuid import uuid4

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.deepseek import OPENROUTER_FREE_MODEL_ORDER
from atticus.providers.model_policy import validate_proposed_task_provider_policy
from atticus.providers.policy import canonical_provider_policy
from atticus.reducer.reducer import ReductionBlocked, reduce_candidate
from atticus.scheduler.lease import LeaseError, acquire_lease
from atticus.scheduler.planner import select_runnable_tasks
from atticus.workers.runtime import execute_codex_work_order, execute_local_work_order, execute_openrouter_work_order

DEFAULT_FREE_MODEL = OPENROUTER_FREE_MODEL_ORDER[0]


def run_free_loop_once(
    conn: sqlite3.Connection,
    *,
    output_dir: str | Path,
    capacity: int = 15,
    execute_workers: bool = True,
    runtime: str = "openrouter",
    allow_live: bool = False,
    env: Mapping[str, str] | None = None,
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
            )
            _ = reduce_candidate(
                conn,
                candidate_id=candidate_id,
                reducer_lease_id=reducer_lease_id,
                dry_run=False,
            )
            reduced_candidates.append(candidate_id)
            imported_tasks.extend(_import_proposed_tasks_from_candidate(conn, candidate))
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
                _ = execute_codex_work_order(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, output_dir=output_dir)
            else:
                raise ValueError(f"unsupported free loop runtime: {runtime}")
            executed_tasks.append(task_id)
        except Exception as exc:
            worker_errors.append({"task_id": task_id, "error": str(exc)})
            _ = repo.record_human_attention(
                conn,
                target_type="task",
                target_id=task_id,
                severity="blocker",
                reason=f"free loop worker failed: {exc}",
            )

    _ = repo.emit_event(
        conn,
        "free_loop.tick",
        payload={
            "reduced_candidates": reduced_candidates,
            "imported_tasks": imported_tasks,
            "leased_tasks": leased_tasks,
            "executed_tasks": executed_tasks,
            "reduction_errors": reduction_errors,
            "worker_errors": worker_errors,
        },
    )
    return {
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
        )
        ticks.append(tick)
        if not tick["reduced_candidates"] and not tick["leased_tasks"]:
            break
    return {"ticks": ticks, "tick_count": len(ticks)}


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


def _import_proposed_tasks_from_candidate(conn: sqlite3.Connection, candidate: Mapping[str, object]) -> list[str]:
    payload = json.loads(str(candidate["payload_json"]))
    if not isinstance(payload, Mapping):
        return []
    raw_tasks = payload.get("proposed_tasks", [])
    if not isinstance(raw_tasks, list):
        return []
    imported: list[str] = []
    parent_task_id = str(candidate["task_id"])
    parent_task = cast(Mapping[str, object] | None, conn.execute("SELECT provider_policy_json FROM tasks WHERE task_id = ?", (parent_task_id,)).fetchone())
    parent_policy = _load_parent_provider_policy(parent_task)
    for index, raw_task in enumerate(raw_tasks, start=1):
        if not isinstance(raw_task, Mapping):
            continue
        task_map = cast(Mapping[object, object], raw_task)
        task_id = str(task_map.get("task_id") or f"{parent_task_id}-followup-{index}")
        if _task_exists(conn, task_id):
            continue
        stage = str(task_map.get("stage") or LegalStage.S0_SOURCE_INVENTORY)
        provider_policy = _provider_policy(task_map, parent_policy=parent_policy)
        repo.add_task(
            conn,
            TaskSpec(
                task_id=task_id,
                title=str(task_map.get("title") or task_id),
                task_type=str(task_map.get("task_type") or "followup"),
                matter_scope=str(task_map.get("matter_scope") or "atticus"),
                stage=cast(LegalStage, cast(object, stage)),
                status=TaskStatus.QUEUED,
                source_dependencies=_string_list(task_map.get("source_dependencies")),
                artifact_dependencies=_string_list(task_map.get("artifact_dependencies")),
                task_dependencies=_string_list(task_map.get("task_dependencies")),
                matter_dependencies=_string_list(task_map.get("matter_dependencies")),
                required_certifications=_mapping_list(task_map.get("required_certifications")),
                validation_gates=_string_list(task_map.get("validation_gates")),
                staleness_rules=_dict(task_map.get("staleness_rules")),
                provider_policy=provider_policy,
                cost_limit_usd=_optional_float(task_map.get("cost_limit_usd")),
                expected_value=_optional_float(task_map.get("expected_value")) or 0.0,
            ),
        )
        imported.append(task_id)
    return imported


def _provider_policy(task_map: Mapping[object, object], *, parent_policy: Mapping[str, object] | None = None) -> dict[str, object]:
    if parent_policy:
        return validate_proposed_task_provider_policy(parent_provider_policy=parent_policy, proposed_task=task_map)
    raw = task_map.get("provider_policy")
    policy = dict(cast(Mapping[str, object], raw)) if isinstance(raw, Mapping) else {}
    provider = str(policy.get("provider") or task_map.get("provider") or "openrouter")
    model = str(policy.get("model") or task_map.get("model") or DEFAULT_FREE_MODEL)
    return canonical_provider_policy(
        provider=provider,
        model=model,
        allow_fallback=bool(policy.get("allow_fallback") or False),
        estimated_cost_usd=_optional_float(policy.get("estimated_cost_usd")) or 0.0,
    )


def _load_parent_provider_policy(parent_task: Mapping[str, object] | None) -> dict[str, object]:
    if parent_task is None:
        return {}
    try:
        raw = json.loads(str(parent_task["provider_policy_json"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): value for key, value in cast(Mapping[object, object], raw).items()}


def _task_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is not None


def _string_list(value: object) -> list[str]:
    return [str(item) for item in cast(list[object], value)] if isinstance(value, list) else []


def _mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(cast(Mapping[str, object], item)) for item in cast(list[object], value) if isinstance(item, Mapping)]


def _dict(value: object) -> dict[str, object]:
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _short_id() -> str:
    return uuid4().hex[:8]
