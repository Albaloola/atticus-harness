"""Read-only status reporting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atticus.db.repo import db_connection


@dataclass(frozen=True)
class StatusReport:
    run_state: str
    counts: dict[str, int]
    blocked_tasks: list[dict[str, Any]]
    stale_artifacts: list[dict[str, Any]]
    active_leases: list[dict[str, Any]]
    human_attention: list[dict[str, Any]]
    budget: dict[str, Any]
    provider_usage: dict[str, Any]


def generate_status(db_path: str) -> StatusReport:
    with db_connection(db_path, read_only=True) as conn:
        run = conn.execute("SELECT state FROM runs ORDER BY updated_at DESC LIMIT 1").fetchone()
        counts = {
            "sources": int(conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]),
            "artifacts": int(conn.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]),
            "tasks": int(conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]),
            "blocked_tasks": int(
                conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'blocked'").fetchone()["n"]
            ),
            "candidate_outputs": int(conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs").fetchone()["n"]),
            "tracked_files": int(conn.execute("SELECT COUNT(*) AS n FROM tracked_files").fetchone()["n"]),
            "open_human_attention": int(
                conn.execute("SELECT COUNT(*) AS n FROM human_attention WHERE status = 'open'").fetchone()["n"]
            ),
        }
        blocked = [
            {
                "task_id": row["task_id"],
                "title": row["title"],
                "stage": row["stage"],
                "reasons": json.loads(row["blocked_reasons_json"]),
            }
            for row in conn.execute(
                """
                SELECT task_id, title, stage, blocked_reasons_json
                FROM tasks
                WHERE status = 'blocked'
                ORDER BY updated_at
                """
            )
        ]
        stale = [
            {"artifact_id": row["artifact_id"], "path": row["path"], "artifact_type": row["artifact_type"]}
            for row in conn.execute(
                "SELECT artifact_id, path, artifact_type FROM artifacts WHERE stale = 1 ORDER BY updated_at DESC LIMIT 25"
            )
        ]
        leases = [
            {
                "lease_id": row["lease_id"],
                "task_id": row["task_id"],
                "worker_id": row["worker_id"],
                "expires_at": row["expires_at"],
                "fencing_token": row["fencing_token"],
            }
            for row in conn.execute(
                "SELECT lease_id, task_id, worker_id, expires_at, fencing_token FROM leases WHERE status = 'active'"
            )
        ]
        attention = [
            {
                "attention_id": row["attention_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "severity": row["severity"],
                "reason": row["reason"],
            }
            for row in conn.execute(
                """
                SELECT attention_id, target_type, target_id, severity, reason
                FROM human_attention
                WHERE status = 'open'
                ORDER BY attention_id DESC
                LIMIT 25
                """
            )
        ]
        provider = {
            "estimated_cost_usd": float(
                conn.execute("SELECT COALESCE(SUM(estimated_cost_usd), 0) AS n FROM provider_runs").fetchone()["n"]
            ),
            "cache_hit_tokens": int(
                conn.execute("SELECT COALESCE(SUM(cache_hit_tokens), 0) AS n FROM provider_runs").fetchone()["n"]
            ),
            "cache_miss_tokens": int(
                conn.execute("SELECT COALESCE(SUM(cache_miss_tokens), 0) AS n FROM provider_runs").fetchone()["n"]
            ),
            "output_tokens": int(
                conn.execute("SELECT COALESCE(SUM(output_tokens), 0) AS n FROM provider_runs").fetchone()["n"]
            ),
        }
        budget = {
            f"{row['scope_type']}:{row['scope_id']}": {
                "limit_usd": row["limit_usd"],
                "spent_usd": row["spent_usd"],
                "remaining_usd": row["limit_usd"] - row["spent_usd"],
            }
            for row in conn.execute(
                """
                SELECT b.scope_type, b.scope_id, b.limit_usd,
                  COALESCE(SUM(be.amount_usd), 0) AS spent_usd
                FROM budgets b
                LEFT JOIN budget_entries be ON be.budget_id = b.budget_id
                GROUP BY b.budget_id
                ORDER BY b.scope_type, b.scope_id
                """
            )
        }
    return StatusReport(
        run_state=run["state"] if run else "uninitialized",
        counts=counts,
        blocked_tasks=blocked,
        stale_artifacts=stale,
        active_leases=leases,
        human_attention=attention,
        budget=budget,
        provider_usage=provider,
    )
