"""Autonomous safe free-model loop for Atticus.

This module is deliberately small and conservative. Workers only create
candidate packets; reducer code remains the single canonical writer. The loop is
bounded by caller-provided ticks so tests and operators can run it safely.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sqlite3
from typing import cast
from uuid import uuid4

from atticus.core.events import utc_now
from atticus.core.policies import LegalStage, TaskStatus
from atticus.agents.decomposition import compact_decomposed_parent_if_needed, decompose_broad_task_if_needed
from atticus.agents.orchestrator import report_worker_failure_to_orchestrator
from atticus.agents.repair_executor import execute_repair_tick
from atticus.db import repo
from atticus.providers.cache_observability import fingerprint_provider_policy
from atticus.providers.live_readiness import probe_live_openrouter
from atticus.reducer.review_queue import enqueue_open_reducer_reviews_for_matter, enqueue_reducer_review
from atticus.reducer.reducer import ReductionBlocked, reduce_candidate
from atticus.scheduler.capacity import MAX_PARALLEL_AGENT_CAPACITY, agent_capacity
from atticus.scheduler.gates import LIVE_CODEX_NOT_ENABLED_BLOCKER, LIVE_OPENROUTER_NOT_ENABLED_BLOCKER
from atticus.scheduler.lease import LeaseError, acquire_lease
from atticus.scheduler.planner import select_runnable_tasks
from atticus.scheduler.supervisor_invariants import evaluate_no_silent_idle
from atticus.workers.proposed_tasks import import_proposed_tasks_from_candidate
from atticus.workers.runtime import execute_codex_work_order, execute_local_work_order, execute_openrouter_work_order


OPENROUTER_TRANSIENT_BLOCKER_PREFIXES = (
    "OpenRouter provider call failed after dispatch: OpenRouter response did not contain a JSON message",
    "OpenRouter provider call failed after dispatch: OpenRouter returned invalid JSON",
    "OpenRouter provider call failed after dispatch: OpenRouter network error",
    "OpenRouter provider call failed after dispatch: OpenRouter request failed",
    "OpenRouter provider call failed after dispatch: OpenRouter HTTP 5",
)


def run_free_loop_once(
    conn: sqlite3.Connection,
    *,
    output_dir: str | Path,
    capacity: int = 15,
    execute_workers: bool = True,
    runtime: str = "openrouter",
    allow_live: bool = False,
    env: Mapping[str, str] | None = None,
    codex_timeout_seconds: float = 180.0,
    codex_reasoning_effort: str = "low",
    matter_scope: str | None = None,
) -> dict[str, object]:
    """Run one safe supervisor tick.

    Order matters: reduce already-completed worker candidates first, import their
    approved follow-up tasks, then fill free capacity with currently unblocked
    runnable tasks. This prevents a completed candidate from stranding the queue.
    """

    capacity_requested = max(0, capacity)
    capacity_effective = agent_capacity(capacity_requested)
    reduced_candidates: list[str] = []
    imported_tasks: list[str] = []
    reduction_errors: list[dict[str, str]] = []
    skipped_reductions: list[dict[str, str]] = []

    for candidate in _pending_candidates(conn):
        candidate_id = str(candidate["candidate_id"])
        task_id = str(candidate["task_id"])
        candidate_matter_scope = str(candidate["matter_scope"])
        skip_reason = _auto_reduce_skip_reason(candidate)
        if skip_reason:
            skipped_reductions.append({"candidate_id": candidate_id, "task_id": task_id, "reason": skip_reason})
            _ = enqueue_reducer_review(
                conn,
                candidate_id=candidate_id,
                reason=skip_reason,
                priority=_reducer_review_priority(candidate),
            )
            _record_auto_reduce_skip_attention(
                conn,
                candidate_id=candidate_id,
                matter_scope=candidate_matter_scope,
                reason=skip_reason,
            )
            _commit_progress(conn)
            continue
        try:
            reducer_lease_id = acquire_lease(
                conn,
                task_id=task_id,
                worker_id=f"atticus-reducer-{_short_id()}",
                seconds=900,
                dry_run=False,
                lease_role="reducer",
            )
            reduction = reduce_candidate(
                conn,
                candidate_id=candidate_id,
                reducer_lease_id=reducer_lease_id,
                dry_run=False,
            )
            reduced_candidates.append(candidate_id)
            reducer_imported = reduction.get("imported_tasks", [])
            if isinstance(reducer_imported, list):
                imported_tasks.extend(str(imported_task_id) for imported_task_id in cast(list[object], reducer_imported))
            else:
                imported_tasks.extend(import_proposed_tasks_from_candidate(conn, candidate))
            _commit_progress(conn)
        except (LeaseError, ReductionBlocked, ValueError, KeyError) as exc:
            reduction_errors.append({"candidate_id": candidate_id, "task_id": task_id, "error": str(exc)})
            _ = repo.record_human_attention(
                conn,
                target_type="candidate",
                target_id=candidate_id,
                severity="blocker",
                reason=f"free loop reduction failed: {exc}",
            )
            _report_failure_without_masking(
                conn,
                task_id=task_id,
                reason=f"free loop reduction failed: {exc}",
            )
            _commit_progress(conn)

    reducer_review_ids: list[str] = []
    repair_execution: dict[str, object] = {
        "attempted": [],
        "applied": [],
        "skipped": [],
        "terminal": [],
        "created_task_ids": [],
        "unblocked_task_ids": [],
        "reducer_review_ids": [],
        "attention_ids": [],
        "made_progress": False,
    }
    if matter_scope:
        reviews = enqueue_open_reducer_reviews_for_matter(conn, matter_scope=matter_scope)
        reducer_review_ids = [review.reducer_review_id for review in reviews]
        if reducer_review_ids:
            _commit_progress(conn)
        repair_result = execute_repair_tick(conn, matter_scope=matter_scope, max_repairs=10, write=True)
        repair_execution = repair_result.as_dict()
        if repair_result.made_progress:
            _commit_progress(conn)

    runnable = select_runnable_tasks(
        conn,
        capacity=capacity_effective,
        matter_scope=matter_scope,
        dry_run=False,
        allow_decomposition=True,
        resolved_transient_blocker_prefixes=_resolved_transient_blocker_prefixes(
            runtime=runtime,
            allow_live=allow_live,
            env=env,
        ),
    )
    leased_tasks: list[str] = []
    executed_tasks: list[str] = []
    worker_errors: list[dict[str, str]] = []
    preflight_groups: list[dict[str, object]] = []
    if execute_workers and runtime == "openrouter":
        preflight = _openrouter_preflight(
            conn,
            runnable_tasks=runnable,
            env=env,
            allow_live=allow_live,
        )
        runnable = preflight["runnable_tasks"]
        worker_errors.extend(preflight["errors"])
        preflight_groups.extend(preflight["preflight_groups"])

    leased_workers: list[dict[str, str]] = []
    for index, task in enumerate(runnable, start=1):
        task_id = str(task["task_id"])
        worker_id = f"atticus-free-{index:02d}-{_short_id()}"
        try:
            lease_id = acquire_lease(conn, task_id=task_id, worker_id=worker_id, seconds=900, dry_run=False)
            leased_tasks.append(task_id)
            leased_workers.append({"task_id": task_id, "worker_id": worker_id, "lease_id": lease_id, "index": str(index)})
            _commit_progress(conn)
        except Exception as exc:
            worker_errors.append({"task_id": task_id, "error": str(exc)})
            _report_failure_without_masking(conn, task_id=task_id, reason=f"free loop lease failed: {exc}")
            _commit_progress(conn)

    if execute_workers and leased_workers:
        results = _execute_leased_workers(
            conn,
            leased_workers=leased_workers,
            output_dir=output_dir,
            runtime=runtime,
            allow_live=allow_live,
            env=env,
            codex_timeout_seconds=codex_timeout_seconds,
            codex_reasoning_effort=codex_reasoning_effort,
        )
        executed_tasks.extend(task_id for task_id in results["executed_tasks"])
        worker_errors.extend(results["worker_errors"])
        _commit_progress(conn)

    ok = not reduction_errors and not worker_errors
    result: dict[str, object] = {
        "ok": ok,
        "capacity_requested": capacity_requested,
        "capacity_effective": capacity_effective,
        "capacity_limit": MAX_PARALLEL_AGENT_CAPACITY,
        "reduced_candidates": reduced_candidates,
        "imported_tasks": imported_tasks,
        "leased_tasks": leased_tasks,
        "executed_tasks": executed_tasks,
        "reduction_errors": reduction_errors,
        "skipped_reductions": skipped_reductions,
        "worker_errors": worker_errors,
        "preflight_groups": preflight_groups,
        "reducer_review_ids": reducer_review_ids,
        "repair_execution": repair_execution,
        "repair_progress": bool(repair_execution.get("made_progress")),
        "created_repair_task_ids": list(repair_execution.get("created_task_ids") or []),
        "unblocked_repair_task_ids": list(repair_execution.get("unblocked_task_ids") or []),
    }
    result["no_silent_idle"] = evaluate_no_silent_idle(
        conn,
        matter_scope,
        result,
        write=True,
        auto_execute=True,
    )
    _ = repo.emit_event(
        conn,
        "free_loop.tick",
        matter_scope=_tick_matter_scope(
            conn,
            leased_tasks=leased_tasks,
            executed_tasks=executed_tasks,
            reduction_errors=reduction_errors,
            skipped_reductions=skipped_reductions,
            worker_errors=worker_errors,
        ),
        payload={
            **result,
        },
    )
    _commit_progress(conn)

    if matter_scope:
        try:
            from atticus.status.human_attention_cleanup import plan_human_attention_cleanup

            cleanup = plan_human_attention_cleanup(
                conn,
                matter_scope=matter_scope,
                write=True,
                resolution_source="free_loop_auto_cleanup",
            )
            result["auto_cleanup"] = {
                "superseded": cleanup.get("superseded", 0),
                "action_count": len(cleanup.get("actions", [])),
            }
        except Exception:
            pass

    return result


def run_free_loop(
    conn: sqlite3.Connection,
    *,
    output_dir: str | Path,
    capacity: int = 15,
    max_ticks: int = 1,
    runtime: str = "openrouter",
    allow_live: bool = False,
    env: Mapping[str, str] | None = None,
    codex_timeout_seconds: float = 180.0,
    codex_reasoning_effort: str = "low",
    matter_scope: str | None = None,
) -> dict[str, object]:
    """Run a bounded autonomous free loop and return per-tick summaries."""

    if max_ticks <= 0:
        return {"ok": False, "ticks": [], "tick_count": 0, "stopped_by": "max_ticks_zero"}

    ticks: list[dict[str, object]] = []
    for _ in range(max(0, max_ticks)):
        run_id = _detect_active_run_id(conn, matter_scope)
        if run_id:
            cancelled = repo.check_run_cancelled(conn, run_id=run_id)
            if cancelled is not None:
                return {
                    "ok": False,
                    "ticks": ticks,
                    "tick_count": len(ticks),
                    "stopped_by": "run_cancelled",
                    "cancel_reason": str(cancelled.get("cancel_reason", "")),
                    "cancelled_by": str(cancelled.get("cancelled_by", "")),
                    "cancelled_at": str(cancelled.get("cancelled_at", "")),
                }

        if matter_scope:
            spent = repo.budget_spent(conn, scope_type="matter", scope_id=matter_scope)
            budget_row = conn.execute(
                "SELECT limit_usd, hard_stop FROM budgets WHERE scope_type='matter' AND scope_id=?",
                (matter_scope,),
            ).fetchone()
            if budget_row is not None and budget_row["hard_stop"]:
                limit = float(budget_row["limit_usd"])
                if spent >= limit:
                    return {
                        "ok": False,
                        "ticks": ticks,
                        "tick_count": len(ticks),
                        "stopped_by": "budget_ceiling",
                        "reason": f"spent ${spent:.2f} >= limit ${limit:.2f}",
                        "spent": spent,
                        "limit": limit,
                    }

        tick = run_free_loop_once(
            conn,
            output_dir=output_dir,
            capacity=capacity,
            execute_workers=True,
            runtime=runtime,
            allow_live=allow_live,
            env=env,
            codex_timeout_seconds=codex_timeout_seconds,
            codex_reasoning_effort=codex_reasoning_effort,
            matter_scope=matter_scope,
        )
        ticks.append(tick)

        if matter_scope and run_id:
            signature = _build_progress_signature(conn, matter_scope, tick)
            prog_result = repo.record_progress_signature(
                conn,
                run_id=run_id,
                signature=signature,
                last_distinct_progress_at=utc_now() if _tick_made_progress(tick) else None,
            )
            if prog_result.get("attempt_count", 1) >= 3:
                stop_reason = f"non_progress_loop: {signature} repeated {prog_result['attempt_count']} times"
                repo.record_progress_signature(
                    conn,
                    run_id=run_id,
                    signature=signature,
                    stop_reason=stop_reason,
                )
                return {
                    "ok": False,
                    "ticks": ticks,
                    "tick_count": len(ticks),
                    "stopped_by": "non_progress_loop",
                    "stop_reason": stop_reason,
                    "loop_signature": signature,
                    "same_signature_count": prog_result["attempt_count"],
                    "last_distinct_progress_at": prog_result.get("last_distinct_progress_at"),
                }

        if _matter_complete(conn, matter_scope):
            break
        if not _tick_made_progress(tick):
            next_action = _safe_next_action(conn, matter_scope)
            owner = str(next_action.get("owner") or "")
            action_type = str(next_action.get("type") or "")
            if owner in {"provider", "operator"} or action_type in {"human_attention", "manual_reducer_review"}:
                break
            break
    ok = all(bool(tick.get("ok")) for tick in ticks)
    return {"ok": ok, "ticks": ticks, "tick_count": len(ticks)}


def _resolved_transient_blocker_prefixes(
    *,
    runtime: str,
    allow_live: bool,
    env: Mapping[str, str] | None,
) -> tuple[str, ...]:
    if not allow_live:
        return ()
    effective_env = env if env is not None else os.environ
    if runtime == "openrouter" and effective_env.get("ATTICUS_ENABLE_LIVE_OPENROUTER") == "1":
        return (LIVE_OPENROUTER_NOT_ENABLED_BLOCKER, *OPENROUTER_TRANSIENT_BLOCKER_PREFIXES)
    if runtime == "codex" and effective_env.get("ATTICUS_ENABLE_LIVE_CODEX") == "1":
        return (LIVE_CODEX_NOT_ENABLED_BLOCKER,)
    return ()


def _openrouter_preflight(
    conn: sqlite3.Connection,
    *,
    runnable_tasks: list[Mapping[str, object]],
    env: Mapping[str, str] | None,
    allow_live: bool,
) -> dict[str, list[Mapping[str, object]] | list[dict[str, str]]]:
    if not runnable_tasks:
        return {"runnable_tasks": [], "errors": [], "preflight_groups": []}
    groups: dict[str, dict[str, object]] = {}
    for task in runnable_tasks:
        task_id = str(task["task_id"])
        matter_scope = str(task["matter_scope"])
        try:
            provider_policy_raw = json.loads(str(task["provider_policy_json"] or "{}"))
        except (json.JSONDecodeError, TypeError) as exc:
            error = f"OpenRouter preflight could not parse provider policy: {exc}"
            groups[f"parse-error:{task_id}"] = {
                "tasks": [task],
                "policy": None,
                "fingerprint": f"parse-error:{task_id}",
                "matter_scope": matter_scope,
                "error": error,
            }
            continue
        if not isinstance(provider_policy_raw, Mapping):
            groups[f"parse-error:{task_id}"] = {
                "tasks": [task],
                "policy": None,
                "fingerprint": f"parse-error:{task_id}",
                "matter_scope": matter_scope,
                "error": "OpenRouter preflight provider policy must be a JSON object",
            }
            continue
        provider_policy = {str(key): value for key, value in provider_policy_raw.items()}
        fingerprint = fingerprint_provider_policy(provider_policy)
        group = groups.setdefault(
            fingerprint,
            {
                "tasks": [],
                "policy": provider_policy,
                "fingerprint": fingerprint,
                "matter_scope": matter_scope,
                "error": "",
            },
        )
        cast(list[Mapping[str, object]], group["tasks"]).append(task)

    allowed: list[Mapping[str, object]] = []
    errors: list[dict[str, str]] = []
    preflight_groups: list[dict[str, object]] = []
    effective_env = env if env is not None else os.environ
    for group in groups.values():
        tasks = cast(list[Mapping[str, object]], group["tasks"])
        task_ids = [str(task["task_id"]) for task in tasks]
        matter_scope = str(group["matter_scope"])
        parse_error = str(group.get("error") or "")
        provider_policy = group.get("policy")
        provider = str(cast(Mapping[str, object], provider_policy).get("provider") or "openrouter") if isinstance(provider_policy, Mapping) else "openrouter"
        model = str(cast(Mapping[str, object], provider_policy).get("model") or "") if isinstance(provider_policy, Mapping) else ""
        if parse_error:
            errors.append(_record_openrouter_preflight_group_failure(
                conn,
                matter_scope=matter_scope,
                task_ids=task_ids,
                policy_fingerprint=str(group["fingerprint"]),
                reason=parse_error,
                provider_policy_result="policy_parse_error",
            ))
            preflight_groups.append(
                {
                    "fingerprint": str(group["fingerprint"]),
                    "provider": provider,
                    "model": model,
                    "task_ids": task_ids,
                    "ok": False,
                    "reason": parse_error,
                    "provider_policy_result": "policy_parse_error",
                }
            )
            continue
        probe = probe_live_openrouter(cast(Mapping[str, object], provider_policy), env=effective_env)
        if probe.get("ok") is True:
            _ = repo.resolve_provider_control_plane_attention(
                conn,
                matter_scope=matter_scope,
                provider="openrouter",
                resolution_source="provider.preflight_ok",
            )
            _ = repo.resolve_local_stub_blockers_after_live_approval(conn, matter_scope=matter_scope)
            allowed.extend(tasks)
            preflight_groups.append(
                {
                    "fingerprint": str(group["fingerprint"]),
                    "provider": provider,
                    "model": model,
                    "task_ids": task_ids,
                    "ok": True,
                    "reason": str(probe.get("reason") or ""),
                    "provider_policy_result": str(probe.get("provider_policy_result") or "probe_ok"),
                }
            )
            continue
        reason = str(probe.get("reason") or "OpenRouter preflight failed")
        errors.append(_record_openrouter_preflight_group_failure(
            conn,
            matter_scope=matter_scope,
            task_ids=task_ids,
            policy_fingerprint=str(group["fingerprint"]),
            reason=reason,
            provider_policy_result=str(probe.get("provider_policy_result") or ""),
        ))
        preflight_groups.append(
            {
                "fingerprint": str(group["fingerprint"]),
                "provider": provider,
                "model": model,
                "task_ids": task_ids,
                "ok": False,
                "reason": reason,
                "provider_policy_result": str(probe.get("provider_policy_result") or ""),
            }
        )
    return {"runnable_tasks": allowed, "errors": errors, "preflight_groups": preflight_groups}


def _record_openrouter_preflight_group_failure(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    task_ids: list[str],
    policy_fingerprint: str,
    reason: str,
    provider_policy_result: str,
) -> dict[str, str]:
    first_task_id = task_ids[0] if task_ids else "unknown"
    message = (
        f"OpenRouter preflight failed before leasing {len(task_ids)} runnable task(s) "
        f"for policy {policy_fingerprint}: {reason}"
    )
    _ = repo.record_human_attention_once(
        conn,
        target_type="task",
        target_id=first_task_id,
        severity="blocker",
        reason=message,
        matter_scope=matter_scope,
    )
    _ = repo.emit_event(
        conn,
        "provider.preflight_failed",
        matter_scope=matter_scope,
        payload={
            "task_id": first_task_id,
            "task_ids": task_ids,
            "runnable_task_count": len(task_ids),
            "provider": "openrouter",
            "provider_policy_fingerprint": policy_fingerprint,
            "reason": reason,
            "provider_policy_result": provider_policy_result,
        },
    )
    _ = repo.record_provider_preflight_failure(
        conn,
        matter_scope=matter_scope,
        task_id=first_task_id,
        provider="openrouter",
        message=message,
        runnable_task_count=len(task_ids),
        provider_policy_result=provider_policy_result,
    )
    return {"task_id": first_task_id, "error": message}


def _execute_leased_workers(
    conn: sqlite3.Connection,
    *,
    leased_workers: list[dict[str, str]],
    output_dir: str | Path,
    runtime: str,
    allow_live: bool,
    env: Mapping[str, str] | None,
    codex_timeout_seconds: float,
    codex_reasoning_effort: str,
) -> dict[str, list[dict[str, str]] | list[str]]:
    db_path = _db_path_for_connection(conn)
    if db_path is None or len(leased_workers) <= 1:
        results = [
            _execute_one_leased_worker(
                conn,
                worker=worker,
                output_dir=output_dir,
                runtime=runtime,
                allow_live=allow_live,
                env=env,
                codex_timeout_seconds=codex_timeout_seconds,
                codex_reasoning_effort=codex_reasoning_effort,
            )
            for worker in leased_workers
        ]
    else:
        results = []
        max_workers = min(len(leased_workers), MAX_PARALLEL_AGENT_CAPACITY)
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="atticus-worker") as executor:
            futures = [
                executor.submit(
                    _execute_one_leased_worker_for_db_path,
                    db_path,
                    worker=worker,
                    output_dir=output_dir,
                    runtime=runtime,
                    allow_live=allow_live,
                    env=dict(env) if env is not None else None,
                    codex_timeout_seconds=codex_timeout_seconds,
                    codex_reasoning_effort=codex_reasoning_effort,
                )
                for worker in leased_workers
            ]
            for future in as_completed(futures):
                results.append(future.result())
    ordered = sorted(results, key=lambda result: int(result["index"]))
    return {
        "executed_tasks": [result["task_id"] for result in ordered if result.get("executed") == "true"],
        "worker_errors": [
            {"task_id": result["task_id"], "error": result["error"]}
            for result in ordered
            if result.get("error")
        ],
    }


def _execute_one_leased_worker_for_db_path(
    db_path: str,
    *,
    worker: dict[str, str],
    output_dir: str | Path,
    runtime: str,
    allow_live: bool,
    env: Mapping[str, str] | None,
    codex_timeout_seconds: float,
    codex_reasoning_effort: str,
) -> dict[str, str]:
    # The supervisor connection already applied additive schema before leasing.
    # Re-running schema DDL in every worker keeps SQLite write transactions open
    # across the whole provider call, which serializes the batch and defeats the
    # capacity window.
    with repo.db_connection(db_path, apply_schema=False) as worker_conn:
        return _execute_one_leased_worker(
            worker_conn,
            worker=worker,
            output_dir=output_dir,
            runtime=runtime,
            allow_live=allow_live,
            env=env,
            codex_timeout_seconds=codex_timeout_seconds,
            codex_reasoning_effort=codex_reasoning_effort,
        )


def _execute_one_leased_worker(
    conn: sqlite3.Connection,
    *,
    worker: dict[str, str],
    output_dir: str | Path,
    runtime: str,
    allow_live: bool,
    env: Mapping[str, str] | None,
    codex_timeout_seconds: float,
    codex_reasoning_effort: str,
) -> dict[str, str]:
    task_id = worker["task_id"]
    lease_id = worker["lease_id"]
    worker_id = worker["worker_id"]
    try:
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
            _ = execute_codex_work_order(
                conn,
                task_id=task_id,
                lease_id=lease_id,
                worker_id=worker_id,
                output_dir=output_dir,
                env=env,
                allow_live=allow_live,
                timeout_seconds=codex_timeout_seconds,
                reasoning_effort=codex_reasoning_effort,
            )
        else:
            raise ValueError(f"unsupported free loop runtime: {runtime}")
        return {"index": worker["index"], "task_id": task_id, "executed": "true", "error": ""}
    except Exception as exc:
        _handle_worker_exception(conn, task_id=task_id, lease_id=lease_id, reason=str(exc))
        return {"index": worker["index"], "task_id": task_id, "executed": "false", "error": str(exc)}


def _handle_worker_exception(conn: sqlite3.Connection, *, task_id: str, lease_id: str, reason: str) -> None:
    _fail_active_lease_after_worker_exception(conn, lease_id=lease_id, task_id=task_id, reason=reason)
    if repo.provider_failure_requires_user_intervention(reason):
        matter_scope = repo.matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown"
        provider = "openrouter" if "openrouter" in reason.lower() else "provider"
        _ = repo.record_provider_control_plane_failure(
            conn,
            matter_scope=matter_scope,
            task_id=task_id,
            provider=provider,
            message=reason,
            runnable_task_count=1,
            provider_policy_result="post_dispatch_user_intervention",
            source="provider.post_dispatch",
            error_type="provider_dispatch_requires_user_intervention",
            attention_prefix="provider runtime",
            trigger_reason_prefix="provider runtime",
            event_prefix="orchestrator.provider_runtime",
        )
        return
    decomposition = decompose_broad_task_if_needed(
        conn,
        task_id=task_id,
        reason=reason,
        write=True,
    )
    compact_retry = (
        compact_decomposed_parent_if_needed(conn, task_id=task_id, reason=reason, write=True)
        if not decomposition.get("applied")
        else {"applied": False}
    )
    if "worker output quarantined" not in reason.lower():
        _ = repo.record_human_attention(
            conn,
            target_type="task",
            target_id=task_id,
            severity="blocker",
            reason="free loop worker failed: "
            + reason
            + ("; task decomposed into bounded source bundles" if decomposition.get("applied") else "")
            + ("; decomposed parent compacted for bounded synthesis retry" if compact_retry.get("applied") else ""),
        )
    _report_failure_without_masking(conn, task_id=task_id, reason=f"free loop worker failed: {reason}")


def _db_path_for_connection(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    path = str(row["file"] or "")
    return path or None


def _fail_active_lease_after_worker_exception(conn: sqlite3.Connection, *, lease_id: str, task_id: str, reason: str) -> None:
    """Defensively close capacity if a worker crashes before its runtime cleanup."""

    row = cast(Mapping[str, object] | None, conn.execute("SELECT status FROM leases WHERE lease_id = ? AND task_id = ?", (lease_id, task_id)).fetchone())
    if row is None or row["status"] != "active":
        return
    now = utc_now()
    _ = conn.execute("UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ?", (now, lease_id))
    repo.update_task_blocked(conn, task_id, [reason])
    _ = repo.emit_event(
        conn,
        "lease.failed",
        matter_scope=repo.matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown",
        payload={"lease_id": lease_id, "task_id": task_id, "reason": reason},
    )


def _commit_progress(conn: sqlite3.Connection) -> None:
    """Make long-running supervisor ticks externally observable and durable."""

    conn.commit()


def _report_failure_without_masking(conn: sqlite3.Connection, *, task_id: str, reason: str) -> None:
    if "worker output quarantined" in reason.lower() and _already_reported_output_quarantine(conn, task_id=task_id):
        return
    try:
        _ = report_worker_failure_to_orchestrator(conn, task_id, reason)
    except Exception as exc:
        _ = repo.emit_event(
            conn,
            "orchestrator.failure_signal_failed",
            matter_scope=repo.matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown",
            payload={"task_id": task_id, "reason": reason, "signal_error": str(exc)},
        )


def _already_reported_output_quarantine(conn: sqlite3.Connection, *, task_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM orchestrator_events
        WHERE event_type = 'orchestrator.worker_failed'
          AND json_extract(payload_json, '$.task_id') = ?
          AND json_extract(payload_json, '$.source') = 'worker_result_quarantine'
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return row is not None


def _pending_candidates(conn: sqlite3.Connection) -> list[Mapping[str, object]]:
    return [
        cast(Mapping[str, object], row)
        for row in conn.execute(
            """
            SELECT co.*, t.stage, t.task_type, t.matter_scope FROM candidate_outputs co
            JOIN tasks t ON t.task_id = co.task_id
            WHERE co.status = 'candidate' AND t.status = ?
            ORDER BY co.created_at ASC
            """,
            (TaskStatus.REDUCER_PENDING,),
        )
    ]


def _auto_reduce_skip_reason(candidate: Mapping[str, object]) -> str:
    try:
        stage = str(candidate["stage"] or "")
    except (KeyError, IndexError):
        stage = ""
    high_risk_stages = {
        str(LegalStage.S6_AUTHORITY_LAW_MAP),
        str(LegalStage.S7_HOSTILE_REVIEW),
        str(LegalStage.S8_DRAFT_PREPARATION),
        str(LegalStage.S9_FINAL_QUALITY_GATE),
    }
    if stage in high_risk_stages:
        return f"free loop auto-reduction is disabled for high-risk legal stage {stage}; manual reducer review required"
    return ""


def _record_auto_reduce_skip_attention(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    matter_scope: str,
    reason: str,
) -> None:
    exists = conn.execute(
        """
        SELECT 1 FROM human_attention
        WHERE matter_scope = ? AND target_type = 'candidate' AND target_id = ?
          AND status = 'open' AND reason = ?
        LIMIT 1
        """,
        (matter_scope, candidate_id, reason),
    ).fetchone()
    if exists:
        return
    _ = repo.record_human_attention(
        conn,
        matter_scope=matter_scope,
        target_type="candidate",
        target_id=candidate_id,
        severity="blocker",
        reason=reason,
    )


def _reducer_review_priority(candidate: Mapping[str, object]) -> int:
    try:
        task_type = str(candidate["task_type"] or "")
    except (KeyError, IndexError):
        task_type = ""
    try:
        stage = str(candidate["stage"] or "")
    except (KeyError, IndexError):
        stage = ""
    if task_type in {"citation_repair", "citation_audit", "final_quality_gate"}:
        return 10
    if stage in {str(LegalStage.S9_FINAL_QUALITY_GATE), str(LegalStage.S8_DRAFT_PREPARATION)}:
        return 20
    if stage in {str(LegalStage.S7_HOSTILE_REVIEW), str(LegalStage.S6_AUTHORITY_LAW_MAP)}:
        return 30
    return 50


def _short_id() -> str:
    return uuid4().hex[:8]


def _tick_matter_scope(
    conn: sqlite3.Connection,
    *,
    leased_tasks: list[str],
    executed_tasks: list[str],
    reduction_errors: list[dict[str, str]],
    skipped_reductions: list[dict[str, str]],
    worker_errors: list[dict[str, str]],
) -> str:
    task_ids = set(leased_tasks) | set(executed_tasks)
    task_ids.update(item["task_id"] for item in reduction_errors if item.get("task_id"))
    task_ids.update(item["task_id"] for item in skipped_reductions if item.get("task_id"))
    task_ids.update(item["task_id"] for item in worker_errors if item.get("task_id"))
    scopes = {
        scope
        for task_id in task_ids
        if (scope := repo.matter_scope_for_target(conn, target_type="task", target_id=task_id))
    }
    if not scopes:
        return "atticus"
    if len(scopes) == 1:
        return scopes.pop()
    return "multi"


def _tick_made_progress(tick: Mapping[str, object]) -> bool:
    progress_keys = (
        "reduced_candidates",
        "imported_tasks",
        "leased_tasks",
        "executed_tasks",
        "reducer_review_ids",
        "created_repair_task_ids",
        "unblocked_repair_task_ids",
    )
    for key in progress_keys:
        value = tick.get(key)
        if isinstance(value, (list, tuple, set, dict)) and bool(value):
            return True
    return bool(tick.get("repair_progress"))


def _matter_complete(conn: sqlite3.Connection, matter_scope: str | None) -> bool:
    if not matter_scope:
        return False
    try:
        from atticus.status.completion import build_matter_completion_report

        return build_matter_completion_report(conn, matter_scope).done
    except Exception:
        return False


def _safe_next_action(conn: sqlite3.Connection, matter_scope: str | None) -> dict[str, object]:
    if not matter_scope:
        return {}
    try:
        from atticus.status.completion import next_resume_action

        return next_resume_action(conn, matter_scope)
    except Exception:
        return {}


def _detect_active_run_id(conn: sqlite3.Connection, matter_scope: str | None) -> str | None:
    if not matter_scope:
        return None
    row = conn.execute(
        "SELECT run_id FROM runs WHERE matter_scope=? AND state IN ('running', 'active', 'initialized') ORDER BY created_at DESC LIMIT 1",
        (matter_scope,),
    ).fetchone()
    return str(row["run_id"]) if row else None


def _build_progress_signature(conn: sqlite3.Connection, matter_scope: str, tick: dict[str, object]) -> str:
    parts = []
    try:
        from atticus.status.completion import build_matter_completion_report, next_resume_action

        report = build_matter_completion_report(conn, matter_scope)
        next_action = next_resume_action(conn, matter_scope)
        parts.append(f"done:{report.done}")
        parts.append(f"blocked:{report.blocked}")
        parts.append(f"runnable:{report.runnable_count}")
        parts.append(f"reducer:{report.reducer_pending_count}")
        parts.append(f"missing:{','.join(report.missing_certifications) if report.missing_certifications else 'none'}")
        parts.append(f"next_type:{next_action.get('type', 'unknown')}")
        parts.append(f"next_owner:{next_action.get('owner', 'unknown')}")
    except Exception:
        parts.append("health_error")
    made_progress = _tick_made_progress(tick)
    parts.append(f"progress:{made_progress}")
    return "|".join(parts)
