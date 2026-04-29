"""Diff collection helpers."""

from __future__ import annotations

from pathlib import Path

from forge.audit.packet import DiffStats
from forge.worktrees.manager import run_git


def changed_files(worktree: Path) -> list[str]:
    proc = run_git(worktree, ["status", "--porcelain"], check=True)
    files: list[str] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip())
    return sorted(set(files))


def collect_diff(worktree: Path) -> str:
    _ = run_git(worktree, ["add", "-N", "."], check=False)
    staged = run_git(worktree, ["diff", "--cached", "--binary"], check=True)
    unstaged = run_git(worktree, ["diff", "--binary"], check=True)
    return "\n".join(part for part in [staged.stdout, unstaged.stdout] if part)


def staged_diff(worktree: Path) -> str:
    proc = run_git(worktree, ["diff", "--cached", "--binary"], check=True)
    return proc.stdout


def diff_stats(worktree: Path) -> DiffStats:
    _ = run_git(worktree, ["add", "-N", "."], check=False)
    staged = run_git(worktree, ["diff", "--cached", "--numstat"], check=True)
    unstaged = run_git(worktree, ["diff", "--numstat"], check=True)
    by_file: dict[str, tuple[int, int]] = {}
    for line in f"{staged.stdout}\n{unstaged.stdout}".splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = parts[2]
        current_added, current_deleted = by_file.get(path, (0, 0))
        if parts[0].isdigit():
            current_added += int(parts[0])
        if parts[1].isdigit():
            current_deleted += int(parts[1])
        by_file[path] = (current_added, current_deleted)
    return DiffStats(files_changed=len(by_file), lines_added=sum(item[0] for item in by_file.values()), lines_deleted=sum(item[1] for item in by_file.values()))


def deleted_files(worktree: Path) -> list[str]:
    proc = run_git(worktree, ["status", "--porcelain"], check=True)
    return sorted(line[3:].strip() for line in proc.stdout.splitlines() if line[:2].strip() == "D")


def new_files(worktree: Path) -> list[str]:
    proc = run_git(worktree, ["status", "--porcelain"], check=True)
    return sorted(line[3:].strip() for line in proc.stdout.splitlines() if line.startswith("??") or line.startswith(" A"))
