"""Deterministic context-pack generation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from dataclasses import dataclass
import hashlib
import json
import sqlite3

from atticus.context.sections import build_default_sections, estimate_tokens as _estimate_tokens
from atticus.db import repo
from atticus.skills.registry import skills_for_task


@dataclass(frozen=True)
class ContextPack:
    context_pack_id: str
    fingerprint: str
    sections: list[dict[str, object]]
    token_budget: int
    estimated_tokens: int

    def as_dict(self) -> dict[str, object]:
        return {
            "context_pack_id": self.context_pack_id,
            "fingerprint": self.fingerprint,
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "sections": self.sections,
        }


def canonicalize_sections(sections: list[dict[str, object]]) -> str:
    return json.dumps(sections, sort_keys=True, separators=(",", ":"))


def fingerprint_sections(sections: list[dict[str, object]]) -> str:
    return hashlib.sha256(canonicalize_sections(sections).encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    return _estimate_tokens(text)


def build_context_pack(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    pack_type: str = "work_order",
    token_budget: int = 16_000,
    persist: bool = True,
) -> ContextPack:
    task = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()))
    if task is None:
        raise KeyError(f"unknown task: {task_id}")

    source_ids = _load_string_list(task, "source_dependencies_json")
    artifact_ids = _load_string_list(task, "artifact_dependencies_json")
    matter_scope = str(task["matter_scope"])
    source_rows = cast(list[Mapping[str, object]], conn.execute(
            """
            SELECT source_id, path, source_type, sha256, trust_status, stale
            FROM sources
            WHERE source_id IN (%s) AND matter_scope = ?
            ORDER BY source_id
            """ % ",".join("?" for _ in source_ids),
            (*source_ids, matter_scope),
        ).fetchall()) if source_ids else []
    sources = [dict(row) for row in source_rows]
    _require_all_dependencies_present(
        requested=source_ids,
        found=[str(row["source_id"]) for row in sources],
        record_type="source",
        matter_scope=matter_scope,
    )
    artifact_rows = cast(list[Mapping[str, object]], conn.execute(
            """
            SELECT artifact_id, path, artifact_type, trust_status, stale, title, content
            FROM artifacts
            WHERE artifact_id IN (%s) AND matter_scope = ?
            ORDER BY artifact_id
            """ % ",".join("?" for _ in artifact_ids),
            (*artifact_ids, matter_scope),
        ).fetchall()) if artifact_ids else []
    artifacts = [
        {
            "artifact_id": row["artifact_id"],
            "path": row["path"],
            "artifact_type": row["artifact_type"],
            "trust_status": row["trust_status"],
            "stale": row["stale"],
            "title": row["title"],
            "content_excerpt": str(row["content"] or "")[:2_000],
        }
        for row in artifact_rows
    ]
    _require_all_dependencies_present(
        requested=artifact_ids,
        found=[str(row["artifact_id"]) for row in artifacts],
        record_type="artifact",
        matter_scope=matter_scope,
    )

    authority_rows = cast(list[Mapping[str, object]], conn.execute(
        """
        SELECT authority_id, jurisdiction, citation, authority_type, title, status, source_url
        FROM legal_authorities
        WHERE matter_scope = ? AND status != 'rejected'
        ORDER BY authority_id
        """,
        (matter_scope,),
    ).fetchall())
    authorities = [dict(row) for row in authority_rows]
    memory_index = _load_memory_index(conn, matter_scope=matter_scope)
    skills = [
        skill.as_work_order_context()
        for skill in skills_for_task(
            task_type=str(task["task_type"]),
            stage=str(task["stage"]),
            title=str(task["title"]),
        )
    ]
    tools = _available_tool_context()
    sections = [
        section.as_dict()
        for section in build_default_sections(
            task=dict(task),
            sources=sources,
            artifacts=artifacts,
            authorities=authorities,
            memory_index=memory_index,
            skills=skills,
            tools=tools,
        )
        if not section.exclusion_reason
    ]

    sections.sort(key=lambda section: (-_section_priority(section), str(section["name"])))

    canonical = canonicalize_sections(sections)
    estimated = estimate_tokens(canonical)
    if estimated > token_budget:
        raise ValueError(f"context pack exceeds token budget: estimated {estimated} > budget {token_budget}")
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    context_pack_id = f"ctx-{fingerprint[:24]}"
    pack = ContextPack(context_pack_id, fingerprint, sections, token_budget, estimated)
    if persist:
        _ = repo.add_context_pack(
            conn,
            context_pack_id=context_pack_id,
            matter_scope=matter_scope,
            task_id=task_id,
            pack_type=pack_type,
            fingerprint=fingerprint,
            token_budget=token_budget,
            estimated_tokens=estimated,
            sections=sections,
        )
    return pack


def _load_memory_index(conn: sqlite3.Connection, *, matter_scope: str) -> list[dict[str, object]]:
    exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = 'legal_memories'").fetchone()
    if exists is None:
        return []
    return [
        {
            "memory_id": row["memory_id"],
            "type": row["type"],
            "name": row["name"],
            "description": row["description"],
            "status": row["status"],
            "confidence": row["confidence"],
            "stale": bool(row["stale"]),
        }
        for row in conn.execute(
            """
            SELECT memory_id, type, name, description, status, confidence, stale
            FROM legal_memories
            WHERE matter_scope = ? AND status = 'active'
            ORDER BY type, name, memory_id
            """,
            (matter_scope,),
        )
    ]


def _section_priority(section: Mapping[str, object]) -> int:
    value = section.get("priority")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError(f"context section has non-integer priority: {value!r}")


def _available_tool_context() -> list[dict[str, object]]:
    # Keep this catalog import-free so context generation cannot cycle through
    # tool implementations that themselves build context packs.
    return [
        {"name": "SearchLegalMemory", "read_only": True, "destructive": False, "concurrency_safe": True},
        {"name": "InspectRecord", "read_only": True, "destructive": False, "concurrency_safe": True},
        {"name": "BuildContextPack", "read_only": True, "destructive": False, "concurrency_safe": True},
        {"name": "ValidateCitation", "read_only": True, "destructive": False, "concurrency_safe": True},
        {"name": "ListMatterArtifacts", "read_only": True, "destructive": False, "concurrency_safe": True},
        {"name": "ListMatterSources", "read_only": True, "destructive": False, "concurrency_safe": True},
        {"name": "ExplainValidationGate", "read_only": True, "destructive": False, "concurrency_safe": True},
        {"name": "ReadDraftArtifact", "read_only": True, "destructive": False, "concurrency_safe": True},
        {"name": "RecordCandidate", "read_only": False, "destructive": False, "concurrency_safe": False},
        {"name": "ReduceCandidate", "read_only": False, "destructive": False, "concurrency_safe": False},
        {"name": "RejectCandidate", "read_only": False, "destructive": False, "concurrency_safe": False},
        {"name": "WriteDraftArtifact", "read_only": False, "destructive": False, "concurrency_safe": False},
        {"name": "EditDraftArtifact", "read_only": False, "destructive": True, "concurrency_safe": False},
        {"name": "MarkMemoryStale", "read_only": False, "destructive": False, "concurrency_safe": True},
        {"name": "CreateProposedTask", "read_only": False, "destructive": False, "concurrency_safe": True},
    ]


def _load_string_list(task: Mapping[str, object], field: str) -> list[str]:
    value = _load_json_value(str(task[field] or "[]"))
    if not isinstance(value, list):
        raise ValueError(f"{field} for task {task['task_id']} must be a JSON array")
    items: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field}[{index}] for task {task['task_id']} must be a non-empty string")
        items.append(item)
    return items


def _load_json_value(text: str) -> object:
    return json.loads(text)


def _require_all_dependencies_present(*, requested: list[str], found: list[str], record_type: str, matter_scope: str) -> None:
    missing = sorted(set(requested) - set(found))
    if missing:
        raise ValueError(
            f"context pack missing or unauthorized {record_type} dependencies for matter {matter_scope}: {', '.join(missing)}"
        )
