"""Type-check gate compatibility module."""

from __future__ import annotations

from pathlib import Path
from forge.audit.packet import GateResult
from forge.gates.commands import run_gate_commands


def run_typecheck(worktree: Path, commands: list[str]) -> list[GateResult]:
    return run_gate_commands(worktree, commands)
