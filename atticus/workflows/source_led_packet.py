"""Deterministic source-led candidate packet generation.

This is the no-live escape hatch for tasks that need reducer-grade evidence
triage but cannot safely run through ``local_stub``. It does not make legal
judgments. It turns current source chunks into a quote-supported
``worker_result_packet.v2`` candidate so the normal citation-support and reducer
gates can accept or reject it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
import sqlite3
from typing import cast

from atticus.retrieval.source_chunks import chunk_extracted_artifact, normalized_text_hash, retrieve_source_chunks_for_task
from atticus.scheduler.lease import acquire_lease
from atticus.validation.citation_support import validate_candidate_citation_support
from atticus.workers.contracts import safe_path_component
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION

KEY_CLAUSE_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "tenancy": [
        ("parties", r"(landlord|tenant|lessor|lessee)"),
        ("premises", r"(premises|dwelling|flat\b|property\s+address)"),
        ("dates", r"(commencement\s*date|term\s+of|start\s*date|end\s*date|expir)"),
        ("rent", r"(rent\b|£[\d,]+[\.\d]*|payment\s*schedule|rental\s*amount|weekly\s*rent)"),
        ("guarantor", r"(guarantor|guarantee|surety)"),
        ("termination", r"(notice\s*to\s*quit|terminat|break\s*clause)"),
        ("jurisdiction", r"(scots\s*law|jurisdiction|governing\s*law)"),
        ("disputes", r"(complaint|dispute\s*resolution|ombudsman|arbitrat)"),
        ("student_exclusion", r"(student\s*accommodation|exclusion\s*wording|not\s+a\s+student)"),
    ],
}


@dataclass(frozen=True)
class SourceLedPacketResult:
    dry_run: bool
    task_id: str
    candidate_id: str
    selected_source_ids: tuple[str, ...]
    citation_count: int
    finding_count: int
    chunk_count: int
    support_summary: dict[str, object]
    packet: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "selected_source_ids": list(self.selected_source_ids),
            "citation_count": self.citation_count,
            "finding_count": self.finding_count,
            "chunk_count": self.chunk_count,
            "support_summary": self.support_summary,
            "packet": self.packet if self.dry_run else {},
        }


def create_source_led_candidate_for_task(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    task_id: str,
    worker_id: str = "deterministic-source-led-generator",
    max_sources: int = 12,
    source_ids: list[str] | None = None,
    write: bool = False,
) -> SourceLedPacketResult:
    task = _task_row(conn, matter_scope=matter_scope, task_id=task_id)
    task_source_ids = _source_dependencies(task)
    selected_input_sources = source_ids or task_source_ids
    outside_task = sorted(set(selected_input_sources) - set(task_source_ids))
    if outside_task:
        raise ValueError(f"source-led packet sources are not task dependencies: {', '.join(outside_task)}")
    if write:
        _ensure_chunks_for_sources(conn, matter_scope=matter_scope, source_ids=selected_input_sources)
    packet, selected_sources, chunk_count = build_source_led_packet(conn, matter_scope=matter_scope, task=task, max_sources=max_sources, source_ids=selected_input_sources)
    candidate_id = ""
    support_summary: dict[str, object] = {"checked": False, "reason": "dry_run"}
    if write:
        lease_id = acquire_lease(conn, task_id=task_id, worker_id=worker_id)
        candidate_id = record_worker_result(conn, task_id=task_id, lease_id=lease_id, worker_id=worker_id, payload=packet)
        support = validate_candidate_citation_support(conn, candidate_id, force_required=True)
        support_summary = {"checked": True, "ok": support.passed, "details": support.details}
    return SourceLedPacketResult(
        dry_run=not write,
        task_id=task_id,
        candidate_id=candidate_id,
        selected_source_ids=tuple(selected_sources),
        citation_count=len(cast(list[object], packet["citations"])),
        finding_count=len(cast(list[object], packet["findings"])),
        chunk_count=chunk_count,
        support_summary=support_summary,
        packet=packet,
    )


def build_source_led_packet(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    task: Mapping[str, object],
    max_sources: int = 12,
    source_ids: list[str] | None = None,
) -> tuple[dict[str, object], list[str], int]:
    task_id = str(task["task_id"])
    source_ids = source_ids or _source_dependencies(task)
    if not source_ids:
        raise ValueError(f"task {task_id} has no source dependencies for source-led packet generation")
    query_text = " ".join([str(task["title"] or ""), str(task["instructions"] or ""), _default_query_terms()])
    task_type = str(task["task_type"] or "")
    title_str = str(task["title"] or "")
    review_task = _task_is_document_review(task_type, title_str)
    clause_patterns = _patterns_for_task(title_str) if review_task else []
    if review_task:
        chunks = retrieve_source_chunks_for_task(
            conn,
            matter_scope=matter_scope,
            source_ids=source_ids,
            query_text=query_text,
            max_chunks_per_source=3,
            max_total_chunks=24,
        )
        if clause_patterns:
            chunks = _select_multi_clause_chunks(chunks, clause_patterns)
    else:
        chunks = retrieve_source_chunks_for_task(
            conn,
            matter_scope=matter_scope,
            source_ids=source_ids,
            query_text=query_text,
            max_chunks_per_source=1,
            max_total_chunks=max_sources,
        )
    if not chunks:
        raise ValueError(f"task {task_id} has no current source chunks; run source extraction/chunking first")

    citations: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    lines = [
        f"# Source-led evidence packet for {task_id}",
        "",
        "This deterministic packet is generated from current source chunks only. It is not external legal advice and must still pass citation-support/reducer review.",
        "",
        "## Evidence anchors",
    ]
    selected_sources: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        source_id = str(chunk["source_id"])
        if review_task:
            quote = _multi_clause_quote(str(chunk.get("text") or ""), clause_patterns=clause_patterns)
        else:
            quote = _quote_from_chunk(str(chunk.get("text") or ""), query_text=query_text)
        if not quote:
            continue
        citation_id = f"src-{index}"
        locator = f"chunk:{chunk['chunk_id']}:{chunk.get('start_offset')}:{chunk.get('end_offset')}"
        citations.append(
            {
                "citation_id": citation_id,
                "target_type": "source",
                "target_id": source_id,
                "locator": locator,
                "quote": quote,
                "quoted_text_hash": normalized_text_hash(quote),
            }
        )
        findings.append(
            {
                "finding_id": f"source-led-{index}",
                "text": f"{quote}",
                "finding_type": "fact",
                "citation_ids": [citation_id],
                "confidence": 0.82,
                "reasoning_status": "supported",
            }
        )
        lines.append(f"- **{source_id}** ({locator}): “{quote}”")
        selected_sources.append(source_id)

    if not citations:
        raise ValueError(f"task {task_id} did not yield quoteable source chunks")

    safe_task = safe_path_component(task_id)
    packet: dict[str, object] = {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "Deterministic source-led packet generated from current source chunks for reducer/citation-support review.",
        "findings": findings,
        "citations": citations,
        "proposed_artifacts": [
            {
                "path": f"candidate/{safe_task}/source_led_evidence_packet.md",
                "artifact_type": "evidence_triage",
                "stage": str(task["stage"] or ""),
                "title": f"Source-led evidence packet for {task_id}",
                "content": "\n".join(lines) + "\n",
            }
        ],
        "proposed_tasks": [],
        "uncertainties": [
            {
                "uncertainty_id": "source-led-generator-limits",
                "text": "This deterministic generator selected source chunks by lexical retrieval; legal conclusions, NTQ validity, and external action decisions still require reducer/operator review.",
                "citation_ids": [],
            }
        ],
        "contradictions": [],
        "risk_flags": [
            {
                "risk_id": "human-review-required-before-external-action",
                "text": "Do not send or file this packet externally without explicit human review and instruction.",
                "citation_ids": [],
            }
        ],
        "redaction_flags": [],
        "external_action_requests": [],
    }
    return packet, list(dict.fromkeys(selected_sources)), len(chunks)


def _task_row(conn: sqlite3.Connection, *, matter_scope: str, task_id: str) -> Mapping[str, object]:
    row = cast(
        Mapping[str, object] | None,
        conn.execute("SELECT * FROM tasks WHERE matter_scope = ? AND task_id = ?", (matter_scope, task_id)).fetchone(),
    )
    if row is None:
        raise ValueError(f"unknown matter task: {matter_scope}:{task_id}")
    return row


def _ensure_chunks_for_sources(conn: sqlite3.Connection, *, matter_scope: str, source_ids: list[str]) -> None:
    for source_id in source_ids:
        existing = conn.execute(
            "SELECT 1 FROM source_chunks WHERE matter_scope = ? AND source_id = ? LIMIT 1",
            (matter_scope, source_id),
        ).fetchone()
        if existing is not None:
            continue
        snapshot_expr = "er.source_snapshot_id" if _table_has_column(conn, "extraction_records", "source_snapshot_id") else "''"
        rows = conn.execute(
            """
            SELECT er.extraction_id, er.artifact_id, {snapshot_expr} AS source_snapshot_id, er.confidence
            FROM extraction_records er
            JOIN artifacts a ON a.artifact_id = er.artifact_id
            JOIN sources s ON s.source_id = er.source_id AND s.matter_scope = ?
            WHERE er.source_id = ?
              AND er.coverage_status = 'complete'
              AND a.stale = 0
              AND s.stale = 0
            ORDER BY er.created_at DESC, er.extraction_id DESC
            LIMIT 1
            """.format(snapshot_expr=snapshot_expr),
            (matter_scope, source_id),
        ).fetchall()
        for row in rows:
            _ = chunk_extracted_artifact(
                conn,
                matter_scope=matter_scope,
                source_id=source_id,
                artifact_id=str(row["artifact_id"]),
                extraction_id=str(row["extraction_id"]),
                source_snapshot_id=str(row["source_snapshot_id"] or ""),
                confidence=float(str(row["confidence"])) if row["confidence"] is not None else None,
            )


def _source_dependencies(task: Mapping[str, object]) -> list[str]:
    try:
        raw = json.loads(str(task["source_dependencies_json"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item)]


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row["name"]) == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _quote_from_chunk(text: str, *, query_text: str) -> str:
    terms = {term for term in re.findall(r"[a-z0-9£]+", query_text.casefold()) if len(term) >= 4}
    candidates = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if len(part.strip()) >= 30]
    if not candidates and text.strip():
        candidates = [text.strip()]
    candidates.sort(key=lambda part: (-len(set(re.findall(r"[a-z0-9£]+", part.casefold())) & terms), len(part)))
    quote = candidates[0] if candidates else ""
    quote = " ".join(quote.split())
    return quote[:700].strip()


def _default_query_terms() -> str:
    return (
        "hardship notice to quit NTQ arrears rent accommodation pause enforcement "
        "debt escalation student loan bursary support failure payment chronology"
    )


def _task_is_document_review(task_type: str, title: str) -> bool:
    text = f"{task_type} {title}".casefold()
    review_indicators = {"review", "triage", "audit", "examination", "check"}
    document_indicators = {"tenancy", "agreement", "contract", "lease", "deed", "document", "licence", "license"}
    return any(r in text for r in review_indicators) and any(d in text for d in document_indicators)


def _patterns_for_task(title: str) -> list[tuple[str, str]]:
    title_lower = title.casefold()
    for keywords, patterns in KEY_CLAUSE_PATTERNS.items():
        if keywords in title_lower:
            return patterns
    return []


def _select_multi_clause_chunks(chunks: list[dict[str, object]], clause_patterns: list[tuple[str, str]], max_chunks: int = 8) -> list[dict[str, object]]:
    scored: list[tuple[dict[str, object], set[str], int]] = []
    for chunk in chunks:
        text = str(chunk.get("text") or "").casefold()
        matched_types: set[str] = set()
        for clause_type, pattern in clause_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                matched_types.add(clause_type)
        scored.append((chunk, matched_types, len(matched_types)))
    scored.sort(key=lambda x: (-x[2], -len(str(x[0].get("text", "")))))
    covered: set[str] = set()
    selected: list[dict[str, object]] = []
    for chunk, types, count in scored:
        if count == 0:
            continue
        if len(selected) >= max_chunks:
            break
        new_types = types - covered
        if new_types or len(selected) < 3:
            selected.append(chunk)
            covered.update(types)
    if len(selected) < 3 and chunks:
        for c in chunks:
            if c not in selected:
                selected.append(c)
            if len(selected) >= 3:
                break
    return selected[:max_chunks]


def _multi_clause_quote(text: str, *, clause_patterns: list[tuple[str, str]]) -> str:
    if not clause_patterns or not text.strip():
        return text[:700].strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) >= 20]
    if not sentences:
        return text[:700].strip()
    seen: set[int] = set()
    selected: list[str] = []
    for _clause_type, pattern in clause_patterns:
        for i, sentence in enumerate(sentences):
            if i in seen:
                continue
            if re.search(pattern, sentence, re.IGNORECASE):
                selected.append(sentence)
                seen.add(i)
                break
    if not selected:
        selected = [sentences[0]]
    quote = " … ".join(selected)
    return quote[:700].strip()
