"""Capacity helpers."""

from __future__ import annotations


def bounded_capacity(requested: int, hard_limit: int) -> int:
    return max(0, min(requested, hard_limit))
