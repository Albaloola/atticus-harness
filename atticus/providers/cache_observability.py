"""Provider/cache provenance and cache-break diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import sqlite3
from typing import cast

from atticus.db import repo


def fingerprint_provider_policy(policy: Mapping[str, object]) -> str:
    return _fingerprint(policy)


def fingerprint_messages(messages: Sequence[Mapping[str, object]]) -> str:
    return _fingerprint([dict(message) for message in messages])


def record_prompt_cache_observation(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    provider_run_id: str | None = None,
    task_id: str | None = None,
    context_pack_id: str | None = None,
    query_source: str = "",
    model: str = "",
    system_fingerprint: str = "",
    tools_fingerprint: str = "",
    context_fingerprint: str = "",
    policy_fingerprint: str = "",
    cache_hit_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_miss_tokens: int = 0,
    reason: str = "",
) -> str:
    return repo.record_prompt_cache_observation(
        conn,
        matter_scope=matter_scope,
        provider_run_id=provider_run_id,
        task_id=task_id,
        context_pack_id=context_pack_id,
        query_source=query_source,
        model=model,
        system_fingerprint=system_fingerprint,
        tools_fingerprint=tools_fingerprint,
        context_fingerprint=context_fingerprint,
        policy_fingerprint=policy_fingerprint,
        cache_hit_tokens=cache_hit_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_miss_tokens=cache_miss_tokens,
        reason=reason or "cache telemetry only; cache hits do not prove legal correctness",
    )


def detect_prompt_cache_break(previous: Mapping[str, object], current: Mapping[str, object]) -> dict[str, object]:
    changed: list[str] = []
    for key in ("system_fingerprint", "tools_fingerprint", "context_fingerprint", "policy_fingerprint", "model"):
        if str(previous.get(key) or "") != str(current.get(key) or ""):
            changed.append(key)
    previous_hits = _int(previous.get("cache_hit_tokens"))
    current_hits = _int(current.get("cache_hit_tokens"))
    if current_hits >= previous_hits:
        return {"possible_cache_break": False, "reason": "cache hit tokens did not drop", "changed_inputs": changed}
    if changed:
        return {"possible_cache_break": True, "reason": "cache hit drop explained by changed " + ", ".join(changed), "changed_inputs": changed}
    return {"possible_cache_break": True, "reason": "cache hit drop with unchanged fingerprints; likely provider TTL/cache behavior", "changed_inputs": []}


def cache_observability_summary(conn: sqlite3.Connection, matter_scope: str) -> dict[str, object]:
    rows = [
        dict(cast(Mapping[str, object], row))
        for row in conn.execute(
            """
            SELECT *
            FROM prompt_cache_observations
            WHERE matter_scope = ?
            ORDER BY created_at DESC
            LIMIT 100
            """,
            (matter_scope,),
        )
    ]
    return {
        "matter_scope": matter_scope,
        "observation_count": len(rows),
        "cache_hit_tokens": sum(_int(row.get("cache_hit_tokens")) for row in rows),
        "cache_write_tokens": sum(_int(row.get("cache_write_tokens")) for row in rows),
        "cache_miss_tokens": sum(_int(row.get("cache_miss_tokens")) for row in rows),
        "possible_cache_breaks": [row for row in rows if _int(row.get("possible_cache_break")) == 1],
        "correctness_note": "cache telemetry is cost/provenance data only; source/context fingerprints drive legal auditability",
    }


def _fingerprint(value: object) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _int(value: object) -> int:
    try:
        return int(str(value or 0))
    except (TypeError, ValueError):
        return 0
