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
OPENROUTER_MODELS.update({model: _FREE_MODEL_COST for model in OPENROUTER_FREE_MODEL_ORDER})

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
