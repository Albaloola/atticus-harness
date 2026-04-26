"""Rebuildable legal-memory search index projections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from atticus.core.matters import require_matter_access
from atticus.core.events import utc_now
from atticus.db import repo
from atticus.retrieval.search import all_memory_rows


DEFAULT_INDEX_NAME = "legal_memory.v1"


def rebuild_search_index(
    conn: sqlite3.Connection,
    *,
    matter_scope: str = "atticus",
    authorized_matter_scope: str = "atticus",
    index_name: str = DEFAULT_INDEX_NAME,
) -> dict[str, Any]:
    """Rebuild the legal-memory projection from durable source tables.

    The source of truth remains the evidence graph. This projection is fully
    disposable and can be recreated from sources, artifacts, and authorities.
    """

    matter_scope = require_matter_access(matter_scope, authorized_matter_scope=authorized_matter_scope)
    rows = all_memory_rows(conn, matter_scope=matter_scope)
    rows.sort(key=lambda row: (str(row["record_type"]), str(row["record_id"])))
    input_fingerprint = _fingerprint_rows(rows)

    conn.execute("DELETE FROM search_index_entries WHERE index_name = ? AND matter_scope = ?", (index_name, matter_scope))
    for row in rows:
        indexed_text = _indexed_text(row)
        content_hash = hashlib.sha256(indexed_text.encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO search_index_entries(search_index_entry_id, index_name, record_type, record_id,
              matter_scope, content_hash, indexed_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"sidx-{uuid4().hex}",
                index_name,
                row["record_type"],
                row["record_id"],
                matter_scope,
                content_hash,
                indexed_text,
                utc_now(),
            ),
        )

    output_rows = [dict(row) for row in conn.execute("SELECT record_type, record_id, content_hash FROM search_index_entries WHERE index_name = ? AND matter_scope = ? ORDER BY record_type, record_id", (index_name, matter_scope))]
    output_fingerprint = _fingerprint_rows(output_rows)
    rebuild_id = f"irebuild-{uuid4().hex}"
    details = {"entry_count": len(rows), "record_types": sorted({str(row["record_type"]) for row in rows})}
    conn.execute(
        """
        INSERT INTO index_rebuilds(index_rebuild_id, index_name, matter_scope, status,
          input_fingerprint, output_fingerprint, details_json, created_at)
        VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)
        """,
        (rebuild_id, index_name, matter_scope, input_fingerprint, output_fingerprint, json.dumps(details, sort_keys=True), utc_now()),
    )
    repo.emit_event(
        conn,
        "search_index.rebuilt",
        matter_scope=matter_scope,
        payload={"index_rebuild_id": rebuild_id, "index_name": index_name, **details},
    )
    return {
        "index_rebuild_id": rebuild_id,
        "index_name": index_name,
        "matter_scope": matter_scope,
        "entry_count": len(rows),
        "input_fingerprint": input_fingerprint,
        "output_fingerprint": output_fingerprint,
    }


def _indexed_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("path", "title", "content", "stage"))


def _fingerprint_rows(rows: list[dict[str, Any]]) -> str:
    material = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
