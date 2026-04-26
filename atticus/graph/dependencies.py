"""Dependency helpers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def task_dependencies(task_row: sqlite3.Row) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    return (
        json.loads(task_row["source_dependencies_json"]),
        json.loads(task_row["artifact_dependencies_json"]),
        json.loads(task_row["required_certifications_json"]),
    )
