"""Read-only record inspection helpers."""

from __future__ import annotations

from collections.abc import Mapping
import json

from typing import cast
from atticus.db.repo import db_connection, source_material_derivatives


TABLES_BY_TYPE: dict[str, tuple[str, str]] = {
    "run": ("runs", "run_id"),
    "task": ("tasks", "task_id"),
    "source": ("sources", "source_id"),
    "artifact": ("artifacts", "artifact_id"),
    "candidate": ("candidate_outputs", "candidate_id"),
    "context-pack": ("context_packs", "context_pack_id"),
    "certification": ("certifications", "certification_id"),
}


def inspect_record(db_path: str, *, record_type: str, record_id: str) -> dict[str, object]:
    table_info = TABLES_BY_TYPE.get(record_type)
    if table_info is None:
        raise KeyError(f"unsupported inspect type: {record_type}")
    table, pk = table_info
    with db_connection(db_path, read_only=True) as conn:
        row = cast(Mapping[str, object] | None, cast(object, conn.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (record_id,)).fetchone()))
        derivatives = (
            source_material_derivatives(conn, matter_scope=str(row["matter_scope"]), source_ids=(record_id,)).get(record_id, [])
            if row is not None and record_type == "source"
            else []
        )
    if row is None:
        raise KeyError(f"{record_type} not found: {record_id}")
    summary = summarize_row(dict(row))
    if record_type == "source":
        summary["source_material_derivatives"] = derivatives
    return summary


def summarize_row(row: dict[str, object]) -> dict[str, object]:
    summarized: dict[str, object] = {}
    for key, value in row.items():
        if isinstance(value, str) and key.endswith("_json"):
            try:
                summarized[key[:-5]] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        summarized[key] = value
    return summarized
