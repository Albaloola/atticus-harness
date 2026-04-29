"""Harvest candidate tasks from local repository signals."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from forge.config import ForgeConfig
from forge.loop.task import TaskPacket


BACKLOG_RE = re.compile(r"^\s*-\s+\[ \]\s+(.+?)\s*$")


def harvest_tasks(repo: Path, config: ForgeConfig, *, limit: int = 10) -> list[TaskPacket]:
    tasks: list[TaskPacket] = []
    tasks.extend(_from_backlog(repo, config))
    if len(tasks) < limit:
        tasks.extend(_from_todos(repo, config, start=len(tasks) + 1))
    if not tasks:
        tasks.append(
            TaskPacket(
                id="T-0001",
                title="Document the next safe Forge improvement",
                reason="No backlog or TODO tasks were found, so Forge should create a small operator-visible backlog item instead of making broad code changes.",
                allowed_paths=["FORGE_BACKLOG.md"],
                forbidden_paths=config.forbidden_paths,
                required_checks=[],
                success_criteria=["FORGE_BACKLOG.md contains one concrete small future task.", "No source code or sensitive files are modified."],
                score=1.0,
            )
        )
    return tasks[:limit]


def _from_backlog(repo: Path, config: ForgeConfig) -> list[TaskPacket]:
    path = repo / "FORGE_BACKLOG.md"
    if not path.exists():
        return []
    tasks: list[TaskPacket] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = BACKLOG_RE.match(line)
        if not match:
            continue
        title = match.group(1).strip()
        tasks.append(
            TaskPacket(
                id=f"T-{len(tasks) + 1:04d}",
                title=title,
                reason=f"Operator backlog item from FORGE_BACKLOG.md line {index}.",
                allowed_paths=_default_allowed_paths(repo),
                forbidden_paths=config.forbidden_paths,
                required_checks=config.required_checks,
                success_criteria=["The backlog item is addressed with the smallest useful diff.", "Required checks pass.", "No forbidden paths change."],
                score=8.0,
            )
        )
    return tasks


def _from_todos(repo: Path, config: ForgeConfig, *, start: int) -> list[TaskPacket]:
    proc = subprocess.run(
        ["git", "grep", "-n", "-E", "TODO|FIXME|HACK|XXX", "--", ":!*.sqlite3", ":!*.sqlite3-shm", ":!*.sqlite3-wal"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode not in {0, 1}:
        return []
    tasks: list[TaskPacket] = []
    for line in proc.stdout.splitlines()[:10]:
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        rel_path, line_no, text = parts
        allowed = _allowed_for_file(rel_path)
        tasks.append(
            TaskPacket(
                id=f"T-{start + len(tasks):04d}",
                title=f"Address TODO in {rel_path}",
                reason=f"Found local maintenance marker at {rel_path}:{line_no}: {text.strip()[:160]}",
                allowed_paths=allowed,
                forbidden_paths=config.forbidden_paths,
                required_checks=config.required_checks,
                success_criteria=["The TODO/FIXME is resolved or clarified.", "The diff is focused on the referenced area.", "Required checks pass."],
                score=6.0,
            )
        )
    return tasks


def _allowed_for_file(rel_path: str) -> list[str]:
    path = Path(rel_path)
    parent = str(path.parent).replace(".", "") or "."
    allowed = [rel_path]
    if parent not in {"", "."}:
        allowed.append(f"{parent}/")
    if rel_path.endswith(".py"):
        allowed.append("tests/")
    if rel_path.endswith(".md"):
        allowed.append("docs/")
    return sorted(set(allowed))


def _default_allowed_paths(repo: Path) -> list[str]:
    paths = ["FORGE_BACKLOG.md", "README.md", "docs/", "tests/"]
    if (repo / "atticus").exists():
        paths.append("atticus/")
    if (repo / "forge").exists():
        paths.append("forge/")
    return paths
