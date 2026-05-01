from __future__ import annotations

from typing import cast
from collections.abc import Mapping
from pathlib import Path
from datetime import UTC, datetime, timedelta
import json

import atticus.cli as cli
from atticus.cli import main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers import live_readiness


import pytest


def _json_output(text: str) -> dict[str, object]:
    value = json.loads(text)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _count_row(row: Mapping[str, object] | None) -> int:
    assert row is not None
    return int(str(row["n"]))


def _strings(value: object) -> list[str]:
    return [str(item) for item in cast(list[object], value)]


def _object_dicts(value: object) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], value)


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_cli_live_resume_accepts_preverified_probe_and_writes_leases(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
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
    output = _json_output(capsys.readouterr().out)

    assert code == 0
    assert output["ready"] is True
    assert _object_dicts(output["leases"])[0]["task_id"] == "safe-cli"


def test_cli_live_resume_write_applies_smart_policy_before_readiness(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    db_path = init_db(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("ATTICUS_ENABLE_LIVE_OPENROUTER", raising=False)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="unset-policy-cli",
                title="Unset policy CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={},
                status=TaskStatus.QUEUED,
            ),
        )

    code = main([
        "live-resume",
        "--db",
        str(db_path),
        "--allow-live",
        "--probe-result-json",
        '{"ok": true, "provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}',
        "--write",
    ])
    output = _json_output(capsys.readouterr().out)

    with repo.db_connection(db_path) as conn:
        row = cast(Mapping[str, object], conn.execute("SELECT provider_policy_json, status FROM tasks WHERE task_id = 'unset-policy-cli'").fetchone())
    policy = json.loads(str(row["provider_policy_json"]))

    assert code == 0
    assert output["ready"] is True
    assert cast(Mapping[str, object], output["live_env_gate"])["established_for_child_commands"] is True
    assert cast(Mapping[str, object], output["provider_policy_plan"])["tasks_updated"] == 1
    assert policy["provider"] == "openrouter"
    assert policy["model"] == "deepseek/deepseek-v4-flash"
    assert row["status"] == TaskStatus.LEASED
    assert cast(Mapping[str, object], output["next"])["classification"] == "scheduler_can_continue"


def test_cli_live_resume_allow_live_sets_probe_env_without_shell_export(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    db_path = init_db(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("ATTICUS_ENABLE_LIVE_OPENROUTER", raising=False)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="probe-env-cli",
                title="Probe env CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={},
                status=TaskStatus.QUEUED,
            ),
        )

    def fake_probe(provider_policy: Mapping[str, object], *, env: Mapping[str, str] | None = None) -> dict[str, object]:
        assert env is not None
        assert env["ATTICUS_ENABLE_LIVE_OPENROUTER"] == "1"
        return {"ok": True, "provider": "openrouter", "model": str(provider_policy["model"])}

    monkeypatch.setattr(cli, "probe_live_openrouter", fake_probe)

    code = main(["live-resume", "--db", str(db_path), "--allow-live", "--probe", "--model", "deepseek/deepseek-v4-flash", "--write"])
    output = _json_output(capsys.readouterr().out)

    assert code == 0
    assert output["ready"] is True
    assert cast(Mapping[str, object], output["live_env_gate"])["enabled"] is True


def test_cli_live_resume_dry_run_reports_smart_policy_without_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    db_path = init_db(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("ATTICUS_ENABLE_LIVE_OPENROUTER", raising=False)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="unset-policy-dry-cli",
                title="Unset policy dry CLI",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={},
                status=TaskStatus.QUEUED,
            ),
        )

    code = main([
        "live-resume",
        "--db",
        str(db_path),
        "--allow-live",
        "--probe-result-json",
        '{"ok": true, "provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}',
    ])
    output = _json_output(capsys.readouterr().out)

    with repo.db_connection(db_path) as conn:
        raw_policy = str(conn.execute("SELECT provider_policy_json FROM tasks WHERE task_id = 'unset-policy-dry-cli'").fetchone()["provider_policy_json"])

    assert code == 2
    assert raw_policy == "{}"
    assert cast(Mapping[str, object], output["provider_policy_plan"])["tasks_would_update"] == 1
    assert "estimated_cost_usd" in json.dumps(output["blocked_tasks"])


def test_cli_live_resume_env_failover_models_match_preverified_probe(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    model_a, model_b = "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"
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
    output = _json_output(capsys.readouterr().out)

    assert code == 0
    assert output["ready"] is True
    assert _object_dicts(output["runnable_tasks"])[0]["models"] == [model_a, model_b]
    assert _object_dicts(output["leases"])[0]["task_id"] == "env-failover-cli"


def test_cli_live_resume_planning_does_not_expire_stale_leases(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
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
        _ = conn.execute(
            """
            INSERT INTO leases(lease_id, task_id, worker_id, status, fencing_token, expires_at, created_at, updated_at)
            VALUES ('lease-stale-planning', 'stale-lease-cli', 'old-worker', 'active', 1, ?, ?, ?)
            """,
            (expired_at, expired_at, expired_at),
        )
        before_events = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()))

    code = main([
        "live-resume",
        "--db",
        str(db_path),
        "--probe-result-json",
        '{"ok": true, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}',
    ])
    output = _json_output(capsys.readouterr().out)

    with repo.db_connection(db_path) as conn:
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = 'lease-stale-planning'").fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'stale-lease-cli'").fetchone())
        attention_count = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM human_attention").fetchone()))
        after_events = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()))

    assert code == 2
    assert output["write_leases"] is False
    assert output["expired_leases"] == []
    assert lease["status"] == "active"
    assert task["status"] == TaskStatus.LEASED
    assert attention_count == 0
    assert after_events == before_events


def test_cli_doctor_reports_expired_leases_without_mutating(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    expired_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="doctor-stale", title="Doctor stale", task_type="source_inventory", status=TaskStatus.LEASED))
        _ = conn.execute(
            """
            INSERT INTO leases(lease_id, task_id, worker_id, status, fencing_token, expires_at, created_at, updated_at)
            VALUES ('lease-doctor-stale', 'doctor-stale', 'old-worker', 'active', 1, ?, ?, ?)
            """,
            (expired_at, expired_at, expired_at),
        )
        before_events = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()))

    assert main(["doctor", "--db", str(db_path)]) == 0
    output = _json_output(capsys.readouterr().out)

    with repo.db_connection(db_path) as conn:
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = 'lease-doctor-stale'").fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'doctor-stale'").fetchone())
        attention_count = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM human_attention").fetchone()))
        after_events = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()))

    assert output["diagnostic_only"] is True
    assert output["expired_leases"] == ["lease-doctor-stale"]
    assert lease["status"] == "active"
    assert task["status"] == TaskStatus.LEASED
    assert attention_count == 0
    assert after_events == before_events


def test_cli_provider_probe_requires_live_opt_in_before_spend(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    class ExplodingClient:
        def __init__(self, *args: object, **kwargs: object):
            raise AssertionError("provider-probe must not construct a client without opt-in")

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("ATTICUS_ENABLE_LIVE_OPENROUTER", raising=False)
    monkeypatch.setattr(live_readiness, "OpenRouterClient", ExplodingClient)

    code = main(["provider-probe", "--model", "deepseek/deepseek-v4-pro"])
    output = _json_output(capsys.readouterr().out)

    assert code == 2
    assert output["ok"] is False
    assert "ATTICUS_ENABLE_LIVE_OPENROUTER" in str(output["reason"])


def test_cli_live_resume_probe_requires_live_opt_in_before_spend(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    class ExplodingClient:
        def __init__(self, *args: object, **kwargs: object):
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
    output = _json_output(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        lease_count = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()))

    assert code == 2
    assert output["ready"] is False
    assert output["leases"] == []
    assert lease_count == 0
    assert any("ATTICUS_ENABLE_LIVE_OPENROUTER" in reason for reason in _strings(output["reasons"]))


def test_cli_live_resume_rejects_truthy_non_boolean_preverified_probe(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
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
    output = _json_output(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        lease_count = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()))

    assert code == 2
    assert output["ready"] is False
    assert output["leases"] == []
    assert lease_count == 0
    assert any("literal ok=true" in reason for reason in _strings(output["reasons"]))


def test_cli_live_resume_rejects_non_object_preverified_probe_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
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
    output = _json_output(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        lease_count = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()))

    assert code == 2
    assert output["ready"] is False
    assert output["leases"] == []
    assert lease_count == 0
    assert any("JSON object" in reason for reason in _strings(output["reasons"]))


def test_cli_live_resume_rejects_invalid_preverified_probe_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
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
    output = _json_output(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        lease_count = _count_row(cast(Mapping[str, object] | None, conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()))

    assert code == 2
    assert output["ready"] is False
    assert output["leases"] == []
    assert lease_count == 0
    assert any("valid JSON" in reason for reason in _strings(output["reasons"]))


def test_cli_reconcile_foundation_exposes_freeze_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
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
    output = _json_output(capsys.readouterr().out)

    assert code == 2
    assert output["ready_for_live_resume"] is False
    assert output["frozen_tasks"] == ["late-cli"]


def test_cli_human_attention_cleanup_plans_conservative_supersession(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="cand-task",
                title="Candidate task",
                task_type="source_inventory",
                matter_scope="alpha",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                status=TaskStatus.QUEUED,
            ),
        )
        conn.execute(
            """
            INSERT INTO candidate_outputs(candidate_id, task_id, lease_id, worker_id, status, output_type, payload_json, payload_hash, created_at, quarantined_reason)
            VALUES ('cand-noisy', 'cand-task', NULL, 'worker', 'quarantined', 'finding_packet', '{}', 'hash', 'now', 'no citations')
            """
        )
        _ = repo.record_human_attention(conn, matter_scope="alpha", target_type="matter", target_id="alpha", severity="blocker", reason="OpenRouter provider call failed after dispatch: OpenRouter network error timeout", owner="provider")
        _ = repo.record_human_attention(conn, matter_scope="alpha", target_type="task", target_id="cand-task", severity="blocker", reason="local/no-live runtime cannot complete S7 work; use a provider-backed worker", owner="provider")
        _ = repo.record_human_attention(conn, matter_scope="alpha", target_type="candidate", target_id="cand-noisy", severity="warning", reason="rejected empty/no-citation candidate warning")
        _ = repo.record_human_attention(conn, matter_scope="alpha", target_type="matter", target_id="alpha", severity="blocker", reason="missing final_quality_gate certification")
        _ = repo.record_human_attention(conn, matter_scope="alpha", target_type="source", target_id="ntq", severity="blocker", reason="obtain clearer NTQ / tenancy material from user")

    code = main([
        "human-attention",
        "--db",
        str(db_path),
        "--matter",
        "alpha",
        "--cleanup",
        "--provider-probe-passed",
        "openrouter",
        "--json",
    ])
    output = _json_output(capsys.readouterr().out)

    assert code == 0
    assert output["dry_run"] is True
    assert output["would_supersede"] == 3
    assert output["superseded"] == 0
    keep_reasons = "\n".join(str(item["keep_reason"]) for item in _object_dicts(output["keep"]))
    assert "final-gate" in keep_reasons or "final_quality_gate" in keep_reasons
    assert "obtain clearer" in keep_reasons
    with repo.db_connection(db_path) as conn:
        assert _count_row(conn.execute("SELECT COUNT(*) AS n FROM human_attention WHERE status = 'open'").fetchone()) == 5


def test_cli_human_attention_cleanup_write_supersedes_only_safe_items(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _ = repo.record_human_attention(conn, matter_scope="alpha", target_type="matter", target_id="alpha", severity="blocker", reason="OpenRouter provider call failed after dispatch: OpenRouter HTTP 503 timeout", owner="provider")
        _ = repo.record_human_attention(conn, matter_scope="alpha", target_type="matter", target_id="alpha", severity="blocker", reason="missing final_quality_gate certification")

    code = main([
        "human-attention",
        "--db",
        str(db_path),
        "--matter",
        "alpha",
        "--cleanup",
        "--provider-probe-passed",
        "openrouter",
        "--write",
        "--json",
    ])
    output = _json_output(capsys.readouterr().out)

    assert code == 0
    assert output["dry_run"] is False
    assert output["superseded"] == 1
    with repo.db_connection(db_path) as conn:
        open_reasons = [str(row["reason"]) for row in conn.execute("SELECT reason FROM human_attention WHERE status = 'open'").fetchall()]
        superseded_reasons = [str(row["reason"]) for row in conn.execute("SELECT reason FROM human_attention WHERE status = 'superseded'").fetchall()]
    assert open_reasons == ["missing final_quality_gate certification"]
    assert "OpenRouter HTTP 503" in superseded_reasons[0]
