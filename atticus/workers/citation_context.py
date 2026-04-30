"""Task-scoped citation target allow-lists."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from typing import cast

ORIENTATION_ONLY_ARTIFACT_TYPES = {
    "draft_complaint",
    "extracted_text",
    "extraction_record",
    "ocr_extract",
    "ocr_text",
    "transcription_record",
    "transcript",
    "draft",
    "rough_note",
    "context_pack",
    "reduced_result",
}
PROOF_ARTIFACT_TYPES = {
    "evidence_registry",
    "evidence_index",
    "production_crosswalk",
    "authority_map",
    "hostile_review",
    "citation_audit",
    "privacy_redaction_audit",
    "final_quality_gate",
}
REVIEW_OR_REPAIR_TASK_TYPES = {
    "authority_audit",
    "citation_audit",
    "citation_fix",
    "citation_repair",
    "final_quality_gate",
    "hostile_opponent_review",
    "privacy_review",
    "privacy_redaction_verification",
    "privacy_redaction_application",
    "privacy_redaction_audit",
    "privacy_redaction_implementation",
    "privacy_redaction_review",
    "redaction_application",
    "redaction_fix",
    "redaction_implementation",
    "redaction_repair",
    "redaction_review",
    "redaction_verification",
}
REVIEW_PROOF_ARTIFACT_TYPES = {
    "citation_audit",
    "draft",
    "draft_complaint",
    "final_quality_gate",
    "hostile_review",
    "privacy_redaction_audit",
    "redacted_draft",
    "redaction_annotation",
}


def allowed_citation_targets_for_task(conn: sqlite3.Connection, *, task_id: str) -> dict[str, set[str]]:
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        return {}
    matter_scope = str(task["matter_scope"])
    source_ids = _source_dependency_ids_for_task(conn, task=task, matter_scope=matter_scope)
    artifact_ids = _artifact_dependency_ids_for_task(conn, task=task, matter_scope=matter_scope)
    return {
        "source": _ids_for_matter_subset(conn, "sources", "source_id", matter_scope, source_ids),
        "artifact": _ids_for_matter_subset(conn, "artifacts", "artifact_id", matter_scope, artifact_ids),
        "authority": _ids_for_matter(conn, "legal_authorities", "authority_id", matter_scope),
        "chronology_event": _ids_for_matter(conn, "chronology_events", "chronology_event_id", matter_scope),
        "claim": _ids_for_matter(conn, "claims", "claim_id", matter_scope),
        "memory": _ids_for_matter_if_exists(conn, "legal_memories", "memory_id", matter_scope),
        "validation_result": _validation_result_ids_for_matter(conn, matter_scope=matter_scope),
    }


def proof_citation_targets_for_task(conn: sqlite3.Connection, *, task_id: str) -> dict[str, set[str]]:
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        return {}
    matter_scope = str(task["matter_scope"])
    source_ids = _source_dependency_ids_for_task(conn, task=task, matter_scope=matter_scope)
    artifact_ids = _artifact_dependency_ids_for_task(conn, task=task, matter_scope=matter_scope)
    review_task = str(task["task_type"] or "") in REVIEW_OR_REPAIR_TASK_TYPES
    return {
        "source": _current_source_ids(conn, matter_scope=matter_scope, ids=source_ids),
        "artifact": _proof_artifact_ids(conn, matter_scope=matter_scope, ids=artifact_ids, review_task=review_task),
        "authority": _proof_authority_ids(conn, matter_scope=matter_scope),
        "chronology_event": _ids_for_matter(conn, "chronology_events", "chronology_event_id", matter_scope),
        "claim": _ids_for_matter(conn, "claims", "claim_id", matter_scope),
        "memory": set(),
        "validation_result": set(),
    }


def _string_list_from_json(raw: object) -> list[str]:
    value = json.loads(str(raw or "[]"))
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value) if isinstance(item, str)]


def _artifact_dependency_ids_for_task(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    matter_scope: str,
) -> list[str]:
    explicit = _string_list_from_json(task["artifact_dependencies_json"])
    task_dependency_ids = _string_list_from_json(task["task_dependencies_json"]) if "task_dependencies_json" in task.keys() else []
    if not task_dependency_ids:
        return _artifact_dependency_closure(conn, matter_scope=matter_scope, artifact_ids=explicit)
    rows = conn.execute(
        """
        SELECT artifact_id
        FROM artifacts
        WHERE matter_scope = ?
          AND stale = 0
          AND produced_by_task_id IN (%s)
        ORDER BY produced_by_task_id, created_at, artifact_id
        """ % ",".join("?" for _ in task_dependency_ids),
        (matter_scope, *task_dependency_ids),
    ).fetchall()
    direct = list(dict.fromkeys([*explicit, *(str(row["artifact_id"]) for row in rows)]))
    return _artifact_dependency_closure(conn, matter_scope=matter_scope, artifact_ids=direct)


def _artifact_dependency_closure(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    artifact_ids: list[str],
) -> list[str]:
    """Include same-matter artifact dependencies reachable from task artifacts.

    Review tasks often depend on a draft artifact, and that draft artifact is
    graph-linked to the evidence registry, authority map, or source bundle it
    used. Those upstream artifacts are legitimate citation context for review,
    while arbitrary same-matter artifacts remain excluded.
    """

    ordered = list(dict.fromkeys(artifact_ids))
    seen = set(ordered)
    frontier = list(ordered)
    while frontier:
        placeholders = ",".join("?" for _ in frontier)
        rows = conn.execute(
            f"""
            SELECT DISTINCT ad.dependency_artifact_id
            FROM artifact_dependencies ad
            JOIN artifacts a ON a.artifact_id = ad.dependency_artifact_id
            WHERE a.matter_scope = ?
              AND a.stale = 0
              AND ad.artifact_id IN ({placeholders})
            ORDER BY ad.dependency_artifact_id
            """,
            (matter_scope, *frontier),
        ).fetchall()
        frontier = []
        for row in rows:
            artifact_id = str(row["dependency_artifact_id"])
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            ordered.append(artifact_id)
            frontier.append(artifact_id)
    return ordered


def _source_dependency_ids_for_task(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    matter_scope: str,
) -> list[str]:
    explicit = _string_list_from_json(task["source_dependencies_json"])
    artifact_ids = _artifact_dependency_ids_for_task(conn, task=task, matter_scope=matter_scope)
    if not artifact_ids:
        return explicit
    rows = conn.execute(
        """
        SELECT DISTINCT s.source_id
        FROM artifact_sources ars
        JOIN sources s ON s.source_id = ars.source_id
        WHERE s.matter_scope = ?
          AND s.stale = 0
          AND ars.artifact_id IN (%s)
        ORDER BY s.source_id
        """ % ",".join("?" for _ in artifact_ids),
        (matter_scope, *artifact_ids),
    ).fetchall()
    return list(dict.fromkeys([*explicit, *(str(row["source_id"]) for row in rows)]))


def _ids_for_matter(conn: sqlite3.Connection, table: str, column: str, matter_scope: str) -> set[str]:
    return {
        str(row[column])
        for row in conn.execute(f"SELECT {column} FROM {table} WHERE matter_scope = ?", (matter_scope,)).fetchall()
    }


def _ids_for_matter_subset(conn: sqlite3.Connection, table: str, column: str, matter_scope: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = conn.execute(
        f"SELECT {column} FROM {table} WHERE matter_scope = ? AND {column} IN (%s)" % ",".join("?" for _ in ids),
        (matter_scope, *ids),
    ).fetchall()
    return {str(row[column]) for row in rows}


def _current_source_ids(conn: sqlite3.Connection, *, matter_scope: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = conn.execute(
        "SELECT source_id FROM sources WHERE matter_scope = ? AND stale = 0 AND source_id IN (%s)" % ",".join("?" for _ in ids),
        (matter_scope, *ids),
    ).fetchall()
    return {str(row["source_id"]) for row in rows}


def _proof_artifact_ids(conn: sqlite3.Connection, *, matter_scope: str, ids: list[str], review_task: bool = False) -> set[str]:
    if not ids:
        return set()
    rows = conn.execute(
        """
        SELECT artifact_id, artifact_type, trust_status, stale
        FROM artifacts
        WHERE matter_scope = ? AND artifact_id IN (%s)
        """ % ",".join("?" for _ in ids),
        (matter_scope, *ids),
    ).fetchall()
    proof: set[str] = set()
    for row in rows:
        artifact_type = str(row["artifact_type"] or "")
        review_proof_allowed = review_task and artifact_type in REVIEW_PROOF_ARTIFACT_TYPES
        if bool(row["stale"]):
            continue
        if artifact_type in ORIENTATION_ONLY_ARTIFACT_TYPES and not review_proof_allowed:
            continue
        if artifact_type not in PROOF_ARTIFACT_TYPES and not review_proof_allowed:
            continue
        if str(row["trust_status"]) not in {"validated", "certified"}:
            continue
        proof.add(str(row["artifact_id"]))
    return proof


def _proof_authority_ids(conn: sqlite3.Connection, *, matter_scope: str) -> set[str]:
    validated = {
        str(row["authority_id"])
        for row in conn.execute(
            "SELECT authority_id FROM legal_authorities WHERE matter_scope = ? AND status IN ('validated', 'certified')",
            (matter_scope,),
        ).fetchall()
    }
    verified = {
        str(row["authority_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT av.authority_id
            FROM authority_verifications av
            JOIN legal_authorities la ON la.authority_id = av.authority_id
            WHERE av.matter_scope = ?
              AND la.matter_scope = ?
              AND la.status != 'rejected'
              AND av.currentness_status = 'current'
              AND av.proposition_supported = 1
            """,
            (matter_scope, matter_scope),
        ).fetchall()
    }
    return validated | verified


def _ids_for_matter_if_exists(conn: sqlite3.Connection, table: str, column: str, matter_scope: str) -> set[str]:
    exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    if exists is None:
        return set()
    return _ids_for_matter(conn, table, column, matter_scope)


def _validation_result_ids_for_matter(conn: sqlite3.Connection, *, matter_scope: str) -> set[str]:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(validation_results)")}
    if "matter_scope" not in columns:
        return set()
    return {
        str(row["validation_result_id"])
        for row in conn.execute(
            "SELECT validation_result_id FROM validation_results WHERE matter_scope = ?",
            (matter_scope,),
        ).fetchall()
    }
