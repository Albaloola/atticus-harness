"""Task proposal entrypoints."""

from __future__ import annotations

from pathlib import Path

from forge.config import ForgeConfig
from forge.loop.harvest import harvest_tasks
from forge.loop.task import TaskPacket


def propose_tasks(repo: Path, config: ForgeConfig, *, limit: int = 10) -> list[TaskPacket]:
    return harvest_tasks(repo, config, limit=limit)
