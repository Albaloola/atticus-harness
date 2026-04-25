from __future__ import annotations

import sqlite3

import pytest

from atticus.adapters.base import AdapterBlocked
from atticus.adapters.openclaw import OpenClawAdapter
from atticus.cli import main as cli_main
from atticus.context.packs import build_context_pack
from atticus.core.policies import LegalStage, TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.graph.certifications import certify_subject
from atticus.migration.import_old_run import import_candidates
from atticus.providers.budget import BudgetExceeded, require_budget
from atticus.providers.policy import ProviderActual, ProviderRequest, record_provider_policy_decision
from atticus.reducer.reducer import reduce_candidate
from atticus.scheduler.lease import acquire_lease
from atticus.scheduler.planner import select_runnable_tasks
from atticus.validation.canonical_write_guard import CanonicalWriteDenied
from atticus.validation.gates import run_validation
from atticus.workers.outputs import record_worker_result


def init_db(tmp_path):
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def valid_packet(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "summary": "candidate summary",
        "findings": [{"text": "finding", "citation_ids": []}],
        "citations": [],
        "proposed_artifacts": [{"path": f"canonical/{task_id}.json", "artifact_type": "evidence_registry"}],
    }


def test_provider_mismatch_is_recorded_and_blocked(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        decision = record_provider_policy_decision(
            conn,
            requested=ProviderRequest("openrouter", "deepseek/deepseek-v4-pro", allow_fallback=False),
            actual=ProviderActual("openrouter", "deepseek/deepseek-v4-flash"),
            task_id="task-provider",
        )
        row = conn.execute("SELECT * FROM provider_runs").fetchone()
        attention = conn.execute("SELECT reason FROM human_attention").fetchone()

    assert not decision.allowed
    assert decision.result == "failed_closed"
    assert row["requested_model"] == "deepseek/deepseek-v4-pro"
    assert row["actual_model"] == "deepseek/deepseek-v4-flash"
    assert "fallback was not allowed" in attention["reason"]


def test_stage_foundation_gates_block_downstream_legacy_tasks(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="draft-too-early",
                title="Draft too early",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                status=TaskStatus.QUEUED,
            ),
        )
        runnable = select_runnable_tasks(conn, capacity=3)
        row = conn.execute("SELECT blocked_reasons_json FROM tasks WHERE task_id = 'draft-too-early'").fetchone()

    assert runnable == []
    assert "missing certification" in row["blocked_reasons_json"]


def test_budget_gate_blocks_over_budget_tasks(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_budget(conn, scope_type="stage", scope_id="S0", limit_usd=0.01)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="expensive",
                title="Expensive indexing",
                task_type="index",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                status=TaskStatus.QUEUED,
                provider_policy={"estimated_cost_usd": 0.50},
            ),
        )
        runnable = select_runnable_tasks(conn, capacity=1)
        row = conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'expensive'").fetchone()
        with pytest.raises(BudgetExceeded):
            require_budget(conn, scope_type="stage", scope_id="S0", requested_usd=0.50)

    assert runnable == []
    assert row["status"] == "blocked"
    assert "budget blocked" in row["blocked_reasons_json"]


def test_context_packs_are_deterministic_and_fingerprinted(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/a.pdf", sha256="a" * 64)
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/a.txt",
            artifact_type="extraction_record",
            content="same content",
            source_ids=[source_id],
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="ctx-task",
                title="Context task",
                task_type="extract",
                source_dependencies=[source_id],
                artifact_dependencies=[artifact_id],
            ),
        )
        first = build_context_pack(conn, task_id="ctx-task")
        second = build_context_pack(conn, task_id="ctx-task")

    assert first.context_pack_id == second.context_pack_id
    assert first.fingerprint == second.fingerprint
    assert first.sections[0]["name"] == "stable_prefix"


def test_context_pack_rejects_oversized_budget_before_persisting(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/huge.txt",
            artifact_type="extraction_record",
            content="oversized " * 2000,
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="ctx-too-small",
                title="Context too small",
                task_type="extract",
                artifact_dependencies=[artifact_id],
            ),
        )
        with pytest.raises(ValueError, match="token budget"):
            build_context_pack(conn, task_id="ctx-too-small", token_budget=100)
        context_count = conn.execute("SELECT COUNT(*) AS n FROM context_packs").fetchone()["n"]

    assert context_count == 0


def test_citation_spans_require_known_records_and_claim_validation(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/evidence.pdf", sha256="b" * 64)
        claim_id = repo.add_claim(conn, claim_text="The record supports this fact.")
        failed = run_validation(conn, gate_name="claim_evidence_support", target_type="claim", target_id=claim_id)
        with pytest.raises(sqlite3.IntegrityError):
            repo.add_citation_span(conn, target_type="claim", target_id=claim_id, source_id="missing")
        repo.add_citation_span(conn, target_type="claim", target_id=claim_id, source_id=source_id)
        passed = run_validation(conn, gate_name="claim_evidence_support", target_type="claim", target_id=claim_id)

    assert not failed.passed
    assert passed.passed


def test_validation_failure_creates_human_attention(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        outcome = run_validation(conn, gate_name="source_inventory", target_type="matter", target_id="atticus")
        attention = conn.execute("SELECT target_type, target_id, reason FROM human_attention").fetchone()

    assert not outcome.passed
    assert attention["target_type"] == "matter"
    assert attention["target_id"] == "atticus"
    assert "validation failed" in attention["reason"]


def test_expired_worker_lease_quarantines_late_output(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="late", title="Late worker", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="late", worker_id="worker-1", seconds=-1)
        candidate_id = record_worker_result(
            conn,
            task_id="late",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=valid_packet("late"),
        )
        candidate = conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE task_id = 'late'").fetchone()

    assert candidate["status"] == "quarantined"
    assert "expired" in candidate["quarantined_reason"]
    assert task["status"] == "quarantined"


def test_reducer_writes_canonical_only_with_valid_lease_and_validations(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="reduce-me", title="Reduce me", task_type="extract"))
        worker_lease = acquire_lease(conn, task_id="reduce-me", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="reduce-me",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=valid_packet("reduce-me"),
        )
        reducer_lease = acquire_lease(conn, task_id="reduce-me", worker_id="reducer-1")
        with pytest.raises(CanonicalWriteDenied):
            reduce_candidate(
                conn,
                candidate_id=candidate_id,
                reducer_lease_id=reducer_lease,
                writer_role="worker",
                dry_run=False,
            )
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        artifact = conn.execute("SELECT trust_status, produced_by_task_id FROM artifacts WHERE artifact_id = ?", (result["artifact_id"],)).fetchone()

    assert result["artifact_id"].startswith("art-")
    assert artifact["trust_status"] == "validated"
    assert artifact["produced_by_task_id"] == "reduce-me"


def test_migration_imports_drafts_as_rough_notes_and_never_certifies(tmp_path):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "legacy"
    drafts = workspace / "case" / "drafts"
    drafts.mkdir(parents=True)
    (drafts / "appeal_draft.md").write_text("draft only", encoding="utf-8")

    with repo.db_connection(db_path) as conn:
        result = import_candidates(conn, workspace=workspace, dry_run=False)
        artifact = conn.execute("SELECT artifact_type, trust_status FROM artifacts").fetchone()
        cert_count = conn.execute("SELECT COUNT(*) AS n FROM certifications").fetchone()["n"]

    assert len(result.candidates) == 1
    assert artifact["artifact_type"] == "draft"
    assert artifact["trust_status"] == "rough_note"
    assert cert_count == 0


def test_openclaw_adapter_never_starts_accidentally():
    with pytest.raises(AdapterBlocked):
        OpenClawAdapter().launch()


def test_factory_cli_dry_runs_do_not_launch_or_mutate_execution_state(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="cli-task", title="CLI task", task_type="extract"))

    assert cli_main(["schedule", "--db", str(db_path), "--capacity", "1", "--dry-run"]) == 0
    assert cli_main(["work-order", "--db", str(db_path), "--task-id", "cli-task", "--dry-run"]) == 0

    with repo.db_connection(db_path) as conn:
        lease_count = conn.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]
        context_count = conn.execute("SELECT COUNT(*) AS n FROM context_packs").fetchone()["n"]

    assert lease_count == 0
    assert context_count == 0


def test_factory_cli_run_local_requires_write_and_then_records_candidate(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="cli-local", title="CLI local", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="cli-local", worker_id="atticus-local")

    assert cli_main([
        "run-local",
        "--db",
        str(db_path),
        "--task-id",
        "cli-local",
        "--lease-id",
        lease_id,
        "--output-dir",
        str(tmp_path / "out"),
    ]) == 0
    with repo.db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs").fetchone()["n"] == 0

    assert cli_main([
        "run-local",
        "--db",
        str(db_path),
        "--task-id",
        "cli-local",
        "--lease-id",
        lease_id,
        "--worker-id",
        "atticus-local",
        "--output-dir",
        str(tmp_path / "out"),
        "--write",
    ]) == 0
    with repo.db_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM candidate_outputs WHERE status = 'candidate'").fetchone()["n"] == 1
