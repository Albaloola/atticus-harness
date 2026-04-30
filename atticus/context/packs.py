"""Deterministic context-pack generation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from dataclasses import dataclass
import hashlib
import json
import sqlite3

from atticus.context.sections import build_default_sections, estimate_tokens as _estimate_tokens
from atticus.context.token_budget import source_token_estimates_by_id
from atticus.db import repo
from atticus.skills.registry import skills_for_task


SOURCE_MATERIAL_TOTAL_CHARS = 20_000
SOURCE_MATERIAL_MIN_CHARS = 250
SOURCE_MATERIAL_MAX_CHARS = 6_000
SOURCE_MATERIAL_COMPACT_THRESHOLD = 25
BULK_SOURCE_CONTEXT_THRESHOLD = 40
BULK_SOURCE_MATERIAL_TOTAL_CHARS = 8_000
BULK_SOURCE_MATERIAL_MIN_CHARS = 80
BULK_SOURCE_CONTEXT_TASK_TYPES = {
    "evidence_issue_map",
    "evidence_organization_plan",
    "production_mapping",
    "source_inventory",
}


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
    token_budget: int = 32_000,
    persist: bool = True,
) -> ContextPack:
    task = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()))
    if task is None:
        raise KeyError(f"unknown task: {task_id}")

    source_ids = _load_string_list(task, "source_dependencies_json")
    explicit_artifact_ids = _load_string_list(task, "artifact_dependencies_json")
    task_dependency_ids = _load_string_list(task, "task_dependencies_json") if "task_dependencies_json" in task.keys() else []
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
    source_materials = _load_source_materials(
        conn,
        matter_scope=matter_scope,
        source_ids=source_ids,
        allowed_artifact_ids=explicit_artifact_ids,
        task_type=str(task["task_type"]),
    )
    compact_source_context = _compact_source_context(task_type=str(task["task_type"]), source_count=len(source_ids))
    token_estimates = source_token_estimates_by_id(conn, matter_scope=matter_scope, source_ids=source_ids)
    source_manifest = _source_manifest_rows(sources, compact=compact_source_context, token_estimates=token_estimates)
    artifact_ids = _dedupe_ordered(
        [
            *explicit_artifact_ids,
            *_artifact_ids_produced_by_task_dependencies(
                conn,
                matter_scope=matter_scope,
                task_dependency_ids=task_dependency_ids,
            ),
        ]
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
                sources=source_manifest,
                source_materials=source_materials,
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


def _artifact_ids_produced_by_task_dependencies(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    task_dependency_ids: list[str],
) -> list[str]:
    if not task_dependency_ids:
        return []
    rows = conn.execute(
        """
        SELECT artifact_id
        FROM artifacts
        WHERE matter_scope = ?
          AND stale = 0
          AND produced_by_task_id IN (%s)
        ORDER BY produced_by_task_id, created_at, artifact_id
        """ % ",".join("?" for _ in task_dependency_ids),
        (matter_scope, *task_dependency_ids),
    ).fetchall()
    return [str(row["artifact_id"]) for row in rows]


def _dedupe_ordered(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _load_source_materials(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_ids: list[str],
    allowed_artifact_ids: list[str],
    task_type: str = "",
) -> list[dict[str, object]]:
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT
          s.source_id,
          s.path AS source_path,
          s.source_type,
          s.sha256 AS source_sha256,
          s.size_bytes AS source_size_bytes,
          s.trust_status AS source_trust_status,
          s.imported_from AS source_imported_from,
          a.artifact_id,
          a.path,
          a.artifact_type,
          a.trust_status,
          a.sha256,
          a.title,
          a.content,
          er.extraction_id,
          er.method AS extraction_method,
          er.coverage_status AS extraction_coverage_status,
          er.confidence,
          er.metadata_json AS extraction_metadata_json,
          er.created_at AS extraction_created_at,
          ocr.ocr_id,
          ocr.engine AS ocr_engine
          , ocr.page_count AS ocr_page_count
          , ocr.coverage_status AS ocr_coverage_status
          , ocr.metadata_json AS ocr_metadata_json
          , ocr.created_at AS ocr_created_at
        FROM sources s
        JOIN artifact_sources af ON af.source_id = s.source_id
        JOIN artifacts a ON a.artifact_id = af.artifact_id
        LEFT JOIN extraction_records er ON er.source_id = s.source_id AND er.artifact_id = a.artifact_id
        LEFT JOIN ocr_records ocr ON ocr.source_id = s.source_id AND ocr.artifact_id = a.artifact_id
        WHERE s.matter_scope = ?
          AND a.matter_scope = ?
          AND s.source_id IN ({placeholders})
          AND a.artifact_type IN (
            'extracted_text',
            'extraction_record',
            'ocr_extract',
            'ocr_text',
            'transcription_record',
            'transcript'
          )
        ORDER BY
          s.source_id,
          CASE a.artifact_type
            WHEN 'extracted_text' THEN 0
            WHEN 'ocr_text' THEN 1
            WHEN 'ocr_extract' THEN 2
            WHEN 'transcript' THEN 3
            ELSE 4
          END,
          a.created_at DESC,
          a.artifact_id
        """,
        (matter_scope, matter_scope, *source_ids),
    ).fetchall()
    compact = _compact_source_context(task_type=task_type, source_count=len(source_ids))
    total_chars = BULK_SOURCE_MATERIAL_TOTAL_CHARS if compact else SOURCE_MATERIAL_TOTAL_CHARS
    min_chars = BULK_SOURCE_MATERIAL_MIN_CHARS if compact else SOURCE_MATERIAL_MIN_CHARS
    per_source_chars = max(
        min_chars,
        min(SOURCE_MATERIAL_MAX_CHARS, total_chars // max(1, len(source_ids))),
    )
    seen: set[str] = set()
    allowed_artifacts = set(allowed_artifact_ids)
    materials: list[dict[str, object]] = []
    for row in rows:
        source_id = str(row["source_id"])
        if source_id in seen:
            continue
        seen.add(source_id)
        content = str(row["content"] or "")
        excerpt = content[:per_source_chars]
        extraction_metadata = _json_object(str(row["extraction_metadata_json"] or "{}"))
        ocr_metadata = _json_object(str(row["ocr_metadata_json"] or "{}"))
        artifact_id = str(row["artifact_id"])
        if compact:
            materials.append(
                {
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "citation_target": {"target_type": "source", "target_id": source_id},
                    "artifact_citation_allowed": artifact_id in allowed_artifacts,
                    "artifact_type": row["artifact_type"],
                    "coverage_status": row["extraction_coverage_status"] or row["ocr_coverage_status"] or "available",
                    "confidence": row["confidence"] if row["confidence"] is not None else None,
                    "content_excerpt": excerpt,
                    "excerpt_truncated": len(content) > len(excerpt),
                }
            )
        else:
            materials.append(
                {
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "citation_target": {"target_type": "source", "target_id": source_id},
                    "artifact_citation_allowed": artifact_id in allowed_artifacts,
                    "source_provenance": {
                        "source_id": source_id,
                        "path": row["source_path"],
                        "source_type": row["source_type"],
                        "sha256": row["source_sha256"],
                        "size_bytes": row["source_size_bytes"],
                        "trust_status": row["source_trust_status"],
                        "imported_from": row["source_imported_from"] or "",
                    },
                    "extraction_provenance": {
                        "extraction_id": row["extraction_id"] or "",
                        "method": row["extraction_method"] or "artifact_text",
                        "tool": str(extraction_metadata.get("extractor") or extraction_metadata.get("extractor_tool") or row["extraction_method"] or "artifact_text"),
                        "performed_by": str(extraction_metadata.get("extracted_by") or "atticus.local_extraction"),
                        "coverage_status": row["extraction_coverage_status"] or "available",
                        "confidence": row["confidence"] if row["confidence"] is not None else None,
                        "created_at": row["extraction_created_at"] or "",
                        "source_path": extraction_metadata.get("source_path") or row["source_path"],
                        "output_path": extraction_metadata.get("output_path") or row["path"],
                        "text_sha256": extraction_metadata.get("text_sha256") or row["sha256"] or "",
                    },
                    "ocr_provenance": _ocr_provenance(row, ocr_metadata),
                    "path": row["path"],
                    "artifact_type": row["artifact_type"],
                    "trust_status": row["trust_status"],
                    "sha256": row["sha256"],
                    "title": row["title"],
                    "content_excerpt": excerpt,
                    "excerpt_chars": len(excerpt),
                    "available_chars": len(content),
                    "excerpt_truncated": len(content) > len(excerpt),
                    "extraction_method": row["extraction_method"] or "artifact_text",
                    "coverage_status": row["extraction_coverage_status"] or "available",
                    "confidence": row["confidence"] if row["confidence"] is not None else None,
                    "ocr_engine": row["ocr_engine"],
                }
            )
    return materials


def _compact_source_context(*, task_type: str, source_count: int) -> bool:
    if task_type.endswith("_bundle"):
        return True
    return source_count > SOURCE_MATERIAL_COMPACT_THRESHOLD or (
        source_count >= BULK_SOURCE_CONTEXT_THRESHOLD and task_type in BULK_SOURCE_CONTEXT_TASK_TYPES
    )


def _source_manifest_rows(sources: list[dict[str, object]], *, compact: bool, token_estimates: Mapping[str, object]) -> list[dict[str, object]]:
    if not compact:
        return [
            {
                **source,
                "estimated_source_tokens": getattr(token_estimates.get(str(source["source_id"])), "estimated_tokens", 0),
                "token_estimation_basis": getattr(token_estimates.get(str(source["source_id"])), "estimation_basis", "missing_estimate"),
            }
            for source in sources
        ]
    return [
        {
            "source_id": source["source_id"],
            "path": source["path"],
            "source_type": source["source_type"],
            "trust_status": source["trust_status"],
            "stale": source["stale"],
            "sha256_prefix": str(source["sha256"])[:16],
            "estimated_source_tokens": getattr(token_estimates.get(str(source["source_id"])), "estimated_tokens", 0),
            "token_estimation_basis": getattr(token_estimates.get(str(source["source_id"])), "estimation_basis", "missing_estimate"),
        }
        for source in sources
    ]


def _json_object(text: str) -> dict[str, object]:
    try:
        value = json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}


def _ocr_provenance(row: Mapping[str, object], metadata: Mapping[str, object]) -> dict[str, object] | None:
    if not row["ocr_id"] and not row["ocr_engine"]:
        return None
    return {
        "ocr_id": row["ocr_id"] or "",
        "engine": row["ocr_engine"] or "",
        "performed_by": str(metadata.get("extracted_by") or "atticus.local_extraction"),
        "page_count": row["ocr_page_count"] if row["ocr_page_count"] is not None else 0,
        "coverage_status": row["ocr_coverage_status"] or "",
        "created_at": row["ocr_created_at"] or "",
    }


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
