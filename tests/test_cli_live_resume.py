from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from atticus.cli import main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.deepseek import OPENROUTER_FREE_MODEL_ORDER
from atticus.providers import live_readiness


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


def test_cli_live_resume_env_failover_models_match_preverified_probe(tmp_path, capsys, monkeypatch):
    model_a, model_b = OPENROUTER_FREE_MODEL_ORDER[:2]
    db_path = init_db(tmp_path)
    monkeypatch.setenv("ATTICUS_ENABLE_LIVE_OPENROUTER", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("ATTICUS_OPENROUTER_FAILOVER_ENABLED", "1")
    monkeypatch.setenv("ATTICUS_OPENROUTER_FAILOVER_MODELS", f"{model_a},{model_b}")
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="env-failover-cli",
                title="Env failover CLI",
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
        json.dumps({"ok": True, "provider": "openrouter", "model": model_b}),
        "--write-leases",
    ])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["ready"] is True
    assert output["runnable_tasks"][0]["models"] == [model_a, model_b]
    assert output["leases"][0]["task_id"] == "env-failover-cli"


def test_cli_live_resume_planning_does_not_expire_stale_leases(tmp_path, capsys, monkeypatch):
    db_path = init_db(tmp_path)
    monkeypatch.setenv("ATTICUS_ENABLE_LIVE_OPENROUTER", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    expired_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="stale-lease-cli",
                title="Stale lease CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.LEASED,
            ),
        )
        conn.execute(
            """
            INSERT INTO leases(lease_id, task_id, worker_id, status, fencing_token, expires_at, created_at, updated_at)
            VALUES ('lease-stale-planning', 'stale-lease-cli', 'old-worker', 'active', 1, ?, ?, ?)
            """,
            (expired_at, expired_at, expired_at),
        )
        before_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    code = main([
        "live-resume",
        "--db",
        str(db_path),
        "--probe-result-json",
        '{"ok": true, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}',
    ])
    output = json.loads(capsys.readouterr().out)

    with repo.db_connection(db_path) as conn:
        lease = conn.execute("SELECT status FROM leases WHERE lease_id = 'lease-stale-planning'").fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE task_id = 'stale-lease-cli'").fetchone()
        attention_count = conn.execute("SELECT COUNT(*) AS n FROM human_attention").fetchone()["n"]
        after_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    assert code == 2
    assert output["write_leases"] is False
    assert output["expired_leases"] == []
    assert lease["status"] == "active"
    assert task["status"] == TaskStatus.LEASED
    assert attention_count == 0
    assert after_events == before_events


def test_cli_doctor_reports_expired_leases_without_mutating(tmp_path, capsys):
    db_path = init_db(tmp_path)
    expired_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="doctor-stale", title="Doctor stale", task_type="source_inventory", status=TaskStatus.LEASED))
        conn.execute(
            """
            INSERT INTO leases(lease_id, task_id, worker_id, status, fencing_token, expires_at, created_at, updated_at)
            VALUES ('lease-doctor-stale', 'doctor-stale', 'old-worker', 'active', 1, ?, ?, ?)
            """,
            (expired_at, expired_at, expired_at),
        )
        before_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    assert main(["doctor", "--db", str(db_path)]) == 0
    output = json.loads(capsys.readouterr().out)

    with repo.db_connection(db_path) as conn:
        lease = conn.execute("SELECT status FROM leases WHERE lease_id = 'lease-doctor-stale'").fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE task_id = 'doctor-stale'").fetchone()
        attention_count = conn.execute("SELECT COUNT(*) AS n FROM human_attention").fetchone()["n"]
        after_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    assert output["diagnostic_only"] is True
    assert output["expired_leases"] == ["lease-doctor-stale"]
    assert lease["status"] == "active"
    assert task["status"] == TaskStatus.LEASED
    assert attention_count == 0
    assert after_events == before_events


def test_cli_provider_probe_requires_live_opt_in_before_spend(capsys, monkeypatch):
    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("provider-probe must not construct a client without opt-in")

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("ATTICUS_ENABLE_LIVE_OPENROUTER", raising=False)
    monkeypatch.setattr(live_readiness, "OpenRouterClient", ExplodingClient)

    code = main(["provider-probe", "--model", "deepseek/deepseek-v4-pro"])
    output = json.loads(capsys.readouterr().out)

    assert code == 2
    assert output["ok"] is False
    assert "ATTICUS_ENABLE_LIVE_OPENROUTER" in output["reason"]


def test_cli_live_resume_probe_requires_live_opt_in_before_spend(tmp_path, capsys, monkeypatch):
    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("live-resume --probe must not construct a client without opt-in")

    db_path = init_db(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("ATTICUS_ENABLE_LIVE_OPENROUTER", raising=False)
    monkeypatch.setattr(live_readiness, "OpenRouterClient", ExplodingClient)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="probe-blocked-cli",
                title="Probe blocked CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )

    code = main(["live-resume", "--db", str(db_path), "--probe", "--write-leases"])
    output = json.loads(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        lease_count = conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]

    assert code == 2
    assert output["ready"] is False
    assert output["leases"] == []
    assert lease_count == 0
    assert any("ATTICUS_ENABLE_LIVE_OPENROUTER" in reason for reason in output["reasons"])


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
