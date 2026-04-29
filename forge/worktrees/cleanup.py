"""Cleanup wrappers."""

from __future__ import annotations

from pathlib import Path

from forge.worktrees.manager import remove_worktree


def cleanup_worktree(repo: Path, worktree: Path) -> dict[str, object]:
    return remove_worktree(repo, worktree)
