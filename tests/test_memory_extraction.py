from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
import json

from atticus.cli import main as cli_main
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _packet(task_id: str, source_id: str) -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "Reduced source-supported finding.",
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "The rent account needs a ledger check before any confident payment demand.",
                "finding_type": "fact",
                "citation_ids": ["cite-1"],
                "confidence": 0.82,
                "reasoning_status": "supported",
            },
            {
                "finding_id": "finding-2",
                "text": "There is a contradiction about the payment cure date.",
                "finding_type": "contradiction",
                "citation_ids": ["cite-1"],
                "confidence": 0.7,
                "reasoning_status": "supported",
            },
            {
                "finding_id": "finding-3",
                "text": "A polished sentence should not become memory.",
                "finding_type": "drafting_note",
                "citation_ids": [],
                "confidence": 0.5,
                "reasoning_status": "uncertain",
            },
        ],
        "citations": [
            {
                "citation_id": "cite-1",
                "target_type": "source",
                "target_id": source_id,
                "locator": "p.2",
                "quoted_text_hash": "a" * 64,
            }
        ],
        "proposed_artifacts": [],
        "proposed_tasks": [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


def _accepted_candidate(conn, *, matter_scope: str = "alpha") -> str:
    source_id = repo.add_source(conn, matter_scope=matter_scope, path=f"/{matter_scope}/source.pdf", sha256="a" * 64)
    repo.add_task(
        conn,
        TaskSpec(
            task_id=f"{matter_scope}-task",
            title="Reduced task",
            task_type="evidence_review",
            matter_scope=matter_scope,
            source_dependencies=[source_id],
        ),
    )
    candidate_id = repo.record_candidate_output(
        conn,
        task_id=f"{matter_scope}-task",
        lease_id=None,
        worker_id="worker-1",
        output_type="worker_result_packet.v2",
        payload=_packet(f"{matter_scope}-task", source_id),
        status="reduced",
    )
    _ = repo.record_reducer_packet(conn, candidate_id=candidate_id, decision="accepted")
    return candidate_id


def test_memory_extract_candidates_requires_reduced_accepted_candidate(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/raw.pdf", sha256="b" * 64)
        repo.add_task(conn, TaskSpec(task_id="raw-task", title="Raw", task_type="extract", matter_scope="alpha", source_dependencies=[source_id]))
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="raw-task",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet.v2",
            payload=_packet("raw-task", source_id),
            status="candidate",
        )

    assert cli_main(["memory", "extract-candidates", "--db", str(db_path), "--matter", "alpha", "--candidate-id", candidate_id]) == 2
    error = cast(Mapping[str, object], json.loads(capsys.readouterr().err))
    assert "accepted reducer packet" in str(error["error"])


def test_memory_extract_candidates_dry_run_and_write_candidate_memory(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _accepted_candidate(conn)

    assert cli_main(["memory", "extract-candidates", "--db", str(db_path), "--matter", "alpha", "--candidate-id", candidate_id]) == 0
    dry = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    with repo.db_connection(db_path) as conn:
        count_row = conn.execute("SELECT COUNT(*) AS n FROM legal_memories").fetchone()
        assert count_row is not None
        assert count_row["n"] == 0

    assert cli_main(["memory", "extract-candidates", "--db", str(db_path), "--matter", "alpha", "--candidate-id", candidate_id, "--write"]) == 0
    written = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    with repo.db_connection(db_path) as conn:
        memories = conn.execute("SELECT type, status, content, source_refs_json FROM legal_memories ORDER BY type").fetchall()

    assert dry["dry_run"] is True
    assert len(cast(list[object], dry["memory_candidates"])) == 2
    assert written["dry_run"] is False
    assert len(cast(list[object], written["created_memory_ids"])) == 2
    assert {row["type"] for row in memories} == {"contradiction", "evidence_fact"}
    assert all(row["status"] == "candidate" for row in memories)
    assert all(json.loads(str(row["source_refs_json"])) for row in memories)


def test_memory_consolidate_dry_run_and_write_review_task(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="c" * 64)
        _ = repo.add_legal_memory(
            conn,
            matter_scope="alpha",
            memory_type="contradiction",
            name="Payment date conflict",
            description="Open contradiction",
            content="There are competing cure dates.",
            status="active",
            confidence=0.6,
            source_refs=[{"target_type": "source", "target_id": source_id, "locator": "p.1"}],
        )
        _ = repo.add_legal_memory(
            conn,
            matter_scope="alpha",
            memory_type="evidence_fact",
            name="Stale fact",
            description="Needs review",
            content="Old posture.",
            status="active",
            confidence=0.6,
            stale=True,
            staleness_trigger="new ledger received",
            source_refs=[{"target_type": "source", "target_id": source_id, "locator": "p.1"}],
        )

    assert cli_main(["memory", "consolidate", "--db", str(db_path), "--matter", "alpha"]) == 0
    dry = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    with repo.db_connection(db_path) as conn:
        task_count_row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE matter_scope = 'alpha'").fetchone()
        assert task_count_row is not None
        assert task_count_row["n"] == 0

    assert cli_main(["memory", "consolidate", "--db", str(db_path), "--matter", "alpha", "--write"]) == 0
    written = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    with repo.db_connection(db_path) as conn:
        tasks = conn.execute("SELECT task_type, instructions FROM tasks WHERE matter_scope = 'alpha'").fetchall()

    assert dry["dry_run"] is True
    assert cast(Mapping[str, object], dry["orient"])["memory_count"] == 2
    assert cast(list[object], dry["proposed_tasks"])
    assert written["dry_run"] is False
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "memory_consolidation_review"
    assert "candidate, not canonical" in str(tasks[0]["instructions"])
