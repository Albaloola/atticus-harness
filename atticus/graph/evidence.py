"""Evidence graph write helpers."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from atticus.core.events import utc_now


def add_extraction_record(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    artifact_id: str | None = None,
    method: str,
    coverage_status: str,
    confidence: float = 0.0,
) -> str:
    extraction_id = f"extract-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
          coverage_status, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (extraction_id, source_id, artifact_id, method, coverage_status, confidence, utc_now()),
    )
    return extraction_id


def add_production_mapping(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    production_id: str,
    source_id: str | None = None,
    artifact_id: str | None = None,
    produced_path: str = "",
    integrity_status: str = "candidate",
) -> str:
    mapping_id = f"prod-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO production_mappings(mapping_id, matter_scope, source_id, artifact_id,
          production_id, produced_path, integrity_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mapping_id, matter_scope, source_id, artifact_id, production_id, produced_path, integrity_status, utc_now()),
    )
    return mapping_id


def add_chronology_event(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    description: str,
    event_date: str = "",
    created_by_artifact_id: str | None = None,
) -> str:
    chronology_event_id = f"chrono-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO chronology_events(chronology_event_id, matter_scope, event_date, description,
          created_by_artifact_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chronology_event_id, matter_scope, event_date, description, created_by_artifact_id, now, now),
    )
    return chronology_event_id


def add_authority(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    citation: str,
    authority_type: str,
    jurisdiction: str = "",
    title: str = "",
    source_url: str = "",
) -> str:
    authority_id = f"auth-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO legal_authorities(authority_id, matter_scope, jurisdiction, citation,
          authority_type, title, source_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (authority_id, matter_scope, jurisdiction, citation, authority_type, title, source_url, now, now),
    )
    return authority_id
