"""Safe worker runtimes for Atticus.

The local runtime exercises the harness with the local stub adapter. The live
OpenRouter runtime is separately gated by explicit opt-in, provider probe,
OpenRouter-only policy, budget checks, active leases, and reducer-only candidate
handoff. Neither path starts OpenClaw, shell agents, or external legal actions.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
from time import perf_counter
from uuid import uuid4
from typing import Any, Mapping

from atticus.adapters.direct_openrouter import DirectOpenRouterAdapter
from atticus.adapters.local_stub import LocalStubAdapter
from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.providers.budget import BudgetExceeded, charge_budget, require_budget
from atticus.providers.live_readiness import check_live_provider_policy, live_openrouter_enabled, parse_estimated_cost_usd
from atticus.providers.openrouter_failover import openrouter_client_for_policy, openrouter_models_for_policy, primary_model_for_policy, safe_openrouter_error_message
from atticus.providers.openrouter import OpenRouterError, validate_usage_tokens
from atticus.providers.policy import ProviderActual, ProviderRequest, check_provider_policy
from atticus.scheduler.lease import LeaseError, require_active_lease
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

    lease = _require_runtime_lease_for_task(conn, lease_id=lease_id, task_id=task_id)
    task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        reason = f"unknown task: {task_id}"
        _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
        conn.commit()
        raise WorkerExecutionBlocked(reason)
    if lease["worker_id"] != worker_id:
        reason = f"lease {lease_id} belongs to worker {lease['worker_id']}, not {worker_id}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if adapter_name != "local_stub":
        reason = f"adapter {adapter_name!r} is not enabled for safe local execution"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)

    provider_policy = _load_provider_policy_after_lease(conn, task=task, lease_id=lease_id, task_id=task_id)
    require_cost_estimate = _requires_local_cost_estimate(conn, task=task)
    try:
        estimated_cost = parse_estimated_cost_usd(provider_policy, task_id=task_id, require_present=require_cost_estimate)
    except ValueError as exc:
        reason = str(exc)
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason) from exc
    if task["cost_limit_usd"] is not None and estimated_cost > float(task["cost_limit_usd"]):
        reason = f"task estimated cost {estimated_cost:.4f} exceeds task cost limit {float(task['cost_limit_usd']):.4f}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked("task cost limit would be exceeded")

    try:
        for scope_type, scope_id in (("task", task_id), ("stage", task["stage"]), ("matter", task["matter_scope"])):
            require_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated_cost)
    except BudgetExceeded as exc:
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=str(exc))
        raise

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


def execute_openrouter_work_order(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str,
    worker_id: str,
    output_dir: str | Path,
    client: Any | None = None,
    env: Mapping[str, str] | None = None,
    allow_live: bool = False,
) -> WorkerExecutionResult:
    """Execute one leased task through OpenRouter after explicit live gates pass."""

    lease = _require_runtime_lease_for_task(conn, lease_id=lease_id, task_id=task_id)
    task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        reason = f"unknown task: {task_id}"
        _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
        conn.commit()
        raise WorkerExecutionBlocked(reason)
    if lease["worker_id"] != worker_id:
        reason = f"lease {lease_id} belongs to worker {lease['worker_id']}, not {worker_id}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if not allow_live or not live_openrouter_enabled(env=env):
        reason = "live OpenRouter execution requires allow_live=True and ATTICUS_ENABLE_LIVE_OPENROUTER=1"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)

    provider_policy = _load_provider_policy_after_lease(conn, task=task, lease_id=lease_id, task_id=task_id)
    live_policy = check_live_provider_policy(provider_policy, env=env)
    if not live_policy.allowed:
        reason = "; ".join(live_policy.reasons)
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)

    try:
        estimated_cost = parse_estimated_cost_usd(provider_policy, task_id=task_id, require_present=True)
    except ValueError as exc:
        reason = str(exc)
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason) from exc
    if task["cost_limit_usd"] is not None and estimated_cost > float(task["cost_limit_usd"]):
        reason = f"task estimated cost {estimated_cost:.4f} exceeds task cost limit {float(task['cost_limit_usd']):.4f}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked("task cost limit would be exceeded")
    try:
        for scope_type, scope_id in (("task", task_id), ("stage", task["stage"]), ("matter", task["matter_scope"])):
            require_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated_cost)
    except BudgetExceeded as exc:
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=str(exc))
        raise

    adapter_name = "direct_openrouter"
    attempt_id = _record_attempt_started(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, adapter=adapter_name)
    started = perf_counter()
    task_component = safe_path_component(task_id)
    output_path = Path(output_dir).resolve() / task_component / f"{attempt_id}.json"
    provider_run_id: str | None = None
    try:
        order = build_work_order(conn, task_id=task_id, lease_id=lease_id, persist_context=True)
        configured_models = openrouter_models_for_policy(provider_policy, env=env)
        requested = ProviderRequest("openrouter", primary_model_for_policy(provider_policy, env=env), allow_fallback=False)
        provider_client = openrouter_client_for_policy(provider_policy, env=env, client=client)
        try:
            response = DirectOpenRouterAdapter(client=provider_client).run(order.as_dict(), model=requested.model)
        except OpenRouterError as exc:
            safe_error = safe_openrouter_error_message(exc)
            reason = f"OpenRouter provider call failed after dispatch: {safe_error}"
            provider_run_id = _record_openrouter_post_dispatch_failure(
                conn,
                task=task,
                task_id=task_id,
                lease_id=lease_id,
                attempt_id=attempt_id,
                output_path=output_path,
                requested=requested,
                estimated_cost=estimated_cost,
                started=started,
                adapter_name=adapter_name,
                reason=reason,
                fallback_policy_result="provider_error",
                raw_usage={"error": safe_error},
            )
            raise WorkerExecutionBlocked(reason) from exc
        if not isinstance(response, Mapping):
            reason = "OpenRouter response must be a JSON object"
            provider_run_id = _record_openrouter_post_dispatch_failure(
                conn,
                task=task,
                task_id=task_id,
                lease_id=lease_id,
                attempt_id=attempt_id,
                output_path=output_path,
                requested=requested,
                estimated_cost=estimated_cost,
                started=started,
                adapter_name=adapter_name,
                reason=reason,
            )
            raise WorkerExecutionBlocked(reason)
        final_requested_model = str(response.get("requested_model") or requested.model)
        requested = ProviderRequest("openrouter", final_requested_model, allow_fallback=False)
        if final_requested_model not in configured_models:
            reason = f"OpenRouter response requested model {final_requested_model} is not in configured model list"
            provider_run_id = _record_openrouter_post_dispatch_failure(
                conn,
                task=task,
                task_id=task_id,
                lease_id=lease_id,
                attempt_id=attempt_id,
                output_path=output_path,
                requested=requested,
                estimated_cost=estimated_cost,
                started=started,
                adapter_name=adapter_name,
                reason=reason,
                response=response,
                actual_provider=_actual_metadata_or_missing(response, "provider"),
                actual_model=_actual_metadata_or_missing(response, "model"),
                raw_usage={"requested_model": final_requested_model, "configured_models": list(configured_models)},
            )
            raise WorkerExecutionBlocked(reason)
        usage_raw = response.get("usage")
        if not isinstance(usage_raw, Mapping):
            reason = "OpenRouter response usage metadata must be a JSON object"
            provider_run_id = _record_openrouter_post_dispatch_failure(
                conn,
                task=task,
                task_id=task_id,
                lease_id=lease_id,
                attempt_id=attempt_id,
                output_path=output_path,
                requested=requested,
                estimated_cost=estimated_cost,
                started=started,
                adapter_name=adapter_name,
                reason=reason,
                response=response,
                actual_provider=_actual_metadata_or_missing(response, "provider"),
                actual_model=_actual_metadata_or_missing(response, "model"),
                raw_usage={"usage": response.get("usage")},
            )
            raise WorkerExecutionBlocked(reason)
        usage = dict(usage_raw)
        latency_ms = int((perf_counter() - started) * 1000)
        try:
            token_usage = validate_usage_tokens(usage)
        except OpenRouterError as exc:
            reason = f"OpenRouter response usage metadata is invalid: {exc}"
            provider_run_id = _record_openrouter_post_dispatch_failure(
                conn,
                task=task,
                task_id=task_id,
                lease_id=lease_id,
                attempt_id=attempt_id,
                output_path=output_path,
                requested=requested,
                estimated_cost=estimated_cost,
                started=started,
                adapter_name=adapter_name,
                reason=reason,
                response=response,
                actual_provider=_actual_metadata_or_missing(response, "provider"),
                actual_model=_actual_metadata_or_missing(response, "model"),
                raw_usage={"usage": usage},
            )
            raise WorkerExecutionBlocked(reason) from exc
        reported_provider = response.get("provider")
        reported_model = response.get("model")
        if not reported_provider or not reported_model:
            reason = "OpenRouter response missing provider/model metadata required for fallback detection"
            provider_run_id = _record_openrouter_post_dispatch_failure(
                conn,
                task=task,
                task_id=task_id,
                lease_id=lease_id,
                attempt_id=attempt_id,
                output_path=output_path,
                requested=requested,
                estimated_cost=estimated_cost,
                started=started,
                adapter_name=adapter_name,
                reason=reason,
                response=response,
                actual_provider=str(reported_provider or "missing"),
                actual_model=str(reported_model or "missing"),
                input_tokens=token_usage["prompt_tokens"],
                output_tokens=token_usage["completion_tokens"],
                raw_usage={"usage": usage},
            )
            raise WorkerExecutionBlocked(reason)
        actual = ProviderActual(str(reported_provider), str(reported_model))
        policy_decision = check_provider_policy(requested, actual=actual)
        provider_run_id = repo.record_provider_run(
            conn,
            task_id=task_id,
            stage=task["stage"],
            requested_provider=requested.provider,
            requested_model=requested.model,
            actual_provider=actual.provider,
            actual_model=actual.model,
            input_tokens=token_usage["prompt_tokens"],
            output_tokens=token_usage["completion_tokens"],
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=None,
            latency_ms=latency_ms,
            fallback_allowed=False,
            fallback_policy_result=policy_decision.result,
            raw_usage=_json_safe_payload(
                {
                    "usage": usage,
                    "adapter": adapter_name,
                    "output_path": str(output_path),
                    "requested_model": requested.model,
                    "raw": response.get("raw", {}),
                }
            ),
        )
        _charge_budget_scopes(conn, task=task, task_id=task_id, amount_usd=estimated_cost, provider_run_id=provider_run_id)
        if not policy_decision.allowed:
            reason = policy_decision.reason
            _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            repo.update_task_blocked(conn, task_id, [reason])
            conn.commit()
            raise WorkerExecutionBlocked(reason)

        content = response.get("content")
        if not isinstance(content, Mapping):
            reason = "OpenRouter response content must be a JSON object candidate packet"
            _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            repo.update_task_blocked(conn, task_id, [reason])
            conn.commit()
            raise WorkerExecutionBlocked(reason)
        payload = dict(content)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        candidate_id = record_worker_result(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, payload=payload)
        candidate = conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if candidate is None or candidate["status"] != "candidate":
            reason = candidate["quarantined_reason"] if candidate is not None else "candidate output was not recorded"
            _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            conn.commit()
            raise WorkerExecutionBlocked(f"OpenRouter worker output quarantined: {reason}")
        _record_attempt_finished(conn, attempt_id=attempt_id, status="succeeded", output_path=output_path)
        return WorkerExecutionResult(candidate_id=candidate_id, worker_attempt_id=attempt_id, output_path=output_path, adapter=adapter_name, provider_run_id=provider_run_id)
    except WorkerExecutionBlocked:
        raise
    except Exception as exc:
        _mark_lease_failed(conn, lease_id=lease_id, reason=str(exc))
        _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": str(exc)})
        repo.update_task_status(conn, task_id, TaskStatus.FAILED, str(exc))
        conn.commit()
        raise


def _require_runtime_lease_for_task(conn: sqlite3.Connection, *, lease_id: str, task_id: str) -> sqlite3.Row:
    try:
        lease = require_active_lease(conn, lease_id=lease_id)
    except LeaseError as exc:
        raise WorkerExecutionBlocked(str(exc)) from exc
    if lease["task_id"] != task_id:
        reason = f"lease {lease_id} belongs to task {lease['task_id']}, not {task_id}"
        failed_task_id = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
        if failed_task_id is not None:
            _restore_task_after_failed_runtime_lease(conn, task_id=failed_task_id)
            conn.commit()
        raise WorkerExecutionBlocked(reason)
    return lease


def _mark_lease_failed(conn: sqlite3.Connection, *, lease_id: str, reason: str) -> str | None:
    """Mark a lease as failed so capacity accounting cannot leak active leases."""

    now = utc_now()
    row = conn.execute("SELECT task_id, status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
    if row is None or row["status"] != "active":
        return None
    conn.execute(
        "UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ?",
        (now, lease_id),
    )
    repo.emit_event(conn, "lease.failed", payload={"lease_id": lease_id, "task_id": row["task_id"], "reason": reason})
    return str(row["task_id"])


def _restore_task_after_failed_runtime_lease(conn: sqlite3.Connection, *, task_id: str) -> None:
    pending_candidate = conn.execute(
        "SELECT 1 FROM candidate_outputs WHERE task_id = ? AND status = 'candidate' LIMIT 1",
        (task_id,),
    ).fetchone()
    next_status = TaskStatus.REDUCER_PENDING if pending_candidate is not None else TaskStatus.QUEUED
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status IN (?, ?)",
        (next_status, utc_now(), task_id, TaskStatus.LEASED, TaskStatus.RUNNING),
    )


def _requires_local_cost_estimate(conn: sqlite3.Connection, *, task: sqlite3.Row) -> bool:
    """Require explicit local cost estimates when any cost/budget gate exists."""

    if task["cost_limit_usd"] is not None:
        return True
    for scope_type, scope_id in (("task", task["task_id"]), ("stage", task["stage"]), ("matter", task["matter_scope"])):
        row = conn.execute(
            "SELECT 1 FROM budgets WHERE scope_type = ? AND scope_id = ? LIMIT 1",
            (scope_type, scope_id),
        ).fetchone()
        if row is not None:
            return True
    return False


def _charge_budget_scopes(
    conn: sqlite3.Connection,
    *,
    task: sqlite3.Row,
    task_id: str,
    amount_usd: float,
    provider_run_id: str | None,
) -> None:
    """Charge configured task/stage/matter budgets for a completed provider call."""

    for scope_type, scope_id in (("task", task_id), ("stage", task["stage"]), ("matter", task["matter_scope"])):
        charge_budget(conn, scope_type=scope_type, scope_id=scope_id, amount_usd=amount_usd, provider_run_id=provider_run_id)


def _record_openrouter_post_dispatch_failure(
    conn: sqlite3.Connection,
    *,
    task: sqlite3.Row,
    task_id: str,
    lease_id: str,
    attempt_id: str,
    output_path: Path,
    requested: ProviderRequest,
    estimated_cost: float,
    started: float,
    adapter_name: str,
    reason: str,
    response: Mapping[str, Any] | None = None,
    actual_provider: str | None = None,
    actual_model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    fallback_policy_result: str = "failed_closed",
    raw_usage: dict[str, Any] | None = None,
) -> str:
    """Record spend telemetry, fail capacity, block task, and commit after dispatch."""

    response = response or {}
    usage_payload = dict(raw_usage or {})
    usage_payload.update(
        {
            "adapter": adapter_name,
            "output_path": str(output_path),
            "raw": response.get("raw", {}),
            "error": reason,
        }
    )
    provider_run_id = repo.record_provider_run(
        conn,
        task_id=task_id,
        stage=task["stage"],
        requested_provider=requested.provider,
        requested_model=requested.model,
        actual_provider=actual_provider or "missing",
        actual_model=actual_model or "missing",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
        actual_cost_usd=None,
        latency_ms=int((perf_counter() - started) * 1000),
        fallback_allowed=False,
        fallback_policy_result=fallback_policy_result,
        raw_usage=_json_safe_payload(usage_payload),
    )
    _charge_budget_scopes(conn, task=task, task_id=task_id, amount_usd=estimated_cost, provider_run_id=provider_run_id)
    _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
    _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
    repo.update_task_blocked(conn, task_id, [reason])
    conn.commit()
    return provider_run_id


def _json_safe_payload(value: Any) -> Any:
    """Return a SQLite JSON-valid telemetry payload, preserving bad values as text."""

    if isinstance(value, Mapping):
        return {str(key): _json_safe_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_payload(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _actual_metadata_or_missing(response: Mapping[str, Any], key: str) -> str:
    value = response.get(key)
    return str(value) if value else "missing"


def _load_provider_policy_after_lease(
    conn: sqlite3.Connection,
    *,
    task: sqlite3.Row,
    lease_id: str,
    task_id: str,
) -> dict[str, Any]:
    """Parse provider policy after leasing without leaking capacity on corrupt state."""

    try:
        provider_policy = json.loads(task["provider_policy_json"] or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        reason = f"malformed provider policy for task {task_id}: {exc}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason) from exc
    if not isinstance(provider_policy, dict):
        reason = f"malformed provider policy for task {task_id}: policy must be a JSON object"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    return provider_policy


def _block_preflight_after_lease(conn: sqlite3.Connection, *, lease_id: str, task_id: str, reason: str) -> None:
    """Fail an active lease and block the task when a preflight gate fails post-lease."""

    _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
    repo.update_task_blocked(conn, task_id, [reason])
    conn.commit()


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
