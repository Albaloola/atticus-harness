"""Stable cache prefix helpers."""

from __future__ import annotations

from atticus.providers.cache import stable_prefix_id


def context_pack_prefix_id(static_parts: list[str]) -> str:
    return stable_prefix_id(static_parts)
