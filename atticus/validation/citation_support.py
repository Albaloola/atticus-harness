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
    support_status: str
    support_level: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "finding_id": self.finding_id,
            "citation_id": self.citation_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "quote_text": self.quote_text,
            "quote_hash": self.quote_hash,
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
            results.append(_check_citation(conn, finding_id=finding_id, finding_type=finding_type, citation=citation))

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
    return _quote_found_in_rows(
        conn,
        quote=quote,
        query="""
            SELECT citation || ' ' || title || ' ' || source_url AS content
            FROM legal_authorities
            WHERE authority_id = ? AND status != 'rejected'
        """,
        params=(authority_id,),
    )


def _check_citation(
    conn: sqlite3.Connection,
    *,
    finding_id: str,
    finding_type: str,
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
            quote=quote,
            quote_hash=actual_hash,
            status="quote_hash_mismatch",
            level="none",
            reason=f"quoted_text_hash mismatch: expected {expected_hash}, actual {actual_hash}",
        )

    if target_type == "source":
        if quote_found_in_source_material(conn, source_id=target_id, quote=quote):
            return _result(
                finding_id=finding_id,
                citation_id=citation_id,
                target_type=target_type,
                target_id=target_id,
                quote=quote,
                quote_hash=actual_hash,
                status="verified_quote_in_source",
                level="quote",
                reason="quote found in current non-stale extracted source material",
            )
        return _result(
            finding_id=finding_id,
            citation_id=citation_id,
            target_type=target_type,
            target_id=target_id,
            quote=quote,
            quote_hash=actual_hash,
            status="quote_not_found",
            level="none",
            reason="quote was not found in current non-stale source material",
        )

    if target_type == "artifact":
        if quote_found_in_artifact(conn, artifact_id=target_id, quote=quote):
            return _result(
                finding_id=finding_id,
                citation_id=citation_id,
                target_type=target_type,
                target_id=target_id,
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
                quote=quote,
                quote_hash=actual_hash,
                status="verified_quote_in_authority",
                level="quote",
                reason="quote found in authority record text",
            )
        return _result(
            finding_id=finding_id,
            citation_id=citation_id,
            target_type=target_type,
            target_id=target_id,
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
    quote: str,
    status: str,
    level: str,
    reason: str,
    quote_hash: str = "",
) -> CitationSupportResult:
    return CitationSupportResult(
        finding_id=finding_id,
        citation_id=citation_id,
        target_type=target_type,
        target_id=target_id,
        quote_text=quote,
        quote_hash=quote_hash or (normalized_quote_sha256(quote) if quote else ""),
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
    return {
        "required": True,
        "quote_support_checked": True,
        "semantic_support_checked": False,
        "checked_citations": checked,
        "support_result_count": len(results),
        "failed_support_result_count": len(failures),
        "missing_quote": missing_quote,
        "hash_mismatch": hash_mismatch,
        "quote_not_found": quote_not_found,
        "unsupported_law_without_verified_authority": unsupported_law,
        "orientation_only_target": orientation_only,
        "support_statuses": [result.as_dict() for result in results],
        "note": "This deterministic gate verifies quoted text/hash presence and target role; it does not infer legal semantic support.",
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
              citation_id, target_type, target_id, quote_text, quote_hash, support_status,
              support_level, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                result.support_status,
                result.support_level,
                result.reason,
                repo.utc_now(),
            ),
        )


def _source_material_query() -> str:
    return """
        SELECT a.content
        FROM artifacts a
        JOIN artifact_sources af ON af.artifact_id = a.artifact_id
        WHERE af.source_id = ? AND a.stale = 0
        UNION
        SELECT a.content
        FROM extraction_records er
        JOIN artifacts a ON a.artifact_id = er.artifact_id
        WHERE er.source_id = ? AND a.stale = 0
        UNION
        SELECT a.content
        FROM ocr_records ocr
        JOIN artifacts a ON a.artifact_id = ocr.artifact_id
        WHERE ocr.source_id = ? AND a.stale = 0
        UNION
        SELECT a.content
        FROM transcription_records tr
        JOIN artifacts a ON a.artifact_id = tr.artifact_id
        WHERE tr.source_id = ? AND a.stale = 0
        UNION
        SELECT text AS content
        FROM source_chunks
        WHERE source_id = ?
    """


def _quote_found_in_rows(conn: sqlite3.Connection, *, quote: str, query: str, params: tuple[object, ...]) -> bool:
    normalized_quote = normalize_quote_text(quote).casefold()
    if not normalized_quote:
        return False
    rows = cast(list[SqlRow], conn.execute(query, params).fetchall())
    for row in rows:
        normalized_content = normalize_quote_text(str(row["content"] or "")).casefold()
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
