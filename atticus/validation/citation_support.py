"""Deterministic citation support checks with a durable audit trail."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import cast
from uuid import uuid4

from atticus.db import repo
from atticus.workers.citation_context import allowed_citation_targets_for_task, proof_citation_targets_for_task
from atticus.workers.result_parser import ResultPacketError, parse_result

SqlRow = Mapping[str, object]
MATERIAL_FINDING_TYPES = frozenset({"fact", "law", "procedure", "contradiction", "risk"})
MATERIAL_REASONING_STATUSES = frozenset({"supported", "inferred", "contradicted"})
SUPPORT_REQUIRED_TASK_TYPES = frozenset(
    {
        "authority_map",
        "authority_audit",
        "citation_audit",
        "citation_repair",
        "draft",
        "draft_preparation",
        "final_quality_gate",
        "hostile_opponent_review",
        "hostile_review",
    }
)
SUPPORT_REQUIRED_STAGES = frozenset({"S6", "S7", "S8", "S9"})


@dataclass(frozen=True)
class CitationSupportResult:
    finding_id: str
    citation_id: str
    target_type: str
    target_id: str
    quote_text: str
    quote_hash: str
    proposition_text: str
    semantic_support_status: str
    authority_support_status: str
    source_chunk_id: str
    start_offset: int | None
    end_offset: int | None
    support_confidence: float | None
    requires_human_review: bool
    support_status: str
    support_level: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "citation_id": self.citation_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "quote_text": self.quote_text,
            "quote_hash": self.quote_hash,
            "proposition_text": self.proposition_text,
            "semantic_support_status": self.semantic_support_status,
            "authority_support_status": self.authority_support_status,
            "source_chunk_id": self.source_chunk_id,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "support_confidence": self.support_confidence,
            "requires_human_review": self.requires_human_review,
            "support_status": self.support_status,
            "support_level": self.support_level,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CitationSupportSummary:
    passed: bool
    details: dict[str, object]


def validate_candidate_citation_support(conn: sqlite3.Connection, candidate_id: str) -> CitationSupportSummary:
    row = cast(
        SqlRow | None,
        conn.execute(
            """
            SELECT co.payload_json, co.task_id, t.matter_scope, t.stage, t.task_type
            FROM candidate_outputs co
            JOIN tasks t ON t.task_id = co.task_id
            WHERE co.candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone(),
    )
    if row is None:
        return CitationSupportSummary(False, {"error": "candidate output not found"})

    try:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, Mapping):
            return CitationSupportSummary(False, {"error": "candidate output payload must be a JSON object"})
        task_id = str(row["task_id"])
        packet = parse_result(
            {str(key): value for key, value in cast(Mapping[object, object], payload).items()},
            allowed_citation_targets=allowed_citation_targets_for_task(conn, task_id=task_id),
            proof_citation_targets=proof_citation_targets_for_task(conn, task_id=task_id),
        )
    except (json.JSONDecodeError, ResultPacketError) as exc:
        return CitationSupportSummary(False, {"error": str(exc)})

    stage = str(row["stage"] or "")
    task_type = str(row["task_type"] or "")
    required = stage in SUPPORT_REQUIRED_STAGES or task_type in SUPPORT_REQUIRED_TASK_TYPES
    if not required:
        _replace_support_results(conn, matter_scope=str(row["matter_scope"]), candidate_id=candidate_id, artifact_id=None, results=[])
        return CitationSupportSummary(
            True,
            {
                "required": False,
                "quote_support_checked": False,
                "reason": "quote support is not mandatory for this stage/task type",
            },
        )

    citations_by_id = {str(citation["citation_id"]): citation for citation in packet.citations}
    results: list[CitationSupportResult] = []
    for finding in packet.findings:
        finding_id = str(finding.get("finding_id") or "")
        finding_type = str(finding.get("finding_type") or "")
        reasoning_status = str(finding.get("reasoning_status") or "")
        if finding_type not in MATERIAL_FINDING_TYPES or reasoning_status not in MATERIAL_REASONING_STATUSES:
            continue
        for citation_id in [str(item) for item in cast(list[object], finding.get("citation_ids") or []) if str(item)]:
            citation = citations_by_id.get(citation_id)
            if citation is None:
                continue
            results.append(
                _check_citation(
                    conn,
                    finding_id=finding_id,
                    finding_type=finding_type,
                    proposition_text=str(finding.get("text") or ""),
                    citation=citation,
                )
            )

    _replace_support_results(conn, matter_scope=str(row["matter_scope"]), candidate_id=candidate_id, artifact_id=None, results=results)
    failures = [result for result in results if not _support_status_passes(result.support_status)]
    return CitationSupportSummary(
        not failures,
        _summary_details(results=results, failures=failures),
    )


def normalized_quote_sha256(text: str) -> str:
    return hashlib.sha256(normalize_quote_text(text).encode("utf-8")).hexdigest()


def normalize_quote_text(text: str) -> str:
    return " ".join(text.split()).strip()


def quote_found_in_source_material(conn: sqlite3.Connection, *, source_id: str, quote: str) -> bool:
    return _quote_found_in_rows(conn, quote=quote, query=_source_material_query(), params=(source_id, source_id, source_id, source_id, source_id))


def quote_found_in_artifact(conn: sqlite3.Connection, *, artifact_id: str, quote: str) -> bool:
    return _quote_found_in_rows(conn, quote=quote, query="SELECT content FROM artifacts WHERE artifact_id = ? AND stale = 0", params=(artifact_id,))


def quote_found_in_authority(conn: sqlite3.Connection, *, authority_id: str, quote: str) -> bool:
    normalized_quote = normalize_quote_text(quote).casefold()
    if not normalized_quote:
        return False
    for content in _verified_current_authority_texts(conn, authority_id=authority_id):
        if quote_matches_text(normalized_quote, normalize_quote_text(content).casefold()):
            return True
    return False


def _check_citation(
    conn: sqlite3.Connection,
    *,
    finding_id: str,
    finding_type: str,
    proposition_text: str,
    citation: Mapping[str, object],
) -> CitationSupportResult:
    citation_id = str(citation.get("citation_id") or "")
    quote = str(citation.get("quote") or citation.get("excerpt") or "").strip()
    target_type = str(citation.get("target_type") or "")
    target_id = str(citation.get("target_id") or "")
    if finding_type == "law" and target_type != "authority":
        return _result(
            finding_id=finding_id,
            citation_id=citation_id,
            target_type=target_type,
            target_id=target_id,
            proposition_text=proposition_text,
            quote=quote,
            status="unsupported_law_without_verified_authority",
            level="none",
            reason="supported law findings require a verified/current authority citation",
        )
    if not quote:
        return _result(
            finding_id=finding_id,
            citation_id=citation_id,
            target_type=target_type,
            target_id=target_id,
            proposition_text=proposition_text,
            quote=quote,
            status="no_quote_supplied",
            level="none",
            reason="material supported finding has no quote or excerpt",
        )

    expected_hash = str(citation.get("quoted_text_hash") or "").strip().lower()
    actual_hash = normalized_quote_sha256(quote)
    if expected_hash and expected_hash != actual_hash:
        return _result(
            finding_id=finding_id,
            citation_id=citation_id,
            target_type=target_type,
            target_id=target_id,
            proposition_text=proposition_text,
            quote=quote,
            quote_hash=actual_hash,
            status="quote_hash_mismatch",
            level="none",
            reason=f"quoted_text_hash mismatch: expected {expected_hash}, actual {actual_hash}",
        )

    if target_type == "source":
        span = trace_quote_in_source_material(conn, source_id=target_id, quote=quote)
        if span:
            chunk_confidence = cast(float | None, span.get("confidence"))
            if chunk_confidence is not None and chunk_confidence < 0.6:
                return _result(
                    finding_id=finding_id,
                    citation_id=citation_id,
                    target_type=target_type,
                    target_id=target_id,
                    proposition_text=proposition_text,
                    quote=quote,
                    quote_hash=actual_hash,
                    status="low_confidence_source_chunk",
                    level="orientation",
                    reason="quote was found only in a low-confidence source chunk and cannot serve as final proof",
                    source_chunk_id=str(span.get("source_chunk_id") or ""),
                    start_offset=cast(int | None, span.get("start_offset")),
                    end_offset=cast(int | None, span.get("end_offset")),
                )
            return _result(
                finding_id=finding_id,
                citation_id=citation_id,
                target_type=target_type,
                target_id=target_id,
                proposition_text=proposition_text,
                quote=quote,
                quote_hash=actual_hash,
                status="verified_quote_in_source",
                level="quote",
                reason="quote found in current non-stale source chunk",
                source_chunk_id=str(span.get("source_chunk_id") or ""),
                start_offset=cast(int | None, span.get("start_offset")),
                end_offset=cast(int | None, span.get("end_offset")),
            )
        if quote_found_in_source_material(conn, source_id=target_id, quote=quote):
            return _result(
                finding_id=finding_id,
                citation_id=citation_id,
                target_type=target_type,
                target_id=target_id,
                proposition_text=proposition_text,
                quote=quote,
                quote_hash=actual_hash,
                status="source_material_fallback_orientation_only",
                level="orientation",
                reason="quote found only in extracted/OCR source material fallback without current source chunk proof",
            )
        return _result(
            finding_id=finding_id,
            citation_id=citation_id,
            target_type=target_type,
            target_id=target_id,
            proposition_text=proposition_text,
            quote=quote,
            quote_hash=actual_hash,
            status="quote_not_found",
            level="none",
            reason="quote was not found in current non-stale source material",
        )

    if target_type == "artifact":
        if _artifact_is_source_material_derivative(conn, artifact_id=target_id) and not _artifact_has_active_registry_certification(conn, artifact_id=target_id):
            return _result(
                finding_id=finding_id,
                citation_id=citation_id,
                target_type=target_type,
                target_id=target_id,
                proposition_text=proposition_text,
                quote=quote,
                quote_hash=actual_hash,
                status="derivative_artifact_not_independent_evidence",
                level="none",
                reason="OCR/extraction derivative artifacts cannot prove material facts directly; cite the source/chunk or an actively certified registry entry",
            )
        if quote_found_in_artifact(conn, artifact_id=target_id, quote=quote):
            return _result(
                finding_id=finding_id,
                citation_id=citation_id,
                target_type=target_type,
                target_id=target_id,
                proposition_text=proposition_text,
                quote=quote,
                quote_hash=actual_hash,
                status="verified_quote_in_artifact",
                level="quote",
                reason="quote found in cited non-stale artifact",
            )
        return _result(
            finding_id=finding_id,
            citation_id=citation_id,
            target_type=target_type,
            target_id=target_id,
            proposition_text=proposition_text,
            quote=quote,
            quote_hash=actual_hash,
            status="quote_not_found",
            level="none",
            reason="quote was not found in cited artifact",
        )

    if target_type == "authority":
        if quote_found_in_authority(conn, authority_id=target_id, quote=quote):
            return _result(
                finding_id=finding_id,
                citation_id=citation_id,
                target_type=target_type,
                target_id=target_id,
                proposition_text=proposition_text,
                quote=quote,
                quote_hash=actual_hash,
                status="verified_quote_in_authority",
                level="quote",
                reason="quote found in authority record text",
                authority_support_status="current_proposition_supported",
            )
        return _result(
            finding_id=finding_id,
            citation_id=citation_id,
            target_type=target_type,
            target_id=target_id,
            proposition_text=proposition_text,
            quote=quote,
            quote_hash=actual_hash,
            status="quote_not_found",
            level="none",
            reason="quote was not found in cited authority record text",
        )

    return _result(
        finding_id=finding_id,
        citation_id=citation_id,
        target_type=target_type,
        target_id=target_id,
        proposition_text=proposition_text,
        quote=quote,
        quote_hash=actual_hash,
        status="orientation_only_target",
        level="none",
        reason=f"{target_type} citations cannot prove material legal support",
    )


def _result(
    *,
    finding_id: str,
    citation_id: str,
    target_type: str,
    target_id: str,
    proposition_text: str,
    quote: str,
    status: str,
    level: str,
    reason: str,
    quote_hash: str = "",
    source_chunk_id: str = "",
    start_offset: int | None = None,
    end_offset: int | None = None,
    authority_support_status: str = "",
) -> CitationSupportResult:
    semantic_status, confidence, requires_human = _semantic_support(
        proposition_text=proposition_text,
        quote=quote,
        support_status=status,
    )
    return CitationSupportResult(
        finding_id=finding_id,
        citation_id=citation_id,
        target_type=target_type,
        target_id=target_id,
        quote_text=quote,
        quote_hash=quote_hash or (normalized_quote_sha256(quote) if quote else ""),
        proposition_text=proposition_text,
        semantic_support_status=semantic_status,
        authority_support_status=authority_support_status,
        source_chunk_id=source_chunk_id,
        start_offset=start_offset,
        end_offset=end_offset,
        support_confidence=confidence,
        requires_human_review=requires_human,
        support_status=status,
        support_level=level,
        reason=reason,
    )


def _summary_details(*, results: list[CitationSupportResult], failures: list[CitationSupportResult]) -> dict[str, object]:
    checked = [
        {
            "finding_id": result.finding_id,
            "citation_id": result.citation_id,
            "target": f"{result.target_type}:{result.target_id}",
            "support_status": result.support_status,
        }
        for result in results
        if _support_status_passes(result.support_status)
    ]
    missing_quote = [_legacy_failure(result) for result in results if result.support_status == "no_quote_supplied"]
    hash_mismatch = [
        {
            "finding_id": result.finding_id,
            "citation_id": result.citation_id,
            "actual": result.quote_hash,
            "reason": result.reason,
        }
        for result in results
        if result.support_status == "quote_hash_mismatch"
    ]
    quote_not_found = [_legacy_failure(result) for result in results if result.support_status == "quote_not_found"]
    unsupported_law = [_legacy_failure(result) for result in results if result.support_status == "unsupported_law_without_verified_authority"]
    orientation_only = [_legacy_failure(result) for result in results if result.support_status == "orientation_only_target"]
    derivative_artifact = [_legacy_failure(result) for result in results if result.support_status == "derivative_artifact_not_independent_evidence"]
    source_fallback = [_legacy_failure(result) for result in results if result.support_status == "source_material_fallback_orientation_only"]
    low_confidence_source_chunk = [_legacy_failure(result) for result in results if result.support_status == "low_confidence_source_chunk"]
    return {
        "required": True,
        "quote_support_checked": True,
        "semantic_support_checked": True,
        "checked_citations": checked,
        "support_result_count": len(results),
        "failed_support_result_count": len(failures),
        "missing_quote": missing_quote,
        "hash_mismatch": hash_mismatch,
        "quote_not_found": quote_not_found,
        "unsupported_law_without_verified_authority": unsupported_law,
        "orientation_only_target": orientation_only,
        "derivative_artifact_not_independent_evidence": derivative_artifact,
        "source_material_fallback_orientation_only": source_fallback,
        "low_confidence_source_chunk": low_confidence_source_chunk,
        "support_statuses": [result.as_dict() for result in results],
        "note": "This deterministic gate verifies quoted text/hash presence, target role, span metadata where available, and lexical proposition support. It marks semantic ambiguity for human/legal review instead of treating draft text as proof.",
    }


def _legacy_failure(result: CitationSupportResult) -> dict[str, str]:
    return {
        "finding_id": result.finding_id,
        "citation_id": result.citation_id,
        "target": f"{result.target_type}:{result.target_id}",
        "reason": result.reason,
    }


def _support_status_passes(status: str) -> bool:
    return status in {"verified_quote_in_source", "verified_quote_in_authority", "verified_quote_in_artifact"}


def _semantic_support(*, proposition_text: str, quote: str, support_status: str) -> tuple[str, float | None, bool]:
    if not _support_status_passes(support_status):
        return "unchecked_requires_human", None, True
    proposition_terms = _semantic_terms(proposition_text)
    quote_terms = _semantic_terms(quote)
    if _negation_mismatch(proposition_text, quote) and (proposition_terms & quote_terms):
        return "contradicted", 0.2, True
    if not proposition_terms:
        return "unchecked_requires_human", None, True
    if not quote_terms:
        return "unchecked_requires_human", None, True
    overlap = len(proposition_terms & quote_terms) / max(1, len(proposition_terms))
    if overlap >= 0.45:
        return "supported", round(min(0.95, 0.55 + overlap / 2), 3), False
    if overlap > 0 or proposition_terms <= {"source", "supported", "fact", "finding", "authority", "legal", "rule"}:
        return "partially_supported", round(max(0.35, overlap), 3), False
    return "unsupported", 0.15, True


def _semantic_terms(text: str) -> set[str]:
    stop = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
    return {term for term in re.findall(r"[a-z0-9£$]+", text.casefold()) if len(term) >= 3 and term not in stop}


def _negation_mismatch(proposition_text: str, quote: str) -> bool:
    negations = {"no", "not", "never", "without", "cannot", "can't", "mustn't", "isn't", "doesn't", "didn't"}
    proposition_has_negation = bool(_semantic_terms(proposition_text) & negations)
    quote_has_negation = bool(_semantic_terms(quote) & negations)
    return proposition_has_negation != quote_has_negation


def trace_quote_in_source_material(conn: sqlite3.Connection, *, source_id: str, quote: str) -> dict[str, object]:
    normalized_quote = normalize_quote_text(quote).casefold()
    if not normalized_quote:
        return {}
    rows = cast(
        list[SqlRow],
        conn.execute(
            """
            SELECT sc.chunk_id, sc.start_offset, sc.end_offset, sc.text, sc.text_hash, sc.confidence
            FROM source_chunks sc
            JOIN sources s ON s.source_id = sc.source_id AND s.matter_scope = sc.matter_scope
            LEFT JOIN artifacts a ON a.artifact_id = sc.artifact_id AND a.matter_scope = sc.matter_scope
            WHERE sc.source_id = ?
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
            ORDER BY sc.start_offset ASC, sc.chunk_id
            """,
            (source_id,),
        ).fetchall(),
    )
    for row in rows:
        text = str(row["text"] or "")
        text_hash = str(row["text_hash"] or "").strip().lower()
        if text_hash and text_hash != normalized_quote_sha256(text):
            continue
        normalized_text = normalize_quote_text(text).casefold()
        index = normalized_text.find(normalized_quote)
        if index < 0 and quote_matches_text(normalized_quote, normalized_text):
            index = _first_quote_fragment_index(normalized_quote, normalized_text)
        if index < 0:
            continue
        return {
            "source_chunk_id": row["chunk_id"],
            "start_offset": int(row["start_offset"] or 0) + index,
            "end_offset": int(row["start_offset"] or 0) + index + len(normalized_quote),
            "confidence": float(str(row["confidence"])) if row["confidence"] is not None else None,
        }
    return {}


def _first_quote_fragment_index(normalized_quote: str, normalized_content: str) -> int:
    if "..." not in normalized_quote and "…" not in normalized_quote:
        return -1
    fragments = [
        fragment.strip()
        for fragment in re.split(r"(?:\.{3,}|…)", normalized_quote)
        if len(fragment.strip()) >= 3
    ]
    if not fragments:
        return -1
    return normalized_content.find(fragments[0])


def _artifact_is_source_material_derivative(conn: sqlite3.Connection, *, artifact_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM artifacts a
        LEFT JOIN extraction_records er ON er.artifact_id = a.artifact_id
        LEFT JOIN ocr_records ocr ON ocr.artifact_id = a.artifact_id
        LEFT JOIN transcription_records tr ON tr.artifact_id = a.artifact_id
        WHERE a.artifact_id = ?
          AND (
            a.artifact_type IN ('extracted_text', 'extraction_record', 'ocr_extract', 'ocr_text', 'transcription_record', 'transcript')
            OR er.extraction_id IS NOT NULL
            OR ocr.ocr_id IS NOT NULL
            OR tr.transcription_id IS NOT NULL
          )
        LIMIT 1
        """,
        (artifact_id,),
    ).fetchone()
    return row is not None


def _artifact_has_active_registry_certification(conn: sqlite3.Connection, *, artifact_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM certifications
        WHERE subject_type = 'artifact'
          AND subject_id = ?
          AND status = 'active'
          AND certification_type IN ('evidence_registry', 'source_registry', 'certified_evidence_registry')
        LIMIT 1
        """,
        (artifact_id,),
    ).fetchone()
    return row is not None


def _replace_support_results(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    candidate_id: str,
    artifact_id: str | None,
    results: list[CitationSupportResult],
) -> None:
    _ = conn.execute("DELETE FROM citation_support_results WHERE candidate_id = ?", (candidate_id,))
    for result in results:
        _ = conn.execute(
            """
            INSERT INTO citation_support_results(
              citation_support_result_id, matter_scope, candidate_id, artifact_id, finding_id,
              citation_id, target_type, target_id, quote_text, quote_hash, proposition_text,
              semantic_support_status, authority_support_status, source_chunk_id, start_offset,
              end_offset, support_confidence, requires_human_review, support_status,
              support_level, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"csr-{uuid4().hex}",
                matter_scope,
                candidate_id,
                artifact_id,
                result.finding_id,
                result.citation_id,
                result.target_type,
                result.target_id,
                result.quote_text,
                result.quote_hash,
                result.proposition_text,
                result.semantic_support_status,
                result.authority_support_status,
                result.source_chunk_id or None,
                result.start_offset,
                result.end_offset,
                result.support_confidence,
                int(result.requires_human_review),
                result.support_status,
                result.support_level,
                result.reason,
                repo.utc_now(),
            ),
        )


def _source_material_query() -> str:
    return """
        SELECT a.content, NULL AS text_hash
        FROM artifacts a
        JOIN artifact_sources af ON af.artifact_id = a.artifact_id
        JOIN sources s ON s.source_id = af.source_id AND s.matter_scope = a.matter_scope
        WHERE af.source_id = ? AND a.stale = 0 AND s.stale = 0
        UNION
        SELECT a.content, NULL AS text_hash
        FROM extraction_records er
        JOIN artifacts a ON a.artifact_id = er.artifact_id
        JOIN sources s ON s.source_id = er.source_id AND s.matter_scope = a.matter_scope
        WHERE er.source_id = ?
          AND a.stale = 0
          AND s.stale = 0
          AND (json_extract(er.metadata_json, '$.source_sha256') IS NULL OR json_extract(er.metadata_json, '$.source_sha256') = s.sha256)
        UNION
        SELECT a.content, NULL AS text_hash
        FROM ocr_records ocr
        JOIN artifacts a ON a.artifact_id = ocr.artifact_id
        JOIN sources s ON s.source_id = ocr.source_id AND s.matter_scope = a.matter_scope
        WHERE ocr.source_id = ?
          AND a.stale = 0
          AND s.stale = 0
          AND (json_extract(ocr.metadata_json, '$.source_sha256') IS NULL OR json_extract(ocr.metadata_json, '$.source_sha256') = s.sha256)
        UNION
        SELECT a.content, NULL AS text_hash
        FROM transcription_records tr
        JOIN artifacts a ON a.artifact_id = tr.artifact_id
        JOIN sources s ON s.source_id = tr.source_id AND s.matter_scope = a.matter_scope
        WHERE tr.source_id = ? AND a.stale = 0 AND s.stale = 0
        UNION
        SELECT sc.text AS content, sc.text_hash
        FROM source_chunks sc
        JOIN sources s ON s.source_id = sc.source_id AND s.matter_scope = sc.matter_scope
        LEFT JOIN artifacts a ON a.artifact_id = sc.artifact_id AND a.matter_scope = sc.matter_scope
        WHERE sc.source_id = ?
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
    """


def _quote_found_in_rows(conn: sqlite3.Connection, *, quote: str, query: str, params: tuple[object, ...]) -> bool:
    normalized_quote = normalize_quote_text(quote).casefold()
    if not normalized_quote:
        return False
    rows = cast(list[SqlRow], conn.execute(query, params).fetchall())
    for row in rows:
        content = str(row["content"] or "")
        if "text_hash" in row.keys():
            text_hash = str(row["text_hash"] or "").strip().lower()
            if text_hash and text_hash != normalized_quote_sha256(content):
                continue
        normalized_content = normalize_quote_text(content).casefold()
        if quote_matches_text(normalized_quote, normalized_content):
            return True
    return False


def quote_matches_text(normalized_quote: str, normalized_content: str) -> bool:
    if normalized_quote in normalized_content:
        return True
    if "..." not in normalized_quote and "…" not in normalized_quote:
        return False
    fragments = [
        fragment.strip()
        for fragment in re.split(r"(?:\.{3,}|…)", normalized_quote)
        if len(fragment.strip()) >= 3
    ]
    if not fragments:
        return False
    position = 0
    for fragment in fragments:
        index = normalized_content.find(fragment, position)
        if index < 0:
            return False
        position = index + len(fragment)
    return True


def _verified_current_authority_texts(conn: sqlite3.Connection, *, authority_id: str) -> list[str]:
    rows = cast(
        list[SqlRow],
        conn.execute(
            """
            SELECT av.details_json
            FROM authority_verifications av
            JOIN legal_authorities la ON la.authority_id = av.authority_id
            WHERE av.authority_id = ?
              AND la.status != 'rejected'
              AND av.currentness_status = 'current'
              AND av.proposition_supported = 1
            ORDER BY av.checked_at DESC, av.authority_verification_id DESC
            """,
            (authority_id,),
        ).fetchall(),
    )
    texts: list[str] = []
    for row in rows:
        details = _json_dict(str(row["details_json"] or "{}"))
        for key in ("authority_text", "text", "excerpt", "quote", "quoted_text"):
            value = details.get(key)
            if isinstance(value, str) and value.strip() and _text_hash_valid(details, key=key, text=value):
                texts.append(value)
        quotes = details.get("quotes")
        if isinstance(quotes, list):
            for quote in quotes:
                if isinstance(quote, str) and quote.strip():
                    texts.append(quote)
                elif isinstance(quote, Mapping):
                    value = quote.get("text") or quote.get("quote") or quote.get("excerpt")
                    if isinstance(value, str) and value.strip() and _text_hash_valid(quote, key="text", text=value):
                        texts.append(value)
    return texts


def _text_hash_valid(details: Mapping[str, object], *, key: str, text: str) -> bool:
    expected = str(
        details.get(f"{key}_hash")
        or details.get(f"{key}_sha256")
        or details.get("authority_text_hash")
        or details.get("text_hash")
        or details.get("quoted_text_hash")
        or ""
    ).strip().lower()
    return not expected or expected == normalized_quote_sha256(text)


def _json_dict(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}
