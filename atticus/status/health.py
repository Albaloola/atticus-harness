"""Health check helpers."""

from __future__ import annotations

from pathlib import Path


def db_exists(path: str) -> bool:
    return Path(path).exists()
