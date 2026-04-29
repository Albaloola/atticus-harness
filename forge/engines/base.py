"""Base engine adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from forge.audit.packet import EngineResult
from forge.loop.task import TaskPacket


class Engine(ABC):
    @abstractmethod
    def run(self, task: TaskPacket, worktree: Path) -> EngineResult:
        """Run exactly one bounded task inside an isolated worktree."""
