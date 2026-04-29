from __future__ import annotations

from pathlib import Path
import hashlib
import json
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.work_runs import summarize_reusable_work


def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_reusable_work_excludes_stale_artifact(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="ALPHA-SRC-0001", matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        artifact_id = repo.add_artifact(
            conn,
            artifact_id="alpha-validated-artifact",
            matter_scope="alpha",
            path="/alpha/artifact.json",
            artifact_type="evidence_registry",
            trust_status=TrustStatus.VALIDATED,
            source_ids=(source_id,),
        )
        work_run_id = repo.start_work_run(conn, matter_scope="alpha", goal="reuse source")
        _ = repo.record_work_run_step(conn, work_run_id=work_run_id, step_type="evidence_registry", status="complete", artifact_id=artifact_id)
        before = summarize_reusable_work(conn, "alpha", "evidence")
        _ = conn.execute("UPDATE artifacts SET stale = 1 WHERE artifact_id = ?", (artifact_id,))
        after = summarize_reusable_work(conn, "alpha", "evidence")

    assert before["reusable_steps"]
    assert after["reusable_steps"] == []
    assert cast(list[dict[str, object]], after["excluded_steps"])[0]["reason"] == "artifact stale"


def test_reusable_work_excludes_candidate_only_output_for_trusted_answer(tmp_path: Path):
    db_path = _init_db(tmp_path)
    payload = {"summary": "candidate only"}
    now = utc_now()
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="candidate-task", title="Candidate", task_type="extract", matter_scope="alpha"))
        candidate_id = "candidate-only"
        _ = conn.execute(
            """
            INSERT INTO candidate_outputs(candidate_id, task_id, lease_id, worker_id, status, output_type, payload_json, payload_hash, created_at)
            VALUES (?, 'candidate-task', NULL, 'worker', 'candidate', 'worker_result_packet.v2', ?, ?, ?)
            """,
            (
                candidate_id,
                json.dumps(payload, sort_keys=True),
                hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest(),
                now,
            ),
        )
        work_run_id = repo.start_work_run(conn, matter_scope="alpha", goal="reuse candidate")
        _ = repo.record_work_run_step(conn, work_run_id=work_run_id, step_type="candidate", status="complete", candidate_id=candidate_id)
        summary = summarize_reusable_work(conn, "alpha", "candidate")

    assert summary["reusable_steps"] == []
    excluded = cast(list[dict[str, object]], summary["excluded_steps"])
    assert excluded
    assert excluded[0]["orientation_allowed"] is True
    assert "candidate-only output" in str(excluded[0]["reason"])


def test_reusable_work_excludes_artifact_when_source_dependency_goes_stale(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="ALPHA-SRC-0002", matter_scope="alpha", path="/alpha/source-2.pdf", sha256="b" * 64)
        artifact_id = repo.add_artifact(
            conn,
            artifact_id="alpha-source-linked-artifact",
            matter_scope="alpha",
            path="/alpha/source-linked.json",
            artifact_type="evidence_registry",
            trust_status=TrustStatus.CERTIFIED,
            source_ids=(source_id,),
        )
        work_run_id = repo.start_work_run(conn, matter_scope="alpha", goal="reuse source linked")
        _ = repo.record_work_run_step(conn, work_run_id=work_run_id, step_type="evidence_registry", status="complete", artifact_id=artifact_id)
        _ = conn.execute("UPDATE sources SET stale = 1 WHERE source_id = ?", (source_id,))
        summary = summarize_reusable_work(conn, "alpha", "source linked")

    assert summary["reusable_steps"] == []
    excluded = cast(list[dict[str, object]], summary["excluded_steps"])
    assert "artifact source dependency stale" in str(excluded[0]["reason"])
