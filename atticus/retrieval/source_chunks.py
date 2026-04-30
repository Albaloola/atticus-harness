"""Source text chunking and deterministic chunk retrieval."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import cast
from uuid import uuid4

from atticus.core.events import utc_now

SqlRow = Mapping[str, object]
TARGET_CHUNK_TOKENS = 1_000
APPROX_CHARS_PER_TOKEN = 4
TARGET_CHUNK_CHARS = TARGET_CHUNK_TOKENS * APPROX_CHARS_PER_TOKEN
MAX_CHUNK_CHARS = 5_200
MIN_TERM_LENGTH = 4
SOURCE_CHUNK_PROOF_CONFIDENCE_THRESHOLD = 0.6


@dataclass(frozen=True)
class SourceChunk:
    chunk_id: str
    matter_scope: str
    source_id: str
    source_snapshot_id: str
    extraction_id: str
    artifact_id: str
    page_number: int | None
    start_offset: int
    end_offset: int
    text_hash: str
    text: str
    confidence: float | None
    metadata: dict[str, object]

    def as_context(self, *, include_text: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "source_snapshot_id": self.source_snapshot_id,
            "extraction_id": self.extraction_id,
            "artifact_id": self.artifact_id,
            "page_number": self.page_number,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "text_hash": self.text_hash,
            "confidence": self.confidence,
        }
        if include_text:
            payload["text"] = self.text
        return payload


def chunk_extracted_artifact(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_id: str,
    artifact_id: str,
    extraction_id: str | None = None,
    source_snapshot_id: str | None = None,
    confidence: float | None = None,
    replace: bool = True,
) -> list[SourceChunk]:
    artifact = cast(
        SqlRow | None,
        conn.execute(
            """
            SELECT content, sha256
            FROM artifacts
            WHERE artifact_id = ? AND matter_scope = ? AND stale = 0
            """,
            (artifact_id, matter_scope),
        ).fetchone(),
    )
    if artifact is None:
        return []
    text = str(artifact["content"] or "")
    if not text.strip():
        return []
    resolved_snapshot_id = source_snapshot_id or _current_source_snapshot_id(conn, source_id=source_id)
    resolved_extraction_id = extraction_id or _current_extraction_id(conn, source_id=source_id, artifact_id=artifact_id)
    resolved_confidence = confidence if confidence is not None else _current_extraction_confidence(conn, extraction_id=resolved_extraction_id)
    chunks = [
        SourceChunk(
            chunk_id=_chunk_id(source_id=source_id, artifact_id=artifact_id, start_offset=start, end_offset=end, text=chunk_text),
            matter_scope=matter_scope,
            source_id=source_id,
            source_snapshot_id=resolved_snapshot_id,
            extraction_id=resolved_extraction_id,
            artifact_id=artifact_id,
            page_number=page_number,
            start_offset=start,
            end_offset=end,
            text_hash=normalized_text_hash(chunk_text),
            text=chunk_text,
            confidence=resolved_confidence,
            metadata={
                "strategy": "paragraph_window",
                "target_tokens": TARGET_CHUNK_TOKENS,
                "estimated_tokens": estimate_chunk_tokens(chunk_text),
                "offset_basis": "artifact_content_utf8_codepoints",
                "normalized_text_hash": normalized_text_hash(chunk_text),
                "source_snapshot_id": resolved_snapshot_id,
            },
        )
        for start, end, page_number, chunk_text in _chunk_windows(text)
    ]
    if replace:
        _ = conn.execute(
            "DELETE FROM source_chunks WHERE matter_scope = ? AND source_id = ? AND artifact_id = ?",
            (matter_scope, source_id, artifact_id),
        )
    for chunk in chunks:
        _insert_chunk(conn, chunk)
    return chunks


def retrieve_source_chunks_for_task(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_ids: list[str],
    query_text: str,
    max_chunks_per_source: int = 2,
    max_total_chunks: int = 24,
) -> list[dict[str, object]]:
    if not source_ids:
        return []
    rows = cast(
        list[SqlRow],
        conn.execute(
            """
            SELECT sc.chunk_id, sc.matter_scope, sc.source_id, sc.source_snapshot_id, sc.extraction_id, sc.artifact_id,
              sc.page_number, sc.start_offset, sc.end_offset, sc.text_hash, sc.text, sc.confidence, sc.metadata_json,
              s.sha256 AS current_source_sha256, s.stale AS source_stale, a.stale AS artifact_stale
            FROM source_chunks sc
            JOIN sources s ON s.source_id = sc.source_id AND s.matter_scope = sc.matter_scope
            LEFT JOIN artifacts a ON a.artifact_id = sc.artifact_id AND a.matter_scope = sc.matter_scope
            WHERE sc.matter_scope = ?
              AND sc.source_id IN (%s)
              AND s.stale = 0
              AND COALESCE(a.stale, 0) = 0
              AND (
                sc.source_snapshot_id IS NULL
                OR sc.source_snapshot_id = ''
                OR sc.source_snapshot_id = (
                  SELECT ss.snapshot_id
                  FROM source_snapshots ss
                  WHERE ss.source_id = sc.source_id AND ss.sha256 = s.sha256
                  ORDER BY ss.created_at DESC, ss.snapshot_id DESC
                  LIMIT 1
                )
              )
            ORDER BY sc.source_id, sc.start_offset, sc.chunk_id
            """ % ",".join("?" for _ in source_ids),
            (matter_scope, *source_ids),
        ).fetchall(),
    )
    if not rows:
        return []
    terms = _query_terms(query_text)
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        item = _row_to_context(row)
        item["retrieval_score"] = _score_chunk(str(row["text"] or ""), terms)
        item["retrieval_query_terms"] = sorted(terms)[:25]
        grouped.setdefault(str(row["source_id"]), []).append(item)
    selected: list[dict[str, object]] = []
    for source_id in source_ids:
        candidates = grouped.get(source_id, [])
        if not candidates:
            continue
        candidates.sort(key=lambda item: (-int(str(item["retrieval_score"])), int(str(item["start_offset"])), str(item["chunk_id"])))
        chosen = candidates[:max_chunks_per_source]
        omitted = max(0, len(candidates) - len(chosen))
        for item in chosen:
            item["omitted_chunk_count_for_source"] = omitted
            selected.append(item)
    selected.sort(key=lambda item: (-int(str(item["retrieval_score"])), str(item["source_id"]), int(str(item["start_offset"]))))
    return selected[:max_total_chunks]


def normalized_text_hash(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest()


def estimate_chunk_tokens(text: str) -> int:
    return max(1, (len(_normalize_text(text)) + APPROX_CHARS_PER_TOKEN - 1) // APPROX_CHARS_PER_TOKEN) if text.strip() else 0


def _insert_chunk(conn: sqlite3.Connection, chunk: SourceChunk) -> None:
    _ = conn.execute(
        """
        INSERT OR REPLACE INTO source_chunks(
          chunk_id, matter_scope, source_id, source_snapshot_id, extraction_id, artifact_id,
          page_number, start_offset, end_offset, text_hash, text, confidence, metadata_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk.chunk_id,
            chunk.matter_scope,
            chunk.source_id,
            chunk.source_snapshot_id or None,
            chunk.extraction_id or None,
            chunk.artifact_id or None,
            chunk.page_number,
            chunk.start_offset,
            chunk.end_offset,
            chunk.text_hash,
            chunk.text,
            chunk.confidence,
            json.dumps(chunk.metadata, sort_keys=True, separators=(",", ":")),
            utc_now(),
        ),
    )


def _chunk_windows(text: str) -> list[tuple[int, int, int | None, str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [(match.start(), match.end(), match.group(0).strip()) for match in re.finditer(r"(?s).+?(?:\n\s*\n|$)", normalized)]
    chunks: list[tuple[int, int, int | None, str]] = []
    current_start: int | None = None
    current_end = 0
    current_parts: list[str] = []
    for start, end, paragraph in paragraphs:
        if not paragraph:
            continue
        if len(paragraph) > MAX_CHUNK_CHARS:
            _flush_window(chunks, current_start, current_end, current_parts, normalized)
            current_start, current_end, current_parts = None, 0, []
            chunks.extend(_split_long_paragraph(paragraph, base_offset=start))
            continue
        pending = "\n\n".join([*current_parts, paragraph]) if current_parts else paragraph
        if current_parts and len(pending) > TARGET_CHUNK_CHARS:
            _flush_window(chunks, current_start, current_end, current_parts, normalized)
            current_start, current_end, current_parts = None, 0, []
        if current_start is None:
            current_start = start
        current_parts.append(paragraph)
        current_end = end
    _flush_window(chunks, current_start, current_end, current_parts, normalized)
    return chunks


def _flush_window(
    chunks: list[tuple[int, int, int | None, str]],
    start: int | None,
    end: int,
    parts: list[str],
    source_text: str,
) -> None:
    if start is None or not parts:
        return
    text = "\n\n".join(parts).strip()
    if text:
        chunks.append((start, min(end, len(source_text)), None, text))


def _split_long_paragraph(paragraph: str, *, base_offset: int) -> list[tuple[int, int, int | None, str]]:
    chunks: list[tuple[int, int, int | None, str]] = []
    cursor = 0
    while cursor < len(paragraph):
        end = min(len(paragraph), cursor + TARGET_CHUNK_CHARS)
        if end < len(paragraph):
            boundary = paragraph.rfind(" ", cursor, end)
            if boundary > cursor + TARGET_CHUNK_CHARS // 2:
                end = boundary
        text = paragraph[cursor:end].strip()
        if text:
            chunks.append((base_offset + cursor, base_offset + end, None, text))
        cursor = max(end, cursor + 1)
    return chunks


def _current_source_snapshot_id(conn: sqlite3.Connection, *, source_id: str) -> str:
    row = conn.execute(
        """
        SELECT snapshot_id
        FROM source_snapshots
        WHERE source_id = ?
        ORDER BY created_at DESC, snapshot_id DESC
        LIMIT 1
        """,
        (source_id,),
    ).fetchone()
    return str(row["snapshot_id"]) if row is not None else ""


def _current_extraction_confidence(conn: sqlite3.Connection, *, extraction_id: str) -> float | None:
    if not extraction_id:
        return None
    row = conn.execute(
        "SELECT confidence FROM extraction_records WHERE extraction_id = ?",
        (extraction_id,),
    ).fetchone()
    if row is None or row["confidence"] is None:
        return None
    try:
        return float(str(row["confidence"]))
    except (TypeError, ValueError):
        return None


def _current_extraction_id(conn: sqlite3.Connection, *, source_id: str, artifact_id: str) -> str:
    row = conn.execute(
        """
        SELECT extraction_id
        FROM extraction_records
        WHERE source_id = ? AND artifact_id = ?
        ORDER BY created_at DESC, extraction_id DESC
        LIMIT 1
        """,
        (source_id, artifact_id),
    ).fetchone()
    return str(row["extraction_id"]) if row is not None else ""


def _chunk_id(*, source_id: str, artifact_id: str, start_offset: int, end_offset: int, text: str) -> str:
    digest = hashlib.sha256(f"{source_id}:{artifact_id}:{start_offset}:{end_offset}:{_normalize_text(text)}".encode("utf-8")).hexdigest()
    return f"chunk-{digest[:32]}" if digest else f"chunk-{uuid4().hex}"


def _row_to_context(row: SqlRow) -> dict[str, object]:
    metadata = _json_metadata(row["metadata_json"])
    confidence = _optional_float(row["confidence"])
    proof_eligible = confidence is None or confidence >= SOURCE_CHUNK_PROOF_CONFIDENCE_THRESHOLD
    return {
        "chunk_id": row["chunk_id"],
        "source_id": row["source_id"],
        "source_snapshot_id": row["source_snapshot_id"] or "",
        "extraction_id": row["extraction_id"] or "",
        "artifact_id": row["artifact_id"] or "",
        "page_number": row["page_number"],
        "start_offset": row["start_offset"],
        "end_offset": row["end_offset"],
        "text_hash": row["text_hash"],
        "text": row["text"],
        "confidence": row["confidence"],
        "proof_eligible": proof_eligible,
        "proof_role": "source_chunk_proof" if proof_eligible else "orientation_only_low_confidence_chunk",
        "confidence_threshold": SOURCE_CHUNK_PROOF_CONFIDENCE_THRESHOLD,
        "estimated_tokens": metadata.get("estimated_tokens") or estimate_chunk_tokens(str(row["text"] or "")),
        "offset_basis": metadata.get("offset_basis") or "artifact_content_utf8_codepoints",
        "metadata": metadata,
    }


def _json_metadata(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    return {str(key): item for key, item in cast(Mapping[object, object], parsed).items()}


def _query_terms(text: str) -> set[str]:
    return {term.casefold() for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", text) if len(term) >= MIN_TERM_LENGTH}


def _score_chunk(text: str, terms: set[str]) -> int:
    if not terms:
        return 0
    haystack = text.casefold()
    return sum(1 for term in terms if term in haystack)


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
