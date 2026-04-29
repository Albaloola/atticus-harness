"""Persistent Forge runtime state."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any


def forge_dir(repo: Path) -> Path:
    return repo / ".forge"


def state_path(repo: Path) -> Path:
    return forge_dir(repo) / "state.json"


def ensure_forge_dirs(repo: Path) -> None:
    base = forge_dir(repo)
    for path in [base, base / "memory", base / "audit", base / "worktrees"]:
        path.mkdir(parents=True, exist_ok=True)
    for name, content in _memory_defaults().items():
        target = base / "memory" / name
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    if not state_path(repo).exists():
        write_state(repo, default_state())


def default_state() -> dict[str, Any]:
    return {
        "running": False,
        "last_iteration": "",
        "current_task": None,
        "last_branch": "",
        "last_commit_sha": "",
        "consecutive_failures": 0,
        "cost_today": 0.0,
        "updated_at": now_iso(),
    }


def read_state(repo: Path) -> dict[str, Any]:
    ensure_forge_dirs(repo)
    try:
        data = json.loads(state_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = default_state()
    if not isinstance(data, dict):
        return default_state()
    return data


def write_state(repo: Path, data: dict[str, Any]) -> None:
    forge_dir(repo).mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = now_iso()
    state_path(repo).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def update_state(repo: Path, **updates: object) -> dict[str, Any]:
    data = read_state(repo)
    data.update(updates)
    write_state(repo, data)
    return data


def stop_file(repo: Path) -> Path:
    return forge_dir(repo) / "STOP"


def request_stop(repo: Path) -> None:
    ensure_forge_dirs(repo)
    stop_file(repo).write_text(f"stop requested at {now_iso()}\n", encoding="utf-8")
    update_state(repo, running=False)


def resume(repo: Path) -> None:
    try:
        stop_file(repo).unlink()
    except FileNotFoundError:
        pass
    update_state(repo, running=False)


def stop_requested(repo: Path) -> bool:
    return stop_file(repo).exists()


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _memory_defaults() -> dict[str, str]:
    return {
        "project_summary.md": "# Project Summary\n\nForge has not summarized this repository yet.\n",
        "architecture_notes.md": "# Architecture Notes\n\n",
        "backlog.json": "[]\n",
        "failed_attempts.json": "[]\n",
        "risky_files.md": "# Risky Files\n\n",
        "test_map.md": "# Test Map\n\n",
        "decisions.md": "# Decisions\n\n",
    }
