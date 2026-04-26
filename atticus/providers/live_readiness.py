"""Live-provider readiness checks for safe Atticus resume."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import sqlite3
from typing import Mapping, Any

from atticus.providers.deepseek import known_model
from atticus.providers.budget import check_budget
from atticus.providers.openrouter_failover import openrouter_client_for_policy, openrouter_models_for_policy, primary_model_for_policy, safe_openrouter_error_message
from atticus.providers.openrouter import OpenRouterClient, OpenRouterError, validate_usage_tokens
from atticus.providers.policy import ProviderActual, ProviderRequest, check_provider_policy
from atticus.scheduler.gates import evaluate_task_gates

LIVE_ENABLE_ENV = "ATTICUS_ENABLE_LIVE_OPENROUTER"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"


@dataclass(frozen=True)
class LiveProviderDecision:
    allowed: bool
    reasons: list[str]


def check_live_provider_policy(provider_policy: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> LiveProviderDecision:
    """Fail-closed policy for any live provider-backed work.

    Atticus live work is currently OpenRouter-only, with fallback disabled and a
    key present. Codex, Claude Code, direct DeepSeek, and silent provider swaps
    are intentionally not live providers for this harness.
    """

    env = env if env is not None else os.environ
    provider = str(provider_policy.get("provider") or "")
    allow_fallback = bool(provider_policy.get("allow_fallback") or False)
    reasons: list[str] = []
    models: tuple[str, ...] = ()

    if provider != "openrouter":
        reasons.append(f"provider must be openrouter for live Atticus work, got {provider or 'unset'}")
    elif provider == "openrouter":
        try:
            models = openrouter_models_for_policy(provider_policy, env=env)
        except (OpenRouterError, TypeError, ValueError) as exc:
            reasons.append(str(exc))
        if not models:
            reasons.append("unknown or unsupported OpenRouter model: unset")
        for model in models:
            if not known_model(provider, model):
                reasons.append(f"unknown or unsupported OpenRouter model: {model or 'unset'}")
    if allow_fallback:
        reasons.append("fallback must be disabled for live Atticus work")
    if not env.get(OPENROUTER_KEY_ENV):
        reasons.append(f"{OPENROUTER_KEY_ENV} is required before live OpenRouter work")

    return LiveProviderDecision(not reasons, reasons)


def live_openrouter_enabled(*, env: Mapping[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    return env.get(LIVE_ENABLE_ENV) == "1"


def probe_live_openrouter(
    provider_policy: Mapping[str, Any],
    *,
    client: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Make a tiny OpenRouter JSON probe and fail closed on provider/model drift."""

    env = env if env is not None else os.environ
    model = str(provider_policy.get("model") or "")
    if not live_openrouter_enabled(env=env):
        return {
            "ok": False,
            "provider": "openrouter",
            "model": model,
            "reason": f"{LIVE_ENABLE_ENV}=1 is required before spending on an OpenRouter provider probe",
            "provider_policy_result": "blocked_before_probe",
        }
    policy = check_live_provider_policy(provider_policy, env=env)
    try:
        model = primary_model_for_policy(provider_policy, env=env)
        configured_models = openrouter_models_for_policy(provider_policy, env=env)
    except (OpenRouterError, TypeError, ValueError) as exc:
        model = str(provider_policy.get("model") or "")
        configured_models = (model,) if model else ()
        policy = LiveProviderDecision(False, [*policy.reasons, str(exc)])
    if not policy.allowed:
        return {
            "ok": False,
            "provider": "openrouter",
            "model": model,
            "reason": "; ".join(policy.reasons),
            "provider_policy_result": "blocked_before_probe",
            "configured_models": list(configured_models),
        }

    probe_client = openrouter_client_for_policy(provider_policy, env=env, client=client) or OpenRouterClient(api_key=env.get(OPENROUTER_KEY_ENV, ""))
    try:
        response = probe_client.chat_json(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "Return only valid JSON for an Atticus OpenRouter provider probe.",
                },
                {
                    "role": "user",
                    "content": '{"instruction":"Return {\\"ok\\": true, \\"probe\\": \\"atticus-live-openrouter\\"}"}',
                },
            ],
            max_tokens=64,
            temperature=0.0,
        )
    except (OpenRouterError, OSError, RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "provider": "openrouter",
            "model": model,
            "reason": f"OpenRouter probe failed: {safe_openrouter_error_message(exc)}",
            "provider_policy_result": "probe_failed",
            "configured_models": list(configured_models),
        }

    if not isinstance(response, Mapping):
        return {
            "ok": False,
            "provider": "openrouter",
            "model": model,
            "reason": "OpenRouter probe response must be a JSON object",
            "provider_policy_result": "probe_failed",
        }
    usage_raw = response.get("usage")
    if "usage" in response and not isinstance(usage_raw, Mapping):
        return {
            "ok": False,
            "provider": str(response.get("provider") or "missing"),
            "model": str(response.get("model") or "missing"),
            "reason": "OpenRouter probe usage metadata must be a JSON object",
            "provider_policy_result": "probe_failed",
        }
    usage = dict(usage_raw) if isinstance(usage_raw, Mapping) else {}
    try:
        validate_usage_tokens(usage)
    except OpenRouterError as exc:
        return {
            "ok": False,
            "provider": str(response.get("provider") or "missing"),
            "model": str(response.get("model") or "missing"),
            "reason": f"OpenRouter probe usage metadata is invalid: {exc}",
            "provider_policy_result": "probe_failed",
            "usage": usage,
        }
    reported_provider = response.get("provider")
    reported_model = response.get("model")
    if not reported_provider or not reported_model:
        return {
            "ok": False,
            "provider": str(reported_provider or ""),
            "model": str(reported_model or ""),
            "reason": "OpenRouter probe response missing provider/model metadata required for fallback detection",
            "provider_policy_result": "probe_failed",
            "usage": usage,
        }
    actual = ProviderActual(str(reported_provider), str(reported_model))
    requested_model = str(response.get("requested_model") or model)
    if requested_model not in configured_models:
        return {
            "ok": False,
            "provider": actual.provider,
            "model": actual.model,
            "requested_model": requested_model,
            "configured_models": list(configured_models),
            "reason": f"OpenRouter probe requested model {requested_model} is not in configured model list",
            "provider_policy_result": "failed_closed",
            "usage": usage,
        }
    request = ProviderRequest("openrouter", requested_model, allow_fallback=False)
    decision = check_provider_policy(request, actual=actual)
    content_raw = response.get("content")
    content = content_raw if isinstance(content_raw, Mapping) else {}
    ok_content = content.get("ok") is True
    ok = decision.allowed and ok_content
    reason = "probe passed" if ok else decision.reason if not decision.allowed else "OpenRouter probe did not return literal ok=true"
    return {
        "ok": ok,
        "provider": actual.provider,
        "model": actual.model,
        "requested_model": requested_model,
        "configured_models": list(configured_models),
        "reason": reason,
        "provider_policy_result": decision.result,
        "usage": usage,
    }


def live_readiness_report(conn: sqlite3.Connection, *, capacity: int = 15, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return a read-only live resume report without acquiring leases or launching workers."""

    env = env if env is not None else os.environ
    capacity_requested = max(0, capacity)
    runnable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    global_reasons: list[str] = []
    if not live_openrouter_enabled(env=env):
        global_reasons.append(f"{LIVE_ENABLE_ENV}=1 is required to enable live OpenRouter work")

    for task in conn.execute(
        """
        SELECT * FROM tasks
        WHERE status IN ('queued', 'ready', 'blocked')
        ORDER BY expected_value DESC, created_at ASC
        """
    ):
        try:
            policy = _parse_provider_policy(task)
        except ValueError as exc:
            blocked.append({"task_id": task["task_id"], "title": task["title"], "reasons": [str(exc)]})
            continue
        reasons: list[str] = []
        try:
            policy_models = openrouter_models_for_policy(policy, env=env)
        except (OpenRouterError, TypeError, ValueError):
            policy_models = (str(policy.get("model") or ""),) if policy.get("model") else ()
        try:
            estimated = parse_estimated_cost_usd(policy, task_id=task["task_id"], require_present=True)
        except ValueError as exc:
            blocked.append({"task_id": task["task_id"], "title": task["title"], "reasons": [str(exc)]})
            continue
        reasons.extend(check_live_provider_policy(policy, env=env).reasons)
        try:
            gate_result = evaluate_task_gates(conn, task)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            reasons.append(f"malformed task gate metadata for task {task['task_id']}: {exc}")
        else:
            reasons.extend(gate_result.reasons)
        if task["cost_limit_usd"] is not None and estimated > float(task["cost_limit_usd"]):
            reasons.append(f"task estimated cost {estimated:.4f} exceeds task cost limit {float(task['cost_limit_usd']):.4f}")
        for scope_type, scope_id in (("task", task["task_id"]), ("stage", task["stage"]), ("matter", task["matter_scope"])):
            budget = check_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=estimated)
            if not budget.allowed:
                reasons.append(f"budget blocked for {scope_type}:{scope_id}: {budget.reason}")
        if reasons:
            blocked.append({"task_id": task["task_id"], "title": task["title"], "reasons": reasons})
        elif len(runnable) < capacity_requested:
            runnable.append(
                {
                    "task_id": task["task_id"],
                    "title": task["title"],
                    "stage": task["stage"],
                    "task_type": task["task_type"],
                    "provider": str(policy.get("provider") or ""),
                    "model": policy_models[0] if policy_models else str(policy.get("model") or ""),
                    "models": list(policy_models),
                }
            )

    ready = not global_reasons and bool(runnable)
    return {
        "ready": ready,
        "reasons": global_reasons,
        "capacity_requested": capacity_requested,
        "capacity_safe": len(runnable),
        "runnable_task_ids": [item["task_id"] for item in runnable],
        "runnable_tasks": runnable,
        "blocked_tasks": blocked,
        "live_openrouter_enabled": live_openrouter_enabled(env=env),
        "openrouter_key_present": bool(env.get(OPENROUTER_KEY_ENV)),
    }


def _parse_provider_policy(task: sqlite3.Row) -> dict[str, Any]:
    try:
        policy = json.loads(task["provider_policy_json"] or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"malformed provider policy for task {task['task_id']}: {exc}") from exc
    if not isinstance(policy, dict):
        raise ValueError(f"malformed provider policy for task {task['task_id']}: policy must be a JSON object")
    return policy


def parse_estimated_cost_usd(provider_policy: Mapping[str, Any], *, task_id: str, require_present: bool = False) -> float:
    """Return a finite non-negative estimated cost or raise a fail-closed error."""

    if "estimated_cost_usd" not in provider_policy or provider_policy.get("estimated_cost_usd") is None:
        if require_present:
            raise ValueError(f"provider policy for task {task_id} must include estimated_cost_usd before live work")
        return 0.0
    raw = provider_policy.get("estimated_cost_usd")
    if raw is None:
        raise ValueError(f"provider policy for task {task_id} must include estimated_cost_usd before live work")
    if isinstance(raw, bool):
        raise ValueError(f"provider policy for task {task_id} has invalid estimated_cost_usd: boolean is not allowed")
    try:
        estimated = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"provider policy for task {task_id} has invalid estimated_cost_usd: {raw!r}") from exc
    if not math.isfinite(estimated) or estimated < 0:
        raise ValueError(f"provider policy for task {task_id} has invalid estimated_cost_usd: must be finite and non-negative")
    return estimated
