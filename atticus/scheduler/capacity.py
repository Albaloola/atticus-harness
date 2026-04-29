"""Capacity helpers."""

from __future__ import annotations


MAX_PARALLEL_AGENT_CAPACITY = 15


def bounded_capacity(requested: int, hard_limit: int = MAX_PARALLEL_AGENT_CAPACITY) -> int:
    return max(0, min(requested, hard_limit))


def agent_capacity(requested: int) -> int:
    """Return the effective worker/orchestrator capacity for one supervisor tick."""

    return bounded_capacity(requested, MAX_PARALLEL_AGENT_CAPACITY)
