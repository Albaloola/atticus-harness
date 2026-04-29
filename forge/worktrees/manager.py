"""Safe git worktree management for Forge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import shutil
import subprocess


class WorktreeError(RuntimeError):
    """Raised when Forge cannot safely manage a git worktree."""


@dataclass(frozen=True)
class WorktreeInfo:
    branch_name: str
    path: Path


def run_git(repo: Path, args: list[str], *, timeout: float = 120.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_MASTER": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            "GIT_EDITOR": ":",
            "GIT_PAGER": "cat",
        }
    )
    proc = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, timeout=timeout, env=env)
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def ensure_git_repo(repo: Path) -> None:
    if not repo.exists():
        raise WorktreeError(f"target repo does not exist: {repo}")
    proc = run_git(repo, ["rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        raise WorktreeError(f"target path is not a git repository: {repo}")
    root = Path(proc.stdout.strip()).resolve()
    if root != repo.resolve():
        raise WorktreeError(f"--repo must point to the git root ({root}), not {repo}")


def ensure_clean_main_worktree(repo: Path) -> None:
    proc = run_git(repo, ["status", "--porcelain"], check=True)
    dirty = [line for line in proc.stdout.splitlines() if line and not line.endswith(" .forge/")]
    if dirty:
        preview = "\n".join(dirty[:20])
        raise WorktreeError(f"target repo working tree is not clean; refusing to run on main checkout:\n{preview}")


def current_branch(repo: Path) -> str:
    proc = run_git(repo, ["branch", "--show-current"], check=True)
    branch = proc.stdout.strip()
    return branch or "HEAD"


def create_worktree(repo: Path, *, task_title: str) -> WorktreeInfo:
    date = datetime.now(UTC).strftime("%Y%m%d")
    slug = slugify(task_title)
    sequence = next_sequence(repo, date)
    component = f"{date}-{sequence:03d}-{slug}"
    branch_name = f"forge/{component}"
    validate_branch_name(repo, branch_name)
    worktree_path = repo / ".forge" / "worktrees" / component
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        raise WorktreeError(f"worktree path already exists: {worktree_path}")
    run_git(repo, ["worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"], timeout=180.0)
    return WorktreeInfo(branch_name=branch_name, path=worktree_path)


def remove_worktree(repo: Path, worktree: Path) -> dict[str, object]:
    removed = False
    error = ""
    try:
        proc = run_git(repo, ["worktree", "remove", "--force", str(worktree)], timeout=180.0, check=False)
        if proc.returncode == 0:
            removed = True
        else:
            error = proc.stderr.strip() or proc.stdout.strip()
    except Exception as exc:  # pragma: no cover - defensive cleanup
        error = str(exc)
    if worktree.exists():
        try:
            shutil.rmtree(worktree)
            removed = True
        except OSError as exc:
            error = f"{error}; filesystem cleanup failed: {exc}" if error else f"filesystem cleanup failed: {exc}"
    _ = run_git(repo, ["worktree", "prune"], timeout=120.0, check=False)
    return {"removed": removed, "error": error}


def next_sequence(repo: Path, date: str) -> int:
    proc = run_git(repo, ["branch", "--list", f"forge/{date}-*"], check=True)
    highest = 0
    for line in proc.stdout.splitlines():
        match = re.search(rf"forge/{re.escape(date)}-(\d{{3}})-", line)
        if match:
            highest = max(highest, int(match.group(1)))
    audit_root = repo / ".forge" / "worktrees"
    if audit_root.exists():
        for path in audit_root.glob(f"{date}-[0-9][0-9][0-9]-*"):
            parts = path.name.split("-", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                highest = max(highest, int(parts[1]))
    return highest + 1


def validate_branch_name(repo: Path, branch_name: str) -> None:
    proc = run_git(repo, ["check-ref-format", "--branch", branch_name], check=False)
    if proc.returncode != 0:
        raise WorktreeError(f"invalid branch name {branch_name!r}: {proc.stderr.strip()}")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug:
        slug = "task"
    return slug[:48].strip("-") or "task"
