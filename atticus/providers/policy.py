"""Fail-closed provider policy enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from atticus.db import repo
from atticus.providers.deepseek import known_model


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
    if not known_model(requested.provider, requested.model):
        return ProviderDecision(False, "blocked", f"unknown or unsupported model: {requested.provider}/{requested.model}")
    actual = actual or ProviderActual(requested.provider, requested.model)
    same = actual.provider == requested.provider and actual.model == requested.model
    if same:
        return ProviderDecision(True, "not_needed", "requested provider/model matched actual provider/model")
    if requested.allow_fallback:
        return ProviderDecision(True, "allowed", "fallback explicitly allowed")
    return ProviderDecision(False, "failed_closed", "actual provider/model differed and fallback was not allowed")


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
    repo.record_provider_run(
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
        repo.record_human_attention(
            conn,
            target_type="provider_policy",
            target_id=task_id or "adhoc",
            severity="blocker",
            reason=decision.reason,
        )
    return decision
