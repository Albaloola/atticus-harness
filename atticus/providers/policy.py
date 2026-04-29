"""Fail-closed provider policy enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import math
import sqlite3

from atticus.db import repo
from atticus.providers.deepseek import is_held_openrouter_model, known_model


@dataclass(frozen=True)
class ProviderRequest:
    provider: str
    model: str
    allow_fallback: bool = False


@dataclass(frozen=True)
class ProviderActual:
    provider: str
    model: str


@dataclass(frozen=True)
class ProviderDecision:
    allowed: bool
    result: str
    reason: str


def check_provider_policy(requested: ProviderRequest, actual: ProviderActual | None = None) -> ProviderDecision:
    requested_provider, requested_model = _canonical_provider_model(requested.provider.strip(), requested.model.strip())
    if requested_provider == "deepseek":
        return ProviderDecision(
            False,
            "failed_closed",
            "direct DeepSeek provider is not executable in this harness; use provider openrouter with deepseek/... model ids",
        )
    if requested_provider in {"anthropic", "anthropic-oauth"}:
        return ProviderDecision(False, "reserved", "Anthropic provider profiles are reserved and disabled by default")
    if requested_provider == "openrouter" and is_held_openrouter_model(requested_model) and not known_model(requested_provider, requested_model):
        return ProviderDecision(False, "blocked", f"held OpenRouter model is not routable by default: {requested_model}")
    if not known_model(requested_provider, requested_model):
        return ProviderDecision(False, "blocked", f"unknown or unsupported model: {requested_provider}/{requested_model}")
    if requested.allow_fallback:
        if requested_provider == "openai-codex":
            return ProviderDecision(False, "failed_closed", "Codex fallback is not allowed")
        return ProviderDecision(
            False,
            "failed_closed",
            "flat provider fallback is not allowed; configure an explicit OpenRouter model pool instead",
        )
    actual = actual or ProviderActual(requested.provider, requested.model)
    actual_provider, actual_model = _canonical_provider_model(actual.provider, actual.model)
    same = actual_provider == requested_provider and actual_model == requested_model
    if same:
        return ProviderDecision(True, "not_needed", "requested provider/model matched actual provider/model")
    if requested_provider == "openai-codex":
        return ProviderDecision(False, "failed_closed", "actual provider/model differed and Codex fallback is not allowed")
    return ProviderDecision(False, "failed_closed", "actual provider/model differed and fallback was not allowed")


def _canonical_provider_model(provider: str, model: str) -> tuple[str, str]:
    if provider == "openai-codex" and model == "openai-codex/gpt-5.5":
        return provider, "gpt-5.5"
    return provider, model


def canonical_provider_policy(
    *,
    provider: str,
    model: str,
    allow_fallback: bool,
    estimated_cost_usd: float = 0.0,
) -> dict[str, object]:
    """Return a normalized provider policy after fail-closed validation."""

    requested = ProviderRequest(provider.strip(), model.strip(), allow_fallback=allow_fallback)
    decision = check_provider_policy(requested)
    if not decision.allowed:
        raise ValueError(decision.reason)
    normalized_cost = float(estimated_cost_usd)
    if not math.isfinite(normalized_cost) or normalized_cost < 0:
        raise ValueError("estimated_cost_usd must be finite and non-negative")
    canonical_provider, canonical_model = _canonical_provider_model(requested.provider, requested.model)
    return {
        "provider": canonical_provider,
        "model": canonical_model,
        "allow_fallback": requested.allow_fallback,
        "estimated_cost_usd": normalized_cost,
    }


def record_provider_policy_decision(
    conn: sqlite3.Connection,
    *,
    requested: ProviderRequest,
    actual: ProviderActual | None = None,
    task_id: str | None = None,
    estimated_cost_usd: float = 0.0,
) -> ProviderDecision:
    decision = check_provider_policy(requested, actual=actual)
    actual = actual or ProviderActual(requested.provider, requested.model)
    _ = repo.record_provider_run(
        conn,
        task_id=task_id,
        requested_provider=requested.provider,
        requested_model=requested.model,
        actual_provider=actual.provider,
        actual_model=actual.model,
        estimated_cost_usd=estimated_cost_usd,
        fallback_allowed=requested.allow_fallback,
        fallback_policy_result=decision.result,
        raw_usage={"policy_decision": decision.reason},
    )
    if not decision.allowed:
        _ = repo.record_human_attention(
            conn,
            target_type="provider_policy",
            target_id=task_id or "adhoc",
            severity="blocker",
            reason=decision.reason,
        )
    return decision
