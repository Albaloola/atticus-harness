"""Archive path helpers."""

from __future__ import annotations

from pathlib import Path


def archive_exists(path: str | Path) -> bool:
    return Path(path).exists()
