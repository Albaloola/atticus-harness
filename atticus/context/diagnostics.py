"""Read-only legal context diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from typing import cast

from atticus.context.packs import build_context_pack
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def build_context_diagnostics(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    token_budget: int = 32_000,
) -> dict[str, object]:
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        raise KeyError(f"unknown task: {task_id}")
    pack = build_context_pack(conn, task_id=task_id, token_budget=token_budget, persist=False)
    source_ids = _string_list(task["source_dependencies_json"])
    artifact_ids = _string_list(task["artifact_dependencies_json"])
    stale_sources = [
        str(row["source_id"])
        for row in conn.execute(
            "SELECT source_id FROM sources WHERE source_id IN (%s) AND stale = 1 ORDER BY source_id" % ",".join("?" for _ in source_ids),
            tuple(source_ids),
        ).fetchall()
    ] if source_ids else []
    stale_artifacts = [
        str(row["artifact_id"])
        for row in conn.execute(
            "SELECT artifact_id FROM artifacts WHERE artifact_id IN (%s) AND stale = 1 ORDER BY artifact_id" % ",".join("?" for _ in artifact_ids),
            tuple(artifact_ids),
        ).fetchall()
    ] if artifact_ids else []
    sections = [
        {
            "name": section["name"],
            "kind": section["kind"],
            "priority": section["priority"],
            "cache_scope": section["cache_scope"],
            "estimated_tokens": section["estimated_tokens"],
            "fingerprint": str(section["fingerprint"])[:16],
            "inclusion_reason": section["inclusion_reason"],
            "exclusion_reason": section.get("exclusion_reason", ""),
        }
        for section in pack.sections
    ]
    return {
        "diagnostic_only": True,
        "context_pack_id": pack.context_pack_id,
        "fingerprint": pack.fingerprint,
        "task_id": task_id,
        "matter_scope": str(task["matter_scope"]),
        "estimated_tokens": pack.estimated_tokens,
        "token_budget": pack.token_budget,
        "sections": sections,
        "source_count": len(source_ids),
        "source_material_count": _section_count(pack.sections, "source_materials"),
        "artifact_count": len(artifact_ids),
        "authority_count": _matter_count(conn, "legal_authorities", str(task["matter_scope"])),
        "memory_count": _memory_count(conn, str(task["matter_scope"])),
        "validation_gates": _string_list(task["validation_gates_json"]),
        "missing_certifications": _missing_certifications(conn, task),
        "stale_sources": stale_sources,
        "stale_artifacts": stale_artifacts,
        "excluded_records": [],
        "attached_skills": _section_content(pack.sections, "attached_skills"),
        "available_tools": _section_content(pack.sections, "available_tools"),
        "result_schema_version": RESULT_PACKET_SCHEMA_VERSION,
    }


def _string_list(raw: object) -> list[str]:
    value = json.loads(str(raw or "[]"))
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _matter_count(conn: sqlite3.Connection, table: str, matter_scope: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE matter_scope = ?", (matter_scope,)).fetchone()
    return int(str(row["n"] if row else 0))


def _memory_count(conn: sqlite3.Connection, matter_scope: str) -> int:
    exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = 'legal_memories'").fetchone()
    if exists is None:
        return 0
    return _matter_count(conn, "legal_memories", matter_scope)


def _missing_certifications(conn: sqlite3.Connection, task: Mapping[str, object]) -> list[dict[str, object]]:
    required = json.loads(str(task["required_certifications_json"] or "[]"))
    if not isinstance(required, list):
        return []
    missing: list[dict[str, object]] = []
    for item in required:
        if not isinstance(item, Mapping):
            continue
        subject_type = str(item.get("subject_type") or "")
        subject_id = str(item.get("subject_id") or "")
        certification_type = str(item.get("certification_type") or "")
        row = conn.execute(
            """
            SELECT 1 FROM certifications
            WHERE subject_type = ? AND subject_id = ? AND certification_type = ? AND status = 'active'
            LIMIT 1
            """,
            (subject_type, subject_id, certification_type),
        ).fetchone()
        if row is None:
            missing.append(
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "certification_type": certification_type,
                }
            )
    return missing


def _section_content(sections: list[dict[str, object]], name: str) -> object:
    for section in sections:
        if section.get("name") == name:
            return section.get("content")
    return []


def _section_count(sections: list[dict[str, object]], name: str) -> int:
    content = _section_content(sections, name)
    return len(content) if isinstance(content, list) else 0
