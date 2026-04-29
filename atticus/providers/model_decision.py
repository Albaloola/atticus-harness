"""Deterministic smart model selection for Atticus tasks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from atticus.providers.deepseek import is_held_openrouter_model, known_model


class ModelProfileLike(Protocol):
    @property
    def profile_id(self) -> str: ...
    @property
    def provider(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def runtime(self) -> str: ...
    @property
    def enabled(self) -> bool: ...
    @property
    def reserved(self) -> bool: ...


class ModelRoutingPolicyLike(Protocol):
    @property
    def profiles(self) -> Mapping[str, ModelProfileLike]: ...
    @property
    def default(self) -> str: ...
    @property
    def layers(self) -> Mapping[str, str]: ...
    @property
    def stages(self) -> Mapping[str, str]: ...
    @property
    def task_types(self) -> Mapping[str, str]: ...
    @property
    def task_ids(self) -> Mapping[str, str]: ...

    def as_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class ModelDecisionInput:
    matter_scope: str
    task_id: str
    stage: str
    task_type: str
    layer: str
    risk_level: str
    legal_complexity: str
    evidence_volume: str
    authority_required: bool
    hostile_review_required: bool
    drafting_finality: str
    contradiction_count: int
    unresolved_uncertainty_count: int
    source_count: int
    extracted_char_count: int
    expected_value: float
    requested_capabilities: tuple[str, ...]
    operator_override: str | None = None


@dataclass(frozen=True)
class ModelDecision:
    provider: str
    model: str
    runtime: str
    profile_id: str
    decision_reason: str
    decision_tier: str
    fallback_allowed: bool
    required_human_review: bool
    policy_fingerprint: str
    input_fingerprint: str


FLASH_TIER = "flash_worker"
PRO_TIER = "pro_orchestrator"
CODEX_TIER = "codex_exact"
ANTHROPIC_TIER = "anthropic_reserved"
BLOCKED_TIER = "blocked"

FLASH_TASK_TYPES = {
    "source_inventory",
    "extract",
    "extraction_qa",
    "classification",
    "duplicate_detection",
    "deduplication",
    "source_triage",
    "production_mapping",
    "evidence_organization_plan",
    "chronology_event_extraction",
    "chunk_retrieval",
    "index_building",
    "routine_redaction_scan",
    "candidate_packet_formatting",
    "followup",
}
PRO_TASK_TYPES = {
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
HIGH_RISK_STAGES = {"S5", "S6", "S7", "S8", "S9"}
HUMAN_REVIEW_STAGES = {"S8", "S9"}


def decide_model(policy: ModelRoutingPolicyLike, decision_input: ModelDecisionInput) -> ModelDecision:
    policy_fingerprint = fingerprint_mapping(policy.as_dict())
    input_fingerprint = fingerprint_mapping(_decision_input_dict(decision_input))
    explicit_profile = _explicit_profile(policy, decision_input)
    explicit_block = _explicit_profile_block_reason(explicit_profile)
    if explicit_block:
        return ModelDecision(
            provider="blocked",
            model="blocked",
            runtime="blocked",
            profile_id=explicit_profile.profile_id if explicit_profile is not None else "blocked",
            decision_reason=explicit_block,
            decision_tier=BLOCKED_TIER,
            fallback_allowed=False,
            required_human_review=True,
            policy_fingerprint=policy_fingerprint,
            input_fingerprint=input_fingerprint,
        )
    target_tier = _target_tier(decision_input, explicit_profile=explicit_profile)
    reason = _decision_reason(decision_input, target_tier=target_tier, explicit_profile=explicit_profile)
    if target_tier == FLASH_TIER and _pro_required(decision_input):
        reason = f"operator requested Flash for Pro-required work; human review required: {reason}"
        profile = _profile_for_tier(policy, FLASH_TIER)
        return _decision(profile, FLASH_TIER, reason, True, policy_fingerprint, input_fingerprint)
    profile = _profile_for_tier(policy, target_tier)
    if profile is None:
        return ModelDecision(
            provider="blocked",
            model="blocked",
            runtime="blocked",
            profile_id="blocked",
            decision_reason=f"no active safe profile for decision tier {target_tier}",
            decision_tier=BLOCKED_TIER,
            fallback_allowed=False,
            required_human_review=True,
            policy_fingerprint=policy_fingerprint,
            input_fingerprint=input_fingerprint,
        )
    required_human_review = decision_input.stage in HUMAN_REVIEW_STAGES or _unknown_material_input(decision_input)
    if target_tier == CODEX_TIER:
        required_human_review = False
    return _decision(profile, target_tier, reason, required_human_review, policy_fingerprint, input_fingerprint)


def fingerprint_mapping(value: Mapping[str, object]) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decision(
    profile: ModelProfileLike | None,
    tier: str,
    reason: str,
    required_human_review: bool,
    policy_fingerprint: str,
    input_fingerprint: str,
) -> ModelDecision:
    if profile is None or is_held_openrouter_model(profile.model):
        return ModelDecision("blocked", "blocked", "blocked", "blocked", "no non-held profile is available", BLOCKED_TIER, False, True, policy_fingerprint, input_fingerprint)
    return ModelDecision(
        provider=profile.provider,
        model=profile.model,
        runtime=profile.runtime,
        profile_id=profile.profile_id,
        decision_reason=reason,
        decision_tier=tier,
        fallback_allowed=False,
        required_human_review=required_human_review,
        policy_fingerprint=policy_fingerprint,
        input_fingerprint=input_fingerprint,
    )


def _target_tier(decision_input: ModelDecisionInput, *, explicit_profile: ModelProfileLike | None) -> str:
    override = (decision_input.operator_override or "").strip().lower()
    if override:
        if override in {"pro", PRO_TIER, "deepseek_pro_orchestrator", "deepseek_pro_or"}:
            return PRO_TIER
        if override in {"flash", FLASH_TIER, "deepseek_flash_worker", "deepseek_flash_or"}:
            return FLASH_TIER
        if override in {"codex", CODEX_TIER, "codex_gpt55_exact", "gpt55_codex"}:
            return CODEX_TIER
        if override in {"anthropic", ANTHROPIC_TIER}:
            return ANTHROPIC_TIER
        return BLOCKED_TIER
    if explicit_profile is not None and explicit_profile.provider == "openai-codex":
        return CODEX_TIER
    if _pro_required(decision_input):
        return PRO_TIER
    return FLASH_TIER


def _explicit_profile(policy: ModelRoutingPolicyLike, decision_input: ModelDecisionInput) -> ModelProfileLike | None:
    target = _resolve_route_target(policy, layer=decision_input.layer, stage=decision_input.stage, task_type=decision_input.task_type, task_id=decision_input.task_id)
    return policy.profiles.get(target)


def _resolve_route_target(
    policy: ModelRoutingPolicyLike,
    *,
    layer: str = "",
    stage: str = "",
    task_type: str = "",
    task_id: str = "",
) -> str:
    for routes, key in (
        (policy.task_ids, task_id),
        (policy.task_types, task_type),
        (policy.layers, layer),
        (policy.stages, stage),
    ):
        if key and key in routes:
            return routes[key]
    return policy.default


def _explicit_profile_block_reason(profile: ModelProfileLike | None) -> str:
    if profile is None:
        return ""
    if profile.provider in {"anthropic", "anthropic-oauth"}:
        return f"explicit route {profile.profile_id} uses reserved Anthropic provider surface"
    if not profile.enabled or profile.reserved:
        return f"explicit route {profile.profile_id} is disabled or reserved"
    if profile.provider == "openrouter" and is_held_openrouter_model(profile.model):
        return f"explicit route {profile.profile_id} uses held OpenRouter model {profile.model}"
    return ""


def _pro_required(decision_input: ModelDecisionInput) -> bool:
    if decision_input.layer in {"orchestrator", "reducer", "hostile_review", "verifier"}:
        return True
    if decision_input.stage in HIGH_RISK_STAGES:
        return True
    if decision_input.task_type in PRO_TASK_TYPES:
        return True
    if decision_input.authority_required or decision_input.hostile_review_required:
        return True
    if decision_input.contradiction_count > 0 or decision_input.unresolved_uncertainty_count > 0:
        return True
    if decision_input.drafting_finality.lower() in {"draft", "final", "filing", "filing_pack", "material"}:
        return True
    if decision_input.risk_level.lower() in {"high", "material", "critical"}:
        return True
    if decision_input.legal_complexity.lower() in {"high", "complex", "material"}:
        return True
    if decision_input.evidence_volume.lower() in {"high", "large"} or decision_input.source_count > 20 or decision_input.extracted_char_count > 200_000:
        return True
    if any(capability in {"authority_mapping", "hostile_review", "final_quality_gate", "legal_reasoning"} for capability in decision_input.requested_capabilities):
        return True
    return False


def _profile_for_tier(policy: ModelRoutingPolicyLike, tier: str) -> ModelProfileLike | None:
    candidates = list(policy.profiles.values())
    if tier == FLASH_TIER:
        return _first_active(candidates, provider="openrouter", model="deepseek/deepseek-v4-flash")
    if tier == PRO_TIER:
        return _first_active(candidates, provider="openrouter", model="deepseek/deepseek-v4-pro")
    if tier == CODEX_TIER:
        return _first_active(candidates, provider="openai-codex", model="gpt-5.5")
    return None


def _first_active(profiles: list[ModelProfileLike], *, provider: str, model: str) -> ModelProfileLike | None:
    for profile in profiles:
        if profile.provider == provider and profile.model == model and profile.enabled and not profile.reserved and known_model(provider, model):
            return profile
    return None


def _decision_reason(decision_input: ModelDecisionInput, *, target_tier: str, explicit_profile: ModelProfileLike | None) -> str:
    if decision_input.operator_override:
        return f"operator_override={decision_input.operator_override} selected {target_tier}"
    if target_tier == CODEX_TIER:
        return "code, schema, migration, or harness task requires exact Codex route"
    if target_tier == PRO_TIER:
        return "task requires high-end orchestration, legal reasoning, contradiction handling, authority work, drafting review, or final quality analysis"
    if explicit_profile is not None:
        return f"routine worker task selected Flash using profile {explicit_profile.profile_id}"
    return "routine worker task selected Flash"


def _unknown_material_input(decision_input: ModelDecisionInput) -> bool:
    return not decision_input.stage or not decision_input.task_type or decision_input.risk_level == "unknown"


def _decision_input_dict(decision_input: ModelDecisionInput) -> dict[str, object]:
    return {
        "matter_scope": decision_input.matter_scope,
        "task_id": decision_input.task_id,
        "stage": decision_input.stage,
        "task_type": decision_input.task_type,
        "layer": decision_input.layer,
        "risk_level": decision_input.risk_level,
        "legal_complexity": decision_input.legal_complexity,
        "evidence_volume": decision_input.evidence_volume,
        "authority_required": decision_input.authority_required,
        "hostile_review_required": decision_input.hostile_review_required,
        "drafting_finality": decision_input.drafting_finality,
        "contradiction_count": decision_input.contradiction_count,
        "unresolved_uncertainty_count": decision_input.unresolved_uncertainty_count,
        "source_count": decision_input.source_count,
        "extracted_char_count": decision_input.extracted_char_count,
        "expected_value": decision_input.expected_value,
        "requested_capabilities": list(decision_input.requested_capabilities),
        "operator_override": decision_input.operator_override,
    }
