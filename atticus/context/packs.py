"""Deterministic context-pack generation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any

from atticus.db import repo


@dataclass(frozen=True)
class ContextPack:
    context_pack_id: str
    fingerprint: str
    sections: list[dict[str, Any]]
    token_budget: int
    estimated_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_pack_id": self.context_pack_id,
            "fingerprint": self.fingerprint,
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "sections": self.sections,
        }


def canonicalize_sections(sections: list[dict[str, Any]]) -> str:
    return json.dumps(sections, sort_keys=True, separators=(",", ":"))


def fingerprint_sections(sections: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonicalize_sections(sections).encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def build_context_pack(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    pack_type: str = "work_order",
    token_budget: int = 16_000,
    persist: bool = True,
) -> ContextPack:
    task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        raise KeyError(f"unknown task: {task_id}")

    source_ids = _load_string_list(task, "source_dependencies_json")
    artifact_ids = _load_string_list(task, "artifact_dependencies_json")
    required_certs = json.loads(task["required_certifications_json"])

    sections: list[dict[str, Any]] = [
        {
            "name": "stable_prefix",
            "kind": "system",
            "content": (
                "Atticus is the durable source of truth. Workers produce candidate packets only. "
                "Reducers write canonical legal memory after validation. External legal actions are blocked."
            ),
        },
        {
            "name": "task_contract",
            "kind": "task",
            "content": {
                "task_id": task["task_id"],
                "title": task["title"],
                "stage": task["stage"],
                "task_type": task["task_type"],
                "matter_scope": task["matter_scope"],
                "validation_gates": json.loads(task["validation_gates_json"]),
                "required_certifications": required_certs,
                "provider_policy": json.loads(task["provider_policy_json"]),
            },
        },
    ]

    sources = [
        dict(row)
        for row in conn.execute(
            """
            SELECT source_id, path, source_type, sha256, trust_status, stale
            FROM sources
            WHERE source_id IN (%s) AND matter_scope = ?
            ORDER BY source_id
            """ % ",".join("?" for _ in source_ids),
            (*source_ids, task["matter_scope"]),
        )
    ] if source_ids else []
    _require_all_dependencies_present(
        requested=source_ids,
        found=[row["source_id"] for row in sources],
        record_type="source",
        matter_scope=task["matter_scope"],
    )
    artifacts = [
        {
            "artifact_id": row["artifact_id"],
            "path": row["path"],
            "artifact_type": row["artifact_type"],
            "trust_status": row["trust_status"],
            "stale": row["stale"],
            "title": row["title"],
            "content_excerpt": (row["content"] or "")[:2_000],
        }
        for row in conn.execute(
            """
            SELECT artifact_id, path, artifact_type, trust_status, stale, title, content
            FROM artifacts
            WHERE artifact_id IN (%s) AND matter_scope = ?
            ORDER BY artifact_id
            """ % ",".join("?" for _ in artifact_ids),
            (*artifact_ids, task["matter_scope"]),
        )
    ] if artifact_ids else []
    _require_all_dependencies_present(
        requested=artifact_ids,
        found=[row["artifact_id"] for row in artifacts],
        record_type="artifact",
        matter_scope=task["matter_scope"],
    )

    sections.extend(
        [
            {"name": "evidence_bundle", "kind": "sources", "content": sources},
            {"name": "artifact_bundle", "kind": "artifacts", "content": artifacts},
            {
                "name": "result_packet_schema",
                "kind": "schema",
                "content": {
                    "required_keys": ["task_id", "summary", "findings", "citations", "proposed_artifacts"],
                    "citation_rule": "Every factual/legal assertion should cite a known source, artifact, or authority.",
                    "canonical_write_rule": "Workers may not write canonical state.",
                },
            },
        ]
    )

    canonical = canonicalize_sections(sections)
    estimated = estimate_tokens(canonical)
    if estimated > token_budget:
        raise ValueError(f"context pack exceeds token budget: estimated {estimated} > budget {token_budget}")
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    context_pack_id = f"ctx-{fingerprint[:24]}"
    pack = ContextPack(context_pack_id, fingerprint, sections, token_budget, estimated)
    if persist:
        repo.add_context_pack(
            conn,
            context_pack_id=context_pack_id,
            matter_scope=task["matter_scope"],
            task_id=task_id,
            pack_type=pack_type,
            fingerprint=fingerprint,
            token_budget=token_budget,
            estimated_tokens=estimated,
            sections=sections,
        )
    return pack


def _load_string_list(task: sqlite3.Row, field: str) -> list[str]:
    value = json.loads(task[field] or "[]")
    if not isinstance(value, list):
        raise ValueError(f"{field} for task {task['task_id']} must be a JSON array")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field}[{index}] for task {task['task_id']} must be a non-empty string")
        items.append(item)
    return items


def _require_all_dependencies_present(*, requested: list[str], found: list[str], record_type: str, matter_scope: str) -> None:
    missing = sorted(set(requested) - set(found))
    if missing:
        raise ValueError(
            f"context pack missing or unauthorized {record_type} dependencies for matter {matter_scope}: {', '.join(missing)}"
        )
