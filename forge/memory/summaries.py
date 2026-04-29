"""Summary helpers."""

from __future__ import annotations

from pathlib import Path


def summarize_repo(repo: Path) -> str:
    parts = []
    for marker in ["pyproject.toml", "package.json", "Cargo.toml", "go.mod"]:
        if (repo / marker).exists():
            parts.append(marker)
    return f"Repository {repo} markers: {', '.join(parts) if parts else 'none'}"
