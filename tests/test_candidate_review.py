from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sqlite3
from typing import cast

from atticus.cli import main as cli_main
from atticus.core.policies import TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.scheduler.lease import acquire_lease
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(str(row[0]))


def _candidate_packet(task_id: str) -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "valid but operator-rejected",
        "findings": [
            {
                "finding_id": "review-finding-1",
                "text": "not enough source context",
                "finding_type": "drafting_note",
                "citation_ids": [],
                "confidence": 0.25,
                "reasoning_status": "uncertain",
            }
        ],
        "citations": [],
        "proposed_artifacts": [
            {
                "path": f"candidate/{task_id}.json",
                "artifact_type": "review_note",
                "stage": "S0",
                "title": "Review note",
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


def test_reject_candidate_cli_is_dry_run_by_default_and_requeues_task(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="needs-review", title="Needs review", task_type="source_inventory"))
        lease_id = acquire_lease(conn, task_id="needs-review", worker_id="worker-01")
        candidate_id = record_worker_result(
            conn,
            task_id="needs-review",
            lease_id=lease_id,
            worker_id="worker-01",
            payload=_candidate_packet("needs-review"),
        )

    dry_code = cli_main(
        [
            "reject-candidate",
            "--db",
            str(db_path),
            "--candidate-id",
            candidate_id,
            "--reason",
            "candidate was generated without the required source dependencies",
        ]
    )
    assert dry_code == 0
    with repo.db_connection(db_path) as conn:
        dry_candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        dry_task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'needs-review'").fetchone())
    assert dry_candidate["status"] == "candidate"
    assert dry_task["status"] == str(TaskStatus.REDUCER_PENDING)

    write_code = cli_main(
        [
            "reject-candidate",
            "--db",
            str(db_path),
            "--candidate-id",
            candidate_id,
            "--reason",
            "candidate was generated without the required source dependencies",
            "--write",
        ]
    )
    assert write_code == 0
    with repo.db_connection(db_path) as conn:
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'needs-review'").fetchone())
        active_leases = _count(conn, "SELECT COUNT(*) FROM leases WHERE status = 'active'")
        attention = cast(Mapping[str, object], conn.execute("SELECT reason FROM human_attention ORDER BY attention_id DESC LIMIT 1").fetchone())

    assert candidate["status"] == "quarantined"
    assert "required source dependencies" in str(candidate["quarantined_reason"])
    assert task["status"] == str(TaskStatus.QUEUED)
    assert active_leases == 0
    assert "candidate rejected by operator" in str(attention["reason"])
