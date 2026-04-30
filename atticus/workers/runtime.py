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
import signal
import sqlite3
import threading
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
from atticus.providers.cache_observability import fingerprint_provider_policy
from atticus.providers.live_readiness import check_live_provider_policy, live_openrouter_enabled, parse_estimated_cost_usd
from atticus.providers.openrouter_failover import openrouter_client_for_policy, openrouter_failover_config_from_policy, openrouter_models_for_policy, primary_model_for_policy, safe_openrouter_error_message
from atticus.providers.openrouter import OpenRouterError, validate_cache_usage_tokens, validate_usage_tokens
from atticus.providers.policy import ProviderActual, ProviderDecision, ProviderRequest, canonical_provider_policy, check_provider_policy
from atticus.scheduler.gates import LIVE_CODEX_NOT_ENABLED_BLOCKER, LIVE_OPENROUTER_NOT_ENABLED_BLOCKER
from atticus.scheduler.lease import LeaseError, require_active_lease
from atticus.workers.contracts import safe_path_component
from atticus.workers.outputs import record_worker_result
from atticus.workers.work_order import build_work_order


class WorkerExecutionBlocked(RuntimeError):
    """Raised when a requested worker execution is outside the safe runtime."""


class ProviderCallTimeout(TimeoutError):
    """Raised when a live provider call exceeds the harness wall-clock deadline."""


OPENROUTER_DEFAULT_TIMEOUT_SECONDS = 180.0
PRO_REQUIRED_RUNTIME_STAGES = {"S5", "S6", "S7", "S8", "S9"}
PRO_REQUIRED_RUNTIME_TASK_TYPES = {
    "matter_orchestration",
    "case_strategy_planning",
    "contradiction_analysis",
    "authority_map",
    "authority_audit",
    "hostile_opponent_review",
    "hostile_review",
    "final_quality_gate",
    "draft_preparation",
    "reducer_decision_support",
    "high_risk_procedural_analysis",
}
MODEL_DOWNGRADE_CERTIFICATION = "model_downgrade_authorized"
LOCAL_STUB_UNSUPPORTED_STAGES = {"S5", "S6", "S7", "S8", "S9"}
LOCAL_STUB_UNSUPPORTED_TASK_TYPES = {
    "authority_audit",
    "authority_map",
    "certification_decision_packet",
    "citation_audit",
    "citation_repair",
    "contradiction_analysis",
    "draft",
    "draft_preparation",
    "evidence_issue_map",
    "evidence_triage",
    "final_quality_gate",
    "high_risk_procedural_analysis",
    "hostile_opponent_review",
    "hostile_review",
    "privacy_redaction_audit",
    "redaction_fix",
    "redaction_review",
}


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
    _enforce_model_decision_runtime_gate(conn, task=task, task_id=task_id, lease_id=lease_id, provider_policy=provider_policy)
    _block_unsafe_local_stub_provider_policy(conn, lease_id=lease_id, task_id=task_id, provider_policy=provider_policy)
    _block_local_stub_unsupported_legal_work(conn, lease_id=lease_id, task=task)
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
            context_pack_id=order.context_pack_id,
            context_fingerprint=str(order.context_pack.get("fingerprint") or ""),
            provider_policy_fingerprint=fingerprint_provider_policy(provider_policy),
            raw_usage={"adapter": adapter_name, "output_path": str(output_path), "provider_policy": dict(provider_policy)},
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


def _block_unsafe_local_stub_provider_policy(
    conn: sqlite3.Connection,
    *,
    lease_id: str,
    task_id: str,
    provider_policy: Mapping[str, object],
) -> None:
    provider = str(provider_policy.get("provider") or "").strip()
    model = str(provider_policy.get("model") or "").strip()
    if bool(provider_policy.get("blocked")) or provider == "blocked" or model == "blocked":
        reason = str(provider_policy.get("model_decision_reason") or provider_policy.get("reason") or "provider policy is blocked")
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if bool(provider_policy.get("reserved")):
        reason = "reserved provider policy cannot execute through local_stub"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if provider in {"anthropic", "anthropic-oauth"}:
        reason = "Anthropic provider policies are reserved and disabled by default"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    if provider == "local":
        if model not in {"", "stub", "local_stub"}:
            reason = f"local_stub cannot execute local model {model!r}"
            _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
            raise WorkerExecutionBlocked(reason)
        return
    if not provider and not model:
        return
    if not provider or not model:
        reason = "provider policy must include both provider and model before local_stub execution"
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)
    decision = check_provider_policy(
        ProviderRequest(
            provider,
            model,
            allow_fallback=bool(provider_policy.get("allow_fallback") or False),
        )
    )
    if not decision.allowed:
        reason = decision.reason
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)


def _block_local_stub_unsupported_legal_work(
    conn: sqlite3.Connection,
    *,
    lease_id: str,
    task: Mapping[str, object],
) -> None:
    """Prevent local_stub from fabricating reducer-grade legal work.

    The local adapter exists to exercise the harness plumbing for low-risk
    source/inventory tasks. It cannot perform quote-supported legal synthesis,
    citation audit, repair, or final-gate review. Blocking here prevents the
    exact failure mode seen in live Napier runs: empty ``local_stub_result``
    packets repeatedly reaching citation-support/reducer gates.
    """

    task_id = str(task["task_id"])
    task_type = str(task["task_type"])
    stage = str(task["stage"])
    validation_gates = _json_list(str(task["validation_gates_json"] if "validation_gates_json" in task else "[]"))
    needs_citation_support = any(str(gate) == "citation_support_integrity" for gate in validation_gates)
    if task_type not in LOCAL_STUB_UNSUPPORTED_TASK_TYPES and stage not in LOCAL_STUB_UNSUPPORTED_STAGES and not needs_citation_support:
        return

    reason = (
        "local_stub capability block: local/no-live runtime cannot produce "
        "reducer-grade citation-supported legal output for "
        f"task_type={task_type!r}, stage={stage!r}. Use a provider-backed worker "
        "with explicit approval, a deterministic source-led generator, or import "
        "a quote-supported worker_result_packet.v2 candidate."
    )
    _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
    raise WorkerExecutionBlocked(reason)


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
        reason = LIVE_OPENROUTER_NOT_ENABLED_BLOCKER
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)

    provider_policy = _load_provider_policy_after_lease(conn, task=task, lease_id=lease_id, task_id=task_id)
    _enforce_model_decision_runtime_gate(conn, task=task, task_id=task_id, lease_id=lease_id, provider_policy=provider_policy)
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
        max_tokens = _positive_int_provider_setting(provider_policy, "max_tokens", default=_default_openrouter_max_tokens(task))
        temperature = _nonnegative_float_provider_setting(provider_policy, "temperature", default=0.1)
        timeout_seconds = _positive_float_provider_setting(provider_policy, "timeout_seconds", default=OPENROUTER_DEFAULT_TIMEOUT_SECONDS)
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
        failover_config = openrouter_failover_config_from_policy(provider_policy, env=env, live=True)
        openrouter_pool_enabled = failover_config is not None
        openrouter_failover_events: list[dict[str, object]] = []
        configured_models = failover_config.models if failover_config is not None else openrouter_models_for_policy(provider_policy, env=env, live=True)
        requested = ProviderRequest("openrouter", primary_model_for_policy(provider_policy, env=env, live=True), allow_fallback=False)
        provider_client = openrouter_client_for_policy(provider_policy, env=env, live=True, client=client, event_sink=openrouter_failover_events.append)
        # Persist the lease, worker_attempt.started event, and context pack before
        # entering provider I/O. Otherwise a long provider wait looks idle to
        # operators and a process crash can erase the in-flight audit trail.
        conn.commit()
        try:
            response = cast(
                object,
                _run_openrouter_adapter_with_deadline(
                    DirectOpenRouterAdapter(client=provider_client, timeout_seconds=timeout_seconds),
                    timeout_seconds=timeout_seconds,
                    work_order=order.as_dict(),
                    model=requested.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
            )
        except ProviderCallTimeout as exc:
            reason = str(exc)
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
                fallback_policy_result="provider_timeout",
                fallback_allowed=openrouter_pool_enabled,
                raw_usage=_openrouter_runtime_usage(
                    usage={"error": "provider_timeout", "timeout_seconds": timeout_seconds},
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
        except OpenRouterError as exc:
            safe_error = safe_openrouter_error_message(exc)
            reason = f"OpenRouter provider call failed after dispatch: {safe_error}"
            timeout_failure = _looks_like_openrouter_timeout(safe_error)
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
                fallback_policy_result="provider_timeout" if timeout_failure else "provider_error",
                fallback_allowed=openrouter_pool_enabled,
                raw_usage=_openrouter_runtime_usage(
                    usage={"error": safe_error, "error_type": "provider_timeout" if timeout_failure else "provider_error"},
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
            cache_write_tokens=cache_usage["cache_write_tokens"],
            estimated_cost_usd=estimated_cost,
            actual_cost_usd=None,
            latency_ms=latency_ms,
            fallback_allowed=openrouter_pool_enabled,
            fallback_policy_result=policy_decision.result,
            context_pack_id=order.context_pack_id,
            context_fingerprint=str(order.context_pack.get("fingerprint") or ""),
            provider_policy_fingerprint=fingerprint_provider_policy(provider_policy),
            configured_models=configured_models,
            failover_events=openrouter_failover_events,
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
        content_diagnostic = _openrouter_empty_content_diagnostic(response)
        if content_diagnostic:
            _ = _mark_lease_failed(conn, lease_id=lease_id, reason=content_diagnostic)
            _record_attempt_finished(conn, attempt_id=attempt_id, status="failed", output_path=output_path, error={"error": content_diagnostic})
            repo.update_task_blocked(conn, task_id, [content_diagnostic])
            conn.commit()
            raise WorkerExecutionBlocked(content_diagnostic)
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
        reason = LIVE_CODEX_NOT_ENABLED_BLOCKER
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
        raise WorkerExecutionBlocked(reason)

    provider_policy = _load_provider_policy_after_lease(conn, task=task, lease_id=lease_id, task_id=task_id)
    _enforce_model_decision_runtime_gate(conn, task=task, task_id=task_id, lease_id=lease_id, provider_policy=provider_policy)
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
    matter_scope = repo.matter_scope_for_target(conn, target_type="task", target_id=str(row["task_id"])) or "unknown"
    _ = repo.emit_event(conn, "lease.failed", matter_scope=matter_scope, payload={"lease_id": lease_id, "task_id": row["task_id"], "reason": reason})
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


def _run_openrouter_adapter_with_deadline(
    adapter: DirectOpenRouterAdapter,
    *,
    timeout_seconds: float,
    work_order: dict[str, object],
    model: str,
    max_tokens: int,
    temperature: float,
) -> object:
    """Run OpenRouter with a total wall-clock deadline in CLI/main-thread use.

    The underlying HTTP client timeout is still useful for stalled sockets, but
    legal harness workers need a harder dispatch boundary so a long provider
    generation cannot leave leases looking alive forever.
    """

    if (
        threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
        or not hasattr(signal, "getitimer")
    ):
        return adapter.run(work_order, model=model, max_tokens=max_tokens, temperature=temperature)

    old_timer = signal.getitimer(signal.ITIMER_REAL)
    if old_timer[0] > 0:
        return adapter.run(work_order, model=model, max_tokens=max_tokens, temperature=temperature)

    old_handler = signal.getsignal(signal.SIGALRM)

    def _timeout_handler(signum: int, frame: object) -> None:
        del signum, frame
        raise ProviderCallTimeout(f"OpenRouter provider call timed out after {timeout_seconds:g}s")

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return adapter.run(work_order, model=model, max_tokens=max_tokens, temperature=temperature)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)


def _looks_like_openrouter_timeout(error_message: str) -> bool:
    normalized = " ".join(error_message.lower().split())
    return "timed out" in normalized or "timeout" in normalized


def _openrouter_empty_content_diagnostic(response: Mapping[str, object]) -> str:
    content = response.get("content")
    if not isinstance(content, Mapping) or content:
        return ""
    raw = response.get("raw")
    if not isinstance(raw, Mapping):
        return ""
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        return ""
    message = first_choice.get("message")
    if not isinstance(message, Mapping):
        return ""
    message_content = message.get("content")
    has_reasoning = bool(message.get("reasoning") or message.get("reasoning_content") or message.get("reasoning_details"))
    finish_reason = str(first_choice.get("finish_reason") or "")
    if (message_content is None or message_content == "") and has_reasoning:
        if finish_reason == "length":
            return "OpenRouter response exhausted max_tokens in reasoning and returned no JSON content; disable thinking or increase task decomposition"
        return "OpenRouter response returned reasoning without JSON content; disable thinking before retrying this structured worker"
    return ""


def _json_list(raw: str) -> list[object]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return cast(list[object], value) if isinstance(value, list) else []


def _default_openrouter_max_tokens(task: Mapping[str, object]) -> int:
    task_type = str(_task_value(task, "task_type") or "")
    source_count = len(_json_list(str(_task_value(task, "source_dependencies_json") or "[]")))
    if source_count > 25 and task_type in {"evidence_issue_map", "production_mapping", "evidence_organization_plan"}:
        return 4096
    if task_type in {"source_inventory", "extraction_qa", "classification", "duplicate_detection"}:
        return 4096
    if task_type in {"chronology_event_extraction", "authority_map", "hostile_opponent_review", "final_quality_gate", "certification_decision_packet"}:
        return 8192
    return 16000


def _task_value(task: Mapping[str, object], key: str, default: object = "") -> object:
    if hasattr(task, "keys") and key in task.keys():
        return task[key]
    return default


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
    nested_usage = usage_payload.get("usage")
    if isinstance(nested_usage, Mapping) and "error_type" in nested_usage:
        usage_payload["error_type"] = nested_usage["error_type"]
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
    _ = repo.record_provider_control_plane_failure(
        conn,
        matter_scope=repo.matter_scope_for_target(conn, target_type="task", target_id=task_id) or str(task["matter_scope"]),
        task_id=task_id,
        provider=requested.provider,
        message=reason,
        runnable_task_count=1,
        provider_policy_result=fallback_policy_result,
        source="provider.post_dispatch",
        error_type="provider_dispatch_failed",
        attention_prefix="provider runtime",
        trigger_reason_prefix="provider runtime",
        event_prefix="orchestrator.provider_runtime",
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
    actual_provider = str(response.get("provider") or "")
    actual_model = str(response.get("model") or "")
    return {
        "usage": dict(usage),
        "adapter": adapter_name,
        "output_path": str(output_path),
        "requested_model": requested_model,
        "openrouter_endpoint_provenance": {
            "requested_model": requested_model,
            "actual_provider": actual_provider,
            "actual_model": actual_model,
            "configured_models": list(configured_models),
            "honored_requested_model": bool(actual_model and _openrouter_model_was_honored(requested_model, actual_model)),
        },
        "configured_models": list(configured_models),
        "openrouter_failover_events": list(openrouter_failover_events),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        "model_profile_id": provider_policy.get("model_profile_id", ""),
        "model_pool_id": provider_policy.get("model_pool_id", ""),
        "resolved_model": provider_policy.get("resolved_model", {}),
        "provider_policy": dict(provider_policy),
        "provider_policy_fingerprint": fingerprint_provider_policy(provider_policy),
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


def _enforce_model_decision_runtime_gate(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    task_id: str,
    lease_id: str,
    provider_policy: Mapping[str, object],
) -> None:
    """Turn model-decision audit flags into executable runtime gates."""

    decision_raw = provider_policy.get("model_decision")
    decision = decision_raw if isinstance(decision_raw, Mapping) else {}
    decision_tier = str(decision.get("decision_tier") or "").strip()
    decision_reason = str(decision.get("decision_reason") or provider_policy.get("model_decision_reason") or "model decision blocked execution")
    if bool(provider_policy.get("blocked")) or decision_tier == "blocked":
        _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=decision_reason)
        raise WorkerExecutionBlocked(decision_reason)

    provider = str(provider_policy.get("provider") or "").strip()
    model = str(provider_policy.get("model") or "").strip()
    flash_policy_for_pro_work = (
        provider == "openrouter"
        and model == "deepseek/deepseek-v4-flash"
        and _task_requires_pro_runtime(task)
    )
    flagged_flash_downgrade = (
        decision_tier == "flash_worker"
        and bool(decision.get("required_human_review"))
        and _task_requires_pro_runtime(task)
    )
    if not flash_policy_for_pro_work and not flagged_flash_downgrade:
        return
    if _has_active_task_certification(conn, task_id=task_id, certification_type=MODEL_DOWNGRADE_CERTIFICATION):
        repo.emit_event(
            conn,
            "model_downgrade_authorized.runtime_gate",
            matter_scope=str(task["matter_scope"]),
            payload={
                "task_id": task_id,
                "stage": str(task["stage"]),
                "task_type": str(task["task_type"]),
                "provider": provider,
                "model": model,
                "decision_tier": decision_tier,
            },
        )
        return
    reason = (
        "model downgrade blocked: Pro-required legal work cannot execute on "
        f"{model or 'an unspecified model'} without task certification {MODEL_DOWNGRADE_CERTIFICATION}"
    )
    _block_preflight_after_lease(conn, lease_id=lease_id, task_id=task_id, reason=reason)
    raise WorkerExecutionBlocked(reason)


def _task_requires_pro_runtime(task: Mapping[str, object]) -> bool:
    return str(task["stage"] or "") in PRO_REQUIRED_RUNTIME_STAGES or str(task["task_type"] or "") in PRO_REQUIRED_RUNTIME_TASK_TYPES


def _has_active_task_certification(conn: sqlite3.Connection, *, task_id: str, certification_type: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM certifications
        WHERE subject_type = 'task'
          AND subject_id = ?
          AND certification_type = ?
          AND status = 'active'
        LIMIT 1
        """,
        (task_id, certification_type),
    ).fetchone()
    return row is not None


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
    matter_scope = repo.matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown"
    _ = repo.emit_event(conn, "worker_attempt.started", matter_scope=matter_scope, payload={"worker_attempt_id": attempt_id, "task_id": task_id, "adapter": adapter})
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
    row = conn.execute("SELECT task_id FROM worker_attempts WHERE worker_attempt_id = ?", (attempt_id,)).fetchone()
    matter_scope = repo.matter_scope_for_target(conn, target_type="task", target_id=str(row["task_id"]) if row is not None else None) or "unknown"
    _ = repo.emit_event(conn, "worker_attempt.finished", matter_scope=matter_scope, payload={"worker_attempt_id": attempt_id, "status": status})
