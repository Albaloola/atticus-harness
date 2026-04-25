"""DeepSeek V4 model policy constants.

Prices are intentionally data, not routing behavior. Provider policy lives in
atticus.providers.policy and remains independent from OpenClaw or any adapter.
"""

from __future__ import annotations

from dataclasses import dataclass


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

OPENROUTER_MODELS: dict[str, ModelCost] = {
    "deepseek/deepseek-v4-flash": DEEPSEEK_DIRECT_MODELS["deepseek-v4-flash"],
    "deepseek/deepseek-v4-pro": DEEPSEEK_DIRECT_MODELS["deepseek-v4-pro"],
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


def known_model(provider: str, model: str) -> bool:
    if provider == "openrouter":
        return model in OPENROUTER_MODELS
    if provider == "deepseek":
        return model in DEEPSEEK_DIRECT_MODELS
    return False


def cost_for_model(provider: str, model: str) -> ModelCost:
    if provider == "openrouter":
        return OPENROUTER_MODELS[model]
    if provider == "deepseek":
        return DEEPSEEK_DIRECT_MODELS[model]
    raise KeyError(f"unknown provider/model: {provider}/{model}")
