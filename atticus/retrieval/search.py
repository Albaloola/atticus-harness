"""Read-only retrieval over candidate and certified sources/artifacts."""

from __future__ import annotations

import sqlite3

from atticus.retrieval.rank import lexical_score


def search_memory(conn: sqlite3.Connection, question: str, *, limit: int = 5) -> list[dict]:
    rows: list[dict] = []
    like = f"%{question[:80]}%"
    for row in conn.execute(
        """
        SELECT 'artifact' AS record_type, artifact_id AS record_id, path, title, content,
          trust_status, stale, stage
        FROM artifacts
        WHERE content LIKE ? OR path LIKE ? OR title LIKE ?
        """,
        (like, like, like),
    ):
        rows.append(dict(row))

    for row in conn.execute(
        """
        SELECT 'source' AS record_type, source_id AS record_id, path, path AS title, '' AS content,
          trust_status, stale, stage
        FROM sources
        WHERE path LIKE ? OR source_type LIKE ?
        """,
        (like, like),
    ):
        rows.append(dict(row))

    if not rows:
        rows.extend(
            dict(row)
            for row in conn.execute(
                """
                SELECT 'artifact' AS record_type, artifact_id AS record_id, path, title, content,
                  trust_status, stale, stage
                FROM artifacts
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    rows.sort(
        key=lambda item: lexical_score(
            question, " ".join(str(item.get(k, "")) for k in ("path", "title", "content"))
        ),
        reverse=True,
    )
    return rows[:limit]
