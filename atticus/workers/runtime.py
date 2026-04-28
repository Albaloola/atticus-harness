"""Safe worker runtimes for Atticus.

The local runtime exercises the harness with the local stub adapter. The live
OpenRouter runtime is separately gated by explicit opt-in, provider probe,
OpenRouter-only policy, budget checks, active leases, and reducer-only candidate
handoff. Neither path starts OpenClaw, shell agents, or external legal actions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
from time import perf_counter
from uuid import uuid4
from typing import Protocol, cast

from atticus.adapters.codex_cli import LIVE_CODEX_ENV, CodexCliAdapter
from atticus.adapters.direct_openrouter import DirectOpenRouterAdapter
from atticus.adapters.local_stub import LocalStubAdapter
from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.providers.budget import BudgetExceeded, charge_budget, require_budget
from atticus.providers.live_readiness import check_live_provider_policy, live_openrouter_enabled, parse_estimated_cost_usd
from atticus.providers.openrouter_failover import openrouter_client_for_policy, openrouter_failover_config_from_policy, openrouter_models_for_policy, primary_model_for_policy, safe_openrouter_error_message
from atticus.providers.openrouter import OpenRouterError, validate_cache_usage_tokens, validate_usage_tokens
from atticus.providers.policy import ProviderActual, ProviderDecision, ProviderRequest, canonical_provider_policy, check_provider_policy
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


class CodexWorkerAdapter(Protocol):
    def run(
        self,
        work_order: dict[str, object],
        *,
        model: str,
        output_dir: Path,
        timeout_seconds: float,
        reasoning_effort: str = "low",
    ) -> object: ...


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
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        reason = f"unknown task: {task_id}"
        _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
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
    if task["cost_limit_usd"] is not None and estimated_cost > float(str(task["cost_limit_usd"])):
        reason = f"task estimated cost {estimated_cost:.4f} exceeds task cost limit {float(str(task['cost_limit_usd'])):.4f}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked("task cost limit would be exceeded")

    try:
        for scope_type, scope_id in (("task", task_id), ("stage", str(task["stage"])), ("matter", str(task["matter_scope"]))):
            _ = require_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated_cost)
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
        _ = output_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        latency_ms = int((perf_counter() - started) * 1000)
        provider_run_id = repo.record_provider_run(
            conn,
            task_id=task_id,
            stage=str(task["stage"]),
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
        for scope_type, scope_id in (("task", task_id), ("stage", str(task["stage"])), ("matter", str(task["matter_scope"]))):
            _ = charge_budget(conn, scope_type=scope_type, scope_id=scope_id, amount_usd=estimated_cost, provider_run_id=provider_run_id)
        candidate_id = record_worker_result(
            conn,
            task_id=task_id,
            lease_id=lease_id,
            worker_id=worker_id,
            payload=payload,
        )
        candidate = cast(Mapping[str, object] | None, conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        if candidate is None or candidate["status"] != "candidate":
            reason = str(candidate["quarantined_reason"] if candidate is not None else "candidate output was not recorded")
            _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
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
        _ = _mark_lease_failed(conn, lease_id=lease_id, reason=str(exc))
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
    client: object | None = None,
    env: Mapping[str, str] | None = None,
    allow_live: bool = False,
) -> WorkerExecutionResult:
    """Execute one leased task through OpenRouter after explicit live gates pass."""

    lease = _require_runtime_lease_for_task(conn, lease_id=lease_id, task_id=task_id)
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        reason = f"unknown task: {task_id}"
        _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
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
    if task["cost_limit_usd"] is not None and estimated_cost > float(str(task["cost_limit_usd"])):
        reason = f"task estimated cost {estimated_cost:.4f} exceeds task cost limit {float(str(task['cost_limit_usd'])):.4f}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked("task cost limit would be exceeded")
    try:
        for scope_type, scope_id in (("task", task_id), ("stage", str(task["stage"])), ("matter", str(task["matter_scope"]))):
            _ = require_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated_cost)
    except BudgetExceeded as exc:
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=str(exc))
        raise
    try:
        max_tokens = _positive_int_provider_setting(provider_policy, "max_tokens", default=16000)
        temperature = _nonnegative_float_provider_setting(provider_policy, "temperature", default=0.1)
        timeout_seconds = _positive_float_provider_setting(provider_policy, "timeout_seconds", default=None)
    except ValueError as exc:
        reason = str(exc)
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason) from exc

    adapter_name = "direct_openrouter"
    attempt_id = _record_attempt_started(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, adapter=adapter_name)
    started = perf_counter()
    task_component = safe_path_component(task_id)
    output_path = Path(output_dir).resolve() / task_component / f"{attempt_id}.json"
    provider_run_id: str | None = None
    try:
        order = build_work_order(conn, task_id=task_id, lease_id=lease_id, persist_context=True)
        failover_config = openrouter_failover_config_from_policy(provider_policy, env=env)
        openrouter_pool_enabled = failover_config is not None
        openrouter_failover_events: list[dict[str, object]] = []
        configured_models = failover_config.models if failover_config is not None else openrouter_models_for_policy(provider_policy, env=env)
        requested = ProviderRequest("openrouter", primary_model_for_policy(provider_policy, env=env), allow_fallback=False)
        provider_client = openrouter_client_for_policy(provider_policy, env=env, client=client, event_sink=openrouter_failover_events.append)
        try:
            response = cast(
                object,
                DirectOpenRouterAdapter(client=provider_client, timeout_seconds=timeout_seconds).run(
                    order.as_dict(),
                    model=requested.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
            )
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
                fallback_allowed=openrouter_pool_enabled,
                raw_usage=_openrouter_runtime_usage(
                    usage={"error": safe_error},
                    adapter_name=adapter_name,
                    output_path=output_path,
                    requested_model=requested.model,
                    configured_models=configured_models,
                    openrouter_failover_events=openrouter_failover_events,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    provider_policy=provider_policy,
                ),
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
                fallback_allowed=openrouter_pool_enabled,
                raw_usage=_openrouter_runtime_usage(
                    usage={},
                    adapter_name=adapter_name,
                    output_path=output_path,
                    requested_model=requested.model,
                    configured_models=configured_models,
                    openrouter_failover_events=openrouter_failover_events,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    provider_policy=provider_policy,
                ),
            )
            raise WorkerExecutionBlocked(reason)
        response = cast(Mapping[str, object], response)
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
                fallback_allowed=openrouter_pool_enabled,
                raw_usage=_openrouter_runtime_usage(
                    usage={"requested_model": final_requested_model},
                    adapter_name=adapter_name,
                    output_path=output_path,
                    requested_model=final_requested_model,
                    configured_models=configured_models,
                    openrouter_failover_events=openrouter_failover_events,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    provider_policy=provider_policy,
                    response=response,
                ),
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
                fallback_allowed=openrouter_pool_enabled,
                raw_usage=_openrouter_runtime_usage(
                    usage={"usage": response.get("usage")},
                    adapter_name=adapter_name,
                    output_path=output_path,
                    requested_model=requested.model,
                    configured_models=configured_models,
                    openrouter_failover_events=openrouter_failover_events,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    provider_policy=provider_policy,
                    response=response,
                ),
            )
            raise WorkerExecutionBlocked(reason)
        usage = dict(cast(Mapping[str, object], usage_raw))
        latency_ms = int((perf_counter() - started) * 1000)
        try:
            token_usage = validate_usage_tokens(usage)
            cache_usage = validate_cache_usage_tokens(usage)
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
                fallback_allowed=openrouter_pool_enabled,
                raw_usage=_openrouter_runtime_usage(
                    usage={"usage": usage},
                    adapter_name=adapter_name,
                    output_path=output_path,
                    requested_model=requested.model,
                    configured_models=configured_models,
                    openrouter_failover_events=openrouter_failover_events,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    provider_policy=provider_policy,
                    response=response,
                ),
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
                fallback_allowed=openrouter_pool_enabled,
                raw_usage=_openrouter_runtime_usage(
                    usage={"usage": usage},
                    adapter_name=adapter_name,
                    output_path=output_path,
                    requested_model=requested.model,
                    configured_models=configured_models,
                    openrouter_failover_events=openrouter_failover_events,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    provider_policy=provider_policy,
                    response=response,
                ),
            )
            raise WorkerExecutionBlocked(reason)
        actual = ProviderActual(str(reported_provider), str(reported_model))
        exact_policy_decision = check_provider_policy(requested, actual=actual)
        if exact_policy_decision.allowed:
            policy_decision = exact_policy_decision
        elif requested.model in configured_models and _openrouter_model_was_honored(requested.model, actual.model):
            # OpenRouter may expose the endpoint provider name (for example
            # DeepSeek or Novita) while still honoring the requested OpenRouter
            # model. It may also report a dated endpoint variant such as
            # ``deepseek/deepseek-v4-pro-20260423``. Allow provenance-only
            # provider names and versioned model suffixes when the requested
            # configured OpenRouter model was still honored.
            policy_decision = ProviderDecision(True, "openrouter_endpoint_provenance", "requested OpenRouter model was honored; endpoint provider recorded as provenance")
        else:
            policy_decision = exact_policy_decision
        provider_run_id = repo.record_provider_run(
            conn,
            task_id=task_id,
            stage=str(task["stage"]),
            requested_provider=requested.provider,
            requested_model=requested.model,
            actual_provider=actual.provider,
            actual_model=actual.model,
            input_tokens=token_usage["prompt_tokens"],
            output_tokens=token_usage["completion_tokens"],
            cache_hit_tokens=cache_usage["cached_tokens"],
            cache_miss_tokens=max(0, token_usage["prompt_tokens"] - cache_usage["cached_tokens"]),
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=None,
            latency_ms=latency_ms,
            fallback_allowed=openrouter_pool_enabled,
            fallback_policy_result=policy_decision.result,
            raw_usage=cast(dict[str, object], _json_safe_payload(
                _openrouter_runtime_usage(
                    usage=usage,
                    adapter_name=adapter_name,
                    output_path=output_path,
                    requested_model=requested.model,
                    configured_models=configured_models,
                    openrouter_failover_events=openrouter_failover_events,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    provider_policy=provider_policy,
                    response=response,
                )
            )),
        )
        _charge_budget_scopes(conn, task=task, task_id=task_id, amount_usd=estimated_cost, provider_run_id=provider_run_id)
        if not policy_decision.allowed:
            reason = policy_decision.reason
            _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            repo.update_task_blocked(conn, task_id, [reason])
            conn.commit()
            raise WorkerExecutionBlocked(reason)

        content = response.get("content")
        if not isinstance(content, Mapping):
            reason = "OpenRouter response content must be a JSON object candidate packet"
            _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            repo.update_task_blocked(conn, task_id, [reason])
            conn.commit()
            raise WorkerExecutionBlocked(reason)
        payload = dict(cast(Mapping[str, object], content))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _ = output_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        candidate_id = record_worker_result(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, payload=payload)
        candidate = cast(Mapping[str, object] | None, conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        if candidate is None or candidate["status"] != "candidate":
            reason = str(candidate["quarantined_reason"] if candidate is not None else "candidate output was not recorded")
            _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            conn.commit()
            raise WorkerExecutionBlocked(f"OpenRouter worker output quarantined: {reason}")
        _record_attempt_finished(conn, attempt_id=attempt_id, status="succeeded", output_path=output_path)
        return WorkerExecutionResult(candidate_id=candidate_id, worker_attempt_id=attempt_id, output_path=output_path, adapter=adapter_name, provider_run_id=provider_run_id)
    except WorkerExecutionBlocked:
        raise
    except Exception as exc:
        _ = _mark_lease_failed(conn, lease_id=lease_id, reason=str(exc))
        _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": str(exc)})
        repo.update_task_status(conn, task_id, TaskStatus.FAILED, str(exc))
        conn.commit()
        raise


def execute_codex_work_order(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str,
    worker_id: str,
    output_dir: str | Path,
    adapter: CodexWorkerAdapter | None = None,
    env: Mapping[str, str] | None = None,
    allow_live: bool = False,
    timeout_seconds: float = 180.0,
    reasoning_effort: str = "low",
) -> WorkerExecutionResult:
    """Execute one leased task through the bounded Codex CLI adapter."""

    lease = _require_runtime_lease_for_task(conn, lease_id=lease_id, task_id=task_id)
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        reason = f"unknown task: {task_id}"
        _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
        conn.commit()
        raise WorkerExecutionBlocked(reason)
    if lease["worker_id"] != worker_id:
        reason = f"lease {lease_id} belongs to worker {lease['worker_id']}, not {worker_id}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if not allow_live or (env or {}).get(LIVE_CODEX_ENV) != "1":
        reason = f"live Codex execution requires allow_live=True and {LIVE_CODEX_ENV}=1"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)

    provider_policy = _load_provider_policy_after_lease(conn, task=task, lease_id=lease_id, task_id=task_id)
    requested = ProviderRequest(
        str(provider_policy.get("provider") or ""),
        str(provider_policy.get("model") or ""),
        allow_fallback=bool(provider_policy.get("allow_fallback") or False),
    )
    decision = check_provider_policy(requested)
    if not decision.allowed:
        reason = decision.reason
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    try:
        estimated_cost = parse_estimated_cost_usd(provider_policy, task_id=task_id, require_present=True)
    except ValueError as exc:
        reason = str(exc)
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason) from exc
    try:
        canonical_policy = canonical_provider_policy(
            provider=requested.provider,
            model=requested.model,
            allow_fallback=requested.allow_fallback,
            estimated_cost_usd=estimated_cost,
        )
    except ValueError as exc:
        reason = str(exc)
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason) from exc
    canonical_provider = str(canonical_policy["provider"])
    canonical_model = str(canonical_policy["model"])
    requested = ProviderRequest(canonical_provider, canonical_model, allow_fallback=False)
    if canonical_provider != "openai-codex":
        reason = f"Codex runtime requires provider openai-codex, got {requested.provider or 'unset'}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if canonical_model != "gpt-5.5":
        reason = f"Codex runtime requires model gpt-5.5, got {canonical_model or 'unset'}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if bool(provider_policy.get("allow_fallback") or False):
        reason = "Codex runtime requires fallback disabled"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if task["cost_limit_usd"] is not None and estimated_cost > float(str(task["cost_limit_usd"])):
        reason = f"task estimated cost {estimated_cost:.4f} exceeds task cost limit {float(str(task['cost_limit_usd'])):.4f}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked("task cost limit would be exceeded")
    try:
        for scope_type, scope_id in (("task", task_id), ("stage", str(task["stage"])), ("matter", str(task["matter_scope"]))):
            _ = require_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated_cost)
    except BudgetExceeded as exc:
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=str(exc))
        raise

    adapter_name = "codex_cli"
    attempt_id = _record_attempt_started(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, adapter=adapter_name)
    started = perf_counter()
    task_component = safe_path_component(task_id)
    output_path = Path(output_dir).resolve() / task_component / f"{attempt_id}.json"
    provider_run_id: str | None = None
    try:
        order = build_work_order(conn, task_id=task_id, lease_id=lease_id, persist_context=True)
        codex_adapter = adapter or CodexCliAdapter()
        codex_output_dir = output_path.parent / "codex-cli"
        try:
            response = codex_adapter.run(
                order.as_dict(),
                model=canonical_model,
                output_dir=codex_output_dir,
                timeout_seconds=timeout_seconds,
                reasoning_effort=reasoning_effort,
            )
        except Exception as exc:
            reason = f"Codex provider call failed after dispatch: {_safe_exception_text(exc)}"
            provider_run_id = _record_codex_post_dispatch_failure(
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
                raw_usage={
                    "error": _safe_exception_text(exc),
                    "timeout_seconds": timeout_seconds,
                    "reasoning_effort": reasoning_effort,
                },
            )
            raise WorkerExecutionBlocked(reason) from exc
        if not isinstance(response, Mapping):
            reason = "Codex response must be a JSON object candidate packet"
            provider_run_id = _record_codex_post_dispatch_failure(
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
                actual_provider="openai-codex",
                actual_model=canonical_model,
            )
            raise WorkerExecutionBlocked(reason)
        payload = dict(cast(Mapping[str, object], response))
        actual = ProviderActual("openai-codex", canonical_model)
        policy_decision = check_provider_policy(requested, actual=actual)
        provider_run_id = repo.record_provider_run(
            conn,
            task_id=task_id,
            stage=str(task["stage"]),
            requested_provider=requested.provider,
            requested_model=requested.model,
            actual_provider=actual.provider,
            actual_model=actual.model,
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=None,
            latency_ms=int((perf_counter() - started) * 1000),
            fallback_allowed=False,
            fallback_policy_result=policy_decision.result,
            raw_usage=cast(dict[str, object], _json_safe_payload(
                {
                    "adapter": adapter_name,
                    "output_path": str(output_path),
                    "codex_output_dir": str(codex_output_dir),
                    "model_profile_id": provider_policy.get("model_profile_id", ""),
                    "model_pool_id": provider_policy.get("model_pool_id", ""),
                    "resolved_model": provider_policy.get("resolved_model", {}),
                    "timeout_seconds": timeout_seconds,
                    "reasoning_effort": reasoning_effort,
                }
            )),
        )
        _charge_budget_scopes(conn, task=task, task_id=task_id, amount_usd=estimated_cost, provider_run_id=provider_run_id)
        if not policy_decision.allowed:
            reason = policy_decision.reason
            _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            repo.update_task_blocked(conn, task_id, [reason])
            conn.commit()
            raise WorkerExecutionBlocked(reason)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _ = output_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        candidate_id = record_worker_result(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, payload=payload)
        candidate = cast(Mapping[str, object] | None, conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        if candidate is None or candidate["status"] != "candidate":
            reason = str(candidate["quarantined_reason"] if candidate is not None else "candidate output was not recorded")
            _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
            conn.commit()
            raise WorkerExecutionBlocked(f"Codex worker output quarantined: {reason}")
        _record_attempt_finished(conn, attempt_id=attempt_id, status="succeeded", output_path=output_path)
        return WorkerExecutionResult(candidate_id=candidate_id, worker_attempt_id=attempt_id, output_path=output_path, adapter=adapter_name, provider_run_id=provider_run_id)
    except WorkerExecutionBlocked:
        raise
    except Exception as exc:
        _ = _mark_lease_failed(conn, lease_id=lease_id, reason=str(exc))
        _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": str(exc)})
        repo.update_task_status(conn, task_id, TaskStatus.FAILED, str(exc))
        conn.commit()
        raise



def _openrouter_model_was_honored(requested_model: str, actual_model: str) -> bool:
    """Return true when OpenRouter reports the requested model or a dated variant.

    OpenRouter may return endpoint-specific model metadata such as
    ``deepseek/deepseek-v4-pro-20260423`` for a request to
    ``deepseek/deepseek-v4-pro``. That is provenance, not fallback, as long as
    the actual model is the requested model or a hyphen-suffixed variant.
    """

    return actual_model == requested_model or actual_model.startswith(f"{requested_model}-")

def _require_runtime_lease_for_task(conn: sqlite3.Connection, *, lease_id: str, task_id: str) -> Mapping[str, object]:
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
    row = cast(Mapping[str, object] | None, conn.execute("SELECT task_id, status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
    if row is None or row["status"] != "active":
        return None
    _ = conn.execute(
        "UPDATE leases SET status = 'failed', updated_at = ? WHERE lease_id = ?",
        (now, lease_id),
    )
    _ = repo.emit_event(conn, "lease.failed", payload={"lease_id": lease_id, "task_id": row["task_id"], "reason": reason})
    return str(row["task_id"])


def _restore_task_after_failed_runtime_lease(conn: sqlite3.Connection, *, task_id: str) -> None:
    pending_candidate = cast(object | None, conn.execute(
        "SELECT 1 FROM candidate_outputs WHERE task_id = ? AND status = 'candidate' LIMIT 1",
        (task_id,),
    ).fetchone())
    next_status = TaskStatus.REDUCER_PENDING if pending_candidate is not None else TaskStatus.QUEUED
    _ = conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ? AND status IN (?, ?)",
        (next_status, utc_now(), task_id, TaskStatus.LEASED, TaskStatus.RUNNING),
    )


def _requires_local_cost_estimate(conn: sqlite3.Connection, *, task: Mapping[str, object]) -> bool:
    """Require explicit local cost estimates when any cost/budget gate exists."""

    if task["cost_limit_usd"] is not None:
        return True
    for scope_type, scope_id in (("task", str(task["task_id"])), ("stage", str(task["stage"])), ("matter", str(task["matter_scope"]))):
        row = cast(object | None, conn.execute(
            "SELECT 1 FROM budgets WHERE scope_type = ? AND scope_id = ? LIMIT 1",
            (scope_type, scope_id),
        ).fetchone())
        if row is not None:
            return True
    return False


def _charge_budget_scopes(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    task_id: str,
    amount_usd: float,
    provider_run_id: str | None,
) -> None:
    """Charge configured task/stage/matter budgets for a completed provider call."""

    for scope_type, scope_id in (("task", task_id), ("stage", str(task["stage"])), ("matter", str(task["matter_scope"]))):
        _ = charge_budget(conn, scope_type=scope_type, scope_id=scope_id, amount_usd=amount_usd, provider_run_id=provider_run_id)


def _record_openrouter_post_dispatch_failure(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    task_id: str,
    lease_id: str,
    attempt_id: str,
    output_path: Path,
    requested: ProviderRequest,
    estimated_cost: float,
    started: float,
    adapter_name: str,
    reason: str,
    response: Mapping[str, object] | None = None,
    actual_provider: str | None = None,
    actual_model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    fallback_allowed: bool = False,
    fallback_policy_result: str = "failed_closed",
    raw_usage: dict[str, object] | None = None,
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
        stage=str(task["stage"]),
        requested_provider=requested.provider,
        requested_model=requested.model,
        actual_provider=actual_provider or "missing",
        actual_model=actual_model or "missing",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
        actual_cost_usd=None,
        latency_ms=int((perf_counter() - started) * 1000),
        fallback_allowed=fallback_allowed,
        fallback_policy_result=fallback_policy_result,
        raw_usage=cast(dict[str, object], _json_safe_payload(usage_payload)),
    )
    _charge_budget_scopes(conn, task=task, task_id=task_id, amount_usd=estimated_cost, provider_run_id=provider_run_id)
    _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
    _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
    repo.update_task_blocked(conn, task_id, [reason])
    conn.commit()
    return provider_run_id


def _record_codex_post_dispatch_failure(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    task_id: str,
    lease_id: str,
    attempt_id: str,
    output_path: Path,
    requested: ProviderRequest,
    estimated_cost: float,
    started: float,
    adapter_name: str,
    reason: str,
    actual_provider: str = "missing",
    actual_model: str = "missing",
    raw_usage: dict[str, object] | None = None,
) -> str:
    """Record Codex telemetry after dispatch, then fail capacity and block."""

    usage_payload = dict(raw_usage or {})
    usage_payload.update({"adapter": adapter_name, "output_path": str(output_path), "error": reason})
    provider_run_id = repo.record_provider_run(
        conn,
        task_id=task_id,
        stage=str(task["stage"]),
        requested_provider=requested.provider,
        requested_model=requested.model,
        actual_provider=actual_provider,
        actual_model=actual_model,
        estimated_cost_usd=estimated_cost,
        actual_cost_usd=None,
        latency_ms=int((perf_counter() - started) * 1000),
        fallback_allowed=False,
        fallback_policy_result="provider_error",
        raw_usage=cast(dict[str, object], _json_safe_payload(usage_payload)),
    )
    _charge_budget_scopes(conn, task=task, task_id=task_id, amount_usd=estimated_cost, provider_run_id=provider_run_id)
    _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
    _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": reason})
    repo.update_task_blocked(conn, task_id, [reason])
    conn.commit()
    return provider_run_id


def _openrouter_runtime_usage(
    *,
    usage: Mapping[str, object],
    adapter_name: str,
    output_path: Path,
    requested_model: str,
    configured_models: tuple[str, ...],
    openrouter_failover_events: list[dict[str, object]],
    max_tokens: int,
    temperature: float,
    timeout_seconds: float | None,
    provider_policy: Mapping[str, object],
    response: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build auditable OpenRouter runtime telemetry without hiding routing state."""

    response = response or {}
    return {
        "usage": dict(usage),
        "adapter": adapter_name,
        "output_path": str(output_path),
        "requested_model": requested_model,
        "configured_models": list(configured_models),
        "openrouter_failover_events": list(openrouter_failover_events),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        "model_profile_id": provider_policy.get("model_profile_id", ""),
        "model_pool_id": provider_policy.get("model_pool_id", ""),
        "resolved_model": provider_policy.get("resolved_model", {}),
        "raw": response.get("raw", {}),
    }


def _json_safe_payload(value: object) -> object:
    """Return a SQLite JSON-valid telemetry payload, preserving bad values as text."""

    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _json_safe_payload(item) for key, item in mapping.items()}
    if isinstance(value, list):
        return [_json_safe_payload(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return [_json_safe_payload(item) for item in cast(tuple[object, ...], value)]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _actual_metadata_or_missing(response: Mapping[str, object], key: str) -> str:
    value = response.get(key)
    return str(value) if value else "missing"


def _safe_exception_text(exc: BaseException, *, limit: int = 400) -> str:
    text = " ".join(str(exc).split())
    return text[:limit] if text else exc.__class__.__name__


def _positive_int_provider_setting(provider_policy: Mapping[str, object], key: str, *, default: int) -> int:
    raw = provider_policy.get(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError(f"provider policy {key} must be a positive integer")
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"provider policy {key} must be a positive integer") from exc
    if value < 1 or str(raw).strip() not in {str(value), f"+{value}"}:
        raise ValueError(f"provider policy {key} must be a positive integer")
    return value


def _nonnegative_float_provider_setting(provider_policy: Mapping[str, object], key: str, *, default: float) -> float:
    raw = provider_policy.get(key)
    if raw is None or raw == "":
        return default
    return _float_provider_setting(raw, key=key, minimum=0.0, allow_zero=True)


def _positive_float_provider_setting(provider_policy: Mapping[str, object], key: str, *, default: float | None) -> float | None:
    raw = provider_policy.get(key)
    if raw is None or raw == "":
        return default
    return _float_provider_setting(raw, key=key, minimum=0.0, allow_zero=False)


def _float_provider_setting(raw: object, *, key: str, minimum: float, allow_zero: bool) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"provider policy {key} must be a {'non-negative' if allow_zero else 'positive'} number")
    try:
        value = float(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"provider policy {key} must be a {'non-negative' if allow_zero else 'positive'} number") from exc
    if not math.isfinite(value) or value < minimum or (value == 0 and not allow_zero):
        raise ValueError(f"provider policy {key} must be a {'non-negative' if allow_zero else 'positive'} number")
    return value


def _load_provider_policy_after_lease(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    lease_id: str,
    task_id: str,
) -> dict[str, object]:
    """Parse provider policy after leasing without leaking capacity on corrupt state."""

    try:
        provider_policy = _load_json_value(str(task["provider_policy_json"] or "{}"))
    except (json.JSONDecodeError, TypeError) as exc:
        reason = f"malformed provider policy for task {task_id}: {exc}"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason) from exc
    if not isinstance(provider_policy, Mapping):
        reason = f"malformed provider policy for task {task_id}: policy must be a JSON object"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    return {str(key): value for key, value in cast(Mapping[object, object], provider_policy).items()}


def _load_json_value(text: str) -> object:
    return json.loads(text)


def _block_preflight_after_lease(conn: sqlite3.Connection, *, lease_id: str, task_id: str, reason: str) -> None:
    """Fail an active lease and block the task when a preflight gate fails post-lease."""

    _ = _mark_lease_failed(conn, lease_id=lease_id, reason=reason)
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
    _ = conn.execute(
        """
        INSERT INTO worker_attempts(worker_attempt_id, task_id, lease_id, worker_id, adapter, status, started_at)
        VALUES (?, ?, ?, ?, ?, 'running', ?)
        """,
        (attempt_id, task_id, lease_id, worker_id, adapter, now),
    )
    _ = conn.execute("UPDATE tasks SET status = 'running', updated_at = ? WHERE task_id = ?", (now, task_id))
    _ = repo.emit_event(conn, "worker_attempt.started", payload={"worker_attempt_id": attempt_id, "task_id": task_id, "adapter": adapter})
    return attempt_id


def _record_attempt_finished(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    status: str,
    output_path: Path,
    error: dict[str, object] | None = None,
) -> None:
    _ = conn.execute(
        """
        UPDATE worker_attempts
        SET status = ?, finished_at = ?, output_path = ?, error_json = ?
        WHERE worker_attempt_id = ?
        """,
        (status, utc_now(), str(output_path), json.dumps(error or {}, sort_keys=True), attempt_id),
    )
    _ = repo.emit_event(conn, "worker_attempt.finished", payload={"worker_attempt_id": attempt_id, "status": status})
