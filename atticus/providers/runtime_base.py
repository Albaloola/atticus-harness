"""Small provider runtime validation/probe abstraction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from atticus.providers.anthropic import (
    ANTHROPIC_PROVIDER,
    ANTHROPIC_RUNTIME,
    resolve_anthropic_model,
)
from atticus.providers.live_readiness import check_live_provider_policy, probe_live_openrouter
from atticus.providers.policy import ProviderDecision, ProviderRequest, check_provider_policy


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    provider: str
    model: str
    reason: str


@dataclass(frozen=True)
class ProviderRequestPayload:
    provider_policy: Mapping[str, object]
    work_order: Mapping[str, object]


@dataclass(frozen=True)
class ProviderResponsePayload:
    requested_provider: str
    requested_model: str
    actual_provider: str
    actual_model: str
    payload: Mapping[str, object]


class ProviderRuntime(Protocol):
    provider: str
    runtime: str

    def validate_policy(self, policy: Mapping[str, object]) -> ProviderDecision: ...
    def probe(self, policy: Mapping[str, object], env: Mapping[str, str]) -> ProbeResult: ...
    def execute_json(self, request: ProviderRequestPayload) -> ProviderResponsePayload: ...


class OpenRouterRuntime:
    provider = "openrouter"
    runtime = "openrouter"

    def validate_policy(self, policy: Mapping[str, object]) -> ProviderDecision:
        return check_provider_policy(ProviderRequest("openrouter", str(policy.get("model") or ""), allow_fallback=bool(policy.get("allow_fallback") or False)))

    def probe(self, policy: Mapping[str, object], env: Mapping[str, str]) -> ProbeResult:
        decision = check_live_provider_policy(policy, env=env)
        if not decision.allowed:
            return ProbeResult(False, self.provider, str(policy.get("model") or ""), "; ".join(decision.reasons))
        result = probe_live_openrouter(policy, env=env)
        return ProbeResult(bool(result.get("ok")), str(result.get("provider") or self.provider), str(result.get("model") or policy.get("model") or ""), str(result.get("reason") or ""))

    def execute_json(self, request: ProviderRequestPayload) -> ProviderResponsePayload:
        raise RuntimeError("OpenRouter execution remains in workers.runtime")


class CodexRuntime:
    provider = "openai-codex"
    runtime = "codex"

    def validate_policy(self, policy: Mapping[str, object]) -> ProviderDecision:
        return check_provider_policy(ProviderRequest(self.provider, str(policy.get("model") or ""), allow_fallback=bool(policy.get("allow_fallback") or False)))

    def probe(self, policy: Mapping[str, object], env: Mapping[str, str]) -> ProbeResult:
        del env
        decision = self.validate_policy(policy)
        return ProbeResult(decision.allowed, self.provider, str(policy.get("model") or ""), decision.reason)

    def execute_json(self, request: ProviderRequestPayload) -> ProviderResponsePayload:
        raise RuntimeError("Codex execution remains in workers.runtime")


class AnthropicRuntime:
    provider = ANTHROPIC_PROVIDER
    runtime = ANTHROPIC_RUNTIME

    def validate_policy(self, policy: Mapping[str, object]) -> ProviderDecision:
        model = str(policy.get("model") or "")
        concrete = resolve_anthropic_model(model)
        if not concrete:
            return ProviderDecision(False, "reserved", "Anthropic provider profiles are reserved and require a concrete configured model before any future adapter can execute")
        return ProviderDecision(False, "reserved", "Anthropic provider profiles are scaffolded only and non-executable in this harness")

    def probe(self, policy: Mapping[str, object], env: Mapping[str, str]) -> ProbeResult:
        del env
        return ProbeResult(False, self.provider, str(policy.get("model") or ""), "Anthropic runtime is reserved and cannot probe by default")

    def execute_json(self, request: ProviderRequestPayload) -> ProviderResponsePayload:
        raise RuntimeError("Anthropic runtime is reserved and disabled by default")


def runtime_for_policy(policy: Mapping[str, object]) -> ProviderRuntime:
    provider = str(policy.get("provider") or "")
    runtime = str(policy.get("runtime") or provider)
    if provider == "openrouter" or runtime == "openrouter":
        return OpenRouterRuntime()
    if provider == "openai-codex" or runtime == "codex":
        return CodexRuntime()
    if provider == ANTHROPIC_PROVIDER or runtime == ANTHROPIC_RUNTIME:
        return AnthropicRuntime()
    raise ValueError(f"unknown provider runtime: {provider or runtime or 'unset'}")
