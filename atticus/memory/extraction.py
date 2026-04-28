"""Reducer-gated legal memory extraction.

This module proposes candidate legal memories from accepted reducer output.
It deliberately refuses raw, unreduced worker candidates so memory cannot
become a hallucination store.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from typing import cast

from atticus.db import repo
from atticus.workers.result_parser import parse_result

_FINDING_MEMORY_TYPES = {
    "fact": "evidence_fact",
    "law": "authority_rule",
    "procedure": "matter_posture",
    "contradiction": "contradiction",
    "risk": "risk_register",
}


def extract_memory_candidates(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    matter_scope: str,
    dry_run: bool = True,
) -> dict[str, object]:
    candidate = _load_reduced_accepted_candidate(conn, candidate_id=candidate_id, matter_scope=matter_scope)
    payload = json.loads(str(candidate["payload_json"]))
    if not isinstance(payload, Mapping):
        raise ValueError("candidate payload must be a JSON object")
    packet = parse_result({str(key): value for key, value in cast(Mapping[object, object], payload).items()})
    citations_by_id = {
        str(citation["citation_id"]): citation
        for citation in packet.citations
        if isinstance(citation, Mapping) and citation.get("citation_id")
    }
    memory_candidates = []
    skipped = []
    for finding in packet.findings:
        finding_type = str(finding.get("finding_type") or "")
        memory_type = _FINDING_MEMORY_TYPES.get(finding_type)
        if not memory_type:
            skipped.append({"finding_id": finding.get("finding_id"), "reason": f"{finding_type or 'unknown'} findings are not durable memory"})
            continue
        raw_citation_ids = finding.get("citation_ids", [])
        citation_ids = [str(item) for item in raw_citation_ids if str(item)] if isinstance(raw_citation_ids, list) else []
        refs = [_source_ref_from_citation(citations_by_id[cid]) for cid in citation_ids if cid in citations_by_id]
        refs = [ref for ref in refs if ref is not None]
        if not refs:
            skipped.append({"finding_id": finding.get("finding_id"), "reason": "source-required memory candidate had no valid citation refs"})
            continue
        text = str(finding.get("text") or "").strip()
        if not text:
            skipped.append({"finding_id": finding.get("finding_id"), "reason": "empty finding text"})
            continue
        memory_candidates.append(
            {
                "type": memory_type,
                "name": _candidate_name(memory_type, text),
                "description": f"Proposed from accepted reducer candidate {candidate_id}.",
                "content": text,
                "status": "candidate",
                "confidence": _safe_float(finding.get("confidence")),
                "source_refs": refs,
                "staleness_trigger": "new contrary evidence, updated procedural posture, or authority change",
                "origin": {
                    "candidate_id": candidate_id,
                    "task_id": str(candidate["task_id"]),
                    "finding_id": str(finding.get("finding_id") or ""),
                    "reasoning_status": str(finding.get("reasoning_status") or ""),
                },
            }
        )

    created_ids: list[str] = []
    if not dry_run:
        for item in memory_candidates:
            if _duplicate_memory_exists(
                conn,
                matter_scope=matter_scope,
                memory_type=str(item["type"]),
                content=str(item["content"]),
            ):
                skipped.append({"name": item["name"], "reason": "duplicate memory content already exists"})
                continue
            memory_id = repo.add_legal_memory(
                conn,
                matter_scope=matter_scope,
                memory_type=str(item["type"]),
                name=str(item["name"]),
                description=str(item["description"]),
                content=str(item["content"]),
                status="candidate",
                confidence=float(item["confidence"]),
                source_refs=cast(list[dict[str, object]], item["source_refs"]),
                staleness_trigger=str(item["staleness_trigger"]),
            )
            created_ids.append(memory_id)
        _ = repo.emit_event(
            conn,
            "legal_memory.candidates_extracted",
            matter_scope=matter_scope,
            payload={"candidate_id": candidate_id, "created_memory_ids": created_ids, "skipped": skipped},
        )

    return {
        "dry_run": dry_run,
        "matter_scope": matter_scope,
        "candidate_id": candidate_id,
        "memory_candidates": memory_candidates,
        "created_memory_ids": created_ids,
        "skipped": skipped,
        "active_memory_written": False,
    }


def _load_reduced_accepted_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    matter_scope: str,
) -> Mapping[str, object]:
    row = cast(Mapping[str, object] | None, conn.execute(
        """
        SELECT co.*
        FROM candidate_outputs co
        JOIN tasks t ON t.task_id = co.task_id
        WHERE co.candidate_id = ? AND t.matter_scope = ?
        """,
        (candidate_id, matter_scope),
    ).fetchone())
    if row is None:
        raise ValueError(f"candidate not found in matter {matter_scope}: {candidate_id}")
    accepted = conn.execute(
        "SELECT 1 FROM reducer_packets WHERE candidate_id = ? AND decision = 'accepted' LIMIT 1",
        (candidate_id,),
    ).fetchone()
    if str(row["status"]) != "reduced" or accepted is None:
        raise ValueError(f"memory extraction requires a reduced candidate with an accepted reducer packet: {candidate_id}")
    return row


def _source_ref_from_citation(citation: Mapping[str, object]) -> dict[str, object] | None:
    target_type = str(citation.get("target_type") or "")
    target_id = str(citation.get("target_id") or "")
    if target_type not in {"source", "artifact", "authority", "claim", "chronology_event", "memory", "validation_result"}:
        return None
    if not target_id:
        return None
    return {
        "target_type": target_type,
        "target_id": target_id,
        "locator": str(citation.get("locator") or ""),
    }


def _candidate_name(memory_type: str, text: str) -> str:
    prefix = {
        "evidence_fact": "Evidence fact",
        "authority_rule": "Authority rule",
        "matter_posture": "Matter posture",
        "contradiction": "Contradiction",
        "risk_register": "Risk",
    }.get(memory_type, "Memory")
    compact = " ".join(text.split())
    return f"{prefix}: {compact[:80]}"


def _duplicate_memory_exists(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    memory_type: str,
    content: str,
) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM legal_memories
        WHERE matter_scope = ? AND type = ? AND content = ? AND status != 'rejected'
        LIMIT 1
        """,
        (matter_scope, memory_type, content),
    ).fetchone() is not None


def _safe_float(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0
