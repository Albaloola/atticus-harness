"""Local memory file updates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_decision(repo: Path, text: str) -> None:
    path = repo / ".forge" / "memory" / "decisions.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        _ = handle.write(f"\n{text}\n")


def append_failed_attempt(repo: Path, entry: dict[str, Any]) -> None:
    path = repo / ".forge" / "memory" / "failed_attempts.json"
    items: list[object]
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        items = parsed if isinstance(parsed, list) else []
    except (OSError, json.JSONDecodeError):
        items = []
    items.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items[-100:], indent=2, sort_keys=True) + "\n", encoding="utf-8")
