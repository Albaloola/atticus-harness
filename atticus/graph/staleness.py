"""Staleness propagation."""

from __future__ import annotations

import sqlite3

from typing import cast
from atticus.db import repo


def update_source_hash_and_mark_dependents_stale(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    new_sha256: str,
) -> list[str]:
    row = cast(sqlite3.Row | None, cast(object, conn.execute(
        "SELECT sha256 FROM sources WHERE source_id = ?",
        (source_id,),
    ).fetchone()))
    if row is None:
        raise KeyError(f"unknown source: {source_id}")
    if row["sha256"] == new_sha256:
        return []

    _ = conn.execute(
        "UPDATE sources SET sha256 = ?, stale = 0 WHERE source_id = ?",
        (new_sha256, source_id),
    )
    artifact_rows = conn.execute(
        "SELECT artifact_id FROM artifact_sources WHERE source_id = ?",
        (source_id,),
    ).fetchall()
    artifact_ids = [str(r["artifact_id"]) for r in artifact_rows]
    seen = set(artifact_ids)
    queue = list(artifact_ids)
    while queue:
        artifact_id = queue.pop(0)
        _ = conn.execute(
            "UPDATE artifacts SET stale = 1, trust_status = 'stale' WHERE artifact_id = ?",
            (artifact_id,),
        )
        for downstream in conn.execute(
            "SELECT artifact_id FROM artifact_dependencies WHERE dependency_artifact_id = ?",
            (artifact_id,),
        ):
            downstream_id = str(downstream["artifact_id"])
            if downstream_id not in seen:
                seen.add(downstream_id)
                artifact_ids.append(downstream_id)
                queue.append(downstream_id)
            _ = conn.execute(
                "UPDATE artifacts SET stale = 1, trust_status = 'stale' WHERE artifact_id = ?",
                (downstream_id,),
            )
    _ = repo.emit_event(
        conn,
        "source.hash_changed",
        payload={"source_id": source_id, "new_sha256": new_sha256, "stale_artifacts": artifact_ids},
    )
    return artifact_ids
