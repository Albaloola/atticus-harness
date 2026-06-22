"""Per-model usage and session cost tracker.

Ported from Claude Code's cost-tracker.ts patterns.
Tracks token usage, cache hits, web search requests, lines changed,
and computes cost estimates per model — all session-persistable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelUsage:
    """Immutable record of token / usage metrics for one model."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_search_requests: int = 0
    lines_changed: int = 0
    cost_usd: float = 0.0


EMPTY_USAGE: ModelUsage = ModelUsage()


def accumulate_usage(total: ModelUsage, current: ModelUsage) -> ModelUsage:
    """Return a new ModelUsage with every field of *current* added to *total*."""
    return ModelUsage(
        input_tokens=total.input_tokens + current.input_tokens,
        output_tokens=total.output_tokens + current.output_tokens,
        cache_read_tokens=total.cache_read_tokens + current.cache_read_tokens,
        cache_write_tokens=total.cache_write_tokens + current.cache_write_tokens,
        web_search_requests=total.web_search_requests + current.web_search_requests,
        lines_changed=total.lines_changed + current.lines_changed,
        cost_usd=total.cost_usd + current.cost_usd,
    )


COST_PER_MODEL: dict[str, dict[str, float]] = {
    "deepseek/deepseek-v4-flash": {"input": 0.00027, "output": 0.00110},
    "deepseek/deepseek-v4-pro": {"input": 0.00055, "output": 0.00219},
    "openai-codex/gpt-5.5": {"input": 0.00055, "output": 0.00219},
    "anthropic/opus": {"input": 0.015, "output": 0.075},
}


class CostTracker:
    """Per-model usage and monetary cost tracker for a session.

    Accumulates token metrics per model identifier and estimates cost
    from ``COST_PER_MODEL``.  Fully serialisable via ``to_dict()`` so
    sessions can be snapshotted and restored.
    """

    __slots__ = ("usage_by_model", "total_cost_usd", "total_api_duration_ms")

    def __init__(self) -> None:
        self.usage_by_model: dict[str, ModelUsage] = {}
        self.total_cost_usd: float = 0.0
        self.total_api_duration_ms: float = 0.0

    def add_usage(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> None:
        """Record token consumption and update the running cost.

        Models not listed in ``COST_PER_MODEL`` accrue zero cost but are
        still tracked for metrics purposes.
        """
        current = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        existing = self.usage_by_model.get(model, EMPTY_USAGE)
        self.usage_by_model[model] = accumulate_usage(existing, current)

        rates = COST_PER_MODEL.get(model)
        if rates is not None:
            inc_input_cost = (input_tokens / 1000.0) * rates["input"]
            inc_output_cost = (output_tokens / 1000.0) * rates["output"]
            self.total_cost_usd += inc_input_cost + inc_output_cost

    def track_total_cost(self, cost_usd: float) -> None:
        self.total_cost_usd += cost_usd

    def track_total_api_duration(self, duration_ms: float) -> None:
        self.total_api_duration_ms += duration_ms

    def get_model_usage(self) -> dict[str, ModelUsage]:
        return dict(self.usage_by_model)

    def get_total_cost(self) -> float:
        return self.total_cost_usd

    def get_total_api_duration(self) -> float:
        return self.total_api_duration_ms

    def reset(self) -> None:
        self.usage_by_model.clear()
        self.total_cost_usd = 0.0
        self.total_api_duration_ms = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "usage_by_model": {
                model: {
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_tokens": u.cache_read_tokens,
                    "cache_write_tokens": u.cache_write_tokens,
                    "web_search_requests": u.web_search_requests,
                    "lines_changed": u.lines_changed,
                    "cost_usd": u.cost_usd,
                }
                for model, u in self.usage_by_model.items()
            },
            "total_cost_usd": self.total_cost_usd,
            "total_api_duration_ms": self.total_api_duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], /) -> CostTracker:
        tracker = cls()
        for model, fields in data.get("usage_by_model", {}).items():
            tracker.usage_by_model[model] = ModelUsage(
                input_tokens=fields.get("input_tokens", 0),
                output_tokens=fields.get("output_tokens", 0),
                cache_read_tokens=fields.get("cache_read_tokens", 0),
                cache_write_tokens=fields.get("cache_write_tokens", 0),
                web_search_requests=fields.get("web_search_requests", 0),
                lines_changed=fields.get("lines_changed", 0),
                cost_usd=fields.get("cost_usd", 0.0),
            )
        tracker.total_cost_usd = float(data.get("total_cost_usd", 0.0))
        tracker.total_api_duration_ms = float(data.get("total_api_duration_ms", 0.0))
        return tracker


_cost_tracker: CostTracker = CostTracker()


def get_tracker() -> CostTracker:
    return _cost_tracker


def set_tracker(tracker: CostTracker) -> None:
    global _cost_tracker
    _cost_tracker = tracker


def reset_cost_tracker() -> None:
    global _cost_tracker
    _cost_tracker = CostTracker()


def get_total_api_duration() -> float:
    return _cost_tracker.total_api_duration_ms


def restore_cost_state(cost_data: dict[str, Any]) -> None:
    global _cost_tracker
    _cost_tracker = CostTracker.from_dict(cost_data)
