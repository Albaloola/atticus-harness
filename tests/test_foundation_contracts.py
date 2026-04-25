from __future__ import annotations

import sqlite3

import pytest

from atticus.core.policies import LegalStage, TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.graph.certifications import CertificationBlocked, certify_subject
from atticus.graph.evidence import add_authority
from atticus.graph.staleness import update_source_hash_and_mark_dependents_stale
from atticus.migration.import_old_run import import_candidates
from atticus.providers.cost import estimate_cost_usd
from atticus.providers.policy import ProviderActual, ProviderRequest, check_provider_policy
from atticus.retrieval.ask import answer_question
from atticus.scheduler.planner import select_runnable_tasks
from atticus.status.report import generate_status
from atticus.validation.canonical_write_guard import (
    CanonicalWriteDenied,
    assert_canonical_write_allowed,
)


def init_db(tmp_path):
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_read_only_ask_mode_never_launches_workers(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_artifact(
            conn,
            path="/evidence/source_index.json",
            artifact_type="source_index",
            title="source index",
            content="production status source index",
            trust_status=TrustStatus.CANDIDATE,
        )
        before_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    class Launcher:
        launched = False

        def launch(self):
            self.launched = True
            raise AssertionError("ask mode must not launch workers")

    launcher = Launcher()
    answer = answer_question(str(db_path), "source index production status", worker_launcher=launcher)

    assert not launcher.launched
    assert answer.citations
    assert answer.trust_level == "candidate-only"
    with repo.db_connection(db_path) as conn:
        after_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert after_events == before_events


def test_read_only_ask_prefers_tokenized_match_over_recent_fallback(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_artifact(
            conn,
            path="/recent/unrelated.txt",
            artifact_type="note",
            title="recent unrelated",
            content="This is recent but irrelevant to the production bundle.",
            trust_status=TrustStatus.CANDIDATE,
        )
        expected = repo.add_artifact(
            conn,
            path="/older/production-map.txt",
            artifact_type="production_crosswalk",
            title="production map",
            content="The UOG production bundle maps Bates UOG-001 to the disclosure email.",
            trust_status=TrustStatus.VALIDATED,
        )

    answer = answer_question(str(db_path), "which production bundle maps bates UOG-001")

    assert answer.citations
    assert answer.citations[0].record_id == expected
    assert "recent/unrelated" not in answer.citations[0].path


def test_read_only_ask_returns_authority_records(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        authority_id = add_authority(
            conn,
            matter_scope="atticus",
            citation="42 U.S.C. § 1983",
            authority_type="statute",
            jurisdiction="US",
            title="Civil action for deprivation of rights",
            source_url="https://www.law.cornell.edu/uscode/text/42/1983",
        )

    answer = answer_question(str(db_path), "42 U.S.C. 1983 deprivation rights")

    assert answer.citations
    assert answer.citations[0].record_type == "authority"
    assert answer.citations[0].record_id == authority_id


def test_legacy_queued_tasks_cannot_bypass_dependency_gates(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="task-missing-source",
                title="Legacy queued task",
                task_type="legacy",
                source_dependencies=["src-missing"],
                status=TaskStatus.QUEUED,
            ),
        )
        runnable = select_runnable_tasks(conn, capacity=5)
        task = conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = ?", ("task-missing-source",)).fetchone()

    assert runnable == []
    assert task["status"] == "blocked"
    assert "missing source dependency" in task["blocked_reasons_json"]


def test_provider_fallback_is_blocked_unless_explicitly_allowed():
    decision = check_provider_policy(
        ProviderRequest("openrouter", "deepseek/deepseek-v4-pro", allow_fallback=False),
        ProviderActual("openrouter", "deepseek/deepseek-v4-flash"),
    )
    assert not decision.allowed
    assert decision.result == "failed_closed"

    allowed = check_provider_policy(
        ProviderRequest("openrouter", "deepseek/deepseek-v4-pro", allow_fallback=True),
        ProviderActual("openrouter", "deepseek/deepseek-v4-flash"),
    )
    assert allowed.allowed
    assert allowed.result == "allowed"


def test_old_indexes_import_as_candidate_artifacts_not_certified(tmp_path):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "legacy"
    workspace.mkdir()
    (workspace / "source_index.json").write_text('{"sources": ["a.pdf"]}', encoding="utf-8")

    with repo.db_connection(db_path) as conn:
        result = import_candidates(conn, workspace=workspace, dry_run=False)
        artifact = conn.execute("SELECT artifact_type, trust_status FROM artifacts").fetchone()

    assert len(result.candidates) == 1
    assert artifact["artifact_type"] == "source_index"
    assert artifact["trust_status"] == "candidate"


def test_certifications_require_validation(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/evidence_index.json",
            artifact_type="evidence_index",
            trust_status=TrustStatus.CANDIDATE,
        )
        with pytest.raises(CertificationBlocked):
            certify_subject(
                conn,
                subject_type="artifact",
                subject_id=artifact_id,
                certification_type="foundation",
                validator="test",
            )
        repo.record_validation(
            conn,
            target_type="artifact",
            target_id=artifact_id,
            gate_name="foundation",
            passed=True,
        )
        certification_id = certify_subject(
            conn,
            subject_type="artifact",
            subject_id=artifact_id,
            certification_type="foundation",
            validator="test",
        )

    assert certification_id.startswith("cert-")


def test_non_reducer_workers_cannot_write_canonical_files():
    with pytest.raises(CanonicalWriteDenied):
        assert_canonical_write_allowed(writer_role="worker", target_path="/canonical/facts.json")

    assert_canonical_write_allowed(writer_role="reducer", target_path="/canonical/facts.json")


def test_scheduler_under_fills_capacity_when_only_fewer_tasks_are_safe(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(
            conn,
            source_id="src-safe",
            path="/raw/a.pdf",
            sha256="a" * 64,
            trust_status=TrustStatus.CANDIDATE,
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="safe",
                title="Safe task",
                task_type="extract",
                source_dependencies=[source_id],
                status=TaskStatus.QUEUED,
                expected_value=10,
            ),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="blocked",
                title="Blocked task",
                task_type="extract",
                source_dependencies=["missing"],
                status=TaskStatus.QUEUED,
                expected_value=9,
            ),
        )
        runnable = select_runnable_tasks(conn, capacity=5)

    assert [task["task_id"] for task in runnable] == ["safe"]


def test_cost_provider_metadata_can_be_recorded(tmp_path):
    db_path = init_db(tmp_path)
    cost = estimate_cost_usd(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        cache_hit_tokens=1000,
        cache_miss_tokens=2000,
        output_tokens=500,
    )
    with repo.db_connection(db_path) as conn:
        run_id = repo.record_provider_run(
            conn,
            requested_provider="openrouter",
            requested_model="deepseek/deepseek-v4-pro",
            actual_provider="openrouter",
            actual_model="deepseek/deepseek-v4-pro",
            input_tokens=3000,
            output_tokens=500,
            cache_hit_tokens=1000,
            cache_miss_tokens=2000,
            estimated_cost_usd=cost,
            fallback_policy_result="not_needed",
        )
        row = conn.execute("SELECT * FROM provider_runs WHERE provider_run_id = ?", (run_id,)).fetchone()

    assert row["requested_model"] == "deepseek/deepseek-v4-pro"
    assert row["actual_model"] == "deepseek/deepseek-v4-pro"
    assert row["cache_hit_tokens"] == 1000
    assert row["estimated_cost_usd"] > 0


def test_stale_source_hash_marks_dependent_artifacts_stale(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(
            conn,
            source_id="src-1",
            path="/raw/evidence.pdf",
            sha256="a" * 64,
            trust_status=TrustStatus.CANDIDATE,
        )
        artifact_id = repo.add_artifact(
            conn,
            path="/artifacts/evidence_index.json",
            artifact_type="evidence_index",
            source_ids=[source_id],
        )
        changed = update_source_hash_and_mark_dependents_stale(
            conn,
            source_id=source_id,
            new_sha256="b" * 64,
        )
        artifact = conn.execute("SELECT stale, trust_status FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()

    assert changed == [artifact_id]
    assert artifact["stale"] == 1
    assert artifact["trust_status"] == "stale"


def test_status_reports_blocked_reasons_and_run_state(tmp_path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.upsert_run(conn, "run-1", "paused", "waiting on human attention")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="blocked-task",
                title="Blocked task",
                task_type="legacy",
                status=TaskStatus.BLOCKED,
            ),
        )
        repo.update_task_blocked(conn, "blocked-task", ["missing certification: artifact:a:foundation"])

    report = generate_status(str(db_path))

    assert report.run_state == "paused"
    assert report.counts["blocked_tasks"] == 1
    assert report.blocked_tasks[0]["task_id"] == "blocked-task"
    assert report.blocked_tasks[0]["reasons"] == ["missing certification: artifact:a:foundation"]


def test_ask_blocks_external_action_intent(tmp_path):
    db_path = init_db(tmp_path)
    answer = answer_question(str(db_path), "email the filing to opposing counsel")

    assert answer.trust_level == "blocked"
    assert "external legal actions are blocked" in answer.answer
