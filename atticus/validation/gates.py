"""Durable validation gates for legal evidence and reducer packets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
import sqlite3
from typing import cast, Protocol

from atticus.db import repo
from atticus.workers.result_parser import ResultPacketError, parse_result

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
    missing = [
        row["source_id"]
        for row in cast(list[SqlRow], conn.execute(
            """
            SELECT s.source_id
            FROM sources s
            LEFT JOIN extraction_records er ON er.source_id = s.source_id
            LEFT JOIN ocr_records ocr ON ocr.source_id = s.source_id
            LEFT JOIN transcription_records tr ON tr.source_id = s.source_id
            WHERE s.matter_scope = ?
              AND er.extraction_id IS NULL
              AND ocr.ocr_id IS NULL
              AND tr.transcription_id IS NULL
            """,
            (target_id,),
        ).fetchall())
    ]
    total = int(str(cast(SqlRow, conn.execute("SELECT COUNT(*) AS n FROM sources WHERE matter_scope = ?", (target_id,)).fetchone())["n"]))
    return total > 0 and not missing, {"source_count": total, "missing_extraction": missing}


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


def validate_reducer_packet_schema(
    conn: sqlite3.Connection, *, target_type: str, target_id: str
) -> tuple[bool, dict[str, object]]:
    if target_type != "candidate":
        return False, {"error": "reducer_packet_schema must target candidate"}
    row = cast(SqlRow | None, conn.execute("SELECT payload_json FROM candidate_outputs WHERE candidate_id = ?", (target_id,)).fetchone())
    if row is None:
        return False, {"error": "candidate output not found"}
    try:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, Mapping):
            return False, {"error": "candidate output payload must be a JSON object"}
        _ = parse_result({str(key): value for key, value in cast(Mapping[object, object], payload).items()})
    except ResultPacketError as exc:
        return False, {"error": str(exc)}
    return True, {"schema": "worker_result_packet.v1"}


def validate_canonical_write_authorization(
    conn: sqlite3.Connection, *, target_type: str, target_id: str
) -> tuple[bool, dict[str, object]]:
    if target_type != "candidate":
        return False, {"error": "canonical_write_authorization must target candidate"}
    row = cast(SqlRow | None, conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (target_id,)).fetchone())
    if row is None:
        return False, {"error": "candidate output not found"}
    return row["status"] == "candidate", {"candidate_status": row["status"], "required_writer_role": "reducer"}
