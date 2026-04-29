"""DeepSeek V4 model policy constants.

Prices are intentionally data, not routing behavior. Provider policy lives in
atticus.providers.policy and remains independent from OpenClaw or any adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import os

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

ENV_ENABLE_HELD_OPENROUTER_MODELS = "ATTICUS_ENABLE_HELD_OPENROUTER_MODELS"
ENV_ALLOW_HELD_MODELS_FOR_LIVE = "ATTICUS_ALLOW_HELD_MODELS_FOR_LIVE"


OPENROUTER_ACTIVE_MODELS: dict[str, ModelCost] = {
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
OPENROUTER_HELD_MODELS: dict[str, ModelCost] = {model: _FREE_MODEL_COST for model in OPENROUTER_FREE_MODEL_ORDER}
OPENROUTER_MODELS: dict[str, ModelCost] = {**OPENROUTER_ACTIVE_MODELS, **OPENROUTER_HELD_MODELS}

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


def held_openrouter_models_enabled(*, env: Mapping[str, str] | None = None, live: bool = False) -> bool:
    env = env if env is not None else os.environ
    enabled = env.get(ENV_ENABLE_HELD_OPENROUTER_MODELS) == "1"
    if not enabled:
        return False
    if live and env.get(ENV_ALLOW_HELD_MODELS_FOR_LIVE) != "1":
        return False
    return True


def is_held_openrouter_model(model: str) -> bool:
    return model in OPENROUTER_HELD_MODELS


def known_model(provider: str, model: str, *, env: Mapping[str, str] | None = None, live: bool = False) -> bool:
    if provider == "openrouter":
        if model in OPENROUTER_ACTIVE_MODELS:
            return True
        return model in OPENROUTER_HELD_MODELS and held_openrouter_models_enabled(env=env, live=live)
    if provider == "deepseek":
        return model in DEEPSEEK_DIRECT_MODELS
    if provider == "openai-codex":
        return model in CODEX_MODELS
    if provider in {"anthropic", "anthropic-oauth"}:
        return known_anthropic_model(model, env=env)
    return False


def cost_for_model(provider: str, model: str) -> ModelCost:
    if provider == "openrouter":
        return OPENROUTER_MODELS[model]
    if provider == "deepseek":
        return DEEPSEEK_DIRECT_MODELS[model]
    if provider == "openai-codex":
        return CODEX_MODELS[model]
    raise KeyError(f"unknown provider/model: {provider}/{model}")
