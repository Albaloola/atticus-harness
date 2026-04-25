from __future__ import annotations

import json

import pytest

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.budget import BudgetExceeded
from atticus.reducer.reducer import reduce_candidate
from atticus.scheduler.lease import acquire_lease
from atticus.workers.runtime import WorkerExecutionBlocked, execute_local_work_order


def init_db(tmp_path):
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_execute_local_work_order_happy_path_records_candidate_only(tmp_path):
    db_path = init_db(tmp_path)
    output_dir = tmp_path / "worker-output"
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="runtime-task",
                title="Runtime task",
                task_type="extract",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "local", "model": "stub", "estimated_cost_usd": 0.0},
                status=TaskStatus.QUEUED,
            ),
        )
        lease_id = acquire_lease(conn, task_id="runtime-task", worker_id="worker-local")
        result = execute_local_work_order(
            conn,
            task_id="runtime-task",
            lease_id=lease_id,
            worker_id="worker-local",
            output_dir=output_dir,
        )
        candidate = conn.execute("SELECT * FROM candidate_outputs WHERE candidate_id = ?", (result.candidate_id,)).fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE task_id = 'runtime-task'").fetchone()
        attempts = conn.execute("SELECT status, adapter, output_path FROM worker_attempts").fetchall()
        canonical_count = conn.execute("SELECT COUNT(*) AS n FROM artifacts WHERE produced_by_task_id = 'runtime-task'").fetchone()["n"]

    assert candidate["status"] == "candidate"
    assert task["status"] == "reducer_pending"
    assert len(attempts) == 1
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["adapter"] == "local_stub"
    assert result.output_path.exists()
    assert json.loads(result.output_path.read_text(encoding="utf-8"))["task_id"] == "runtime-task"
    assert canonical_count == 0


def test_acquire_lease_expires_stale_active_lease_before_reacquiring(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="stale-lease-task", title="Stale lease task", task_type="extract"))
        stale_lease = acquire_lease(conn, task_id="stale-lease-task", worker_id="worker-stale", seconds=-1)
        new_lease = acquire_lease(conn, task_id="stale-lease-task", worker_id="worker-new")
        leases = conn.execute("SELECT lease_id, status FROM leases WHERE task_id = ? ORDER BY created_at", ("stale-lease-task",)).fetchall()
        task = conn.execute("SELECT status FROM tasks WHERE task_id = ?", ("stale-lease-task",)).fetchone()

    assert stale_lease != new_lease
    assert [(row["lease_id"], row["status"]) for row in leases] == [(stale_lease, "expired"), (new_lease, "active")]
    assert task["status"] == TaskStatus.LEASED


def test_execute_local_work_order_rejects_non_local_adapter(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="unsafe", title="Unsafe", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="unsafe", worker_id="worker-local")
        with pytest.raises(WorkerExecutionBlocked):
            execute_local_work_order(
                conn,
                task_id="unsafe",
                lease_id=lease_id,
                worker_id="worker-local",
                adapter_name="openclaw",
                output_dir=tmp_path / "out",
            )
        candidate_count = conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs").fetchone()["n"]
        canonical_count = conn.execute("SELECT COUNT(*) AS n FROM artifacts WHERE produced_by_task_id = 'unsafe'").fetchone()["n"]
        lease = conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        task = conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'unsafe'").fetchone()

    assert candidate_count == 0
    assert canonical_count == 0
    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "safe local execution" in task["blocked_reasons_json"]


def test_execute_local_work_order_wrong_worker_fails_lease_and_blocks_task(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="wrong-worker-local", title="Wrong worker local", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="wrong-worker-local", worker_id="worker-local")
        with pytest.raises(WorkerExecutionBlocked, match="belongs to worker"):
            execute_local_work_order(
                conn,
                task_id="wrong-worker-local",
                lease_id=lease_id,
                worker_id="impostor-worker",
                output_dir=tmp_path / "out",
            )
        lease = conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        task = conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'wrong-worker-local'").fetchone()
        candidate_count = conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'wrong-worker-local'").fetchone()["n"]

    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "belongs to worker" in task["blocked_reasons_json"]
    assert candidate_count == 0


def test_execute_local_work_order_sanitizes_task_output_paths(tmp_path):
    db_path = init_db(tmp_path)
    output_root = (tmp_path / "safe-output").resolve()
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="../escape", title="Escape", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="../escape", worker_id="worker-local")
        result = execute_local_work_order(
            conn,
            task_id="../escape",
            lease_id=lease_id,
            worker_id="worker-local",
            output_dir=output_root,
        )
        payload = json.loads(conn.execute("SELECT payload_json FROM candidate_outputs WHERE candidate_id = ?", (result.candidate_id,)).fetchone()["payload_json"])

    assert result.output_path.resolve().is_relative_to(output_root)
    assert ".." not in payload["proposed_artifacts"][0]["path"]
    assert payload["proposed_artifacts"][0]["path"].startswith("candidate/")


def test_failed_local_execution_persists_failure_audit_when_exception_escapes(tmp_path, monkeypatch):
    from atticus.adapters.local_stub import LocalStubAdapter

    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="bad-payload", title="Bad payload", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="bad-payload", worker_id="worker-local")

    def invalid_packet(self, payload):
        return {"task_id": payload["task_id"], "summary": "missing required lists"}

    monkeypatch.setattr(LocalStubAdapter, "run", invalid_packet)
    with pytest.raises(WorkerExecutionBlocked):
        with repo.db_connection(db_path) as conn:
            execute_local_work_order(
                conn,
                task_id="bad-payload",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )

    with repo.db_connection(db_path) as conn:
        attempt = conn.execute("SELECT status, error_json FROM worker_attempts").fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE task_id = 'bad-payload'").fetchone()
        lease = conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        candidate = conn.execute("SELECT status FROM candidate_outputs WHERE task_id = 'bad-payload'").fetchone()

    assert attempt["status"] == "failed"
    assert task["status"] == "quarantined"
    assert lease["status"] == "failed"
    assert candidate["status"] == "quarantined"


def test_execute_local_work_order_budget_blocks_before_candidate(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_budget(conn, scope_type="stage", scope_id="S0", limit_usd=0.01)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="too-expensive",
                title="Too expensive",
                task_type="extract",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "local", "model": "stub", "estimated_cost_usd": 0.50},
            ),
        )
        lease_id = acquire_lease(conn, task_id="too-expensive", worker_id="worker-local")
        with pytest.raises(BudgetExceeded):
            execute_local_work_order(
                conn,
                task_id="too-expensive",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )
        candidate_count = conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs").fetchone()["n"]
        attention = conn.execute("SELECT reason FROM human_attention ORDER BY attention_id DESC LIMIT 1").fetchone()
        lease = conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()

    assert lease["status"] == "failed"
    assert candidate_count == 0
    assert "budget" in attention["reason"]


def test_execute_local_work_order_requires_estimated_cost_when_cost_limit_exists(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="missing-local-estimate",
                title="Missing local estimate",
                task_type="extract",
                provider_policy={"provider": "local", "model": "stub"},
                cost_limit_usd=0.10,
            ),
        )
        lease_id = acquire_lease(conn, task_id="missing-local-estimate", worker_id="worker-local")
        with pytest.raises(WorkerExecutionBlocked, match="estimated_cost_usd"):
            execute_local_work_order(
                conn,
                task_id="missing-local-estimate",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )
        task = conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'missing-local-estimate'").fetchone()
        lease = conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        candidate_count = conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'missing-local-estimate'").fetchone()["n"]

    assert task["status"] == TaskStatus.BLOCKED
    assert "estimated_cost_usd" in task["blocked_reasons_json"]
    assert lease["status"] == "failed"
    assert candidate_count == 0


def test_malformed_provider_policy_after_lease_fails_lease_and_blocks_task(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="bad-policy", title="Bad policy", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="bad-policy", worker_id="worker-local")
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE tasks SET provider_policy_json = ? WHERE task_id = ?", ("{not valid json", "bad-policy"))

        with pytest.raises(WorkerExecutionBlocked, match="malformed provider policy"):
            execute_local_work_order(
                conn,
                task_id="bad-policy",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )
        task = conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'bad-policy'").fetchone()
        lease = conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        candidate_count = conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'bad-policy'").fetchone()["n"]
        attention = conn.execute("SELECT reason FROM human_attention ORDER BY attention_id DESC LIMIT 1").fetchone()

    assert task["status"] == TaskStatus.BLOCKED
    assert "malformed provider policy" in task["blocked_reasons_json"]
    assert lease["status"] == "failed"
    assert candidate_count == 0
    assert "malformed provider policy" in attention["reason"]


def test_local_execution_can_flow_into_reducer_without_worker_canonical_write(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="e2e", title="E2E", task_type="extract"))
        worker_lease = acquire_lease(conn, task_id="e2e", worker_id="worker-local")
        worker_result = execute_local_work_order(
            conn,
            task_id="e2e",
            lease_id=worker_lease,
            worker_id="worker-local",
            output_dir=tmp_path / "out",
        )
        assert conn.execute("SELECT COUNT(*) AS n FROM artifacts WHERE produced_by_task_id = 'e2e'").fetchone()["n"] == 0
        reducer_lease = acquire_lease(conn, task_id="e2e", worker_id="reducer-local")
        reduction = reduce_candidate(conn, candidate_id=worker_result.candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        artifact = conn.execute("SELECT trust_status, produced_by_task_id FROM artifacts WHERE artifact_id = ?", (reduction["artifact_id"],)).fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE task_id = 'e2e'").fetchone()

    assert artifact["trust_status"] == "validated"
    assert artifact["produced_by_task_id"] == "e2e"
    assert task["status"] == "complete"
