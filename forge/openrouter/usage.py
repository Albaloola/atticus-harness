"""Usage metadata helpers."""

from __future__ import annotations

from collections.abc import Mapping


def cost_from_usage(usage: Mapping[str, object]) -> dict[str, object]:
    return {
        "prompt_tokens": _int(usage.get("prompt_tokens") or usage.get("input_tokens")),
        "completion_tokens": _int(usage.get("completion_tokens") or usage.get("output_tokens")),
        "cached_tokens": _cached_tokens(usage),
        "total_cost_usd": _float(usage.get("cost") or usage.get("total_cost") or 0.0),
    }


def _cached_tokens(usage: Mapping[str, object]) -> int:
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    if isinstance(details, Mapping):
        return _int(details.get("cached_tokens"))
    return 0


def _int(value: object) -> int:
    try:
        return int(str(value or 0))
    except ValueError:
        return 0


def _float(value: object) -> float:
    try:
        return float(str(value or 0.0))
    except ValueError:
        return 0.0
