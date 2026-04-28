from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
import json

from atticus.cli import main as cli_main
from atticus.db import repo
from atticus.workflows.registry import load_workflow, list_workflows, plan_workflow


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_workflow_registry_loads_markdown_frontmatter():
    names = [workflow.name for workflow in list_workflows()]
    assert "chronology-build" in names
    assert "hostile-review" in names
    workflow = load_workflow("complaint-draft")
    assert workflow.frontmatter["jurisdiction"] == "scotland"
    assert workflow.required_certifications


def test_workflow_run_dry_run_creates_no_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path, read_only=True) as conn:
        plan = plan_workflow(conn, name="chronology-build", matter_scope="alpha", dry_run=True)
    with repo.db_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE matter_scope = 'alpha'").fetchone()
        assert row is not None
        count = row["n"]

    assert plan["dry_run"] is True
    assert plan["workflow"] == "chronology-build"
    tasks = cast(list[Mapping[str, object]], plan["tasks"])
    assert len(tasks) >= 2
    assert all(task["matter_scope"] == "alpha" for task in tasks)
    assert count == 0


def test_workflow_cli_write_creates_matter_scoped_tasks_without_execution(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    assert cli_main(["workflow", "run", "hostile-review", "--db", str(db_path), "--matter", "alpha", "--write"]) == 0
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    with repo.db_connection(db_path) as conn:
        tasks = conn.execute("SELECT task_id, matter_scope, status FROM tasks WHERE matter_scope = 'alpha' ORDER BY task_id").fetchall()
        lease_row = conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()
        candidate_row = conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs").fetchone()
        assert lease_row is not None
        assert candidate_row is not None
        leases = lease_row["n"]
        candidates = candidate_row["n"]

    assert output["dry_run"] is False
    assert tasks
    assert all(row["status"] == "queued" for row in tasks)
    assert leases == 0
    assert candidates == 0


def test_workflow_cli_list_and_show(capsys):
    assert cli_main(["workflow", "list"]) == 0
    listed = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    workflow_rows = cast(list[Mapping[str, object]], listed["workflows"])
    assert any(item["name"] == "sar-disclosure-review" for item in workflow_rows)

    assert cli_main(["workflow", "show", "witness-statement-prep"]) == 0
    shown = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    assert shown["name"] == "witness-statement-prep"
    frontmatter = cast(Mapping[str, object], shown["frontmatter"])
    assert frontmatter["risk_level"]
