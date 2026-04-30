"""Durable validation gates for legal evidence and reducer packets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import cast, Protocol

from atticus.db import repo
from atticus.workers.citation_context import allowed_citation_targets_for_task, proof_citation_targets_for_task
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION, ResultPacketError, parse_result

SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
AUTHORITY_CITATION_RE = re.compile(r"(\d{4}|\[[0-9]{4}\]|\b[A-Z][A-Za-z]+ v [A-Z])")
SqlRow = Mapping[str, object]


@dataclass(frozen=True)
class ValidationOutcome:
    gate_name: str
    target_type: str
    target_id: str
    passed: bool
    details: dict[str, object]
    validation_result_id: int


class ValidationHandler(Protocol):
    def __call__(self, conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]: ...


def run_validation(
    conn: sqlite3.Connection,
    *,
    gate_name: str,
    target_type: str,
    target_id: str,
) -> ValidationOutcome:
    handlers: dict[str, ValidationHandler] = {
        "source_inventory": validate_source_inventory,
        "hash_validity": validate_hash_validity,
        "extraction_coverage": validate_extraction_coverage,
        "production_mapping": validate_production_mapping_integrity,
        "evidence_registry": validate_evidence_registry,
        "chronology_citations": validate_chronology_citation_completeness,
        "claim_evidence_support": validate_claim_evidence_support,
        "authority_citation_format": validate_authority_citation_format,
        "citation_target_integrity": validate_citation_integrity,
        "citation_integrity": validate_citation_integrity,
        "legal_citation_integrity": validate_citation_integrity,
        "citation_support_integrity": validate_citation_support_integrity,
        "privacy_redaction": validate_privacy_redaction,
        "hostile_review": validate_hostile_review_certification,
        "cross_matter_isolation": validate_cross_matter_isolation,
        "stale_dependency": validate_no_stale_dependencies,
        "reducer_packet_schema": validate_reducer_packet_schema,
        "canonical_write_authorization": validate_canonical_write_authorization,
        "foundation": validate_foundation,
    }
    handler = handlers.get(gate_name)
    if handler is None:
        details: dict[str, object] = {"error": f"unknown validation gate: {gate_name}"}
        passed = False
    else:
        passed, details = handler(conn, target_type=target_type, target_id=target_id)
    validation_id = repo.record_validation(
        conn,
        target_type=target_type,
        target_id=target_id,
        gate_name=gate_name,
        passed=passed,
        details=details,
        severity="info" if passed else "error",
    )
    return ValidationOutcome(gate_name, target_type, target_id, passed, details, validation_id)


def validate_foundation(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type == "artifact":
        row = cast(SqlRow | None, conn.execute("SELECT stale, trust_status FROM artifacts WHERE artifact_id = ?", (target_id,)).fetchone())
        if row is None:
            return False, {"error": "artifact not found"}
        return not bool(row["stale"]), {"trust_status": row["trust_status"], "stale": bool(row["stale"])}
    if target_type == "matter":
        count = int(str(cast(SqlRow, conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE matter_scope = ?",
            (target_id,),
        ).fetchone())["n"]))
        return count > 0, {"source_count": count}
    return False, {"error": f"unsupported target type for foundation: {target_type}"}


def validate_source_inventory(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type != "matter":
        return False, {"error": "source_inventory must target matter"}
    rows = cast(list[SqlRow], conn.execute(
        "SELECT source_id, sha256, stale FROM sources WHERE matter_scope = ?",
        (target_id,),
    ).fetchall())
    bad_hashes = [row["source_id"] for row in rows if not SHA256_RE.match(str(row["sha256"]))]
    stale = [row["source_id"] for row in rows if row["stale"]]
    return bool(rows) and not bad_hashes and not stale, {
        "source_count": len(rows),
        "bad_hashes": bad_hashes,
        "stale_sources": stale,
    }


def validate_hash_validity(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type == "source":
        row = cast(SqlRow | None, conn.execute("SELECT sha256 FROM sources WHERE source_id = ?", (target_id,)).fetchone())
    elif target_type == "artifact":
        row = cast(SqlRow | None, conn.execute("SELECT sha256 FROM artifacts WHERE artifact_id = ?", (target_id,)).fetchone())
    else:
        return False, {"error": "hash_validity supports source or artifact"}
    if row is None:
        return False, {"error": f"{target_type} not found"}
    value = row["sha256"]
    return bool(value and SHA256_RE.match(str(value))), {"sha256": value}


def validate_extraction_coverage(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type != "matter":
        return False, {"error": "extraction_coverage must target matter"}
    sources = cast(
        list[SqlRow],
        conn.execute("SELECT source_id, sha256 FROM sources WHERE matter_scope = ? ORDER BY source_id", (target_id,)).fetchall(),
    )
    missing: list[str] = []
    stale_extraction: list[str] = []
    hash_mismatch_extraction: list[str] = []
    low_confidence_ocr: list[str] = []
    for source in sources:
        coverage = _current_extraction_coverage(conn, source_id=str(source["source_id"]), source_sha256=str(source["sha256"]))
        if not coverage["has_rows"]:
            missing.append(str(source["source_id"]))
        if coverage["stale_or_missing_artifact"]:
            stale_extraction.append(str(source["source_id"]))
        if coverage["hash_mismatch"]:
            hash_mismatch_extraction.append(str(source["source_id"]))
        if coverage["low_confidence_ocr"]:
            low_confidence_ocr.append(str(source["source_id"]))
        if coverage["has_rows"] and not coverage["current_complete"] and not (
            coverage["stale_or_missing_artifact"] or coverage["hash_mismatch"] or coverage["low_confidence_ocr"]
        ):
            missing.append(str(source["source_id"]))
    total = len(sources)
    passed = total > 0 and not (missing or stale_extraction or hash_mismatch_extraction or low_confidence_ocr)
    return passed, {
        "source_count": total,
        "missing_extraction": sorted(set(missing)),
        "stale_extraction": sorted(set(stale_extraction)),
        "hash_mismatch_extraction": sorted(set(hash_mismatch_extraction)),
        "low_confidence_ocr": sorted(set(low_confidence_ocr)),
    }


def _current_extraction_coverage(conn: sqlite3.Connection, *, source_id: str, source_sha256: str) -> dict[str, bool]:
    rows = conn.execute(
        """
        SELECT 'extraction' AS record_type, er.coverage_status, er.confidence, er.metadata_json,
          a.artifact_id, a.stale AS artifact_stale
        FROM extraction_records er
        LEFT JOIN artifacts a ON a.artifact_id = er.artifact_id
        WHERE er.source_id = ?
        UNION ALL
        SELECT 'ocr' AS record_type, ocr.coverage_status, NULL AS confidence, ocr.metadata_json,
          a.artifact_id, a.stale AS artifact_stale
        FROM ocr_records ocr
        LEFT JOIN artifacts a ON a.artifact_id = ocr.artifact_id
        WHERE ocr.source_id = ?
        UNION ALL
        SELECT 'transcription' AS record_type, tr.coverage_status, NULL AS confidence, tr.metadata_json,
          a.artifact_id, a.stale AS artifact_stale
        FROM transcription_records tr
        LEFT JOIN artifacts a ON a.artifact_id = tr.artifact_id
        WHERE tr.source_id = ?
        """,
        (source_id, source_id, source_id),
    ).fetchall()
    result = {
        "has_rows": bool(rows),
        "current_complete": False,
        "stale_or_missing_artifact": False,
        "hash_mismatch": False,
        "low_confidence_ocr": False,
    }
    for row in cast(list[SqlRow], rows):
        metadata = _json_dict(str(row["metadata_json"] or "{}"))
        row_sha = str(metadata.get("source_sha256") or "")
        artifact_missing_or_stale = row["artifact_id"] is None or int(row["artifact_stale"] or 0) == 1
        if artifact_missing_or_stale:
            result["stale_or_missing_artifact"] = True
        if row_sha != source_sha256:
            result["hash_mismatch"] = True
        if str(row["record_type"]) == "ocr":
            confidence = _float(metadata.get("confidence"), default=1.0)
            if confidence < 0.6:
                result["low_confidence_ocr"] = True
        elif row["confidence"] is not None and _float(row["confidence"], default=1.0) < 0.6:
            result["low_confidence_ocr"] = True
        if str(row["coverage_status"]) == "complete" and not artifact_missing_or_stale and row_sha == source_sha256 and not result["low_confidence_ocr"]:
            result["current_complete"] = True
    if result["current_complete"]:
        result["stale_or_missing_artifact"] = False
        result["hash_mismatch"] = False
        result["low_confidence_ocr"] = False
    return result


def validate_production_mapping_integrity(
    conn: sqlite3.Connection, *, target_type: str, target_id: str
) -> tuple[bool, dict[str, object]]:
    if target_type != "matter":
        return False, {"error": "production_mapping must target matter"}
    rows = cast(list[SqlRow], conn.execute(
        "SELECT mapping_id, source_id, artifact_id, production_id FROM production_mappings WHERE matter_scope = ?",
        (target_id,),
    ).fetchall())
    broken = [
        row["mapping_id"]
        for row in rows
        if not row["production_id"] or (row["source_id"] is None and row["artifact_id"] is None)
    ]
    return bool(rows) and not broken, {"mapping_count": len(rows), "broken_mappings": broken}


def validate_evidence_registry(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type != "matter":
        return False, {"error": "evidence_registry must target matter"}
    rows = cast(list[SqlRow], conn.execute(
        """
        SELECT artifact_id, path, trust_status, stale
        FROM artifacts
        WHERE matter_scope = ?
          AND artifact_type IN ('evidence_registry', 'evidence_index', 'production_crosswalk')
        """,
        (target_id,),
    ).fetchall())
    usable = [
        row["artifact_id"]
        for row in rows
        if not bool(row["stale"]) and row["trust_status"] not in {"rejected", "stale", "unverified_legacy"}
    ]
    return bool(usable), {
        "registry_artifact_count": len(rows),
        "usable_registry_artifacts": usable,
    }


def validate_chronology_citation_completeness(
    conn: sqlite3.Connection, *, target_type: str, target_id: str
) -> tuple[bool, dict[str, object]]:
    if target_type == "matter":
        missing = [
            row["chronology_event_id"]
            for row in cast(list[SqlRow], conn.execute(
                """
                SELECT ce.chronology_event_id
                FROM chronology_events ce
                LEFT JOIN citation_spans cs
                  ON cs.target_type = 'chronology_event' AND cs.target_id = ce.chronology_event_id
                WHERE ce.matter_scope = ? AND cs.citation_span_id IS NULL
                """,
                (target_id,),
            ).fetchall())
        ]
        count = int(str(cast(SqlRow, conn.execute(
            "SELECT COUNT(*) AS n FROM chronology_events WHERE matter_scope = ?",
            (target_id,),
        ).fetchone())["n"]))
    else:
        count = int(str(cast(SqlRow, conn.execute(
            "SELECT COUNT(*) AS n FROM chronology_events WHERE chronology_event_id = ?",
            (target_id,),
        ).fetchone())["n"]))
        span = conn.execute(
            "SELECT citation_span_id FROM citation_spans WHERE target_type = 'chronology_event' AND target_id = ?",
            (target_id,),
        ).fetchone()
        missing = [] if span else [target_id]
    return count > 0 and not missing, {"chronology_event_count": count, "missing_citations": missing}


def validate_claim_evidence_support(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type == "matter":
        missing = [
            row["claim_id"]
            for row in cast(list[SqlRow], conn.execute(
                """
                SELECT c.claim_id
                FROM claims c
                LEFT JOIN citation_spans cs ON cs.target_type = 'claim' AND cs.target_id = c.claim_id
                WHERE c.matter_scope = ? AND cs.citation_span_id IS NULL
                """,
                (target_id,),
            ).fetchall())
        ]
        claim_count = int(str(cast(SqlRow, conn.execute("SELECT COUNT(*) AS n FROM claims WHERE matter_scope = ?", (target_id,)).fetchone())["n"]))
    else:
        claim_count = int(str(cast(SqlRow, conn.execute("SELECT COUNT(*) AS n FROM claims WHERE claim_id = ?", (target_id,)).fetchone())["n"]))
        span = conn.execute(
            "SELECT citation_span_id FROM citation_spans WHERE target_type = 'claim' AND target_id = ?",
            (target_id,),
        ).fetchone()
        missing = [] if span else [target_id]
    return claim_count > 0 and not missing, {"claim_count": claim_count, "unsupported_claims": missing}


def validate_authority_citation_format(
    conn: sqlite3.Connection, *, target_type: str, target_id: str
) -> tuple[bool, dict[str, object]]:
    if target_type == "matter":
        rows = cast(list[SqlRow], conn.execute("SELECT authority_id, citation FROM legal_authorities WHERE matter_scope = ?", (target_id,)).fetchall())
    else:
        rows = cast(list[SqlRow], conn.execute("SELECT authority_id, citation FROM legal_authorities WHERE authority_id = ?", (target_id,)).fetchall())
    bad = [row["authority_id"] for row in rows if not AUTHORITY_CITATION_RE.search(str(row["citation"]))]
    return bool(rows) and not bad, {"authority_count": len(rows), "bad_citations": bad}


def validate_no_stale_dependencies(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type == "task":
        task = cast(SqlRow | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (target_id,)).fetchone())
        if task is None:
            return False, {"error": "task not found"}
        try:
            source_ids = _string_list_from_json(task["source_dependencies_json"], field="source_dependencies_json")
            artifact_ids = _string_list_from_json(task["artifact_dependencies_json"], field="artifact_dependencies_json")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return False, {"error": f"malformed dependency metadata: {exc}"}
    elif target_type == "artifact":
        source_ids = [str(row["source_id"]) for row in cast(list[SqlRow], conn.execute("SELECT source_id FROM artifact_sources WHERE artifact_id = ?", (target_id,)).fetchall())]
        artifact_ids = [
            str(row["dependency_artifact_id"])
            for row in cast(list[SqlRow], conn.execute("SELECT dependency_artifact_id FROM artifact_dependencies WHERE artifact_id = ?", (target_id,)).fetchall())
        ]
    else:
        return False, {"error": "stale_dependency supports task or artifact"}
    stale_sources = [
        row["source_id"]
        for row in cast(list[SqlRow], conn.execute(
            "SELECT source_id FROM sources WHERE stale = 1 AND source_id IN (%s)" % ",".join("?" for _ in source_ids),
            tuple(source_ids),
        ).fetchall())
    ] if source_ids else []
    stale_artifacts = [
        row["artifact_id"]
        for row in cast(list[SqlRow], conn.execute(
            "SELECT artifact_id FROM artifacts WHERE stale = 1 AND artifact_id IN (%s)" % ",".join("?" for _ in artifact_ids),
            tuple(artifact_ids),
        ).fetchall())
    ] if artifact_ids else []
    return not stale_sources and not stale_artifacts, {
        "stale_sources": stale_sources,
        "stale_artifacts": stale_artifacts,
    }


def _string_list_from_json(raw: object, *, field: str) -> list[str]:
    value = json.loads(str(raw or "[]"))
    if not isinstance(value, list):
        raise ValueError(f"{field} must contain a JSON array of strings")
    result: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str):
            raise ValueError(f"{field} must contain a JSON array of strings")
        result.append(item)
    return result


def _json_dict(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()} if isinstance(value, Mapping) else {}


def _float(value: object, *, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def validate_reducer_packet_schema(
    conn: sqlite3.Connection, *, target_type: str, target_id: str
) -> tuple[bool, dict[str, object]]:
    if target_type != "candidate":
        return False, {"error": "reducer_packet_schema must target candidate"}
    row = cast(SqlRow | None, conn.execute("SELECT payload_json, task_id FROM candidate_outputs WHERE candidate_id = ?", (target_id,)).fetchone())
    if row is None:
        return False, {"error": "candidate output not found"}
    try:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, Mapping):
            return False, {"error": "candidate output payload must be a JSON object"}
        task_id = str(row["task_id"])
        _ = parse_result(
            {str(key): value for key, value in cast(Mapping[object, object], payload).items()},
            allowed_citation_targets=allowed_citation_targets_for_task(conn, task_id=task_id),
            proof_citation_targets=proof_citation_targets_for_task(conn, task_id=task_id),
        )
    except ResultPacketError as exc:
        return False, {"error": str(exc)}
    return True, {"schema": RESULT_PACKET_SCHEMA_VERSION}


def validate_canonical_write_authorization(
    conn: sqlite3.Connection, *, target_type: str, target_id: str
) -> tuple[bool, dict[str, object]]:
    if target_type != "candidate":
        return False, {"error": "canonical_write_authorization must target candidate"}
    row = cast(SqlRow | None, conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (target_id,)).fetchone())
    if row is None:
        return False, {"error": "candidate output not found"}
    return row["status"] == "candidate", {"candidate_status": row["status"], "required_writer_role": "reducer"}


def validate_citation_integrity(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type == "candidate":
        row = cast(SqlRow | None, conn.execute("SELECT payload_json, task_id FROM candidate_outputs WHERE candidate_id = ?", (target_id,)).fetchone())
        if row is None:
            return False, {"error": "candidate output not found"}
        try:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, Mapping):
                return False, {"error": "candidate output payload must be a JSON object"}
            task_id = str(row["task_id"])
            packet = parse_result(
                {str(key): value for key, value in cast(Mapping[object, object], payload).items()},
                allowed_citation_targets=allowed_citation_targets_for_task(conn, task_id=task_id),
                proof_citation_targets=proof_citation_targets_for_task(conn, task_id=task_id),
            )
        except (json.JSONDecodeError, ResultPacketError) as exc:
            return False, {"error": str(exc)}
        return True, {
            "citation_count": len(packet.citations),
            "proof_target_checked": True,
            "quote_support_checked": False,
            "semantic_support_checked": False,
            "note": "This gate verifies citation target eligibility only; use citation_support_integrity for quote/hash support checks.",
        }
    if target_type == "artifact":
        row = cast(SqlRow | None, conn.execute("SELECT stale, trust_status FROM artifacts WHERE artifact_id = ?", (target_id,)).fetchone())
        if row is None:
            return False, {"error": "artifact not found"}
        ok = not bool(row["stale"]) and str(row["trust_status"]) in {"validated", "certified"}
        return ok, {"trust_status": row["trust_status"], "stale": bool(row["stale"])}
    return False, {"error": "citation_integrity supports candidate or artifact"}


def validate_citation_support_integrity(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type != "candidate":
        return False, {"error": "citation_support_integrity must target candidate"}
    row = cast(
        SqlRow | None,
        conn.execute(
            """
            SELECT co.payload_json, co.task_id, t.stage, t.task_type
            FROM candidate_outputs co
            JOIN tasks t ON t.task_id = co.task_id
            WHERE co.candidate_id = ?
            """,
            (target_id,),
        ).fetchone(),
    )
    if row is None:
        return False, {"error": "candidate output not found"}
    try:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, Mapping):
            return False, {"error": "candidate output payload must be a JSON object"}
        task_id = str(row["task_id"])
        packet = parse_result(
            {str(key): value for key, value in cast(Mapping[object, object], payload).items()},
            allowed_citation_targets=allowed_citation_targets_for_task(conn, task_id=task_id),
            proof_citation_targets=proof_citation_targets_for_task(conn, task_id=task_id),
        )
    except (json.JSONDecodeError, ResultPacketError) as exc:
        return False, {"error": str(exc)}

    stage = str(row["stage"] or "")
    task_type = str(row["task_type"] or "")
    required = stage in {"S6", "S7", "S8", "S9"} or task_type in {
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
    if not required:
        return True, {
            "required": False,
            "quote_support_checked": False,
            "reason": "quote support is not mandatory for this stage/task type",
        }

    citations_by_id = {str(citation["citation_id"]): citation for citation in packet.citations}
    missing_quote: list[dict[str, str]] = []
    hash_mismatch: list[dict[str, str]] = []
    quote_not_found: list[dict[str, str]] = []
    checked: list[dict[str, str]] = []
    material_types = {"fact", "law", "procedure", "contradiction", "risk"}
    material_statuses = {"supported", "inferred", "contradicted"}
    for finding in packet.findings:
        finding_id = str(finding.get("finding_id") or "")
        finding_type = str(finding.get("finding_type") or "")
        reasoning_status = str(finding.get("reasoning_status") or "")
        if finding_type not in material_types or reasoning_status not in material_statuses:
            continue
        for citation_id in [str(item) for item in cast(list[object], finding.get("citation_ids") or []) if str(item)]:
            citation = citations_by_id.get(citation_id)
            if citation is None:
                continue
            quote = str(citation.get("quote") or citation.get("excerpt") or "").strip()
            target_type_raw = str(citation.get("target_type") or "")
            target_id_raw = str(citation.get("target_id") or "")
            if not quote:
                missing_quote.append({"finding_id": finding_id, "citation_id": citation_id, "target": f"{target_type_raw}:{target_id_raw}"})
                continue
            expected_hash = str(citation.get("quoted_text_hash") or "").strip().lower()
            actual_hash = _normalized_quote_sha256(quote)
            if expected_hash and expected_hash != actual_hash:
                hash_mismatch.append({"finding_id": finding_id, "citation_id": citation_id, "expected": expected_hash, "actual": actual_hash})
                continue
            if target_type_raw == "source" and not _quote_found_in_source_material(conn, source_id=target_id_raw, quote=quote):
                quote_not_found.append({"finding_id": finding_id, "citation_id": citation_id, "target": f"source:{target_id_raw}"})
                continue
            checked.append({"finding_id": finding_id, "citation_id": citation_id, "target": f"{target_type_raw}:{target_id_raw}"})

    passed = not (missing_quote or hash_mismatch or quote_not_found)
    return passed, {
        "required": True,
        "quote_support_checked": True,
        "semantic_support_checked": False,
        "checked_citations": checked,
        "missing_quote": missing_quote,
        "hash_mismatch": hash_mismatch,
        "quote_not_found": quote_not_found,
        "note": "This deterministic gate verifies quoted text/hash presence; it does not infer legal semantic support.",
    }


def _normalized_quote_sha256(text: str) -> str:
    return hashlib.sha256(_normalize_quote_text(text).encode("utf-8")).hexdigest()


def _normalize_quote_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _quote_found_in_source_material(conn: sqlite3.Connection, *, source_id: str, quote: str) -> bool:
    normalized_quote = _normalize_quote_text(quote).casefold()
    if not normalized_quote:
        return False
    rows = cast(
        list[SqlRow],
        conn.execute(
            """
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
            """,
            (source_id, source_id, source_id, source_id),
        ).fetchall(),
    )
    for row in rows:
        normalized_content = _normalize_quote_text(str(row["content"] or "")).casefold()
        if _quote_matches_text(normalized_quote, normalized_content):
            return True
    return False


def _quote_matches_text(normalized_quote: str, normalized_content: str) -> bool:
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


def validate_privacy_redaction(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    matter_scope = _matter_scope_for_validation_target(conn, target_type=target_type, target_id=target_id)
    if matter_scope is None:
        return False, {"error": f"{target_type} target not found or has no matter scope"}
    return _matter_certification_present(conn, matter_scope=matter_scope, certification_type="privacy_redaction_audit")


def validate_hostile_review_certification(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    matter_scope = _matter_scope_for_validation_target(conn, target_type=target_type, target_id=target_id)
    if matter_scope is None:
        return False, {"error": f"{target_type} target not found or has no matter scope"}
    return _matter_certification_present(conn, matter_scope=matter_scope, certification_type="hostile_review")


def validate_cross_matter_isolation(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> tuple[bool, dict[str, object]]:
    if target_type != "task":
        return False, {"error": "cross_matter_isolation must target task"}
    task = cast(SqlRow | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (target_id,)).fetchone())
    if task is None:
        return False, {"error": "task not found"}
    matter_scope = str(task["matter_scope"])
    problems: list[str] = []
    for source_id in _safe_string_list(task, "source_dependencies_json"):
        actual = repo.matter_scope_for_target(conn, target_type="source", target_id=source_id)
        if actual != matter_scope:
            problems.append(f"source {source_id} belongs to {actual or 'missing'}")
    for artifact_id in _safe_string_list(task, "artifact_dependencies_json"):
        actual = repo.matter_scope_for_target(conn, target_type="artifact", target_id=artifact_id)
        if actual != matter_scope:
            problems.append(f"artifact {artifact_id} belongs to {actual or 'missing'}")
    for dependency_task_id in _safe_string_list(task, "task_dependencies_json"):
        actual = repo.matter_scope_for_target(conn, target_type="task", target_id=dependency_task_id)
        if actual != matter_scope:
            problems.append(f"task {dependency_task_id} belongs to {actual or 'missing'}")
    return not problems, {"matter_scope": matter_scope, "problems": problems}


def _matter_scope_for_validation_target(conn: sqlite3.Connection, *, target_type: str, target_id: str) -> str | None:
    if target_type == "matter":
        return target_id
    return repo.matter_scope_for_target(conn, target_type=target_type, target_id=target_id)


def _matter_certification_present(conn: sqlite3.Connection, *, matter_scope: str, certification_type: str) -> tuple[bool, dict[str, object]]:
    row = conn.execute(
        """
        SELECT certification_id
        FROM certifications
        WHERE subject_type = 'matter' AND subject_id = ? AND certification_type = ? AND status = 'active'
        LIMIT 1
        """,
        (matter_scope, certification_type),
    ).fetchone()
    return row is not None, {"matter_scope": matter_scope, "certification_type": certification_type, "certification_id": row["certification_id"] if row else ""}


def _safe_string_list(row: Mapping[str, object], field: str) -> list[str]:
    try:
        return _string_list_from_json(row[field], field=field)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return []
