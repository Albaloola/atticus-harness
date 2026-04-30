from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import cast

import pytest

from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.scheduler import free_loop as free_loop_module
from atticus.scheduler.free_loop import run_free_loop_once
from atticus.scheduler.lease import acquire_lease
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


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
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "foundation shard complete",
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "indexed source inventory shard",
                "finding_type": "drafting_note",
                "citation_ids": [],
                "confidence": 0.5,
                "reasoning_status": "uncertain",
            }
        ],
        "citations": [],
        "proposed_artifacts": [
            {
                "path": f"canonical/{task_id}.json",
                "artifact_type": "foundation_note",
                "stage": str(LegalStage.S0_SOURCE_INVENTORY),
                "title": f"Reduced {task_id}",
                "content": "{}",
            }
        ],
        "proposed_tasks": [
            {
                "task_id": "followup-source-shard",
                "title": "Follow up source shard",
                "task_type": "source_inventory",
                "stage": str(LegalStage.S0_SOURCE_INVENTORY),
                "matter_scope": "atticus",
                "instructions": "Follow up the source inventory shard.",
                "provider_policy": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.0,
                },
                "expected_value": 5.0,
            }
        ],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
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


def test_free_loop_once_skips_high_stage_auto_reduction(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="draft-auto-reduce",
                title="Draft auto reduce",
                task_type="draft_preparation",
                matter_scope="alpha",
                stage=LegalStage.S8_DRAFT_PREPARATION,
            ),
        )
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="draft-auto-reduce",
            lease_id=None,
            worker_id="worker-01",
            output_type="worker_result_packet",
            payload=_packet("draft-auto-reduce"),
        )
        _ = conn.execute("UPDATE tasks SET status = ? WHERE task_id = 'draft-auto-reduce'", (TaskStatus.REDUCER_PENDING,))

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False)

        candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'draft-auto-reduce'").fetchone())
        attention = cast(Mapping[str, object], conn.execute("SELECT matter_scope, target_type, target_id, reason FROM human_attention WHERE target_id = ?", (candidate_id,)).fetchone())

    assert result["reduced_candidates"] == []
    assert result["skipped_reductions"] == [
        {
            "candidate_id": candidate_id,
            "task_id": "draft-auto-reduce",
            "reason": "free loop auto-reduction is disabled for high-risk legal stage S8; manual reducer review required",
        }
    ]
    assert candidate["status"] == "candidate"
    assert task["status"] == str(TaskStatus.REDUCER_PENDING)
    assert attention["matter_scope"] == "alpha"
    assert attention["target_type"] == "candidate"
    assert "manual reducer review required" in str(attention["reason"])


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
    assert "ATTICUS_ENABLE_LIVE_CODEX" in str(task["blocked_reasons_json"])
    assert active_leases == 0
    assert failed_leases == 1
    assert provider_runs == 0
    assert candidates == 0


def test_free_loop_once_closes_active_lease_if_worker_crashes_before_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = init_db(tmp_path)
    captured: dict[str, object] = {}

    def fake_execute_codex_work_order(
        conn: sqlite3.Connection,
        *,
        task_id: str,
        lease_id: str,
        worker_id: str,
        output_dir: Path,
        env: Mapping[str, str] | None,
        allow_live: bool,
        timeout_seconds: float,
        reasoning_effort: str,
    ) -> None:
        del conn, output_dir, env, allow_live
        captured.update(
            {
                "task_id": task_id,
                "lease_id": lease_id,
                "worker_id": worker_id,
                "timeout_seconds": timeout_seconds,
                "reasoning_effort": reasoning_effort,
            }
        )
        with repo.db_connection(db_path, read_only=True) as observer:
            visible_lease = observer.execute(
                "SELECT status FROM leases WHERE lease_id = ? AND task_id = ?",
                (lease_id, task_id),
            ).fetchone()
        assert visible_lease is not None
        assert visible_lease["status"] == "active"
        raise RuntimeError("simulated worker crash before cleanup")

    monkeypatch.setattr(free_loop_module, "execute_codex_work_order", fake_execute_codex_work_order)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-crash",
                title="Codex crash",
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

        result = run_free_loop_once(
            conn,
            output_dir=tmp_path / "out",
            capacity=1,
            execute_workers=True,
            runtime="codex",
            env={"ATTICUS_ENABLE_LIVE_CODEX": "1"},
            allow_live=True,
            codex_timeout_seconds=12.5,
            codex_reasoning_effort="medium",
        )

        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'codex-crash'").fetchone())
        active_leases = _scalar_int(conn, "SELECT COUNT(*) FROM leases WHERE status = 'active'")
        failed_leases = _scalar_int(conn, "SELECT COUNT(*) FROM leases WHERE status = 'failed'")
        orchestrator_events = _scalar_int(
            conn,
            "SELECT COUNT(*) FROM orchestrator_events WHERE event_type = 'orchestrator.worker_failed' AND matter_scope = 'atticus'",
        )

    assert result["worker_errors"] == [{"task_id": "codex-crash", "error": "simulated worker crash before cleanup"}]
    assert captured["timeout_seconds"] == 12.5
    assert captured["reasoning_effort"] == "medium"
    assert task["status"] == str(TaskStatus.BLOCKED)
    assert "simulated worker crash before cleanup" in str(task["blocked_reasons_json"])
    assert active_leases == 0
    assert failed_leases == 1
    assert orchestrator_events == 1


def test_run_free_loop_cli_returns_nonzero_on_worker_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="codex-no-live",
                title="Codex no live",
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

    code = cli_main(
        [
            "run-free-loop",
            "--db",
            str(db_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--capacity",
            "1",
            "--max-ticks",
            "1",
            "--runtime",
            "codex",
        ]
    )
    stdout = capsys.readouterr().out
    payload_raw = json.loads(stdout)
    assert isinstance(payload_raw, Mapping)
    payload = cast(Mapping[str, object], payload_raw)
    ticks = cast(list[Mapping[str, object]], payload["ticks"])
    worker_errors = cast(list[Mapping[str, object]], ticks[0]["worker_errors"])

    assert code == 2
    assert payload["ok"] is False
    assert ticks[0]["ok"] is False
    assert worker_errors[0]["task_id"] == "codex-no-live"


def test_run_free_loop_cli_openrouter_without_live_gate_does_not_dispatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="openrouter-no-live",
                title="OpenRouter no live",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.0,
                },
            ),
        )

    code = cli_main(
        [
            "run-free-loop",
            "--db",
            str(db_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--capacity",
            "1",
            "--max-ticks",
            "1",
            "--runtime",
            "openrouter",
        ]
    )
    stdout = capsys.readouterr().out
    payload_raw = json.loads(stdout)
    assert isinstance(payload_raw, Mapping)
    payload = cast(Mapping[str, object], payload_raw)
    ticks = cast(list[Mapping[str, object]], payload["ticks"])
    worker_errors = cast(list[Mapping[str, object]], ticks[0]["worker_errors"])

    with repo.db_connection(db_path) as conn:
        provider_runs = _scalar_int(conn, "SELECT COUNT(*) FROM provider_runs")
        candidates = _scalar_int(conn, "SELECT COUNT(*) FROM candidate_outputs")
        active_leases = _scalar_int(conn, "SELECT COUNT(*) FROM leases WHERE status = 'active'")
        failed_leases = _scalar_int(conn, "SELECT COUNT(*) FROM leases WHERE status = 'failed'")
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'openrouter-no-live'").fetchone())

    assert code == 2
    assert payload["ok"] is False
    assert worker_errors[0]["task_id"] == "openrouter-no-live"
    assert "OpenRouter preflight failed before leasing" in str(worker_errors[0]["error"])
    assert provider_runs == 0
    assert candidates == 0
    assert active_leases == 0
    assert failed_leases == 0
    assert task["status"] == str(TaskStatus.QUEUED)
    assert task["blocked_reasons_json"] == "[]"


def test_run_free_loop_cli_self_migrates_stale_v5_db_before_failure_logging(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="stale-v5-openrouter-no-live",
                title="Stale v5 OpenRouter no live",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.0,
                },
            ),
        )

    raw = sqlite3.connect(db_path)
    try:
        _ = raw.execute("DROP TABLE error_logs")
        _ = raw.execute("DROP TABLE maintenance_reports")
        _ = raw.execute("DROP TABLE maintenance_runs")
        _ = raw.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
        raw.commit()
    finally:
        raw.close()

    code = cli_main(
        [
            "run-free-loop",
            "--db",
            str(db_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--capacity",
            "1",
            "--max-ticks",
            "1",
            "--runtime",
            "openrouter",
        ]
    )
    stdout = capsys.readouterr().out
    payload_raw = json.loads(stdout)
    assert isinstance(payload_raw, Mapping)
    payload = cast(Mapping[str, object], payload_raw)

    with repo.db_connection(db_path, read_only=True) as conn:
        schema_version = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        error_logs = _scalar_int(conn, "SELECT COUNT(*) FROM error_logs WHERE target_id = 'stale-v5-openrouter-no-live'")
        maintenance_tables = _scalar_int(
            conn,
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'table' AND name IN ('maintenance_runs', 'maintenance_reports')
            """,
        )

    assert code == 2
    assert payload["ok"] is False
    assert schema_version is not None and schema_version["value"] == "6"
    assert error_logs >= 1
    assert maintenance_tables == 2


def test_worker_failure_signal_self_migrates_already_open_stale_connection(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="stale-direct-failure",
                title="Stale direct failure",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
            ),
        )

    raw = sqlite3.connect(db_path)
    try:
        _ = raw.execute("DROP TABLE error_logs")
        _ = raw.execute("DROP TABLE orchestrator_events")
        _ = raw.execute("DROP TABLE matter_orchestrators")
        _ = raw.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
        raw.commit()
    finally:
        raw.close()

    conn = repo.connect(db_path)
    try:
        event_id = repo.record_orchestrator_worker_failure(
            conn,
            task_id="stale-direct-failure",
            failure_reason="simulated stale direct connection failure",
            matter_scope="atticus",
        )
        conn.commit()
        schema_version = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        error_logs = _scalar_int(conn, "SELECT COUNT(*) FROM error_logs WHERE target_id = 'stale-direct-failure'")
        orchestrator_events_row = conn.execute(
            "SELECT COUNT(*) AS n FROM orchestrator_events WHERE orchestrator_event_id = ?",
            (event_id,),
        ).fetchone()
        assert orchestrator_events_row is not None
        orchestrator_events = int(str(orchestrator_events_row["n"]))
    finally:
        conn.close()

    assert event_id.startswith("orchevt-")
    assert schema_version is not None and schema_version["value"] == "6"
    assert error_logs == 1
    assert orchestrator_events == 1
