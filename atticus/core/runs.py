"""Run state primitives."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunState:
    run_id: str
    state: str
    reason: str = ""
