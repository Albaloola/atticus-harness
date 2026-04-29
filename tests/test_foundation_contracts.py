from __future__ import annotations

from typing import cast
from collections.abc import Mapping
from pathlib import Path
import inspect
import sqlite3

import pytest

from atticus.core.policies import TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.graph.certifications import CertificationBlocked, certify_subject
from atticus.graph.evidence import add_authority
from atticus.graph.staleness import update_source_hash_and_mark_dependents_stale
from atticus.migration.import_old_run import import_candidates
from atticus.providers.cost import estimate_cost_usd
from atticus.providers.policy import ProviderActual, ProviderRequest, check_provider_policy
from atticus.reducer.canonical_writer import write_canonical_text
from atticus.retrieval.ask import answer_question
from atticus.retrieval.index import rebuild_search_index
from atticus.scheduler.lease import LeaseError, acquire_lease, complete_lease
from atticus.scheduler.planner import select_runnable_tasks
from atticus.status.report import generate_status
from atticus.validation.canonical_write_guard import (
    CanonicalWriteDenied,
    assert_canonical_write_allowed,
)


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(float(str(row["n"])))


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_read_only_ask_mode_never_launches_workers(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_artifact(
            conn,
            path="/evidence/source_index.json",
            artifact_type="source_index",
            title="source index",
            content="production status source index",
            trust_status=TrustStatus.CANDIDATE,
        )
        before_events = _count(conn, "SELECT COUNT(*) AS n FROM events")

    class Launcher:
        launched: bool = False

        def launch(self):
            self.launched = True
            raise AssertionError("ask mode must not launch workers")

    launcher = Launcher()
    answer = answer_question(str(db_path), "source index production status", worker_launcher=launcher)

    assert not launcher.launched
    assert answer.citations
    assert answer.trust_level == "candidate-only"
    with repo.db_connection(db_path) as conn:
        after_events = _count(conn, "SELECT COUNT(*) AS n FROM events")
    assert after_events == before_events


def test_read_only_ask_prefers_tokenized_match_over_recent_fallback(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_artifact(
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


def test_read_only_ask_returns_authority_records(tmp_path: Path):
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


def test_search_index_rebuild_is_durable_and_ask_uses_projection(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        artifact_id = repo.add_artifact(
            conn,
            path="/validated/production-map.txt",
            artifact_type="production_crosswalk",
            title="production map",
            content="Bates UOG-001 maps to the disclosure bundle.",
            trust_status=TrustStatus.VALIDATED,
        )
        first = rebuild_search_index(conn)
        second = rebuild_search_index(conn)
        entries = cast(list[Mapping[str, object]], conn.execute("SELECT record_type, record_id FROM search_index_entries ORDER BY record_type, record_id").fetchall())
        rebuild_count = _count(conn, "SELECT COUNT(*) AS n FROM index_rebuilds WHERE index_name = ?", (first["index_name"],))
        event_count = _count(conn, "SELECT COUNT(*) AS n FROM events WHERE event_type = 'search_index.rebuilt'")

    answer = answer_question(str(db_path), "Bates UOG-001 disclosure bundle")

    assert first["entry_count"] == 1
    assert second["input_fingerprint"] == first["input_fingerprint"]
    assert [(row["record_type"], row["record_id"]) for row in entries] == [("artifact", artifact_id)]
    assert rebuild_count == 2
    assert event_count == 2
    assert answer.citations[0].record_id == artifact_id


def test_indexed_ask_is_limited_to_requested_matter(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        alpha_artifact = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/settlement.txt",
            artifact_type="matter_note",
            title="alpha settlement",
            content="alphaexclusive privileged settlement memo",
            trust_status=TrustStatus.VALIDATED,
        )
        beta_artifact = repo.add_artifact(
            conn,
            matter_scope="beta",
            path="/beta/settlement.txt",
            artifact_type="matter_note",
            title="beta settlement",
            content="betaexclusive privileged settlement memo",
            trust_status=TrustStatus.VALIDATED,
        )
        _ = rebuild_search_index(conn, matter_scope="alpha", authorized_matter_scope="alpha")
        _ = rebuild_search_index(conn, matter_scope="beta", authorized_matter_scope="beta")

    blocked = answer_question(str(db_path), "alphaexclusive", matter_scope="beta", authorized_matter_scope="beta")
    beta = answer_question(str(db_path), "betaexclusive", matter_scope="beta", authorized_matter_scope="beta")

    assert blocked.citations == []
    assert blocked.trust_level == "unsupported"
    assert beta.citations
    assert beta.citations[0].record_id == beta_artifact
    assert beta.citations[0].record_id != alpha_artifact


def test_fallback_ask_is_limited_to_requested_matter(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        alpha_artifact = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/source.txt",
            artifact_type="matter_note",
            title="alpha source",
            content="alphafallback source memo",
            trust_status=TrustStatus.VALIDATED,
        )
        beta_artifact = repo.add_artifact(
            conn,
            matter_scope="beta",
            path="/beta/source.txt",
            artifact_type="matter_note",
            title="beta source",
            content="betafallback source memo",
            trust_status=TrustStatus.VALIDATED,
        )

    blocked = answer_question(str(db_path), "alphafallback", matter_scope="beta", authorized_matter_scope="beta")
    beta = answer_question(str(db_path), "betafallback", matter_scope="beta", authorized_matter_scope="beta")

    assert blocked.citations == []
    assert blocked.trust_level == "unsupported"
    assert beta.citations
    assert beta.citations[0].record_id == beta_artifact
    assert beta.citations[0].record_id != alpha_artifact


def test_legacy_queued_tasks_cannot_bypass_dependency_gates(tmp_path: Path):
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
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = ?", ("task-missing-source",)).fetchone())
    assert runnable == []
    assert task["status"] == "blocked"
    assert "missing source dependency" in str(task["blocked_reasons_json"])


def test_scheduler_capacity_zero_selects_no_tasks_without_mutating(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="capacity-zero", title="Capacity zero", task_type="extract"))
        runnable = select_runnable_tasks(conn, capacity=0)
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'capacity-zero'").fetchone())
    assert runnable == []
    assert task["status"] == TaskStatus.QUEUED
    assert task["blocked_reasons_json"] == "[]"


def test_scheduler_rechecks_blocked_tasks_after_dependency_is_satisfied(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="recheck-blocked",
                title="Recheck blocked",
                task_type="extract",
                source_dependencies=["src-now-present"],
            ),
        )
        assert select_runnable_tasks(conn, capacity=5) == []
        blocked = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'recheck-blocked'").fetchone())
        _ = repo.add_source(conn, source_id="src-now-present", path="/raw/now-present.pdf", sha256="c" * 64)
        runnable = select_runnable_tasks(conn, capacity=5)
        requeued = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'recheck-blocked'").fetchone())
    assert blocked["status"] == TaskStatus.BLOCKED
    assert "missing source dependency" in str(blocked["blocked_reasons_json"])
    assert [task["task_id"] for task in runnable] == ["recheck-blocked"]
    assert requeued["status"] == TaskStatus.QUEUED
    assert requeued["blocked_reasons_json"] == "[]"


def test_scheduler_fails_closed_on_malformed_provider_policy_cost(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="bad-scheduler-policy", title="Bad policy", task_type="extract"))
        _ = conn.execute("PRAGMA ignore_check_constraints = ON")
        _ = conn.execute("UPDATE tasks SET provider_policy_json = ? WHERE task_id = ?", ("{not valid json", "bad-scheduler-policy"))
        runnable = select_runnable_tasks(conn, capacity=5)
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'bad-scheduler-policy'").fetchone())
    assert runnable == []
    assert task["status"] == TaskStatus.BLOCKED
    assert "malformed provider policy" in str(task["blocked_reasons_json"])


def test_manual_lease_cannot_bypass_dependency_gates(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(task_id="lease-missing-source", title="Lease missing source", task_type="extract", source_dependencies=["src-missing"]),
        )
        with pytest.raises(LeaseError, match="blocked by gates"):
            _ = acquire_lease(conn, task_id="lease-missing-source", worker_id="worker-1")
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'lease-missing-source'").fetchone())
        lease_count = _count(conn, "SELECT COUNT(*) AS n FROM leases WHERE task_id = 'lease-missing-source'")

    assert task["status"] == TaskStatus.BLOCKED
    assert "missing source dependency" in str(task["blocked_reasons_json"])
    assert lease_count == 0


def test_task_gates_block_cross_matter_source_dependencies(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_source(conn, source_id="src-alpha-only", matter_scope="alpha", path="/alpha/source.pdf", sha256="d" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="beta-cross-source",
                title="Beta cross source",
                task_type="extract",
                matter_scope="beta",
                source_dependencies=["src-alpha-only"],
            ),
        )
        runnable = select_runnable_tasks(conn, capacity=5)
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'beta-cross-source'").fetchone())
    assert runnable == []
    assert task["status"] == TaskStatus.BLOCKED
    assert "cross-matter source dependency" in str(task["blocked_reasons_json"])


def test_flat_provider_fallback_is_blocked_without_explicit_pool():
    decision = check_provider_policy(
        ProviderRequest("openrouter", "deepseek/deepseek-v4-pro", allow_fallback=False),
        ProviderActual("openrouter", "deepseek/deepseek-v4-flash"),
    )
    assert not decision.allowed
    assert decision.result == "failed_closed"

    fallback = check_provider_policy(
        ProviderRequest("openrouter", "deepseek/deepseek-v4-pro", allow_fallback=True),
        ProviderActual("openrouter", "deepseek/deepseek-v4-flash"),
    )
    assert not fallback.allowed
    assert fallback.result == "failed_closed"
    assert "explicit OpenRouter model pool" in fallback.reason


def test_codex_provider_policy_accepts_only_codex_55_without_fallback():
    decision = check_provider_policy(ProviderRequest("openai-codex", "gpt-5.5", allow_fallback=False))
    assert decision.allowed
    assert decision.result == "not_needed"

    alias = check_provider_policy(
        ProviderRequest("openai-codex", "openai-codex/gpt-5.5", allow_fallback=False),
        ProviderActual("openai-codex", "gpt-5.5"),
    )
    assert alias.allowed

    drift = check_provider_policy(
        ProviderRequest("openai-codex", "gpt-5.5", allow_fallback=False),
        ProviderActual("openrouter", "deepseek/deepseek-v4-pro"),
    )
    assert not drift.allowed
    assert drift.result == "failed_closed"

    forced_fallback = check_provider_policy(
        ProviderRequest("openai-codex", "gpt-5.5", allow_fallback=True),
        ProviderActual("openrouter", "deepseek/deepseek-v4-pro"),
    )
    assert not forced_fallback.allowed
    assert forced_fallback.result == "failed_closed"

    unknown = check_provider_policy(ProviderRequest("openai-codex", "gpt-5.4", allow_fallback=False))
    assert not unknown.allowed
    assert unknown.result == "blocked"


def test_old_indexes_import_as_candidate_artifacts_not_certified(tmp_path: Path):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "legacy"
    workspace.mkdir()
    _ = (workspace / "source_index.json").write_text('{"sources": ["a.pdf"]}', encoding="utf-8")

    with repo.db_connection(db_path) as conn:
        result = import_candidates(conn, workspace=workspace, dry_run=False)
        artifact = cast(Mapping[str, object], conn.execute("SELECT artifact_type, trust_status FROM artifacts").fetchone())
    assert len(result.candidates) == 1
    assert artifact["artifact_type"] == "source_index"
    assert artifact["trust_status"] == "candidate"


def test_certifications_require_validation(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/evidence_index.json",
            artifact_type="evidence_index",
            trust_status=TrustStatus.CANDIDATE,
        )
        with pytest.raises(CertificationBlocked):
            _ = certify_subject(
                conn,
                subject_type="artifact",
                subject_id=artifact_id,
                certification_type="foundation",
                validator="test",
            )
        _ = repo.record_validation(
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

    with pytest.raises(CanonicalWriteDenied):
        assert_canonical_write_allowed(writer_role="reducer", target_path="/canonical/facts.json")

    signature = inspect.signature(write_canonical_text)
    empty: object = inspect.Signature.empty
    required_params = {name for name, parameter in signature.parameters.items() if cast(object, parameter.default) is empty}
    assert {"conn", "lease_id", "task_id"} <= required_params


def test_canonical_writer_requires_active_reducer_lease_context(tmp_path: Path):
    db_path = init_db(tmp_path)
    target_path = "canonical.txt"
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="canonical-task", title="Canonical task", task_type="reduce"))
        worker_lease = acquire_lease(conn, task_id="canonical-task", worker_id="worker-1")
        with pytest.raises(CanonicalWriteDenied, match="not issued for reducer"):
            write_canonical_text(
                conn=conn,
                lease_id=worker_lease,
                task_id="canonical-task",
                writer_role="canonical_writer",
                target_path=target_path,
                text="unsafe",
            )
        _ = conn.execute("UPDATE leases SET status = 'failed' WHERE lease_id = ?", (worker_lease,))
        _ = conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (TaskStatus.REDUCER_PENDING, "canonical-task"))
        fake_reducer_lease = acquire_lease(conn, task_id="canonical-task", worker_id="reducer-fake")
        with pytest.raises(CanonicalWriteDenied, match="not issued for reducer"):
            write_canonical_text(
                conn=conn,
                lease_id=fake_reducer_lease,
                task_id="canonical-task",
                writer_role="canonical_writer",
                target_path=target_path,
                text="unsafe",
            )
        _ = conn.execute("UPDATE leases SET status = 'failed' WHERE lease_id = ?", (fake_reducer_lease,))
        _ = conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (TaskStatus.REDUCER_PENDING, "canonical-task"))
        reducer_lease = acquire_lease(conn, task_id="canonical-task", worker_id="reducer-1", lease_role="reducer")
        write_canonical_text(
            conn=conn,
            lease_id=reducer_lease,
            task_id="canonical-task",
            writer_role="canonical_writer",
            target_path=target_path,
            text="safe",
        )

    assert (tmp_path / "canonical" / "canonical.txt").read_text(encoding="utf-8") == "safe"


def test_canonical_writer_rejects_path_escape_and_symlink_targets(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="canonical-sandbox", title="Canonical sandbox", task_type="reduce"))
        _ = conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (TaskStatus.REDUCER_PENDING, "canonical-sandbox"))
        reducer_lease = acquire_lease(conn, task_id="canonical-sandbox", worker_id="reducer-1", lease_role="reducer")
        for target_path in (str(tmp_path / "outside.txt"), "../outside.txt", "canonical"):
            with pytest.raises(CanonicalWriteDenied):
                write_canonical_text(
                    conn=conn,
                    lease_id=reducer_lease,
                    task_id="canonical-sandbox",
                    writer_role="canonical_writer",
                    target_path=target_path,
                    text="blocked",
                )
        canonical_root = tmp_path / "canonical"
        canonical_root.mkdir(exist_ok=True)
        symlink = canonical_root / "link.txt"
        symlink.symlink_to(tmp_path / "outside.txt")
        with pytest.raises(CanonicalWriteDenied, match="symlink"):
            write_canonical_text(
                conn=conn,
                lease_id=reducer_lease,
                task_id="canonical-sandbox",
                writer_role="canonical_writer",
                target_path="link.txt",
                text="blocked",
            )


def test_legacy_validation_result_schema_migrates_before_index_creation(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        _ = conn.execute(
            """
            CREATE TABLE validation_results (
              validation_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
              target_type TEXT NOT NULL,
              target_id TEXT NOT NULL,
              gate_name TEXT NOT NULL,
              passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
              severity TEXT NOT NULL DEFAULT 'info',
              details_json TEXT NOT NULL CHECK(json_valid(details_json)),
              created_at TEXT NOT NULL
            ) STRICT
            """
        )
        _ = conn.execute(
            "CREATE INDEX validation_target_idx ON validation_results(target_type, target_id, gate_name, passed)"
        )
        _ = conn.execute(
            "INSERT INTO validation_results(target_type, target_id, gate_name, passed, severity, details_json, created_at) VALUES ('matter', 'beta', 'foundation', 1, 'info', '{}', 'now')"
        )

    repo.initialize_database(db_path)

    with repo.db_connection(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(validation_results)")}
        index_columns = [row["name"] for row in conn.execute("PRAGMA index_info(validation_target_idx)")]
        validation = cast(Mapping[str, object], conn.execute("SELECT matter_scope FROM validation_results").fetchone())

    assert "matter_scope" in columns
    assert index_columns[:2] == ["matter_scope", "target_type"]
    assert validation["matter_scope"] == "beta"


def test_lease_events_use_task_matter_scope(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="beta-lease-events", title="Beta lease events", task_type="extract", matter_scope="beta"))
        lease_id = acquire_lease(conn, task_id="beta-lease-events", worker_id="worker-1")
        complete_lease(conn, lease_id=lease_id, task_status=TaskStatus.COMPLETE)
        scopes = [
            str(row["matter_scope"])
            for row in conn.execute(
                "SELECT matter_scope FROM events WHERE event_type IN ('lease.acquired', 'lease.completed') ORDER BY event_id"
            ).fetchall()
        ]

    assert scopes == ["beta", "beta"]


def test_scheduler_under_fills_capacity_when_only_fewer_tasks_are_safe(tmp_path: Path):
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


def test_cost_provider_metadata_can_be_recorded(tmp_path: Path):
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
        row = cast(Mapping[str, object], conn.execute("SELECT * FROM provider_runs WHERE provider_run_id = ?", (run_id,)).fetchone())
    assert row["requested_model"] == "deepseek/deepseek-v4-pro"
    assert row["actual_model"] == "deepseek/deepseek-v4-pro"
    assert row["cache_hit_tokens"] == 1000
    assert float(str(row["estimated_cost_usd"])) > 0


def test_stale_source_hash_marks_dependent_artifacts_stale(tmp_path: Path):
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
        artifact = cast(Mapping[str, object], conn.execute("SELECT stale, trust_status FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone())
        source = cast(Mapping[str, object], conn.execute("SELECT stale, sha256 FROM sources WHERE source_id = ?", (source_id,)).fetchone())
        snapshots = _count(conn, "SELECT COUNT(*) AS n FROM source_snapshots WHERE source_id = ? AND sha256 = ?", (source_id, "b" * 64))
        event = cast(Mapping[str, object], conn.execute("SELECT matter_scope FROM events WHERE event_type = 'source.hash_changed' ORDER BY event_id DESC LIMIT 1").fetchone())
    assert changed == [artifact_id]
    assert source["stale"] == 1
    assert source["sha256"] == "b" * 64
    assert snapshots == 1
    assert event["matter_scope"] == "atticus"
    assert artifact["stale"] == 1
    assert artifact["trust_status"] == "stale"


def test_status_reports_blocked_reasons_and_run_state(tmp_path: Path):
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


def test_ask_blocks_external_action_intent(tmp_path: Path):
    db_path = init_db(tmp_path)
    answer = answer_question(str(db_path), "email the filing to opposing counsel")

    assert answer.trust_level == "blocked"
    assert "external legal actions are blocked" in answer.answer
