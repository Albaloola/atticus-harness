from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sqlite3
from typing import cast

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.scheduler.free_loop import run_free_loop_once
from atticus.scheduler.lease import acquire_lease
from atticus.workers.outputs import record_worker_result


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _scalar_int(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    value = row[0]
    assert value is not None
    return int(str(value))


def _packet(task_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "summary": "foundation shard complete",
        "findings": [{"text": "indexed source inventory shard", "citation_ids": []}],
        "citations": [],
        "proposed_artifacts": [
            {
                "path": f"canonical/{task_id}.json",
                "artifact_type": "foundation_note",
                "stage": str(LegalStage.S0_SOURCE_INVENTORY),
                "title": f"Reduced {task_id}",
            }
        ],
        "proposed_tasks": [
            {
                "task_id": "followup-source-shard",
                "title": "Follow up source shard",
                "task_type": "source_inventory",
                "stage": str(LegalStage.S0_SOURCE_INVENTORY),
                "provider_policy": {
                    "provider": "openrouter",
                    "model": "inclusionai/ling-2.6-1t:free",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.0,
                },
                "expected_value": 5.0,
            }
        ],
    }


def test_free_loop_once_reduces_pending_candidates_and_imports_proposed_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="seed-source-shard",
                title="Seed source shard",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
            ),
        )
        lease_id = acquire_lease(conn, task_id="seed-source-shard", worker_id="worker-01", dry_run=False)
        candidate_id = record_worker_result(
            conn,
            task_id="seed-source-shard",
            lease_id=lease_id,
            worker_id="worker-01",
            payload=_packet("seed-source-shard"),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False)

        candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        seed_task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'seed-source-shard'").fetchone())
        followup = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = 'followup-source-shard'").fetchone())
        provider_runs = _scalar_int(conn, "SELECT COUNT(*) FROM provider_runs")

    assert result["reduced_candidates"] == [candidate_id]
    assert result["imported_tasks"] == ["followup-source-shard"]
    assert candidate["status"] == "reduced"
    assert seed_task["status"] == str(TaskStatus.COMPLETE)
    assert followup is not None
    assert followup["status"] == str(TaskStatus.QUEUED)
    assert provider_runs == 0


def test_free_loop_once_executes_local_capacity_and_leaves_reducer_pending_for_next_tick(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="local-source-shard",
                title="Local source shard",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "local", "model": "stub", "estimated_cost_usd": 0.0},
            ),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=1, execute_workers=True, runtime="local")

        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'local-source-shard'").fetchone())
        candidates = _scalar_int(conn, "SELECT COUNT(*) FROM candidate_outputs WHERE task_id = 'local-source-shard' AND status = 'candidate'")
        active_leases = _scalar_int(conn, "SELECT COUNT(*) FROM leases WHERE status = 'active'")

    assert result["leased_tasks"] == ["local-source-shard"]
    assert result["executed_tasks"] == ["local-source-shard"]
    assert task["status"] == str(TaskStatus.REDUCER_PENDING)
    assert candidates == 1
    assert active_leases == 0


def test_free_loop_once_codex_runtime_fails_closed_without_provider_fallback(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-source-shard",
                title="Codex source shard",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={
                    "provider": "openai-codex",
                    "model": "gpt-5.5",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.0,
                },
            ),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=1, execute_workers=True, runtime="codex")

        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'codex-source-shard'").fetchone())
        active_leases = _scalar_int(conn, "SELECT COUNT(*) FROM leases WHERE status = 'active'")
        failed_leases = _scalar_int(conn, "SELECT COUNT(*) FROM leases WHERE status = 'failed'")
        provider_runs = _scalar_int(conn, "SELECT COUNT(*) FROM provider_runs")
        candidates = _scalar_int(conn, "SELECT COUNT(*) FROM candidate_outputs")

    assert result["leased_tasks"] == ["codex-source-shard"]
    assert result["executed_tasks"] == []
    assert result["worker_errors"]
    assert task["status"] == str(TaskStatus.BLOCKED)
    assert "Codex provider is policy-configurable" in str(task["blocked_reasons_json"])
    assert active_leases == 0
    assert failed_leases == 1
    assert provider_runs == 0
    assert candidates == 0
