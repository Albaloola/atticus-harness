from __future__ import annotations

from pathlib import Path
import hashlib
import json
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.context.packs import build_context_pack
from atticus.db import repo
from atticus.retrieval.work_reuse import find_reusable_context_packs
from atticus.work_runs import record_work_step, start_work_run, summarize_reusable_work


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


def test_context_pack_reuse_fails_when_source_hash_changes(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="ALPHA-SRC-0003", matter_scope="alpha", path="/alpha/source-3.pdf", sha256="c" * 64)
        artifact_id = repo.add_artifact(
            conn,
            artifact_id="alpha-extracted-source-3",
            matter_scope="alpha",
            path="/alpha/source-3.txt",
            artifact_type="extracted_text",
            content="rent difficulty appears in the extracted source material",
            sha256="d" * 64,
            source_ids=(source_id,),
        )
        _ = conn.execute(
            """
            INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
              coverage_status, confidence, metadata_json, created_at)
            VALUES ('extract-source-3', ?, ?, 'pdf_text', 'complete', 0.9, ?, ?)
            """,
            (source_id, artifact_id, json.dumps({"text_sha256": "d" * 64}), utc_now()),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="reuse-context-pack",
                title="Reuse context pack",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        pack = build_context_pack(conn, task_id="reuse-context-pack", persist=True)
        before = find_reusable_context_packs(conn, "alpha", "rent difficulty")
        _ = conn.execute("UPDATE sources SET sha256 = ? WHERE source_id = ?", ("e" * 64, source_id))
        after = find_reusable_context_packs(conn, "alpha", "rent difficulty")
        link = conn.execute("SELECT * FROM context_pack_sources WHERE context_pack_id = ?", (pack.context_pack_id,)).fetchone()

    assert before
    assert after == []
    assert link is not None
    assert link["source_sha256"] == "c" * 64
    assert link["extraction_artifact_id"] == artifact_id
    assert link["extraction_text_sha256"] == "d" * 64


def test_context_pack_reuse_fails_when_extraction_artifact_changes(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="ALPHA-SRC-0004", matter_scope="alpha", path="/alpha/source-4.pdf", sha256="f" * 64)
        artifact_id = repo.add_artifact(
            conn,
            artifact_id="alpha-extracted-source-4",
            matter_scope="alpha",
            path="/alpha/source-4.txt",
            artifact_type="extracted_text",
            content="the decisive accommodation clause sits here",
            sha256="1" * 64,
            source_ids=(source_id,),
        )
        _ = conn.execute(
            """
            INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
              coverage_status, confidence, metadata_json, created_at)
            VALUES ('extract-source-4', ?, ?, 'pdf_text', 'complete', 0.9, ?, ?)
            """,
            (source_id, artifact_id, json.dumps({"text_sha256": "1" * 64}), utc_now()),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="reuse-context-pack-extraction",
                title="Reuse context pack extraction",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        _ = build_context_pack(conn, task_id="reuse-context-pack-extraction", persist=True)
        before = find_reusable_context_packs(conn, "alpha", "accommodation clause")
        _ = conn.execute("UPDATE artifacts SET sha256 = ? WHERE artifact_id = ?", ("2" * 64, artifact_id))
        after = find_reusable_context_packs(conn, "alpha", "accommodation clause")

    assert before
    assert after == []


def test_work_step_source_links_are_recorded_from_context_pack(tmp_path: Path):
    db_path = _init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="ALPHA-SRC-0005", matter_scope="alpha", path="/alpha/source-5.pdf", sha256="3" * 64)
        artifact_id = repo.add_artifact(
            conn,
            artifact_id="alpha-extracted-source-5",
            matter_scope="alpha",
            path="/alpha/source-5.txt",
            artifact_type="extracted_text",
            content="hardship evidence and support request",
            sha256="4" * 64,
            source_ids=(source_id,),
        )
        _ = conn.execute(
            """
            INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
              coverage_status, confidence, metadata_json, created_at)
            VALUES ('extract-source-5', ?, ?, 'pdf_text', 'complete', 0.9, ?, ?)
            """,
            (source_id, artifact_id, json.dumps({"text_sha256": "4" * 64}), utc_now()),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="reuse-work-step-links",
                title="Reuse work step links",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        pack = build_context_pack(conn, task_id="reuse-work-step-links", persist=True)
        _ = conn.execute("UPDATE tasks SET status = 'complete' WHERE task_id = 'reuse-work-step-links'")
        work_run = start_work_run(conn, matter_scope="alpha", goal="hardship evidence")
        step = record_work_step(
            conn,
            work_run_id=str(work_run["work_run_id"]),
            step_type="context_review",
            status="complete",
            task_id="reuse-work-step-links",
            context_pack_id=pack.context_pack_id,
        )
        link = conn.execute("SELECT * FROM work_step_source_links WHERE work_run_step_id = ?", (step["work_run_step_id"],)).fetchone()
        before = summarize_reusable_work(conn, "alpha", "hardship evidence")
        _ = conn.execute("UPDATE sources SET stale = 1 WHERE source_id = ?", (source_id,))
        after = summarize_reusable_work(conn, "alpha", "hardship evidence")

    assert link is not None
    assert link["source_id"] == source_id
    assert link["source_sha256"] == "3" * 64
    assert link["extraction_artifact_id"] == artifact_id
    assert link["extraction_text_sha256"] == "4" * 64
    assert before["reusable_steps"]
    assert after["reusable_steps"] == []
    excluded = cast(list[dict[str, object]], after["excluded_steps"])
    assert "source stale" in str(excluded[0]["reason"])
