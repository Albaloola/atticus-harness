from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atticus.core.policies import TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.scheduler.capacity import MAX_PARALLEL_AGENT_CAPACITY
from atticus.scheduler.lease import LeaseError, acquire_lease


def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_global_capacity_limit_enforced_for_worker_leases(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        for index in range(MAX_PARALLEL_AGENT_CAPACITY + 1):
            repo.add_task(conn, TaskSpec(task_id=f"task-{index}", title=f"Task {index}", task_type="source_inventory"))
        for index in range(MAX_PARALLEL_AGENT_CAPACITY):
            acquire_lease(conn, task_id=f"task-{index}", worker_id=f"worker-{index}")

        with pytest.raises(LeaseError, match="global worker capacity reached"):
            acquire_lease(conn, task_id=f"task-{MAX_PARALLEL_AGENT_CAPACITY}", worker_id="worker-extra")


def test_expired_leases_are_released_before_capacity_count(tmp_path: Path):
    db_path = _init_db(tmp_path)
    expired_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with repo.db_connection(db_path) as conn:
        for index in range(MAX_PARALLEL_AGENT_CAPACITY):
            repo.add_task(conn, TaskSpec(task_id=f"expired-task-{index}", title=f"Expired {index}", task_type="source_inventory", status=TaskStatus.LEASED))
            _ = conn.execute(
                """
                INSERT INTO leases(lease_id, task_id, worker_id, lease_role, status, fencing_token, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, 'worker', 'active', 1, ?, ?, ?)
                """,
                (f"expired-lease-{index}", f"expired-task-{index}", f"old-worker-{index}", expired_at, expired_at, expired_at),
            )
        repo.add_task(conn, TaskSpec(task_id="new-task", title="New", task_type="source_inventory"))

        lease_id = acquire_lease(conn, task_id="new-task", worker_id="new-worker")
        active = conn.execute("SELECT COUNT(*) AS n FROM leases WHERE status = 'active' AND lease_role = 'worker'").fetchone()

    assert lease_id.startswith("lease-")
    assert active is not None
    assert int(str(active["n"])) == 1
