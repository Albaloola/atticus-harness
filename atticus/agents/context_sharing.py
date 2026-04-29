"""Cache-safe context sharing for candidate-only subagent tasks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import cast

from atticus.db import repo


@dataclass(frozen=True)
class CacheSafeContext:
    stable_sections: tuple[str, ...]
    stable_fingerprint: str
    matter_fingerprint: str
    tool_fingerprint: str
    schema_fingerprint: str
    model_family: str

    def as_dict(self) -> dict[str, object]:
        return {
            "stable_sections": list(self.stable_sections),
            "stable_fingerprint": self.stable_fingerprint,
            "matter_fingerprint": self.matter_fingerprint,
            "tool_fingerprint": self.tool_fingerprint,
            "schema_fingerprint": self.schema_fingerprint,
            "model_family": self.model_family,
        }


STABLE_SECTION_NAMES = {
    "stable_prefix",
    "untrusted_evidence_boundary",
    "matter_posture",
    "evidence_manifest",
    "source_materials",
    "artifact_bundle",
    "authority_map",
    "legal_memory_index",
    "required_output_schema",
    "available_tools",
}


def build_cache_safe_context(
    sections: list[Mapping[str, object]],
    *,
    model_decision: Mapping[str, object] | None = None,
) -> CacheSafeContext:
    stable = [dict(section) for section in sections if str(section.get("name") or "") in STABLE_SECTION_NAMES]
    stable.sort(key=lambda section: str(section.get("name") or ""))
    names = tuple(str(section.get("name") or "") for section in stable)
    by_name = {str(section.get("name") or ""): section for section in stable}
    model = str((model_decision or {}).get("model") or "")
    return CacheSafeContext(
        stable_sections=names,
        stable_fingerprint=_fingerprint(stable),
        matter_fingerprint=_fingerprint(by_name.get("matter_posture", {})),
        tool_fingerprint=_fingerprint(by_name.get("available_tools", {})),
        schema_fingerprint=_fingerprint(by_name.get("required_output_schema", {})),
        model_family=model.split("/", 1)[0] if model else "",
    )


def cache_safe_context_from_pack(
    conn: sqlite3.Connection,
    *,
    context_pack_id: str,
    model_decision: Mapping[str, object] | None = None,
) -> CacheSafeContext:
    row = conn.execute("SELECT sections_json FROM context_packs WHERE context_pack_id = ?", (context_pack_id,)).fetchone()
    if row is None:
        raise ValueError(f"context pack not found: {context_pack_id}")
    sections_raw = json.loads(str(row["sections_json"] or "[]"))
    if not isinstance(sections_raw, list):
        raise ValueError("context pack sections_json must be a list")
    sections = [cast(Mapping[str, object], item) for item in sections_raw if isinstance(item, Mapping)]
    return build_cache_safe_context(sections, model_decision=model_decision)


def store_cache_safe_context(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    cache_sharing_group_id: str,
    context: CacheSafeContext,
) -> str:
    context_pack_id = f"ctx-share-{_fingerprint({'matter_scope': matter_scope, 'group': cache_sharing_group_id, 'stable': context.stable_fingerprint})[:24]}"
    return repo.add_context_pack(
        conn,
        context_pack_id=context_pack_id,
        matter_scope=matter_scope,
        task_id=None,
        pack_type="cache_safe_context",
        fingerprint=context.stable_fingerprint,
        token_budget=0,
        estimated_tokens=0,
        sections=[{"name": "cache_safe_context", "content": context.as_dict()}],
    )


def _fingerprint(value: object) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
