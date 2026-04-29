"""Shell engine for explicit local dry integration tests."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time

from forge.audit.packet import EngineResult
from forge.engines.base import Engine
from forge.loop.task import TaskPacket
from forge.worktrees.diff import changed_files, collect_diff, deleted_files, new_files


class ShellEngine(Engine):
    def __init__(self, command: str, timeout_seconds: int = 900) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds

    def run(self, task: TaskPacket, worktree: Path) -> EngineResult:
        del task
        start = time.monotonic()
        proc = subprocess.run(self.command, cwd=worktree, text=True, capture_output=True, shell=True, timeout=self.timeout_seconds)
        return EngineResult(
            engine="shell",
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_seconds=time.monotonic() - start,
            changed_files=changed_files(worktree),
            new_files=new_files(worktree),
            deleted_files=deleted_files(worktree),
            git_diff=collect_diff(worktree),
        )
