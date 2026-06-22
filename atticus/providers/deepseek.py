"""DeepSeek V4 model policy constants.

Prices are intentionally data, not routing behavior. Provider policy lives in
atticus.providers.policy and remains independent from OpenClaw or any adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from atticus.providers.anthropic import known_anthropic_model


@dataclass(frozen=True)
class ModelCost:
    input_cache_hit_per_million: float
    input_cache_miss_per_million: float
    output_per_million: float
    context_tokens: int
    max_output_tokens: int
    normal_input_cache_hit_per_million: float | None = None
    normal_input_cache_miss_per_million: float | None = None
    normal_output_per_million: float | None = None
    discount_expires_utc: str | None = None


DEEPSEEK_DIRECT_MODELS: dict[str, ModelCost] = {
    "deepseek-v4-flash": ModelCost(0.028, 0.14, 0.28, 1_048_576, 384_000),
    "deepseek-v4-pro": ModelCost(
        0.03625,
        0.435,
        0.87,
        1_048_576,
        384_000,
        normal_input_cache_hit_per_million=0.145,
        normal_input_cache_miss_per_million=1.74,
        normal_output_per_million=3.48,
        discount_expires_utc="2026-05-05T15:59:00Z",
    ),
}

OPENROUTER_FREE_MODEL_ORDER = [
    "qwen/qwen3-coder:free",
    "openai/gpt-oss-120b:free",
    "inclusionai/ling-2.6-1t:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "z-ai/glm-4.5-air:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "minimax/minimax-m2.5:free",
    "tencent/hy3-preview:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "inclusionai/ling-2.6-flash:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
    "baidu/qianfan-ocr-fast:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-3-27b-it:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "google/gemma-3-12b-it:free",
]

_FREE_MODEL_COST = ModelCost(0.0, 0.0, 0.0, 0, 0)
OPENROUTER_MODELS: dict[str, ModelCost] = {
    "deepseek/deepseek-v4-flash": DEEPSEEK_DIRECT_MODELS["deepseek-v4-flash"],
    "deepseek/deepseek-v4-pro": DEEPSEEK_DIRECT_MODELS["deepseek-v4-pro"],
    **{model: _FREE_MODEL_COST for model in OPENROUTER_FREE_MODEL_ORDER},
}

CODEX_MODELS: dict[str, ModelCost] = {
    "gpt-5.5": ModelCost(0.0, 0.0, 0.0, 0, 0),
    "openai-codex/gpt-5.5": ModelCost(0.0, 0.0, 0.0, 0, 0),
}

FLASH_USE_CASES = {
    "triage",
    "indexing",
    "extraction_qa",
    "classification",
    "duplicate_detection",
    "preliminary_summary",
    "file_organization",
    "structured_extraction",
}

PRO_USE_CASES = {
    "legal_reasoning",
    "contradiction_analysis",
    "hostile_review",
    "synthesis",
    "reducer_decision",
    "high_risk_answer",
}

ENV_ENABLE_HELD_OPENROUTER_MODELS = "ATTICUS_ENABLE_HELD_OPENROUTER_MODELS"
ENV_ALLOW_HELD_MODELS_FOR_LIVE = "ATTICUS_ALLOW_HELD_MODELS_FOR_LIVE"

OPENROUTER_ACTIVE_MODELS: dict[str, ModelCost] = {
    "deepseek/deepseek-v4-flash": DEEPSEEK_DIRECT_MODELS["deepseek-v4-flash"],
    "deepseek/deepseek-v4-pro": DEEPSEEK_DIRECT_MODELS["deepseek-v4-pro"],
}

OPENROUTER_HELD_MODELS: dict[str, ModelCost] = {model: _FREE_MODEL_COST for model in OPENROUTER_FREE_MODEL_ORDER}


def _registry_has_model(model: str) -> bool:
    from atticus.providers.openrouter_registry import get_registry
    return get_registry().has_model(model)


def held_openrouter_models_enabled(*, env: Mapping[str, str] | None = None, live: bool = False) -> bool:
    del env, live
    return False


def is_held_openrouter_model(model: str) -> bool:
    del model
    return False


def known_model(provider: str, model: str, *, env: Mapping[str, str] | None = None, live: bool = False) -> bool:
    del live
    if provider == "openrouter":
        if model in OPENROUTER_MODELS:
            return True
        return _registry_has_model(model)
    if provider == "deepseek":
        return model in DEEPSEEK_DIRECT_MODELS
    if provider == "openai-codex":
        return model in CODEX_MODELS
    if provider in {"anthropic", "anthropic-oauth"}:
        return known_anthropic_model(model, env=env)
    return False


def pricing_from_registry(model_id: str) -> tuple[float, float] | None:
    from atticus.providers.openrouter_registry import pricing_from_registry as _pfr
    return _pfr(model_id)


def cost_for_model(provider: str, model: str) -> ModelCost:
    if provider == "openrouter":
        if model in OPENROUTER_MODELS:
            return OPENROUTER_MODELS[model]
        pricing = pricing_from_registry(model)
        if pricing is not None:
            prompt_pm, completion_pm = pricing
            from atticus.providers.openrouter_registry import get_registry
            entry = get_registry().get_by_id(model)
            ctx = entry.context_length if entry else 0
            max_tok = entry.max_completion_tokens if entry else 0
            return ModelCost(
                input_cache_hit_per_million=prompt_pm,
                input_cache_miss_per_million=prompt_pm,
                output_per_million=completion_pm,
                context_tokens=ctx,
                max_output_tokens=max_tok,
            )
        raise KeyError(f"unknown provider/model: {provider}/{model}")
    if provider == "deepseek":
        return DEEPSEEK_DIRECT_MODELS[model]
    if provider == "openai-codex":
        return CODEX_MODELS[model]
    raise KeyError(f"unknown provider/model: {provider}/{model}")
