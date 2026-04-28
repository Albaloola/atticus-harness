from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sqlite3
from typing import cast

import pytest

from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.scheduler.lease import acquire_lease
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION, ResultPacketError, parse_result


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(str(row[0]))


def v2_packet(task_id: str, *, citation_target_id: str | None = None) -> dict[str, object]:
    citation_id = "cite-source-1"
    citations = []
    citation_ids: list[str] = []
    if citation_target_id:
        citation_ids = [citation_id]
        citations.append(
            {
                "citation_id": citation_id,
                "target_type": "source",
                "target_id": citation_target_id,
                "locator": "p.1",
                "quoted_text_hash": "a" * 64,
            }
        )
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "Evidence-bound candidate summary.",
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "The candidate states only what the cited source supports.",
                "finding_type": "fact" if citation_target_id else "drafting_note",
                "citation_ids": citation_ids,
                "confidence": 0.85 if citation_target_id else 0.5,
                "reasoning_status": "supported" if citation_target_id else "uncertain",
            }
        ],
        "citations": citations,
        "proposed_artifacts": [
            {
                "path": f"candidate/{task_id}.json",
                "artifact_type": "evidence_registry",
                "stage": "S0",
                "title": "Candidate evidence registry",
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


def test_result_packet_v2_rejects_missing_version_and_extra_fields():
    packet = v2_packet("packet-task")
    missing_version = dict(packet)
    del missing_version["schema_version"]

    with pytest.raises(ResultPacketError, match="schema_version"):
        _ = parse_result(missing_version)

    extra = dict(packet)
    extra["model_notes"] = "uncontrolled"
    with pytest.raises(ResultPacketError, match="unexpected worker result keys"):
        _ = parse_result(extra, strict=True)


def test_result_packet_v2_rejects_finding_citation_id_not_defined():
    packet = v2_packet("packet-task")
    findings = cast(list[dict[str, object]], packet["findings"])
    findings[0]["citation_ids"] = ["missing-citation"]

    with pytest.raises(ResultPacketError, match="undefined citation ids"):
        _ = parse_result(packet)


def test_record_worker_result_rejects_citation_outside_task_context(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(conn, TaskSpec(task_id="alpha-task", title="Alpha task", task_type="extract", matter_scope="alpha"))
        lease_id = acquire_lease(conn, task_id="alpha-task", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="alpha-task",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=v2_packet("alpha-task", citation_target_id=source_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())

    assert candidate["status"] == "quarantined"
    assert "outside work order context" in str(candidate["quarantined_reason"])
    assert lease["status"] == "failed"


def test_record_worker_result_accepts_v2_citations_inside_task_context(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="alpha-task",
                title="Alpha task",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        lease_id = acquire_lease(conn, task_id="alpha-task", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="alpha-task",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=v2_packet("alpha-task", citation_target_id=source_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'alpha-task'").fetchone())

    assert candidate["status"] == "candidate"
    assert task["status"] == "reducer_pending"


def test_external_action_request_is_blocked_and_quarantined(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="unsafe-action", title="Unsafe action", task_type="draft"))
        lease_id = acquire_lease(conn, task_id="unsafe-action", worker_id="worker-1")
        packet = v2_packet("unsafe-action")
        packet["external_action_requests"] = [{"action_type": "email", "recipient": "other@example.test"}]
        candidate_id = record_worker_result(
            conn,
            task_id="unsafe-action",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=packet,
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        blocks = _count(conn, "SELECT COUNT(*) FROM external_action_blocks")

    assert candidate["status"] == "quarantined"
    assert "external action requests are blocked" in str(candidate["quarantined_reason"])
    assert blocks == 1
