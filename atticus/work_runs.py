"""Resumable matter work-run ledger helpers."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from typing import cast

from atticus.db import repo


def start_work_run(conn: sqlite3.Connection, matter_scope: str, goal: str) -> dict[str, object]:
    work_run_id = repo.start_work_run(conn, matter_scope=matter_scope, goal=goal)
    row = _work_run_row(conn, work_run_id=work_run_id)
    if row is None:
        raise RuntimeError("work run was not persisted")
    return row


def record_work_step(
    conn: sqlite3.Connection,
    *,
    work_run_id: str,
    step_type: str,
    status: str,
    task_id: str | None = None,
    candidate_id: str | None = None,
    artifact_id: str | None = None,
    context_pack_id: str | None = None,
    provider_run_id: str | None = None,
    input_fingerprint: str = "",
    output_fingerprint: str = "",
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    step_id = repo.record_work_run_step(
        conn,
        work_run_id=work_run_id,
        step_type=step_type,
        status=status,
        task_id=task_id,
        candidate_id=candidate_id,
        artifact_id=artifact_id,
        context_pack_id=context_pack_id,
        provider_run_id=provider_run_id,
        input_fingerprint=input_fingerprint,
        output_fingerprint=output_fingerprint,
        metadata=dict(metadata or {}),
    )
    row = conn.execute("SELECT * FROM work_run_steps WHERE work_run_step_id = ?", (step_id,)).fetchone()
    return _step_row(row)


def resume_work_run(conn: sqlite3.Connection, resume_token: str, *, matter_scope: str | None = None) -> dict[str, object]:
    row = conn.execute("SELECT * FROM work_runs WHERE resume_token = ?", (resume_token,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "resume token not found", "work_run": None, "steps": []}
    row_matter_scope = str(row["matter_scope"])
    if matter_scope is not None and row_matter_scope != matter_scope:
        return {
            "ok": False,
            "reason": f"resume token belongs to matter {row_matter_scope}, not {matter_scope}",
            "work_run": None,
            "steps": [],
        }
    try:
        work_run = _work_run_plain(row)
        steps = [
            _step_row(step)
            for step in conn.execute(
                "SELECT * FROM work_run_steps WHERE work_run_id = ? ORDER BY created_at, work_run_step_id",
                (work_run["work_run_id"],),
            )
        ]
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        _ = repo.record_human_attention(
            conn,
            target_type="work_run",
            target_id=str(row["work_run_id"]),
            severity="blocker",
            reason=f"corrupted work-run row blocked resume: {exc}",
            matter_scope=str(row["matter_scope"] or "unknown"),
        )
        return {"ok": False, "reason": "corrupted work-run row; human attention created", "work_run_id": row["work_run_id"], "steps": []}
    return {"ok": True, "work_run": work_run, "steps": steps}


def summarize_reusable_work(conn: sqlite3.Connection, matter_scope: str, goal: str) -> dict[str, object]:
    tokens = {token for token in goal.lower().split() if len(token) > 2}
    rows = [
        _step_row(row)
        for row in conn.execute(
            """
            SELECT wrs.*
            FROM work_run_steps wrs
            JOIN work_runs wr ON wr.work_run_id = wrs.work_run_id
            WHERE wrs.matter_scope = ? AND wrs.status = 'complete'
              AND NOT EXISTS (
                SELECT 1 FROM work_reuse_records reuse
                WHERE reuse.reused_from_step_id = wrs.work_run_step_id AND reuse.valid = 0
              )
            ORDER BY wrs.updated_at DESC
            LIMIT 50
            """,
            (matter_scope,),
        )
    ]
    if tokens:
        rows = [row for row in rows if tokens.intersection(" ".join(str(row.get(key) or "").lower() for key in ("step_type", "metadata")).split()) or not row.get("metadata")]
    return {
        "matter_scope": matter_scope,
        "goal": goal,
        "reusable_steps": rows,
        "rule": "same matter, complete status, and not invalidated; provider/model choice is not proof",
    }


def invalidate_reuse_for_stale_sources(conn: sqlite3.Connection, source_ids: list[str]) -> dict[str, object]:
    matters = [
        str(row["matter_scope"])
        for row in conn.execute(
            "SELECT DISTINCT matter_scope FROM sources WHERE source_id IN (%s)" % ",".join("?" for _ in source_ids),
            tuple(source_ids),
        )
    ] if source_ids else []
    invalidated = 0
    for matter_scope in matters:
        cur = conn.execute(
            """
            UPDATE work_reuse_records
            SET valid = 0, invalidation_reason = ?
            WHERE matter_scope = ? AND valid = 1
            """,
            (f"source snapshot changed or went stale: {', '.join(source_ids)}", matter_scope),
        )
        invalidated += cur.rowcount if cur.rowcount is not None else 0
        _ = repo.emit_event(conn, "work_reuse.invalidated", matter_scope=matter_scope, payload={"source_ids": source_ids})
    return {"source_ids": source_ids, "matter_scopes": matters, "invalidated_records": invalidated}


def migrate_work_runs_after_schema_update(conn: sqlite3.Connection) -> dict[str, object]:
    corrupt: list[str] = []
    for row in conn.execute("SELECT work_run_id, matter_scope, metadata_json FROM work_runs"):
        try:
            _ = json.loads(str(row["metadata_json"] or "{}"))
        except json.JSONDecodeError:
            corrupt.append(str(row["work_run_id"]))
            _ = repo.record_human_attention(
                conn,
                target_type="work_run",
                target_id=str(row["work_run_id"]),
                severity="blocker",
                reason="work run metadata_json is not valid JSON after schema migration",
                matter_scope=str(row["matter_scope"]),
            )
    return {"ok": not corrupt, "corrupt_work_run_ids": corrupt}


def _work_run_row(conn: sqlite3.Connection, *, work_run_id: str) -> dict[str, object] | None:
    row = conn.execute("SELECT * FROM work_runs WHERE work_run_id = ?", (work_run_id,)).fetchone()
    return _work_run_plain(row) if row is not None else None


def _work_run_plain(row: sqlite3.Row) -> dict[str, object]:
    result = {key: row[key] for key in row.keys()}
    result["metadata"] = json.loads(str(result.pop("metadata_json") or "{}"))
    return result


def _step_row(row: sqlite3.Row) -> dict[str, object]:
    result = {key: row[key] for key in row.keys()}
    result["metadata"] = json.loads(str(result.pop("metadata_json") or "{}"))
    return result
