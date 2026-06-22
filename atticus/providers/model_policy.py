"""Validated model routing policies for Atticus tasks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import cast

from atticus.providers.anthropic import (
    ANTHROPIC_OAUTH_PROVIDER,
    ANTHROPIC_PROVIDER,
    ANTHROPIC_RUNTIME,
)
from atticus.providers.deepseek import known_model
from atticus.providers.openrouter_registry import get_registry
from atticus.providers.policy import canonical_provider_policy


class ModelPolicyError(ValueError):
    """Raised when a model routing policy is not safe to use."""


DEFAULT_SMART_POLICY_PAYLOAD: dict[str, object] = {
    "version": 1,
    "profiles": {
        "deepseek_flash_or": {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "runtime": "openrouter",
            "allow_fallback": False,
            "estimated_cost_usd": 0.01,
            "capabilities": ["triage", "indexing", "structured_extraction"],
        },
        "deepseek_pro_or": {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "runtime": "openrouter",
            "allow_fallback": False,
            "estimated_cost_usd": 0.03,
            "capabilities": ["legal_reasoning", "hostile_review", "synthesis"],
        },
        "gpt55_codex": {
            "provider": "openai-codex",
            "model": "gpt-5.5",
            "runtime": "codex",
            "allow_fallback": False,
            "estimated_cost_usd": 0.0,
            "capabilities": ["coding_agent", "schema_migration"],
        },
        "anthropic_opus_reserved": {
            "provider": ANTHROPIC_PROVIDER,
            "model": "opus",
            "runtime": ANTHROPIC_RUNTIME,
            "allow_fallback": False,
            "estimated_cost_usd": 0.0,
            "capabilities": ["reserved_high_reasoning"],
            "reserved": True,
            "enabled": False,
        },
        "anthropic_sonnet_reserved": {
            "provider": ANTHROPIC_PROVIDER,
            "model": "sonnet",
            "runtime": ANTHROPIC_RUNTIME,
            "allow_fallback": False,
            "estimated_cost_usd": 0.0,
            "capabilities": ["reserved_balanced_reasoning"],
            "reserved": True,
            "enabled": False,
        },
    },
    "routes": {
        "default": "deepseek_flash_or",
        "layers": {
            "worker": "deepseek_flash_or",
            "subagent": "deepseek_flash_or",
            "orchestrator": "deepseek_pro_or",
            "reducer": "deepseek_pro_or",
            "hostile_review": "deepseek_pro_or",
            "verifier": "deepseek_pro_or",
        },
        "stages": {
            "S0": "deepseek_flash_or",
            "S1": "deepseek_flash_or",
            "S5": "deepseek_pro_or",
            "S6": "deepseek_pro_or",
            "S7": "deepseek_pro_or",
            "S8": "deepseek_pro_or",
            "S9": "deepseek_pro_or",
        },
        "task_types": {
            "source_inventory": "deepseek_flash_or",
            "schema_migration": "gpt55_codex",
            "coding_agent": "gpt55_codex",
            "harness_self_improvement": "gpt55_codex",
            "hostile_opponent_review": "deepseek_pro_or",
            "internal_repair": "deepseek_flash_or",
        },
    },
}


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    provider: str
    model: str
    runtime: str
    allow_fallback: bool = False
    estimated_cost_usd: float = 0.0
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None
    capabilities: tuple[str, ...] = ()
    reserved: bool = False
    enabled: bool = True

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "profile_id": self.profile_id,
            "provider": self.provider,
            "model": self.model,
            "runtime": self.runtime,
            "allow_fallback": self.allow_fallback,
            "estimated_cost_usd": self.estimated_cost_usd,
            "capabilities": list(self.capabilities),
            "reserved": self.reserved,
            "enabled": self.enabled,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.timeout_seconds is not None:
            payload["timeout_seconds"] = self.timeout_seconds
        return payload


@dataclass(frozen=True)
class ModelPool:
    pool_id: str
    profile_ids: tuple[str, ...]
    strategy: str = "fallback_loop"
    max_failed_cycles: int = 5
    cooldown_seconds: float = 300.0
    per_model_timeout_seconds: float | None = None
    backoff_seconds: float | None = None
    jitter_seconds: float | None = None
    allow_cross_provider_fallback: bool = False

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "pool_id": self.pool_id,
            "profiles": list(self.profile_ids),
            "strategy": self.strategy,
            "max_failed_cycles": self.max_failed_cycles,
            "cooldown_seconds": self.cooldown_seconds,
            "allow_cross_provider_fallback": self.allow_cross_provider_fallback,
        }
        if self.per_model_timeout_seconds is not None:
            payload["per_model_timeout_seconds"] = self.per_model_timeout_seconds
        if self.backoff_seconds is not None:
            payload["backoff_seconds"] = self.backoff_seconds
        if self.jitter_seconds is not None:
            payload["jitter_seconds"] = self.jitter_seconds
        return payload


@dataclass(frozen=True)
class ModelRoutingPolicy:
    profiles: dict[str, ModelProfile]
    pools: dict[str, ModelPool]
    default: str
    layers: dict[str, str] = field(default_factory=dict)
    stages: dict[str, str] = field(default_factory=dict)
    task_types: dict[str, str] = field(default_factory=dict)
    task_ids: dict[str, str] = field(default_factory=dict)
    version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "profiles": {key: profile.as_dict() for key, profile in sorted(self.profiles.items())},
            "pools": {key: pool.as_dict() for key, pool in sorted(self.pools.items())},
            "routes": {
                "default": self.default,
                "layers": dict(sorted(self.layers.items())),
                "stages": dict(sorted(self.stages.items())),
                "task_types": dict(sorted(self.task_types.items())),
                "task_ids": dict(sorted(self.task_ids.items())),
            },
        }


def load_model_routing_policy(value: Mapping[str, object] | str | Path) -> ModelRoutingPolicy:
    """Parse and validate a model routing policy from JSON data or a path."""

    raw = _load_json_source(value)
    if _is_legacy_flat_provider_policy(raw):
        if "openrouter_failover" in raw:
            raise ModelPolicyError("legacy flat provider policy cannot include openrouter_failover; use a model routing policy with pools")
        return normalize_legacy_provider_policy(raw)
    version = _int_value(raw.get("version"), default=1)
    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, Mapping) or not profiles_raw:
        raise ModelPolicyError("model policy requires a non-empty profiles object")
    profiles = {
        str(profile_id): _parse_profile(str(profile_id), profile_raw)
        for profile_id, profile_raw in cast(Mapping[object, object], profiles_raw).items()
    }
    pools_raw = raw.get("pools", {})
    if pools_raw is None:
        pools_raw = {}
    if not isinstance(pools_raw, Mapping):
        raise ModelPolicyError("model policy pools must be an object when present")
    pools = {
        str(pool_id): _parse_pool(str(pool_id), pool_raw, profiles=profiles)
        for pool_id, pool_raw in cast(Mapping[object, object], pools_raw).items()
    }
    routes_raw = raw.get("routes")
    if not isinstance(routes_raw, Mapping):
        raise ModelPolicyError("model policy requires routes object")
    routes = _mapping_to_dict(cast(Mapping[object, object], routes_raw))
    default = _required_text(routes.get("default"), "routes.default")
    policy = ModelRoutingPolicy(
        profiles=profiles,
        pools=pools,
        default=default,
        layers=_string_mapping(routes.get("layers")),
        stages=_string_mapping(routes.get("stages")),
        task_types=_string_mapping(routes.get("task_types")),
        task_ids=_string_mapping(routes.get("task_ids")),
        version=version,
    )
    _validate_route_targets(policy)
    return policy


def default_smart_model_policy() -> ModelRoutingPolicy:
    """Return the built-in fail-closed smart routing policy."""

    return load_model_routing_policy(cast(dict[str, object], json.loads(json.dumps(DEFAULT_SMART_POLICY_PAYLOAD, sort_keys=True))))


def normalize_legacy_provider_policy(provider_policy: Mapping[str, object]) -> ModelRoutingPolicy:
    """Wrap an existing flat task provider policy as a single-profile route."""

    policy = canonical_provider_policy(
        provider=str(provider_policy.get("provider") or ""),
        model=str(provider_policy.get("model") or ""),
        allow_fallback=bool(provider_policy.get("allow_fallback") or False),
        estimated_cost_usd=_float_value(provider_policy.get("estimated_cost_usd"), default=0.0),
    )
    runtime = _runtime_for_provider(str(policy["provider"]))
    profile = ModelProfile(
        profile_id="legacy",
        provider=str(policy["provider"]),
        model=str(policy["model"]),
        runtime=runtime,
        allow_fallback=bool(policy["allow_fallback"]),
        estimated_cost_usd=float(str(policy["estimated_cost_usd"])),
    )
    return ModelRoutingPolicy(profiles={"legacy": profile}, pools={}, default="legacy")


def provider_policy_for_route(
    policy: ModelRoutingPolicy,
    *,
    layer: str = "",
    stage: str = "",
    task_type: str = "",
    task_id: str = "",
    include_routing: bool = True,
) -> dict[str, object]:
    """Resolve a task/layer/stage route to a runtime provider_policy_json payload."""

    target = resolve_route_target(policy, layer=layer, stage=stage, task_type=task_type, task_id=task_id)
    payload = _provider_policy_for_target(policy, target)
    if include_routing and not _is_normalized_legacy_policy(policy):
        payload["model_routing"] = policy.as_dict()
    return payload


def smart_provider_policy_for_route(
    policy: ModelRoutingPolicy,
    *,
    layer: str = "",
    stage: str = "",
    task_type: str = "",
    task_id: str = "",
    matter_scope: str = "atticus",
    risk_level: str = "unknown",
    legal_complexity: str = "unknown",
    evidence_volume: str = "unknown",
    authority_required: bool = False,
    hostile_review_required: bool = False,
    drafting_finality: str = "",
    contradiction_count: int = 0,
    unresolved_uncertainty_count: int = 0,
    source_count: int = 0,
    extracted_char_count: int = 0,
    expected_value: float = 0.0,
    requested_capabilities: tuple[str, ...] = (),
    operator_override: str | None = None,
) -> dict[str, object]:
    """Resolve a policy with the deterministic model decision layer attached."""

    from atticus.providers.model_decision import ModelDecisionInput, ModelRoutingPolicyLike, decide_model

    decision = decide_model(
        cast(ModelRoutingPolicyLike, cast(object, policy)),
        ModelDecisionInput(
            matter_scope=matter_scope,
            task_id=task_id,
            stage=stage,
            task_type=task_type,
            layer=layer,
            risk_level=risk_level,
            legal_complexity=legal_complexity,
            evidence_volume=evidence_volume,
            authority_required=authority_required,
            hostile_review_required=hostile_review_required,
            drafting_finality=drafting_finality,
            contradiction_count=contradiction_count,
            unresolved_uncertainty_count=unresolved_uncertainty_count,
            source_count=source_count,
            extracted_char_count=extracted_char_count,
            expected_value=expected_value,
            requested_capabilities=requested_capabilities,
            operator_override=operator_override,
        ),
    )
    if decision.decision_tier == "blocked":
        return {
            "provider": decision.provider,
            "model": decision.model,
            "runtime": decision.runtime,
            "allow_fallback": False,
            "model_decision": decision.__dict__,
            "model_decision_reason": decision.decision_reason,
            "blocked": True,
        }
    if decision.profile_id not in policy.profiles:
        return {
            "provider": "blocked",
            "model": "blocked",
            "runtime": "blocked",
            "allow_fallback": False,
            "model_decision": decision.__dict__,
            "model_decision_reason": decision.decision_reason,
            "blocked": True,
        }
    payload = _provider_policy_for_target(policy, decision.profile_id)
    if not _is_normalized_legacy_policy(policy):
        payload["model_routing"] = policy.as_dict()
    payload["model_decision"] = decision.__dict__
    payload["model_decision_reason"] = decision.decision_reason
    payload["allow_fallback"] = decision.fallback_allowed
    return payload


def resolve_route_target(
    policy: ModelRoutingPolicy,
    *,
    layer: str = "",
    stage: str = "",
    task_type: str = "",
    task_id: str = "",
) -> str:
    base_task_type = _base_task_type(task_type)
    for routes, key in (
        (policy.task_ids, task_id),
        (policy.task_types, task_type),
        (policy.task_types, base_task_type),
        (policy.layers, layer),
        (policy.stages, stage),
    ):
        if key and key in routes:
            return routes[key]
    return policy.default


def _base_task_type(task_type: str) -> str:
    if task_type.endswith("_bundle"):
        return task_type[: -len("_bundle")]
    return task_type


def validate_proposed_task_provider_policy(
    *,
    parent_provider_policy: Mapping[str, object],
    proposed_task: Mapping[object, object],
    layer: str = "subagent",
) -> dict[str, object]:
    """Return inherited/normalized provider policy for a proposed follow-up task."""

    resolved = resolve_provider_policy_from_parent(
        parent_provider_policy,
        proposed_task=proposed_task,
        layer=layer,
    )
    proposed = proposed_task.get("provider_policy")
    if not isinstance(proposed, Mapping):
        return resolved
    proposed_map = _mapping_to_dict(cast(Mapping[object, object], proposed))
    try:
        proposed_policy = canonical_provider_policy(
            provider=str(proposed_map.get("provider") or ""),
            model=str(proposed_map.get("model") or ""),
            allow_fallback=bool(proposed_map.get("allow_fallback") or False),
            estimated_cost_usd=_float_value(proposed_map.get("estimated_cost_usd"), default=0.0),
        )
    except ValueError as exc:
        return _with_audit(resolved, f"proposed task provider policy rejected: {exc}")
    if _provider_model_tuple(proposed_policy) == _provider_model_tuple(resolved):
        return {**resolved, "estimated_cost_usd": proposed_policy["estimated_cost_usd"]}
    return _with_audit(
        resolved,
        f"proposed task provider policy outside active routing policy: {proposed_policy['provider']}/{proposed_policy['model']}",
    )


def resolve_provider_policy_from_parent(
    parent_provider_policy: Mapping[str, object],
    *,
    proposed_task: Mapping[object, object],
    layer: str = "subagent",
) -> dict[str, object]:
    routing_raw = parent_provider_policy.get("model_routing")
    if isinstance(routing_raw, Mapping):
        policy = load_model_routing_policy(cast(Mapping[str, object], routing_raw))
        if _parent_uses_smart_decision(parent_provider_policy):
            return smart_provider_policy_for_route(
                policy,
                layer=layer,
                stage=str(proposed_task.get("stage") or ""),
                task_type=str(proposed_task.get("task_type") or ""),
                task_id=str(proposed_task.get("task_id") or ""),
                matter_scope=str(proposed_task.get("matter_scope") or "atticus"),
                expected_value=_float_value(proposed_task.get("expected_value"), default=0.0),
                source_count=_int_value(proposed_task.get("source_count"), default=0),
                extracted_char_count=_int_value(proposed_task.get("extracted_char_count"), default=0),
                requested_capabilities=_string_tuple_or_empty(proposed_task.get("validation_gates")),
            )
        return provider_policy_for_route(
            policy,
            layer=layer,
            stage=str(proposed_task.get("stage") or ""),
            task_type=str(proposed_task.get("task_type") or ""),
            task_id=str(proposed_task.get("task_id") or ""),
        )
    if _parent_uses_smart_decision(parent_provider_policy):
        return smart_provider_policy_for_route(
            default_smart_model_policy(),
            layer=layer,
            stage=str(proposed_task.get("stage") or ""),
            task_type=str(proposed_task.get("task_type") or ""),
            task_id=str(proposed_task.get("task_id") or ""),
            matter_scope=str(proposed_task.get("matter_scope") or "atticus"),
            expected_value=_float_value(proposed_task.get("expected_value"), default=0.0),
            source_count=_int_value(proposed_task.get("source_count"), default=0),
            extracted_char_count=_int_value(proposed_task.get("extracted_char_count"), default=0),
            requested_capabilities=_string_tuple_or_empty(proposed_task.get("validation_gates")),
        )
    if _is_legacy_flat_provider_policy(parent_provider_policy):
        return dict(parent_provider_policy)
    return {}


def _parent_uses_smart_decision(parent_provider_policy: Mapping[str, object]) -> bool:
    return "model_decision" in parent_provider_policy or "model_decision_reason" in parent_provider_policy


def _provider_policy_for_target(policy: ModelRoutingPolicy, target: str) -> dict[str, object]:
    if target in policy.profiles:
        return _profile_provider_policy(policy.profiles[target], pool_id=None)
    if target in policy.pools:
        pool = policy.pools[target]
        first = policy.profiles[pool.profile_ids[0]]
        payload = _profile_provider_policy(first, pool_id=pool.pool_id)
        payload["allow_fallback"] = True
        payload["openrouter_failover"] = _openrouter_failover_policy(pool, [policy.profiles[profile_id] for profile_id in pool.profile_ids])
        payload["resolved_model"] = {
            "target": target,
            "pool_id": pool.pool_id,
            "profile_ids": list(pool.profile_ids),
            "provider": first.provider,
            "model": first.model,
            "runtime": first.runtime,
        }
        return payload
    raise ModelPolicyError(f"unknown route target: {target}")


def _is_normalized_legacy_policy(policy: ModelRoutingPolicy) -> bool:
    return (
        policy.default == "legacy"
        and set(policy.profiles) == {"legacy"}
        and not policy.pools
        and not policy.layers
        and not policy.stages
        and not policy.task_types
        and not policy.task_ids
    )


def _profile_provider_policy(profile: ModelProfile, *, pool_id: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": profile.provider,
        "model": profile.model,
        "runtime": profile.runtime,
        "allow_fallback": profile.allow_fallback,
        "estimated_cost_usd": profile.estimated_cost_usd,
        "model_profile_id": profile.profile_id,
        "resolved_model": {
            "target": profile.profile_id,
            "profile_id": profile.profile_id,
            "provider": profile.provider,
            "model": profile.model,
            "runtime": profile.runtime,
        },
    }
    if pool_id is not None:
        payload["model_pool_id"] = pool_id
    if profile.max_tokens is not None:
        payload["max_tokens"] = profile.max_tokens
    if profile.temperature is not None:
        payload["temperature"] = profile.temperature
    if profile.timeout_seconds is not None:
        payload["timeout_seconds"] = profile.timeout_seconds
    if profile.capabilities:
        payload["capabilities"] = list(profile.capabilities)
    return payload


def _openrouter_failover_policy(pool: ModelPool, profiles: list[ModelProfile]) -> dict[str, object]:
    payload: dict[str, object] = {
        "enabled": True,
        "models": [profile.model for profile in profiles],
        "max_failed_cycles": pool.max_failed_cycles,
        "cooldown_seconds": pool.cooldown_seconds,
    }
    if pool.per_model_timeout_seconds is not None:
        payload["per_model_timeout_seconds"] = pool.per_model_timeout_seconds
    if pool.backoff_seconds is not None:
        payload["backoff_seconds"] = pool.backoff_seconds
    if pool.jitter_seconds is not None:
        payload["jitter_seconds"] = pool.jitter_seconds
    return payload


def _parse_profile(profile_id: str, raw: object) -> ModelProfile:
    if not isinstance(raw, Mapping):
        raise ModelPolicyError(f"profile {profile_id} must be an object")
    raw_map = _mapping_to_dict(cast(Mapping[object, object], raw))
    provider = _required_text(raw_map.get("provider"), f"profiles.{profile_id}.provider")
    model = _required_text(raw_map.get("model"), f"profiles.{profile_id}.model")
    if provider == "openai-codex" and model == "openai-codex/gpt-5.5":
        model = "gpt-5.5"
    runtime = _required_text(raw_map.get("runtime") or _runtime_for_provider(provider), f"profiles.{profile_id}.runtime")
    reserved = bool(raw_map.get("reserved") or False)
    enabled = bool(raw_map.get("enabled", True))
    if provider == "deepseek" or runtime == "deepseek":
        raise ModelPolicyError("direct DeepSeek runtime is not supported; use provider openrouter with deepseek/... model ids")
    if provider in {ANTHROPIC_PROVIDER, ANTHROPIC_OAUTH_PROVIDER}:
        if runtime != ANTHROPIC_RUNTIME:
            raise ModelPolicyError(f"profile {profile_id} runtime {runtime!r} does not match provider {provider!r}")
        if not known_model(provider, model):
            raise ModelPolicyError(f"unknown or unsupported model: {provider}/{model}")
        if not reserved and enabled:
            raise ModelPolicyError("enabled Anthropic profiles are scaffolded only; mark the profile reserved/disabled until a live runtime is implemented")
        return ModelProfile(
            profile_id=profile_id,
            provider=provider,
            model=model,
            runtime=runtime,
            allow_fallback=False,
            estimated_cost_usd=_float_value(raw_map.get("estimated_cost_usd"), default=0.0),
            max_tokens=_optional_int(raw_map.get("max_tokens")),
            temperature=_optional_float(raw_map.get("temperature")),
            timeout_seconds=_optional_float(raw_map.get("timeout_seconds")),
            capabilities=_string_tuple(raw_map.get("capabilities")),
            reserved=reserved or not enabled,
            enabled=enabled,
        )
    if not known_model(provider, model):
        if provider == "openrouter" and not get_registry().has_model(model):
            raise ModelPolicyError(f"unknown or unsupported model: {provider}/{model}")
        if provider != "openrouter":
            raise ModelPolicyError(f"unknown or unsupported model: {provider}/{model}")
    expected_runtime = _runtime_for_provider(provider)
    if runtime != expected_runtime:
        raise ModelPolicyError(f"profile {profile_id} runtime {runtime!r} does not match provider {provider!r}")
    policy = canonical_provider_policy(
        provider=provider,
        model=model,
        allow_fallback=bool(raw_map.get("allow_fallback") or False),
        estimated_cost_usd=_float_value(raw_map.get("estimated_cost_usd"), default=0.0),
    )
    return ModelProfile(
        profile_id=profile_id,
        provider=str(policy["provider"]),
        model=str(policy["model"]),
        runtime=runtime,
        allow_fallback=bool(policy["allow_fallback"]),
        estimated_cost_usd=float(str(policy["estimated_cost_usd"])),
        max_tokens=_optional_int(raw_map.get("max_tokens")),
        temperature=_optional_float(raw_map.get("temperature")),
        timeout_seconds=_optional_float(raw_map.get("timeout_seconds")),
        capabilities=_string_tuple(raw_map.get("capabilities")),
        reserved=reserved,
        enabled=enabled,
    )


def _parse_pool(pool_id: str, raw: object, *, profiles: Mapping[str, ModelProfile]) -> ModelPool:
    if not isinstance(raw, Mapping):
        raise ModelPolicyError(f"pool {pool_id} must be an object")
    raw_map = _mapping_to_dict(cast(Mapping[object, object], raw))
    profile_ids = _string_tuple(raw_map.get("profiles") or raw_map.get("profile_ids"))
    if not profile_ids:
        raise ModelPolicyError(f"pool {pool_id} requires at least one profile")
    for profile_id in profile_ids:
        if profile_id not in profiles:
            raise ModelPolicyError(f"pool {pool_id} references unknown profile {profile_id}")
    selected = [profiles[profile_id] for profile_id in profile_ids]
    providers = {profile.provider for profile in selected}
    runtimes = {profile.runtime for profile in selected}
    allow_cross = bool(raw_map.get("allow_cross_provider_fallback") or False)
    if len(providers) > 1 and not allow_cross:
        raise ModelPolicyError(f"pool {pool_id} crosses providers without allow_cross_provider_fallback")
    if len(providers) > 1 and ("codex" in runtimes or "openai-codex" in providers):
        raise ModelPolicyError(f"pool {pool_id} includes Codex but no safe Codex live adapter exists")
    strategy = str(raw_map.get("strategy") or "fallback_loop")
    if strategy != "fallback_loop":
        raise ModelPolicyError(f"pool {pool_id} has unsupported strategy {strategy!r}")
    return ModelPool(
        pool_id=pool_id,
        profile_ids=profile_ids,
        strategy=strategy,
        max_failed_cycles=_int_value(raw_map.get("max_failed_cycles"), default=5),
        cooldown_seconds=_float_value(raw_map.get("cooldown_seconds"), default=300.0),
        per_model_timeout_seconds=_optional_float(raw_map.get("per_model_timeout_seconds")),
        backoff_seconds=_optional_float(raw_map.get("backoff_seconds")),
        jitter_seconds=_optional_float(raw_map.get("jitter_seconds")),
        allow_cross_provider_fallback=allow_cross,
    )


def _validate_route_targets(policy: ModelRoutingPolicy) -> None:
    targets = {policy.default, *policy.layers.values(), *policy.stages.values(), *policy.task_types.values(), *policy.task_ids.values()}
    known = {*policy.profiles, *policy.pools}
    for target in targets:
        if target not in known:
            raise ModelPolicyError(f"route references unknown target: {target}")


def _load_json_source(value: Mapping[str, object] | str | Path) -> dict[str, object]:
    if isinstance(value, Mapping):
        return _mapping_to_dict(cast(Mapping[object, object], value))
    path = Path(value)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelPolicyError(f"model policy file is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ModelPolicyError("model policy JSON must be an object")
    return _mapping_to_dict(cast(Mapping[object, object], raw))


def _is_legacy_flat_provider_policy(value: Mapping[str, object]) -> bool:
    return "provider" in value and "model" in value and "profiles" not in value


def _runtime_for_provider(provider: str) -> str:
    if provider == "openrouter":
        return "openrouter"
    if provider == "openai-codex":
        return "codex"
    if provider in {ANTHROPIC_PROVIDER, ANTHROPIC_OAUTH_PROVIDER}:
        return ANTHROPIC_RUNTIME
    raise ModelPolicyError(f"unsupported provider for model routing: {provider or 'unset'}")


def _with_audit(policy: dict[str, object], reason: str) -> dict[str, object]:
    payload = dict(policy)
    payload["model_policy_audit"] = {"action": "normalized_to_active_route", "reason": reason}
    return payload


def _provider_model_tuple(policy: Mapping[str, object]) -> tuple[str, str]:
    return str(policy.get("provider") or ""), str(policy.get("model") or "")


def _mapping_to_dict(value: Mapping[object, object]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}


def _string_mapping(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ModelPolicyError("route maps must be JSON objects")
    return {str(key): str(item) for key, item in cast(Mapping[object, object], value).items()}


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if not isinstance(value, list | tuple):
        raise ModelPolicyError("expected a string list")
    values: list[str] = []
    for item in cast(list[object] | tuple[object, ...], value):
        text = str(item).strip()
        if text:
            values.append(text)
    return tuple(values)


def _string_tuple_or_empty(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        return _string_tuple(value)
    except ModelPolicyError:
        return ()


def _required_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ModelPolicyError(f"{name} is required")
    return text


def _int_value(value: object, *, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ModelPolicyError("integer policy fields must not be booleans")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ModelPolicyError(f"invalid integer policy value: {value!r}") from exc
    if result < 1:
        raise ModelPolicyError("integer policy fields must be positive")
    return result


def _float_value(value: object, *, default: float) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ModelPolicyError("float policy fields must not be booleans")
    try:
        result = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ModelPolicyError(f"invalid float policy value: {value!r}") from exc
    if not math.isfinite(result) or result < 0:
        raise ModelPolicyError("float policy fields must be finite and non-negative")
    return result


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return _float_value(value, default=0.0)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return _int_value(value, default=1)
