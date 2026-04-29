"""Task-scoped citation target allow-lists."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from typing import cast


def allowed_citation_targets_for_task(conn: sqlite3.Connection, *, task_id: str) -> dict[str, set[str]]:
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        return {}
    matter_scope = str(task["matter_scope"])
    source_ids = _string_list_from_json(task["source_dependencies_json"])
    artifact_ids = _string_list_from_json(task["artifact_dependencies_json"])
    return {
        "source": _ids_for_matter_subset(conn, "sources", "source_id", matter_scope, source_ids),
        "artifact": _ids_for_matter_subset(conn, "artifacts", "artifact_id", matter_scope, artifact_ids),
        "authority": _ids_for_matter(conn, "legal_authorities", "authority_id", matter_scope),
        "chronology_event": _ids_for_matter(conn, "chronology_events", "chronology_event_id", matter_scope),
        "claim": _ids_for_matter(conn, "claims", "claim_id", matter_scope),
        "memory": _ids_for_matter_if_exists(conn, "legal_memories", "memory_id", matter_scope),
        "validation_result": _validation_result_ids_for_matter(conn, matter_scope=matter_scope),
    }


def _string_list_from_json(raw: object) -> list[str]:
    value = json.loads(str(raw or "[]"))
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value) if isinstance(item, str)]


def _ids_for_matter(conn: sqlite3.Connection, table: str, column: str, matter_scope: str) -> set[str]:
    return {
        str(row[column])
        for row in conn.execute(f"SELECT {column} FROM {table} WHERE matter_scope = ?", (matter_scope,)).fetchall()
    }


def _ids_for_matter_subset(conn: sqlite3.Connection, table: str, column: str, matter_scope: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = conn.execute(
        f"SELECT {column} FROM {table} WHERE matter_scope = ? AND {column} IN (%s)" % ",".join("?" for _ in ids),
        (matter_scope, *ids),
    ).fetchall()
    return {str(row[column]) for row in rows}


def _ids_for_matter_if_exists(conn: sqlite3.Connection, table: str, column: str, matter_scope: str) -> set[str]:
    exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    if exists is None:
        return set()
    return _ids_for_matter(conn, table, column, matter_scope)


def _validation_result_ids_for_matter(conn: sqlite3.Connection, *, matter_scope: str) -> set[str]:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(validation_results)")}
    if "matter_scope" not in columns:
        return set()
    return {
        str(row["validation_result_id"])
        for row in conn.execute(
            "SELECT validation_result_id FROM validation_results WHERE matter_scope = ?",
            (matter_scope,),
        ).fetchall()
    }
