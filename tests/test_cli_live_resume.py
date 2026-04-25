from __future__ import annotations

import json

from atticus.cli import main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo


def init_db(tmp_path):
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_cli_live_resume_accepts_preverified_probe_and_writes_leases(tmp_path, capsys, monkeypatch):
    db_path = init_db(tmp_path)
    monkeypatch.setenv("ATTICUS_ENABLE_LIVE_OPENROUTER", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="safe-cli",
                title="Safe CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )

    code = main([
        "live-resume",
        "--db",
        str(db_path),
        "--capacity",
        "15",
        "--probe-result-json",
        '{"ok": true, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}',
        "--write-leases",
    ])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["ready"] is True
    assert output["leases"][0]["task_id"] == "safe-cli"


def test_cli_live_resume_rejects_truthy_non_boolean_preverified_probe(tmp_path, capsys, monkeypatch):
    db_path = init_db(tmp_path)
    monkeypatch.setenv("ATTICUS_ENABLE_LIVE_OPENROUTER", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="truthy-cli",
                title="Truthy CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )

    code = main([
        "live-resume",
        "--db",
        str(db_path),
        "--probe-result-json",
        '{"ok": "false", "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}',
        "--write-leases",
    ])
    output = json.loads(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        lease_count = conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]

    assert code == 2
    assert output["ready"] is False
    assert output["leases"] == []
    assert lease_count == 0
    assert any("literal ok=true" in reason for reason in output["reasons"])


def test_cli_live_resume_rejects_non_object_preverified_probe_json(tmp_path, capsys, monkeypatch):
    db_path = init_db(tmp_path)
    monkeypatch.setenv("ATTICUS_ENABLE_LIVE_OPENROUTER", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="non-object-cli",
                title="Non-object CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )

    code = main([
        "live-resume",
        "--db",
        str(db_path),
        "--probe-result-json",
        "true",
        "--write-leases",
    ])
    output = json.loads(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        lease_count = conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]

    assert code == 2
    assert output["ready"] is False
    assert output["leases"] == []
    assert lease_count == 0
    assert any("JSON object" in reason for reason in output["reasons"])


def test_cli_live_resume_rejects_invalid_preverified_probe_json(tmp_path, capsys, monkeypatch):
    db_path = init_db(tmp_path)
    monkeypatch.setenv("ATTICUS_ENABLE_LIVE_OPENROUTER", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="invalid-json-cli",
                title="Invalid JSON CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )

    code = main([
        "live-resume",
        "--db",
        str(db_path),
        "--probe-result-json",
        "{not valid json",
        "--write-leases",
    ])
    output = json.loads(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        lease_count = conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]

    assert code == 2
    assert output["ready"] is False
    assert output["leases"] == []
    assert lease_count == 0
    assert any("valid JSON" in reason for reason in output["reasons"])


def test_cli_reconcile_foundation_exposes_freeze_result(tmp_path, capsys):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="late-cli",
                title="Late CLI",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )

    code = main(["reconcile-foundation", "--db", str(db_path), "--write"])
    output = json.loads(capsys.readouterr().out)

    assert code == 2
    assert output["ready_for_live_resume"] is False
    assert output["frozen_tasks"] == ["late-cli"]
