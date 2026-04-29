from __future__ import annotations

from typing import cast
from collections.abc import Mapping
from pathlib import Path
import json
import sqlite3


import pytest

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.budget import BudgetExceeded
from atticus.reducer.reducer import reduce_candidate
from atticus.scheduler.lease import acquire_lease
from atticus.workers.outputs import record_worker_result
from atticus.workers.runtime import WorkerExecutionBlocked, execute_codex_work_order, execute_local_work_order
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _json_mapping(text: str) -> Mapping[str, object]:
    value = json.loads(text)
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(float(str(row["n"])))


def valid_packet(task_id: str) -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "candidate summary",
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "finding",
                "finding_type": "drafting_note",
                "citation_ids": [],
                "confidence": 0.5,
                "reasoning_status": "uncertain",
            }
        ],
        "citations": [],
        "proposed_artifacts": [
            {
                "path": f"candidate/{task_id}.json",
                "artifact_type": "evidence_registry",
                "stage": "S0",
                "title": "Evidence registry",
                "content": "{}",
            }
        ],
        "proposed_tasks": [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


class FakeCodexAdapter:
    def __init__(self, payload: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.payload: dict[str, object] | None = payload
        self.error: Exception | None = error
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        work_order: dict[str, object],
        *,
        model: str,
        output_dir: Path,
        timeout_seconds: float,
        reasoning_effort: str = "low",
    ) -> dict[str, object]:
        self.calls.append(
            {
                "work_order": work_order,
                "model": model,
                "output_dir": output_dir,
                "timeout_seconds": timeout_seconds,
                "reasoning_effort": reasoning_effort,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.payload is not None
        return self.payload


def test_execute_local_work_order_happy_path_records_candidate_only(tmp_path: Path):
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
        candidate = cast(Mapping[str, object], conn.execute("SELECT * FROM candidate_outputs WHERE candidate_id = ?", (result.candidate_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'runtime-task'").fetchone())
        attempts = cast(list[Mapping[str, object]], conn.execute("SELECT status, adapter, output_path FROM worker_attempts").fetchall())
        canonical_count = _count(conn, "SELECT COUNT(*) AS n FROM artifacts WHERE produced_by_task_id = 'runtime-task'")

    assert candidate["status"] == "candidate"
    assert task["status"] == "reducer_pending"
    assert len(attempts) == 1
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["adapter"] == "local_stub"
    assert result.output_path.exists()
    assert _json_mapping(result.output_path.read_text(encoding="utf-8"))["task_id"] == "runtime-task"
    assert canonical_count == 0


def test_acquire_lease_expires_stale_active_lease_before_reacquiring(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="stale-lease-task", title="Stale lease task", task_type="extract"))
        stale_lease = acquire_lease(conn, task_id="stale-lease-task", worker_id="worker-stale", seconds=-1)
        new_lease = acquire_lease(conn, task_id="stale-lease-task", worker_id="worker-new")
        leases = cast(list[Mapping[str, object]], conn.execute("SELECT lease_id, status FROM leases WHERE task_id = ? ORDER BY created_at", ("stale-lease-task",)).fetchall())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = ?", ("stale-lease-task",)).fetchone())
    assert stale_lease != new_lease
    assert [(row["lease_id"], row["status"]) for row in leases] == [(stale_lease, "expired"), (new_lease, "active")]
    assert task["status"] == TaskStatus.LEASED


def test_execute_local_work_order_rejects_non_local_adapter(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="unsafe", title="Unsafe", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="unsafe", worker_id="worker-local")
        with pytest.raises(WorkerExecutionBlocked):
            _ = execute_local_work_order(
                conn,
                task_id="unsafe",
                lease_id=lease_id,
                worker_id="worker-local",
                adapter_name="openclaw",
                output_dir=tmp_path / "out",
            )
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")
        canonical_count = _count(conn, "SELECT COUNT(*) AS n FROM artifacts WHERE produced_by_task_id = 'unsafe'")
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'unsafe'").fetchone())
    assert candidate_count == 0
    assert canonical_count == 0
    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "safe local execution" in str(task["blocked_reasons_json"])


def test_execute_codex_work_order_requires_live_gate_before_dispatch(tmp_path: Path):
    db_path = init_db(tmp_path)
    adapter = FakeCodexAdapter(valid_packet("codex-policy-only"))
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-policy-only",
                title="Codex policy only",
                task_type="extract",
                provider_policy={"provider": "openai-codex", "model": "gpt-5.5", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="codex-policy-only", worker_id="worker-codex")
        with pytest.raises(WorkerExecutionBlocked, match="ATTICUS_ENABLE_LIVE_CODEX"):
            _ = execute_codex_work_order(
                conn,
                task_id="codex-policy-only",
                lease_id=lease_id,
                worker_id="worker-codex",
                output_dir=tmp_path / "out",
                adapter=adapter,
                env={},
                allow_live=False,
            )
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'codex-policy-only'").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")

    assert task["status"] == TaskStatus.BLOCKED
    assert "ATTICUS_ENABLE_LIVE_CODEX" in str(task["blocked_reasons_json"])
    assert lease["status"] == "failed"
    assert provider_runs == 0
    assert candidate_count == 0
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("allow_live", "env"),
    [
        (True, {}),
        (False, {"ATTICUS_ENABLE_LIVE_CODEX": "1"}),
    ],
)
def test_execute_codex_work_order_requires_both_live_gate_keys_before_dispatch(tmp_path: Path, allow_live: bool, env: dict[str, str]):
    db_path = init_db(tmp_path)
    adapter = FakeCodexAdapter(valid_packet("codex-half-gated"))
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-half-gated",
                title="Codex half gated",
                task_type="extract",
                provider_policy={"provider": "openai-codex", "model": "gpt-5.5", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="codex-half-gated", worker_id="worker-codex")
        with pytest.raises(WorkerExecutionBlocked, match="ATTICUS_ENABLE_LIVE_CODEX"):
            _ = execute_codex_work_order(
                conn,
                task_id="codex-half-gated",
                lease_id=lease_id,
                worker_id="worker-codex",
                output_dir=tmp_path / "out",
                adapter=adapter,
                env=env,
                allow_live=allow_live,
            )
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'codex-half-gated'").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")

    assert task["status"] == TaskStatus.BLOCKED
    assert lease["status"] == "failed"
    assert provider_runs == 0
    assert candidate_count == 0
    assert adapter.calls == []


def test_execute_codex_work_order_happy_path_records_candidate_and_provider_run(tmp_path: Path):
    db_path = init_db(tmp_path)
    output_dir = tmp_path / "codex-output"
    adapter = FakeCodexAdapter(valid_packet("codex-live"))
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-live",
                title="Codex live",
                task_type="extract",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openai-codex", "model": "gpt-5.5", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="codex-live", worker_id="worker-codex")
        result = execute_codex_work_order(
            conn,
            task_id="codex-live",
            lease_id=lease_id,
            worker_id="worker-codex",
            output_dir=output_dir,
            adapter=adapter,
            env={"ATTICUS_ENABLE_LIVE_CODEX": "1"},
            allow_live=True,
            timeout_seconds=77.0,
            reasoning_effort="medium",
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (result.candidate_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'codex-live'").fetchone())
        provider_run = cast(Mapping[str, object], conn.execute("SELECT requested_provider, requested_model, actual_provider, actual_model, fallback_allowed, fallback_policy_result FROM provider_runs").fetchone())
        attempts = cast(list[Mapping[str, object]], conn.execute("SELECT status, adapter, output_path FROM worker_attempts").fetchall())
        canonical_count = _count(conn, "SELECT COUNT(*) AS n FROM artifacts WHERE produced_by_task_id = 'codex-live'")

    assert candidate["status"] == "candidate"
    assert task["status"] == TaskStatus.REDUCER_PENDING
    assert provider_run["requested_provider"] == "openai-codex"
    assert provider_run["requested_model"] == "gpt-5.5"
    assert provider_run["actual_provider"] == "openai-codex"
    assert provider_run["actual_model"] == "gpt-5.5"
    assert provider_run["fallback_allowed"] == 0
    assert provider_run["fallback_policy_result"] == "not_needed"
    assert len(attempts) == 1
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["adapter"] == "codex_cli"
    assert result.output_path.exists()
    assert result.output_path.resolve().is_relative_to(output_dir.resolve())
    assert adapter.calls[0]["model"] == "gpt-5.5"
    assert adapter.calls[0]["timeout_seconds"] == 77.0
    assert adapter.calls[0]["reasoning_effort"] == "medium"
    assert canonical_count == 0


def test_execute_codex_work_order_malformed_packet_quarantines_after_dispatch(tmp_path: Path):
    db_path = init_db(tmp_path)
    adapter = FakeCodexAdapter({"task_id": "bad-codex-packet", "summary": "missing lists"})
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="bad-codex-packet",
                title="Bad Codex packet",
                task_type="extract",
                provider_policy={"provider": "openai-codex", "model": "gpt-5.5", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="bad-codex-packet", worker_id="worker-codex")
        with pytest.raises(WorkerExecutionBlocked, match="Codex worker output quarantined"):
            _ = execute_codex_work_order(
                conn,
                task_id="bad-codex-packet",
                lease_id=lease_id,
                worker_id="worker-codex",
                output_dir=tmp_path / "out",
                adapter=adapter,
                env={"ATTICUS_ENABLE_LIVE_CODEX": "1"},
                allow_live=True,
            )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE task_id = 'bad-codex-packet'").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'bad-codex-packet'").fetchone())
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")
        attempt = cast(Mapping[str, object], conn.execute("SELECT status, error_json FROM worker_attempts").fetchone())

    assert candidate["status"] == "quarantined"
    assert "missing worker result keys" in str(candidate["quarantined_reason"])
    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.QUARANTINED
    assert provider_runs == 1
    assert attempt["status"] == "failed"


def test_execute_codex_work_order_adapter_error_fails_after_dispatch_without_candidate(tmp_path: Path):
    db_path = init_db(tmp_path)
    adapter = FakeCodexAdapter(error=RuntimeError("codex cli exited 42"))
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-adapter-error",
                title="Codex adapter error",
                task_type="extract",
                provider_policy={"provider": "openai-codex", "model": "gpt-5.5", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="codex-adapter-error", worker_id="worker-codex")
        with pytest.raises(WorkerExecutionBlocked, match="Codex provider call failed after dispatch"):
            _ = execute_codex_work_order(
                conn,
                task_id="codex-adapter-error",
                lease_id=lease_id,
                worker_id="worker-codex",
                output_dir=tmp_path / "out",
                adapter=adapter,
                env={"ATTICUS_ENABLE_LIVE_CODEX": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'codex-adapter-error'").fetchone())
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")
        attempt = cast(Mapping[str, object], conn.execute("SELECT status, error_json FROM worker_attempts").fetchone())

    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "Codex provider call failed after dispatch" in str(task["blocked_reasons_json"])
    assert provider_runs == 1
    assert candidate_count == 0
    assert attempt["status"] == "failed"


def test_execute_codex_work_order_rejects_non_codex_policy(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-no-openrouter",
                title="Codex no OpenRouter",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="codex-no-openrouter", worker_id="worker-codex")
        with pytest.raises(WorkerExecutionBlocked, match="Codex runtime requires provider openai-codex"):
            _ = execute_codex_work_order(
                conn,
                task_id="codex-no-openrouter",
                lease_id=lease_id,
                worker_id="worker-codex",
                output_dir=tmp_path / "out",
                adapter=FakeCodexAdapter(valid_packet("codex-no-openrouter")),
                env={"ATTICUS_ENABLE_LIVE_CODEX": "1"},
                allow_live=True,
            )
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'codex-no-openrouter'").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")

    assert task["status"] == TaskStatus.BLOCKED
    assert "Codex runtime requires provider openai-codex" in str(task["blocked_reasons_json"])
    assert lease["status"] == "failed"
    assert provider_runs == 0
    assert candidate_count == 0


def test_execute_codex_work_order_rejects_wrong_codex_model_before_dispatch(tmp_path: Path):
    db_path = init_db(tmp_path)
    adapter = FakeCodexAdapter(valid_packet("codex-wrong-model"))
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-wrong-model",
                title="Codex wrong model",
                task_type="extract",
                provider_policy={"provider": "openai-codex", "model": "gpt-5.4", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="codex-wrong-model", worker_id="worker-codex")
        with pytest.raises(WorkerExecutionBlocked, match="unknown or unsupported model"):
            _ = execute_codex_work_order(
                conn,
                task_id="codex-wrong-model",
                lease_id=lease_id,
                worker_id="worker-codex",
                output_dir=tmp_path / "out",
                adapter=adapter,
                env={"ATTICUS_ENABLE_LIVE_CODEX": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")

    assert lease["status"] == "failed"
    assert provider_runs == 0
    assert candidate_count == 0
    assert adapter.calls == []


def test_execute_codex_work_order_rejects_codex_fallback_before_dispatch(tmp_path: Path):
    db_path = init_db(tmp_path)
    adapter = FakeCodexAdapter(valid_packet("codex-fallback"))
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-fallback",
                title="Codex fallback",
                task_type="extract",
                provider_policy={"provider": "openai-codex", "model": "gpt-5.5", "allow_fallback": True, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="codex-fallback", worker_id="worker-codex")
        with pytest.raises(WorkerExecutionBlocked, match="Codex fallback is not allowed"):
            _ = execute_codex_work_order(
                conn,
                task_id="codex-fallback",
                lease_id=lease_id,
                worker_id="worker-codex",
                output_dir=tmp_path / "out",
                adapter=adapter,
                env={"ATTICUS_ENABLE_LIVE_CODEX": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")

    assert lease["status"] == "failed"
    assert provider_runs == 0
    assert candidate_count == 0
    assert adapter.calls == []


def test_execute_local_work_order_wrong_worker_fails_lease_and_blocks_task(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="wrong-worker-local", title="Wrong worker local", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="wrong-worker-local", worker_id="worker-local")
        with pytest.raises(WorkerExecutionBlocked, match="belongs to worker"):
            _ = execute_local_work_order(
                conn,
                task_id="wrong-worker-local",
                lease_id=lease_id,
                worker_id="impostor-worker",
                output_dir=tmp_path / "out",
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'wrong-worker-local'").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'wrong-worker-local'")

    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "belongs to worker" in str(task["blocked_reasons_json"])
    assert candidate_count == 0


def test_record_worker_result_wrong_worker_quarantines_and_fails_lease(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="direct-wrong-worker", title="Direct wrong worker", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="direct-wrong-worker", worker_id="worker-owner")
        candidate_id = record_worker_result(
            conn,
            task_id="direct-wrong-worker",
            lease_id=lease_id,
            worker_id="worker-impostor",
            payload=valid_packet("direct-wrong-worker"),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'direct-wrong-worker'").fetchone())
    assert candidate["status"] == "quarantined"
    assert "belongs to worker" in str(candidate["quarantined_reason"])
    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.QUARANTINED


def test_record_worker_result_task_mismatch_quarantines_candidate(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="leased-task", title="Leased task", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="leased-task", worker_id="worker-owner")
        payload = valid_packet("other-task")
        candidate_id = record_worker_result(
            conn,
            task_id="leased-task",
            lease_id=lease_id,
            worker_id="worker-owner",
            payload=payload,
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
    assert candidate["status"] == "quarantined"
    assert "does not match leased task" in str(candidate["quarantined_reason"])
    assert lease["status"] == "failed"


def test_record_worker_result_cross_task_lease_mismatch_fails_actual_lease(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="actual-lease-task", title="Actual lease task", task_type="extract"))
        repo.add_task(conn, TaskSpec(task_id="claimed-output-task", title="Claimed output task", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="actual-lease-task", worker_id="worker-owner")
        candidate_id = record_worker_result(
            conn,
            task_id="claimed-output-task",
            lease_id=lease_id,
            worker_id="worker-owner",
            payload=valid_packet("claimed-output-task"),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        actual_task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'actual-lease-task'").fetchone())
        claimed_task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'claimed-output-task'").fetchone())
    assert candidate["status"] == "quarantined"
    assert "belongs to task actual-lease-task" in str(candidate["quarantined_reason"])
    assert lease["status"] == "failed"
    assert actual_task["status"] == TaskStatus.QUEUED
    assert claimed_task["status"] == TaskStatus.QUARANTINED


def test_record_worker_result_malformed_list_items_quarantine_instead_of_crashing(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="bad-list-item", title="Bad list item", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="bad-list-item", worker_id="worker-owner")
        payload = valid_packet("bad-list-item")
        payload["findings"] = ["not an object"]
        candidate_id = record_worker_result(
            conn,
            task_id="bad-list-item",
            lease_id=lease_id,
            worker_id="worker-owner",
            payload=payload,
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
    assert candidate["status"] == "quarantined"
    assert "findings[0]" in str(candidate["quarantined_reason"])
    assert lease["status"] == "failed"


def test_execute_local_work_order_sanitizes_task_output_paths(tmp_path: Path):
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
        row = cast(Mapping[str, object], conn.execute("SELECT payload_json FROM candidate_outputs WHERE candidate_id = ?", (result.candidate_id,)).fetchone())
        payload = _json_mapping(str(row["payload_json"]))
        proposed_artifacts = cast(list[dict[str, object]], payload["proposed_artifacts"])

    assert result.output_path.resolve().is_relative_to(output_root)
    assert ".." not in str(proposed_artifacts[0]["path"])
    assert str(proposed_artifacts[0]["path"]).startswith("candidate/")


def test_execute_local_work_order_cross_task_lease_mismatch_fails_actual_lease(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="local-actual-lease", title="Local actual lease", task_type="extract"))
        repo.add_task(conn, TaskSpec(task_id="local-claimed-task", title="Local claimed task", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="local-actual-lease", worker_id="worker-local")

    with pytest.raises(WorkerExecutionBlocked, match="belongs to task local-actual-lease"):
        with repo.db_connection(db_path) as conn:
            _ = execute_local_work_order(
                conn,
                task_id="local-claimed-task",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )

    with repo.db_connection(db_path) as conn:
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        actual_task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'local-actual-lease'").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")

    assert lease["status"] == "failed"
    assert actual_task["status"] == TaskStatus.QUEUED
    assert candidate_count == 0


def test_failed_local_execution_persists_failure_audit_when_exception_escapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from atticus.adapters.local_stub import LocalStubAdapter

    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="bad-payload", title="Bad payload", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="bad-payload", worker_id="worker-local")

    def invalid_packet(self: object, payload: dict[str, object]) -> dict[str, object]:
        del self
        return {"task_id": payload["task_id"], "summary": "missing required lists"}

    monkeypatch.setattr(LocalStubAdapter, "run", invalid_packet)
    with pytest.raises(WorkerExecutionBlocked):
        with repo.db_connection(db_path) as conn:
            _ = execute_local_work_order(
                conn,
                task_id="bad-payload",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )

    with repo.db_connection(db_path) as conn:
        attempt = cast(Mapping[str, object], conn.execute("SELECT status, error_json FROM worker_attempts").fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'bad-payload'").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE task_id = 'bad-payload'").fetchone())
    assert attempt["status"] == "failed"
    assert task["status"] == "quarantined"
    assert lease["status"] == "failed"
    assert candidate["status"] == "quarantined"


def test_execute_local_work_order_budget_blocks_before_candidate(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="stage", scope_id="S0", limit_usd=0.01)
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
            _ = execute_local_work_order(
                conn,
                task_id="too-expensive",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")
        attention = cast(Mapping[str, object], conn.execute("SELECT reason FROM human_attention ORDER BY attention_id DESC LIMIT 1").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
    assert lease["status"] == "failed"
    assert candidate_count == 0
    assert "budget" in str(attention["reason"])


def test_execute_local_work_order_requires_estimated_cost_when_cost_limit_exists(tmp_path: Path):
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
            _ = execute_local_work_order(
                conn,
                task_id="missing-local-estimate",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'missing-local-estimate'").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'missing-local-estimate'")

    assert task["status"] == TaskStatus.BLOCKED
    assert "estimated_cost_usd" in str(task["blocked_reasons_json"])
    assert lease["status"] == "failed"
    assert candidate_count == 0


def test_malformed_provider_policy_after_lease_fails_lease_and_blocks_task(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="bad-policy", title="Bad policy", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="bad-policy", worker_id="worker-local")
        _ = conn.execute("PRAGMA ignore_check_constraints = ON")
        _ = conn.execute("UPDATE tasks SET provider_policy_json = ? WHERE task_id = ?", ("{not valid json", "bad-policy"))

        with pytest.raises(WorkerExecutionBlocked, match="malformed provider policy"):
            _ = execute_local_work_order(
                conn,
                task_id="bad-policy",
                lease_id=lease_id,
                worker_id="worker-local",
                output_dir=tmp_path / "out",
            )
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'bad-policy'").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'bad-policy'")
        attention = cast(Mapping[str, object], conn.execute("SELECT reason FROM human_attention ORDER BY attention_id DESC LIMIT 1").fetchone())
    assert task["status"] == TaskStatus.BLOCKED
    assert "malformed provider policy" in str(task["blocked_reasons_json"])
    assert lease["status"] == "failed"
    assert candidate_count == 0
    assert "malformed provider policy" in str(attention["reason"])


def test_local_execution_can_flow_into_reducer_without_worker_canonical_write(tmp_path: Path):
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
        assert _count(conn, "SELECT COUNT(*) AS n FROM artifacts WHERE produced_by_task_id = 'e2e'") == 0
        reducer_lease = acquire_lease(conn, task_id="e2e", worker_id="reducer-local", lease_role="reducer")
        reduction = reduce_candidate(conn, candidate_id=worker_result.candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        artifact = cast(Mapping[str, object], conn.execute("SELECT trust_status, produced_by_task_id FROM artifacts WHERE artifact_id = ?", (reduction["artifact_id"],)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'e2e'").fetchone())
    assert artifact["trust_status"] == "validated"
    assert artifact["produced_by_task_id"] == "e2e"
    assert task["status"] == "complete"
