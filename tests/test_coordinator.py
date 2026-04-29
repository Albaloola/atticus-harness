from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
import json

from atticus.cli import main as cli_main
from atticus.db import repo
from atticus.workers.work_order import build_work_order


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _count(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


def test_coordinator_plan_is_dry_run_and_creates_self_contained_tasks(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)

    assert (
        cli_main(
            [
                "coordinator",
                "plan",
                "--db",
                str(db_path),
                "--matter",
                "alpha",
                "--goal",
                "Draft a formal complaint about rent arrears handling",
                "--source-id",
                source_id,
            ]
        )
        == 0
    )
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    tasks = cast(list[Mapping[str, object]], output["tasks"])

    with repo.db_connection(db_path) as conn:
        assert _count(conn, "tasks") == 0
        assert _count(conn, "leases") == 0
        assert _count(conn, "candidate_outputs") == 0
        assert _count(conn, "provider_runs") == 0

    assert output["dry_run"] is True
    assert output["external_actions"] == "blocked"
    assert all(task["matter_scope"] == "alpha" for task in tasks)
    assert any(task["role"] == "drafting_worker" for task in tasks)
    assert any(task["role"] == "hostile_reviewer" for task in tasks)
    assert any(task["role"] == "citation_auditor" for task in tasks)
    assert all(source_id in cast(list[str], task["source_dependencies"]) for task in tasks)
    assert all("Workers produce candidate packets only" in str(task["instructions"]) for task in tasks)
    assert all("Do not send, file, serve, upload, email, contact, message" in str(task["instructions"]) for task in tasks)
    assert all("matter_scope: alpha" in str(task["instructions"]) for task in tasks)


def test_coordinator_write_is_idempotent_and_preserves_task_instructions(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    goal = "Prepare a court correspondence draft and hostile review"

    first = [
        "coordinator",
        "create-tasks",
        "--db",
        str(db_path),
        "--matter",
        "alpha",
        "--goal",
        goal,
        "--write",
    ]
    assert cli_main(first) == 0
    first_output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    assert cli_main(first) == 0
    second_output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))

    with repo.db_connection(db_path) as conn:
        tasks = conn.execute(
            "SELECT task_id, task_type, matter_scope, status, instructions FROM tasks WHERE matter_scope = 'alpha' ORDER BY task_id"
        ).fetchall()
        task_count = len(tasks)
        leases = _count(conn, "leases")
        candidates = _count(conn, "candidate_outputs")
        provider_runs = _count(conn, "provider_runs")
        final_task = next(row for row in tasks if row["task_type"] == "final_quality_gate")
        work_order = build_work_order(conn, task_id=str(final_task["task_id"]), persist_context=False)

    assert first_output["dry_run"] is False
    assert cast(list[str], first_output["created_task_ids"])
    assert second_output["created_task_ids"] == []
    assert task_count == len(cast(list[object], first_output["tasks"]))
    assert all(row["matter_scope"] == "alpha" and row["status"] == "queued" for row in tasks)
    assert all("candidate packets only" in str(row["instructions"]) for row in tasks)
    assert "Task-specific coordinator contract" in work_order.instructions
    assert "final quality gate" in work_order.instructions.lower()
    assert leases == 0
    assert candidates == 0
    assert provider_runs == 0


def test_coordinator_write_assigns_smart_model_decisions(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)

    assert (
        cli_main(
            [
                "coordinator",
                "create-tasks",
                "--db",
                str(db_path),
                "--matter",
                "alpha",
                "--goal",
                "Draft a formal complaint and run hostile review",
                "--write",
            ]
        )
        == 0
    )
    _ = capsys.readouterr()

    with repo.db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT task_type, provider_policy_json FROM tasks WHERE matter_scope = 'alpha' ORDER BY task_id"
        ).fetchall()

    policies = {str(row["task_type"]): cast(Mapping[str, object], json.loads(str(row["provider_policy_json"]))) for row in rows}
    evidence_decision = cast(Mapping[str, object], policies["evidence_issue_map"]["model_decision"])
    hostile_decision = cast(Mapping[str, object], policies["hostile_opponent_review"]["model_decision"])

    assert policies["evidence_issue_map"]["model"] == "deepseek/deepseek-v4-flash"
    assert evidence_decision["decision_tier"] == "flash_worker"
    assert policies["hostile_opponent_review"]["model"] == "deepseek/deepseek-v4-pro"
    assert hostile_decision["decision_tier"] == "pro_orchestrator"


def test_coordinator_rejects_cross_matter_dependencies(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        beta_source = repo.add_source(conn, matter_scope="beta", path="/beta/source.pdf", sha256="b" * 64)

    assert (
        cli_main(
            [
                "coordinator",
                "plan",
                "--db",
                str(db_path),
                "--matter",
                "alpha",
                "--goal",
                "Build a chronology",
                "--source-id",
                beta_source,
            ]
        )
        == 2
    )
