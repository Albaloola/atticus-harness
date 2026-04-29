"""Command gate execution."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from forge.audit.packet import GateResult


def run_gate_commands(worktree: Path, commands: list[str], *, timeout_seconds: float = 900.0) -> list[GateResult]:
    results: list[GateResult] = []
    env = dict(os.environ)
    env.update(
        {
            "CI": "true",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            "PAGER": "cat",
        }
    )
    for command in commands:
        start = time.monotonic()
        try:
            proc = subprocess.run(command, cwd=worktree, text=True, capture_output=True, shell=True, timeout=timeout_seconds, env=env)
            duration = time.monotonic() - start
            results.append(
                GateResult(
                    name=f"command: {command}",
                    passed=proc.returncode == 0,
                    details="passed" if proc.returncode == 0 else f"exit {proc.returncode}",
                    command=command,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    duration_seconds=duration,
                )
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            results.append(
                GateResult(
                    name=f"command: {command}",
                    passed=False,
                    details=f"timed out after {timeout_seconds} seconds",
                    command=command,
                    stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                    stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
                    duration_seconds=duration,
                )
            )
    return results
