"""Read-only retrieval over candidate and certified sources/artifacts."""

from __future__ import annotations

import sqlite3
from typing import Any

from atticus.retrieval.rank import lexical_score

_TRUST_BOOST = {
    "certified": 0.35,
    "validated": 0.25,
    "candidate": 0.05,
    "rough_note": -0.05,
    "unverified_legacy": -0.10,
    "stale": -0.40,
    "rejected": -1.00,
}


def search_memory(conn: sqlite3.Connection, question: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Return relevant memory rows without launching or mutating anything.

    This is deliberately schema-light but much safer than the original LIKE
    fallback: it scores all indexed legal memory rows, includes authorities,
    boosts records with citation spans, penalizes stale/low-trust material, and
    returns no result instead of arbitrary recent artifacts when nothing matches.
    """

    rows = _indexed_memory_rows(conn) or all_memory_rows(conn)
    scored: list[tuple[float, str, str, dict[str, Any]]] = []
    for row in rows:
        score = _score_row(conn, question, row)
        if score <= 0:
            continue
        scored.append((score, str(row.get("created_at") or ""), str(row["record_id"]), row))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in scored[:limit]]


def all_memory_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _artifact_rows(conn) + _source_rows(conn) + _authority_rows(conn)


def _indexed_memory_rows(conn: sqlite3.Connection, *, index_name: str = "legal_memory.v1") -> list[dict[str, Any]]:
    entries = conn.execute(
        """
        SELECT record_type, record_id
        FROM search_index_entries
        WHERE index_name = ?
        ORDER BY record_type, record_id
        """,
        (index_name,),
    ).fetchall()
    if not entries:
        return []
    by_key = {(row["record_type"], row["record_id"]): row for row in all_memory_rows(conn)}
    return [by_key[(entry["record_type"], entry["record_id"])] for entry in entries if (entry["record_type"], entry["record_id"]) in by_key]


def _artifact_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT 'artifact' AS record_type, artifact_id AS record_id, path, title, content,
              trust_status, stale, stage, matter_scope, created_at
            FROM artifacts
            WHERE trust_status != 'rejected'
            """
        )
    ]


def _source_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT 'source' AS record_type, source_id AS record_id, path, path AS title,
              source_type AS content, trust_status, stale, stage, matter_scope, created_at
            FROM sources
            WHERE trust_status != 'rejected'
            """
        )
    ]


def _authority_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "record_type": "authority",
            "record_id": row["authority_id"],
            "path": row["source_url"] or row["citation"],
            "title": row["title"] or row["citation"],
            "content": " ".join(
                part
                for part in (
                    row["citation"],
                    row["title"],
                    row["authority_type"],
                    row["jurisdiction"],
                    row["source_url"],
                )
                if part
            ),
            "trust_status": row["status"],
            "stale": 0,
            "stage": "S6",
            "matter_scope": row["matter_scope"],
            "created_at": row["created_at"],
        }
        for row in conn.execute(
            """
            SELECT authority_id, matter_scope, jurisdiction, citation, authority_type, title, status, source_url, created_at
            FROM legal_authorities
            WHERE status != 'rejected'
            """
        )
    ]


def _score_row(conn: sqlite3.Connection, question: str, row: dict[str, Any]) -> float:
    haystack = " ".join(str(row.get(key) or "") for key in ("path", "title", "content", "stage"))
    lexical = lexical_score(question, haystack)
    if lexical <= 0:
        return 0.0
    citation_boost = min(_citation_count(conn, row) * 0.10, 0.30)
    trust_boost = _TRUST_BOOST.get(str(row.get("trust_status") or ""), 0.0)
    stale_penalty = -0.50 if int(row.get("stale") or 0) else 0.0
    return lexical + citation_boost + trust_boost + stale_penalty


def _citation_count(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    record_type = row["record_type"]
    column = {
        "artifact": "artifact_id",
        "source": "source_id",
        "authority": "authority_id",
    }.get(record_type)
    if column is None:
        return 0
    result = conn.execute(f"SELECT COUNT(*) AS n FROM citation_spans WHERE {column} = ?", (row["record_id"],)).fetchone()
    return int(result["n"] if result else 0)
