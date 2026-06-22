"""Read-only status reporting."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from typing import cast
from dataclasses import dataclass

from atticus.db.repo import db_connection


@dataclass(frozen=True)
class StatusReport:
    run_state: str
    counts: dict[str, int]
    blocked_tasks: list[dict[str, object]]
    stale_artifacts: list[dict[str, object]]
    active_leases: list[dict[str, object]]
    human_attention: list[dict[str, object]]
    budget: dict[str, object]
    provider_usage: dict[str, object]


def generate_status(db_path: str, *, matter_scope: str | None = None) -> StatusReport:
    with db_connection(db_path, read_only=True) as conn:
        run = _latest_run(conn, matter_scope=matter_scope)
        counts = {
            "sources": _scoped_count(conn, "sources", matter_scope=matter_scope),
            "artifacts": _scoped_count(conn, "artifacts", matter_scope=matter_scope),
            "tasks": _scoped_count(conn, "tasks", matter_scope=matter_scope),
            "blocked_tasks": _blocked_task_count(conn, matter_scope=matter_scope),
            "candidate_outputs": _candidate_output_count(conn, matter_scope=matter_scope),
            "tracked_files": _scoped_count(conn, "tracked_files", matter_scope=matter_scope),
            "open_human_attention": _open_attention_count(conn, matter_scope=matter_scope),
        }
        task_filter = ""
        task_params: tuple[object, ...] = ()
        if matter_scope is not None:
            task_filter = "AND matter_scope = ?"
            task_params = (matter_scope,)
        blocked: list[dict[str, object]] = [
            {
                "task_id": row["task_id"],
                "title": row["title"],
                "stage": row["stage"],
                "reasons": json.loads(str(row["blocked_reasons_json"])),
            }
            for row in conn.execute(
                f"""
                SELECT task_id, title, stage, blocked_reasons_json
                FROM tasks
                WHERE status = 'blocked'
                {task_filter}
                ORDER BY updated_at
                """,
                task_params,
            )
        ]
        artifact_filter = ""
        artifact_params: tuple[object, ...] = ()
        if matter_scope is not None:
            artifact_filter = "AND matter_scope = ?"
            artifact_params = (matter_scope,)
        stale: list[dict[str, object]] = [
            {"artifact_id": row["artifact_id"], "path": row["path"], "artifact_type": row["artifact_type"]}
            for row in conn.execute(
                f"SELECT artifact_id, path, artifact_type FROM artifacts WHERE stale = 1 {artifact_filter} ORDER BY updated_at DESC LIMIT 25",
                artifact_params,
            )
        ]
        leases: list[dict[str, object]] = [
            {
                "lease_id": row["lease_id"],
                "task_id": row["task_id"],
                "worker_id": row["worker_id"],
                "expires_at": row["expires_at"],
                "fencing_token": row["fencing_token"],
            }
            for row in _active_leases(conn, matter_scope=matter_scope)
        ]
        attention_params: tuple[object, ...] = ()
        matter_filter = ""
        if matter_scope is not None:
            matter_filter = "AND matter_scope = ?"
            attention_params = (matter_scope,)
        attention: list[dict[str, object]] = [
            {
                "attention_id": row["attention_id"],
                "matter_scope": row["matter_scope"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "severity": row["severity"],
                "reason": row["reason"],
                "owner": row["owner"],
                "signature": row["signature"],
            }
            for row in conn.execute(
                f"""
                SELECT attention_id, matter_scope, target_type, target_id, severity, reason, owner, signature
                FROM human_attention
                WHERE status = 'open'
                {matter_filter}
                ORDER BY attention_id DESC
                LIMIT 25
                """,
                attention_params,
            )
        ]
        provider = _provider_usage(conn, matter_scope=matter_scope)
        budget: dict[str, object] = {
            f"{row['scope_type']}:{row['scope_id']}": {
                "limit_usd": row["limit_usd"],
                "spent_usd": row["spent_usd"],
                "remaining_usd": float(str(row["limit_usd"])) - float(str(row["spent_usd"])),
            }
            for row in _budget_rows(conn, matter_scope=matter_scope)
        }
    return StatusReport(
        run_state=str(run["state"] if run else "uninitialized"),
        counts=counts,
        blocked_tasks=blocked,
        stale_artifacts=stale,
        active_leases=leases,
        human_attention=attention,
        budget=budget,
        provider_usage=provider,
    )


def _scalar_int(row: Mapping[str, object] | None, key: str) -> int:
    return int(float(str(row[key] if row is not None else 0)))


def _scalar_float(row: Mapping[str, object] | None, key: str) -> float:
    return float(str(row[key] if row is not None else 0))


def _latest_run(conn: sqlite3.Connection, *, matter_scope: str | None) -> Mapping[str, object] | None:
    if matter_scope is None:
        row = conn.execute("SELECT state FROM runs ORDER BY updated_at DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT state FROM runs WHERE matter_scope = ? ORDER BY updated_at DESC LIMIT 1", (matter_scope,)).fetchone()
    return cast(Mapping[str, object] | None, row)


def _scoped_count(conn: sqlite3.Connection, table: str, *, matter_scope: str | None) -> int:
    if matter_scope is None:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    else:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE matter_scope = ?", (matter_scope,)).fetchone()
    return _scalar_int(cast(Mapping[str, object] | None, row), "n")


def _blocked_task_count(conn: sqlite3.Connection, *, matter_scope: str | None) -> int:
    if matter_scope is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'blocked'").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'blocked' AND matter_scope = ?", (matter_scope,)).fetchone()
    return _scalar_int(cast(Mapping[str, object] | None, row), "n")


def _candidate_output_count(conn: sqlite3.Connection, *, matter_scope: str | None) -> int:
    if matter_scope is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs").fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM candidate_outputs co
            JOIN tasks t ON t.task_id = co.task_id
            WHERE t.matter_scope = ?
            """,
            (matter_scope,),
        ).fetchone()
    return _scalar_int(cast(Mapping[str, object] | None, row), "n")


def _active_leases(conn: sqlite3.Connection, *, matter_scope: str | None) -> list[Mapping[str, object]]:
    if matter_scope is None:
        rows = conn.execute(
            "SELECT lease_id, task_id, worker_id, expires_at, fencing_token FROM leases WHERE status = 'active'"
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT l.lease_id, l.task_id, l.worker_id, l.expires_at, l.fencing_token
            FROM leases l
            JOIN tasks t ON t.task_id = l.task_id
            WHERE l.status = 'active' AND t.matter_scope = ?
            """,
            (matter_scope,),
        ).fetchall()
    return [cast(Mapping[str, object], row) for row in rows]


def _provider_usage(conn: sqlite3.Connection, *, matter_scope: str | None) -> dict[str, object]:
    if matter_scope is None:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
              COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens,
              COALESCE(SUM(cache_miss_tokens), 0) AS cache_miss_tokens,
              COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM provider_runs
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pr.estimated_cost_usd), 0) AS estimated_cost_usd,
              COALESCE(SUM(pr.cache_hit_tokens), 0) AS cache_hit_tokens,
              COALESCE(SUM(pr.cache_miss_tokens), 0) AS cache_miss_tokens,
              COALESCE(SUM(pr.output_tokens), 0) AS output_tokens
            FROM provider_runs pr
            LEFT JOIN tasks t ON t.task_id = pr.task_id
            LEFT JOIN runs r ON r.run_id = pr.run_id
            WHERE COALESCE(t.matter_scope, r.matter_scope) = ?
            """,
            (matter_scope,),
        ).fetchone()
    data = cast(Mapping[str, object] | None, row)
    return {
        "estimated_cost_usd": _scalar_float(data, "estimated_cost_usd"),
        "cache_hit_tokens": _scalar_int(data, "cache_hit_tokens"),
        "cache_miss_tokens": _scalar_int(data, "cache_miss_tokens"),
        "output_tokens": _scalar_int(data, "output_tokens"),
    }


def _budget_rows(conn: sqlite3.Connection, *, matter_scope: str | None) -> list[Mapping[str, object]]:
    matter_filter = ""
    params: tuple[object, ...] = ()
    if matter_scope is not None:
        matter_filter = """
        WHERE (b.scope_type = 'matter' AND b.scope_id = ?)
          OR (b.scope_type = 'task' AND EXISTS (SELECT 1 FROM tasks t WHERE t.task_id = b.scope_id AND t.matter_scope = ?))
          OR (b.scope_type = 'run' AND EXISTS (SELECT 1 FROM runs r WHERE r.run_id = b.scope_id AND r.matter_scope = ?))
        """
        params = (matter_scope, matter_scope, matter_scope)
    rows = conn.execute(
        f"""
        SELECT b.scope_type, b.scope_id, b.limit_usd,
          COALESCE(SUM(be.amount_usd), 0) AS spent_usd
        FROM budgets b
        LEFT JOIN budget_entries be ON be.budget_id = b.budget_id
        {matter_filter}
        GROUP BY b.budget_id
        ORDER BY b.scope_type, b.scope_id
        """,
        params,
    ).fetchall()
    return [cast(Mapping[str, object], row) for row in rows]


def _open_attention_count(conn: sqlite3.Connection, *, matter_scope: str | None) -> int:
    if matter_scope is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM human_attention WHERE status = 'open'").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM human_attention WHERE status = 'open' AND matter_scope = ?",
            (matter_scope,),
        ).fetchone()
    return _scalar_int(cast(Mapping[str, object] | None, row), "n")
