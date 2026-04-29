"""Backlog memory helpers."""

from __future__ import annotations

import json
from pathlib import Path


def read_backlog(repo: Path) -> list[dict[str, object]]:
    path = repo / ".forge" / "memory" / "backlog.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def write_backlog(repo: Path, items: list[dict[str, object]]) -> None:
    path = repo / ".forge" / "memory" / "backlog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
