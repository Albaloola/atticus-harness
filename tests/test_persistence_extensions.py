from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage
from atticus.core.tasks import TaskSpec
from atticus.db import repo


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _scalar_int(row: object) -> int:
    assert row is not None
    return int(str(cast(dict[str, object], row)["n"]))


def test_matter_profile_activation_supersedes_prior_active_profile(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        first = repo.create_matter_profile(conn, matter_scope="napier", profile_name="Initial")
        second = repo.create_matter_profile(
            conn,
            matter_scope="napier",
            profile_name="Discovery",
            stages=[{"stage": "S0", "enabled": True, "model_policy": {"tier": "flash_worker"}}],
            reason="matter-specific discovery plan",
        )
        active = repo.get_active_matter_profile(conn, matter_scope="napier")
        superseded = conn.execute("SELECT status FROM matter_profiles WHERE matter_profile_id = ?", (first,)).fetchone()

    assert first != second
    assert active is not None
    active_stages = cast(list[dict[str, object]], active["stages"])
    assert active["matter_profile_id"] == second
    assert active["profile_name"] == "Discovery"
    assert active_stages == [
        {
            "profile_stage_id": active_stages[0]["profile_stage_id"],
            "stage": "S0",
            "enabled": True,
            "gate_policy": {},
            "worker_policy": {},
            "model_policy": {"tier": "flash_worker"},
            "created_at": active_stages[0]["created_at"],
        }
    ]
    assert superseded is not None
    assert superseded["status"] == "superseded"


def test_orchestrator_work_run_reuse_and_cache_observation_are_durable(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="source-task", title="Source", task_type="source_inventory", matter_scope="napier", stage=LegalStage.S0_SOURCE_INVENTORY))
        _ = repo.add_context_pack(
            conn,
            context_pack_id="ctx-test",
            matter_scope="napier",
            task_id="source-task",
            pack_type="work_order",
            fingerprint="ctx-fingerprint",
            token_budget=1000,
            estimated_tokens=50,
            sections=[],
        )
        orchestrator_id = repo.upsert_matter_orchestrator(
            conn,
            matter_scope="napier",
            status="running",
            current_goal="inventory sources",
            model_decision={"decision_tier": "pro_orchestrator"},
        )
        event_id = repo.record_orchestrator_event(conn, orchestrator_id=orchestrator_id, event_type="tick", payload={"n": 1})
        work_run_id = repo.start_work_run(conn, matter_scope="napier", goal="inventory sources")
        step_id = repo.record_work_run_step(
            conn,
            work_run_id=work_run_id,
            step_type="source_inventory",
            status="complete",
            task_id="source-task",
            context_pack_id="ctx-test",
            input_fingerprint="input-1",
            output_fingerprint="output-1",
        )
        reusable = repo.find_reusable_work_step(conn, matter_scope="napier", step_type="source_inventory", input_fingerprint="input-1")
        reuse_id = repo.record_work_reuse(conn, matter_scope="napier", reused_from_step_id=step_id, reused_by_step_id=step_id)
        provider_run_id = repo.record_provider_run(
            conn,
            task_id="source-task",
            requested_provider="openrouter",
            requested_model="deepseek/deepseek-v4-flash",
            actual_provider="openrouter",
            actual_model="deepseek/deepseek-v4-flash",
            input_tokens=100,
            output_tokens=20,
            cache_hit_tokens=40,
            cache_miss_tokens=60,
            cache_write_tokens=10,
            configured_models=("deepseek/deepseek-v4-flash",),
            failover_events=({"event": "model_success"},),
            raw_usage={"provider_policy": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}},
        )
        orchestrator = repo.get_matter_orchestrator(conn, matter_scope="napier")
        provider_row = conn.execute("SELECT * FROM provider_runs WHERE provider_run_id = ?", (provider_run_id,)).fetchone()
        cache_row = conn.execute("SELECT * FROM prompt_cache_observations WHERE provider_run_id = ?", (provider_run_id,)).fetchone()

    assert event_id.startswith("orchevt-")
    assert reusable is not None
    assert reusable["work_run_step_id"] == step_id
    assert reuse_id.startswith("reuse-")
    assert orchestrator is not None
    assert orchestrator["model_decision"] == {"decision_tier": "pro_orchestrator"}
    assert provider_row is not None
    assert provider_row["context_pack_id"] == "ctx-test"
    assert provider_row["context_fingerprint"] == "ctx-fingerprint"
    assert json.loads(str(provider_row["configured_models_json"])) == ["deepseek/deepseek-v4-flash"]
    assert json.loads(str(provider_row["failover_events_json"])) == [{"event": "model_success"}]
    assert provider_row["cache_write_tokens"] == 10
    assert cache_row is not None
    assert cache_row["cache_hit_tokens"] == 40
    assert cache_row["reason"] == "provider cache telemetry only; cache hits are not evidence correctness"


def test_work_run_steps_reject_cross_matter_targets(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="alpha-task", title="Alpha", task_type="source_inventory", matter_scope="alpha"))
        repo.add_task(conn, TaskSpec(task_id="beta-task", title="Beta", task_type="source_inventory", matter_scope="beta"))
        beta_artifact_id = repo.add_artifact(conn, artifact_id="beta-artifact", matter_scope="beta", path="/beta/artifact.json", artifact_type="note")
        beta_context_pack_id = repo.add_context_pack(
            conn,
            context_pack_id="beta-context",
            matter_scope="beta",
            task_id="beta-task",
            pack_type="work_order",
            fingerprint="beta-context-fingerprint",
            token_budget=100,
            estimated_tokens=10,
            sections=[],
        )
        beta_provider_run_id = repo.record_provider_run(
            conn,
            task_id="beta-task",
            requested_provider="openrouter",
            requested_model="deepseek/deepseek-v4-flash",
            actual_provider="openrouter",
            actual_model="deepseek/deepseek-v4-flash",
        )
        alpha_work_run_id = repo.start_work_run(conn, matter_scope="alpha", goal="alpha work")

        with pytest.raises(ValueError, match="task_id beta-task belongs to matter beta"):
            repo.record_work_run_step(conn, work_run_id=alpha_work_run_id, step_type="bad-task", status="complete", task_id="beta-task")
        with pytest.raises(ValueError, match="artifact_id beta-artifact belongs to matter beta"):
            repo.record_work_run_step(conn, work_run_id=alpha_work_run_id, step_type="bad-artifact", status="complete", artifact_id=beta_artifact_id)
        with pytest.raises(ValueError, match="context_pack_id beta-context belongs to matter beta"):
            repo.record_work_run_step(conn, work_run_id=alpha_work_run_id, step_type="bad-context", status="complete", context_pack_id=beta_context_pack_id)
        with pytest.raises(ValueError, match="provider_run_id .* belongs to matter beta"):
            repo.record_work_run_step(conn, work_run_id=alpha_work_run_id, step_type="bad-provider-run", status="complete", provider_run_id=beta_provider_run_id)

        step_count = conn.execute("SELECT COUNT(*) AS n FROM work_run_steps WHERE matter_scope = 'alpha'").fetchone()

    assert step_count is not None
    assert int(str(step_count["n"])) == 0


def test_work_reuse_rejects_cross_matter_steps(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        alpha_work_run_id = repo.start_work_run(conn, matter_scope="alpha", goal="alpha work")
        beta_work_run_id = repo.start_work_run(conn, matter_scope="beta", goal="beta work")
        alpha_step_id = repo.record_work_run_step(conn, work_run_id=alpha_work_run_id, step_type="source_inventory", status="complete")
        beta_step_id = repo.record_work_run_step(conn, work_run_id=beta_work_run_id, step_type="source_inventory", status="complete")

        with pytest.raises(ValueError, match="reused_from_step_id .* belongs to matter beta"):
            repo.record_work_reuse(conn, matter_scope="alpha", reused_from_step_id=beta_step_id, reused_by_step_id=alpha_step_id)
        with pytest.raises(ValueError, match="reused_by_step_id .* belongs to matter beta"):
            repo.record_work_reuse(conn, matter_scope="alpha", reused_from_step_id=alpha_step_id, reused_by_step_id=beta_step_id)

        reuse_count = conn.execute("SELECT COUNT(*) AS n FROM work_reuse_records").fetchone()

    assert reuse_count is not None
    assert int(str(reuse_count["n"])) == 0


def test_prompt_cache_observations_reject_cross_matter_links(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="beta-task", title="Beta", task_type="source_inventory", matter_scope="beta"))
        provider_run_id = repo.record_provider_run(
            conn,
            task_id="beta-task",
            requested_provider="openrouter",
            requested_model="deepseek/deepseek-v4-flash",
            actual_provider="openrouter",
            actual_model="deepseek/deepseek-v4-flash",
        )

        with pytest.raises(ValueError, match="provider_run_id .* belongs to matter beta"):
            repo.record_prompt_cache_observation(conn, matter_scope="alpha", provider_run_id=provider_run_id, query_source="test")


def test_cli_matter_profile_orchestrator_and_work_run_smokes(tmp_path: Path):
    db_path = init_db(tmp_path)

    assert cli_main(["matter-profile", "create", "--db", str(db_path), "--matter", "napier", "--name", "CLI profile"]) == 0
    assert cli_main(["matter-profile", "create", "--db", str(db_path), "--matter", "napier", "--name", "CLI profile", "--write"]) == 0
    assert cli_main(["matter-profile", "show", "--db", str(db_path), "--matter", "napier"]) == 0
    assert cli_main(["orchestrator", "upsert", "--db", str(db_path), "--matter", "napier", "--status", "running", "--goal", "inventory", "--write"]) == 0
    assert cli_main(["orchestrator", "event", "--db", str(db_path), "--matter", "napier", "--event-type", "tick", "--payload-json", '{"n": 1}', "--write"]) == 0
    assert cli_main(["work-run", "start", "--db", str(db_path), "--matter", "napier", "--goal", "inventory", "--write"]) == 0

    with repo.db_connection(db_path) as conn:
        work_run = conn.execute("SELECT work_run_id FROM work_runs WHERE matter_scope = 'napier'").fetchone()
        assert work_run is not None
        work_run_id = str(work_run["work_run_id"])

    assert cli_main(["work-run", "step", "--db", str(db_path), "--matter", "napier", "--work-run-id", work_run_id, "--step-type", "source_inventory", "--input-fingerprint", "input-cli", "--output-fingerprint", "output-cli", "--write"]) == 0
    assert cli_main(["work-run", "reusable", "--db", str(db_path), "--matter", "napier", "--step-type", "source_inventory", "--input-fingerprint", "input-cli"]) == 0

    with repo.db_connection(db_path) as conn:
        profile_count = _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM matter_profiles WHERE matter_scope = 'napier'").fetchone())
        event_count = _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM orchestrator_events WHERE matter_scope = 'napier'").fetchone())
        step_count = _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM work_run_steps WHERE matter_scope = 'napier'").fetchone())

    assert profile_count == 1
    assert event_count == 1
    assert step_count == 1


def test_cli_dry_run_profile_and_orchestrator_commands_do_not_create_state(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="napier-task", title="Napier", task_type="source_inventory", matter_scope="napier"))

    assert cli_main(["matter-profile", "show", "--db", str(db_path), "--matter", "napier"]) == 0
    assert cli_main(["matter-profile", "propose", "--db", str(db_path), "--matter", "napier", "--goal", "inventory"]) == 0
    assert cli_main(["orchestrator", "tick", "--db", str(db_path), "--matter", "napier", "--capacity", "1"]) == 0

    with repo.db_connection(db_path) as conn:
        profile_count = _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM matter_profiles WHERE matter_scope = 'napier'").fetchone())
        orchestrator_count = _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM matter_orchestrators WHERE matter_scope = 'napier'").fetchone())
        lease_count = _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone())

    assert profile_count == 0
    assert orchestrator_count == 0
    assert lease_count == 0


def test_cli_work_run_commands_reject_wrong_matter(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="beta-task", title="Beta", task_type="source_inventory", matter_scope="beta"))
        beta_work_run_id = repo.start_work_run(conn, matter_scope="beta", goal="beta work")
        beta_resume_token = str(conn.execute("SELECT resume_token FROM work_runs WHERE work_run_id = ?", (beta_work_run_id,)).fetchone()["resume_token"])
        beta_step_id = repo.record_work_run_step(conn, work_run_id=beta_work_run_id, step_type="source_inventory", status="complete")

    assert cli_main(["work-run", "resume", "--db", str(db_path), "--matter", "alpha", "--resume-token", beta_resume_token]) == 2
    assert cli_main(["work-run", "complete", "--db", str(db_path), "--matter", "alpha", "--work-run-id", beta_work_run_id, "--write"]) == 2
    assert cli_main(["work-run", "step", "--db", str(db_path), "--matter", "alpha", "--work-run-id", beta_work_run_id, "--step-type", "source_inventory", "--write"]) == 2
    assert cli_main(["work-run", "reuse", "--db", str(db_path), "--matter", "alpha", "--reused-from-step-id", beta_step_id, "--write"]) == 2


def test_cli_orchestrator_worker_failed_rejects_wrong_matter(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="beta-task", title="Beta", task_type="source_inventory", matter_scope="beta"))

    assert cli_main(["orchestrator", "worker-failed", "--db", str(db_path), "--matter", "alpha", "--task-id", "beta-task", "--reason", "failed"]) == 2
    assert cli_main(["orchestrator", "worker-failed", "--db", str(db_path), "--matter", "alpha", "--task-id", "beta-task", "--reason", "failed", "--write"]) == 2

    with repo.db_connection(db_path) as conn:
        event_count = _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM orchestrator_events").fetchone())
        attention_count = _scalar_int(conn.execute("SELECT COUNT(*) AS n FROM human_attention").fetchone())

    assert event_count == 0
    assert attention_count == 0
