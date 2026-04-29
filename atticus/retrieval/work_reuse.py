"""High-level same-matter reuse helpers for follow-up work."""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import cast

from atticus.retrieval.rank import lexical_score


def find_reusable_artifacts(conn: sqlite3.Connection, matter_scope: str, goal: str) -> list[dict[str, object]]:
    rows = [
        dict(cast(Mapping[str, object], row))
        for row in conn.execute(
            """
            SELECT artifact_id, matter_scope, path, artifact_type, stage, trust_status, stale, title, content
            FROM artifacts
            WHERE matter_scope = ? AND stale = 0 AND trust_status IN ('validated', 'certified')
            ORDER BY updated_at DESC, artifact_id
            """,
            (matter_scope,),
        )
    ]
    return _rank(goal, rows, id_key="artifact_id")


def find_reusable_candidates(conn: sqlite3.Connection, matter_scope: str, goal: str) -> list[dict[str, object]]:
    rows = [
        {
            **dict(cast(Mapping[str, object], row)),
            "trusted_as_proof": False,
            "reuse_note": "candidate-only output may orient follow-up work but is not trusted evidence",
        }
        for row in conn.execute(
            """
            SELECT co.candidate_id, t.matter_scope, t.task_type, t.title, co.status, co.payload_json
            FROM candidate_outputs co
            JOIN tasks t ON t.task_id = co.task_id
            WHERE t.matter_scope = ? AND co.status = 'candidate'
            ORDER BY co.created_at DESC
            """,
            (matter_scope,),
        )
    ]
    return _rank(goal, rows, id_key="candidate_id")


def find_reusable_context_packs(conn: sqlite3.Connection, matter_scope: str, goal: str) -> list[dict[str, object]]:
    rows = [
        dict(cast(Mapping[str, object], row))
        for row in conn.execute(
            """
            SELECT context_pack_id, matter_scope, task_id, pack_type, fingerprint, estimated_tokens, sections_json
            FROM context_packs
            WHERE matter_scope = ?
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (matter_scope,),
        )
    ]
    return _rank(goal, rows, id_key="context_pack_id")


def build_followup_context(conn: sqlite3.Connection, matter_scope: str, question: str) -> dict[str, object]:
    memories = [
        dict(cast(Mapping[str, object], row))
        for row in conn.execute(
            """
            SELECT memory_id, type, name, description, confidence, stale
            FROM legal_memories
            WHERE matter_scope = ? AND status = 'active' AND stale = 0
            ORDER BY type, name
            LIMIT 25
            """,
            (matter_scope,),
        )
    ]
    return {
        "matter_scope": matter_scope,
        "question": question,
        "artifacts": find_reusable_artifacts(conn, matter_scope, question),
        "candidates": find_reusable_candidates(conn, matter_scope, question),
        "context_packs": find_reusable_context_packs(conn, matter_scope, question),
        "memory_orientation": memories,
        "rules": [
            "reuse is same-matter only",
            "validated/certified artifacts may be reused when source snapshots remain current",
            "candidate output and active memory orient work only; neither is proof",
            "provider/model decisions are provenance, not correctness evidence",
        ],
    }


def explain_reuse_decision(conn: sqlite3.Connection, matter_scope: str, records: list[Mapping[str, object]]) -> dict[str, object]:
    del conn
    explanations = []
    for record in records:
        trust = str(record.get("trust_status") or record.get("status") or "")
        trusted_as_proof = trust in {"validated", "certified"}
        orientation_only = record.get("trusted_as_proof") is False
        explanations.append(
            {
                "record_id": str(record.get("artifact_id") or record.get("candidate_id") or record.get("context_pack_id") or ""),
                "reuse_allowed": trusted_as_proof,
                "orientation_allowed": orientation_only,
                "proof_status": "orientation_only" if orientation_only else trust,
                "reason": "same matter and non-stale; recheck citations before legal reliance",
            }
        )
    return {"matter_scope": matter_scope, "reuse_explanations": explanations}


def _rank(goal: str, rows: list[dict[str, object]], *, id_key: str) -> list[dict[str, object]]:
    scored: list[tuple[float, str, dict[str, object]]] = []
    for row in rows:
        text = " ".join(str(value) for value in row.values())
        score = lexical_score(goal, text) if goal else 1.0
        if score <= 0 and goal:
            continue
        scored.append((score, str(row.get(id_key) or ""), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:10]]
