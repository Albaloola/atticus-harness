from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import json
import sqlite3
from typing import cast

from atticus.cli import main as cli_main
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.scheduler.lease import acquire_lease
from atticus.verifier import verify_candidate
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _packet(task_id: str, *, content: str = "Supported draft content.") -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "candidate summary",
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "The packet is a draft note, not proof.",
                "finding_type": "drafting_note",
                "citation_ids": [],
                "confidence": 0.5,
                "reasoning_status": "uncertain",
            }
        ],
        "citations": [],
        "proposed_artifacts": [
            {
                "path": f"candidate/{task_id}.md",
                "artifact_type": "draft",
                "stage": "S8",
                "title": "Draft",
                "content": content,
            }
        ],
        "proposed_tasks": [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


def _candidate(conn: sqlite3.Connection, task_id: str, packet: dict[str, object]) -> str:
    repo.add_task(conn, TaskSpec(task_id=task_id, title=task_id, task_type="draft"))
    lease_id = acquire_lease(conn, task_id=task_id, worker_id="worker-1")
    return record_worker_result(conn, task_id=task_id, lease_id=lease_id, worker_id="worker-1", payload=packet)


def test_verifier_fails_external_action_language_in_draft_artifacts(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _candidate(conn, "unsafe-draft", _packet("unsafe-draft", content="I have emailed the court and served the letter."))
        result = verify_candidate(conn, candidate_id=candidate_id, verifier_type="hostile_opponent_review")

    assert result.passed is False
    assert "external_action_language" in result.defect_types
    assert result.validation_result_id is None


def test_verifier_passes_uncertain_draft_note_without_writing_by_default(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _candidate(conn, "careful-draft", _packet("careful-draft"))
        result = verify_candidate(conn, candidate_id=candidate_id, verifier_type="citation_audit")
        row = conn.execute("SELECT COUNT(*) AS n FROM validation_results").fetchone()
        assert row is not None
        validations = row["n"]

    assert result.passed is True
    assert result.validation_result_id is None
    assert validations == 0


def test_verifier_cli_writes_validation_only_with_write(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _candidate(conn, "cli-draft", _packet("cli-draft"))

    assert cli_main(["verifier", "run", "--db", str(db_path), "--candidate-id", candidate_id, "--type", "citation_audit", "--json"]) == 0
    dry = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    assert dry["dry_run"] is True
    with repo.db_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM validation_results").fetchone()
        assert row is not None
        assert row["n"] == 0

    assert cli_main(["verifier", "run", "--db", str(db_path), "--candidate-id", candidate_id, "--type", "citation_audit", "--write", "--json"]) == 0
    written = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    assert written["dry_run"] is False
    assert written["validation_result_id"]
