from __future__ import annotations

from pathlib import Path
import json

from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.reducer.review_queue import enqueue_reducer_review, get_reducer_review_by_candidate, reject_reducer_review
from atticus.scheduler.free_loop import run_free_loop_once
from atticus.status.completion import next_resume_action


MATTER = "napier-accommodation-arrears"


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _add_high_risk_candidate(conn, *, task_id: str = "citation-repair", candidate_id: str = "cand-citation-repair") -> str:
    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            matter_scope=MATTER,
            title="Repair citations",
            task_type="citation_repair",
            stage=LegalStage.S7_HOSTILE_REVIEW,
            status=TaskStatus.REDUCER_PENDING,
        ),
    )
    return repo.record_candidate_output(
        conn,
        candidate_id=candidate_id,
        task_id=task_id,
        lease_id=None,
        worker_id="test-worker",
        output_type="result_packet",
        payload={
            "summary": "candidate needs manual reducer review",
            "findings": [],
            "citations": [],
            "risk_flags": [],
            "redaction_flags": [],
        },
        status="candidate",
    )


def test_high_risk_auto_reduce_skip_creates_reducer_review_queue_item(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _add_high_risk_candidate(conn)

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", execute_workers=False, matter_scope=MATTER)
        item = get_reducer_review_by_candidate(conn, candidate_id)

    assert result["skipped_reductions"]
    assert item is not None
    assert item.status == "open"
    assert item.task_type == "citation_repair"
    assert item.priority == 10


def test_reducer_review_queue_dedupes_candidate(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _add_high_risk_candidate(conn)

        first = enqueue_reducer_review(conn, candidate_id=candidate_id, reason="manual review required")
        second = enqueue_reducer_review(conn, candidate_id=candidate_id, reason="manual review still required")
        count = conn.execute("SELECT COUNT(*) AS n FROM reducer_review_queue").fetchone()

    assert first.reducer_review_id == second.reducer_review_id
    assert int(count["n"]) == 1
    assert second.reason == "manual review still required"


def test_matter_health_next_action_uses_reducer_review_queue(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _add_high_risk_candidate(conn)
        _ = enqueue_reducer_review(conn, candidate_id=candidate_id, reason="manual reducer review required")

        action = next_resume_action(conn, MATTER)

    assert action["type"] == "manual_reducer_review"
    assert action["candidate_id"] == candidate_id
    assert action["resume_command"].endswith(f"reducer-review show --db DB --candidate-id {candidate_id} --json")


def test_reducer_review_reject_creates_repair_plan(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _add_high_risk_candidate(conn)
        _ = enqueue_reducer_review(conn, candidate_id=candidate_id, reason="manual reducer review required")

        result = reject_reducer_review(conn, candidate_id=candidate_id, reason="unsupported citations", write=True)
        candidate = conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone()

    assert result["repair_plan"]["blocker_type"] == "generic_blocker"
    assert result["review"]["status"] == "rejected"
    assert candidate["status"] == "quarantined"
    assert candidate["quarantined_reason"] == "unsupported citations"


def test_reducer_review_cli_lists_and_shows_items(tmp_path: Path, capsys) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _add_high_risk_candidate(conn)
        _ = enqueue_reducer_review(conn, candidate_id=candidate_id, reason="manual reducer review required")

    list_code = cli_main(["reducer-review", "list", "--db", str(db_path), "--matter", MATTER, "--json"])
    list_payload = json.loads(capsys.readouterr().out)
    show_code = cli_main(["reducer-review", "show", "--db", str(db_path), "--candidate-id", candidate_id, "--json"])
    show_payload = json.loads(capsys.readouterr().out)

    assert list_code == 0
    assert show_code == 0
    assert list_payload["reviews"][0]["candidate_id"] == candidate_id
    assert show_payload["review"]["candidate_id"] == candidate_id
    assert show_payload["review"]["could_unblock"] == "citation_audit or final_quality_gate"


def test_reducer_review_list_write_backfills_existing_reducer_pending_candidates(tmp_path: Path, capsys) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _add_high_risk_candidate(conn)

    exit_code = cli_main(["reducer-review", "list", "--db", str(db_path), "--matter", MATTER, "--write", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["reviews"][0]["candidate_id"] == candidate_id
    assert payload["reviews"][0]["status"] == "open"
