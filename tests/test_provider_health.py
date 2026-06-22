from __future__ import annotations

from pathlib import Path
import hashlib
import json
import sqlite3
from typing import cast

from atticus.cli import main as cli_main
from atticus.core.tasks import TaskSpec
from atticus.db import repo


def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _scalar_int(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(str(row[0]))


def _seed_policy_group_with_failure(conn: sqlite3.Connection) -> str:
    policy = {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash",
        "allow_fallback": False,
        "estimated_cost_usd": 0.01,
    }
    repo.add_task(
        conn,
        TaskSpec(
            task_id="provider-health-a",
            title="Provider health A",
            task_type="source_inventory",
            matter_scope="alpha",
            provider_policy=policy,
        ),
    )
    repo.add_task(
        conn,
        TaskSpec(
            task_id="provider-health-b",
            title="Provider health B",
            task_type="source_inventory",
            matter_scope="alpha",
            provider_policy=policy,
        ),
    )
    policy_row = conn.execute("SELECT provider_policy_json FROM tasks WHERE task_id = 'provider-health-a'").fetchone()
    policy_raw = str(policy_row["provider_policy_json"])
    fingerprint = hashlib.sha256(" ".join(policy_raw.split()).encode("utf-8")).hexdigest()
    _ = repo.record_loop_guard_failure(
        conn,
        matter_scope="alpha",
        target_type="task",
        target_id="provider-health-a",
        error_type="provider_preflight_failed",
        message=f"provider policy {fingerprint[:12]} OpenRouter HTTP 401 unauthorized",
        source="test",
        payload={"provider_failure_class": "auth", "requires_user_intervention": True},
    )
    return fingerprint


def test_provider_health_dry_run_groups_by_policy_without_writing(tmp_path: Path, capsys) -> None:
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        fingerprint = _seed_policy_group_with_failure(conn)

    exit_code = cli_main(["provider-health", "--db", str(db_path), "--matter", "alpha", "--group-by-policy", "--json"])
    payload = json.loads(capsys.readouterr().out)

    with repo.db_connection(db_path) as conn:
        health_rows = _scalar_int(conn, "SELECT COUNT(*) FROM provider_health_checks")

    assert exit_code == 0
    assert health_rows == 0
    groups = cast(list[dict[str, object]], payload["groups"])
    assert len(groups) == 1
    assert groups[0]["provider_policy_fingerprint"] == fingerprint
    assert groups[0]["task_count"] == 2
    assert groups[0]["status"] == "blocked"
    assert groups[0]["failure_taxonomy"] == "auth"
    assert groups[0]["failure_count"] == 1


def test_provider_health_write_persists_taxonomy_row(tmp_path: Path, capsys) -> None:
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        fingerprint = _seed_policy_group_with_failure(conn)

    exit_code = cli_main(
        ["provider-health", "--db", str(db_path), "--matter", "alpha", "--group-by-policy", "--write", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    with repo.db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT provider_policy_fingerprint, status, failure_taxonomy, details_json FROM provider_health_checks"
        ).fetchone()

    assert exit_code == 0
    assert row is not None
    assert row["provider_policy_fingerprint"] == fingerprint
    assert row["status"] == "blocked"
    assert row["failure_taxonomy"] == "auth"
    details = json.loads(str(row["details_json"]))
    assert details["failure_taxonomy"] == "auth"
    assert cast(list[dict[str, object]], payload["groups"])[0]["failure_taxonomy"] == "auth"
