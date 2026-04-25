"""Provider cost estimation."""

from __future__ import annotations

from atticus.providers.deepseek import cost_for_model


def estimate_cost_usd(
    *,
    provider: str,
    model: str,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    cost = cost_for_model(provider, model)
    return (
        (cache_hit_tokens / 1_000_000) * cost.input_cache_hit_per_million
        + (cache_miss_tokens / 1_000_000) * cost.input_cache_miss_per_million
        + (output_tokens / 1_000_000) * cost.output_per_million
    )
