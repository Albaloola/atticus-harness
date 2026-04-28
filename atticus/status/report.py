"""Read-only status reporting."""

from __future__ import annotations

from collections.abc import Mapping
import json
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


def generate_status(db_path: str) -> StatusReport:
    with db_connection(db_path, read_only=True) as conn:
        run = cast(Mapping[str, object], conn.execute("SELECT state FROM runs ORDER BY updated_at DESC LIMIT 1").fetchone())
        counts = {
            "sources": _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone(), "n"),
            "artifacts": _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone(), "n"),
            "tasks": _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone(), "n"),
            "blocked_tasks": _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'blocked'").fetchone(), "n"),
            "candidate_outputs": _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs").fetchone(), "n"),
            "tracked_files": _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM tracked_files").fetchone(), "n"),
            "open_human_attention": _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM human_attention WHERE status = 'open'").fetchone(), "n"),
        }
        blocked: list[dict[str, object]] = [
            {
                "task_id": row["task_id"],
                "title": row["title"],
                "stage": row["stage"],
                "reasons": json.loads(str(row["blocked_reasons_json"])),
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
        stale: list[dict[str, object]] = [
            {"artifact_id": row["artifact_id"], "path": row["path"], "artifact_type": row["artifact_type"]}
            for row in conn.execute(
                "SELECT artifact_id, path, artifact_type FROM artifacts WHERE stale = 1 ORDER BY updated_at DESC LIMIT 25"
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
            for row in conn.execute(
                "SELECT lease_id, task_id, worker_id, expires_at, fencing_token FROM leases WHERE status = 'active'"
            )
        ]
        attention: list[dict[str, object]] = [
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
        provider: dict[str, object] = {
            "estimated_cost_usd": _scalar_float(conn.execute("SELECT COALESCE(SUM(estimated_cost_usd), 0) AS n FROM provider_runs").fetchone(), "n"),
            "cache_hit_tokens": _scalar_int(conn.execute("SELECT COALESCE(SUM(cache_hit_tokens), 0) AS n FROM provider_runs").fetchone(), "n"),
            "cache_miss_tokens": _scalar_int(conn.execute("SELECT COALESCE(SUM(cache_miss_tokens), 0) AS n FROM provider_runs").fetchone(), "n"),
            "output_tokens": _scalar_int(conn.execute("SELECT COALESCE(SUM(output_tokens), 0) AS n FROM provider_runs").fetchone(), "n"),
        }
        budget: dict[str, object] = {
            f"{row['scope_type']}:{row['scope_id']}": {
                "limit_usd": row["limit_usd"],
                "spent_usd": row["spent_usd"],
                "remaining_usd": float(str(row["limit_usd"])) - float(str(row["spent_usd"])),
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
