"""Adapter for Claude-Code-style command line coding engines."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from forge.audit.packet import EngineResult
from forge.engines.base import Engine
from forge.loop.task import TaskPacket
from forge.worktrees.diff import changed_files, collect_diff, deleted_files, new_files


class ClaudeCodeStyleEngine(Engine):
    def __init__(self, command: list[str], timeout_seconds: int = 2700) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds

    def run(self, task: TaskPacket, worktree: Path) -> EngineResult:
        if not self.command:
            return EngineResult(
                engine="claude_code_style",
                exit_code=2,
                stderr="No coding engine command configured. Set FORGE_ENGINE_COMMAND or pass --engine-command.",
            )
        prompt_path = worktree / ".forge_task.md"
        prompt = task.to_builder_prompt()
        prompt_path.write_text(prompt, encoding="utf-8")
        help_text = self._help_text(worktree)
        cmd = self._command_for_prompt(prompt_path, prompt, help_text)
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never", "PAGER": "cat"})
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, cwd=worktree, text=True, capture_output=True, timeout=self.timeout_seconds, env=env)
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            stderr = f"{stderr}\nEngine timed out after {self.timeout_seconds} seconds".strip()
        duration = time.monotonic() - start
        return EngineResult(
            engine="claude_code_style",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            changed_files=changed_files(worktree),
            new_files=new_files(worktree),
            deleted_files=deleted_files(worktree),
            git_diff=collect_diff(worktree),
        )

    def _help_text(self, worktree: Path) -> str:
        try:
            proc = subprocess.run([*self.command, "--help"], cwd=worktree, text=True, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return f"{proc.stdout}\n{proc.stderr}"

    def _command_for_prompt(self, prompt_path: Path, prompt: str, help_text: str) -> list[str]:
        if "--prompt-file" in help_text:
            cmd = [*self.command]
            if "--non-interactive" in help_text:
                cmd.append("--non-interactive")
            return [*cmd, "--prompt-file", str(prompt_path)]
        if "agent" in help_text and "--message" in help_text:
            return [*self.command, "agent", "--message", prompt, "--timeout", str(self.timeout_seconds)]
        if "-p" in help_text or "--print" in help_text:
            return [*self.command, "-p", prompt]
        return [*self.command, str(prompt_path)]
