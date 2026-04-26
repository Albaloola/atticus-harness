"""Read-only record inspection helpers."""

from __future__ import annotations

import json
from typing import Any

from atticus.db.repo import db_connection


TABLES_BY_TYPE: dict[str, tuple[str, str]] = {
    "run": ("runs", "run_id"),
    "task": ("tasks", "task_id"),
    "source": ("sources", "source_id"),
    "artifact": ("artifacts", "artifact_id"),
    "candidate": ("candidate_outputs", "candidate_id"),
    "context-pack": ("context_packs", "context_pack_id"),
    "certification": ("certifications", "certification_id"),
}


def inspect_record(db_path: str, *, record_type: str, record_id: str) -> dict[str, Any]:
    table_info = TABLES_BY_TYPE.get(record_type)
    if table_info is None:
        raise KeyError(f"unsupported inspect type: {record_type}")
    table, pk = table_info
    with db_connection(db_path, read_only=True) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (record_id,)).fetchone()
    if row is None:
        raise KeyError(f"{record_type} not found: {record_id}")
    return summarize_row(dict(row))


def summarize_row(row: dict[str, Any]) -> dict[str, Any]:
    summarized: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, str) and key.endswith("_json"):
            try:
                summarized[key[:-5]] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        summarized[key] = value
    return summarized
