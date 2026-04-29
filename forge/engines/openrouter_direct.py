"""Placeholder direct OpenRouter engine adapter.

Direct OpenRouter can plan and review, but it cannot safely edit files without a
tool runner. Forge keeps it as an adapter slot for future tool-enabled engines.
"""

from __future__ import annotations

from pathlib import Path

from forge.audit.packet import EngineResult
from forge.engines.base import Engine
from forge.loop.task import TaskPacket


class OpenRouterDirectEngine(Engine):
    def run(self, task: TaskPacket, worktree: Path) -> EngineResult:
        del task, worktree
        return EngineResult(engine="openrouter_direct", exit_code=2, stderr="openrouter_direct cannot edit files yet; use a coding-engine adapter")
