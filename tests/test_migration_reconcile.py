from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import json
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import LegalStage, TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.migration.reconcile import reconcile_foundation
from atticus.scheduler.planner import select_runnable_tasks


def _strings(value: object) -> list[str]:
    return [str(item) for item in cast(list[object], value)]


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_reconcile_certifies_foundation_layers_and_unblocks_safe_stage4_work(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(
            conn,
            source_id="src-a",
            path="/raw/a.pdf",
            sha256="a" * 64,
            trust_status=TrustStatus.CANDIDATE,
        )
        evidence_artifact = repo.add_artifact(
            conn,
            path="/candidate/evidence_index.json",
            artifact_type="evidence_registry",
            stage=LegalStage.S2_EVIDENCE_REGISTRY,
            trust_status=TrustStatus.CANDIDATE,
            source_ids=[source_id],
        )
        now = utc_now()
        _ = conn.execute(
            """
            INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method, coverage_status, confidence, metadata_json, created_at)
            VALUES ('ext-a', ?, ?, 'text', 'complete', 0.99, '{}', ?)
            """,
            (source_id, evidence_artifact, now),
        )
        _ = conn.execute(
            """
            INSERT INTO production_mappings(mapping_id, matter_scope, source_id, artifact_id, production_id, produced_path, integrity_status, metadata_json, created_at)
            VALUES ('prod-a', 'atticus', ?, ?, 'UOG-001', '/prod/a.pdf', 'candidate', '{}', ?)
            """,
            (source_id, evidence_artifact, now),
        )
        _ = conn.execute(
            """
            INSERT INTO chronology_events(chronology_event_id, matter_scope, event_date, event_date_precision, description, status, created_by_artifact_id, created_at, updated_at)
            VALUES ('chrono-a', 'atticus', '2024-01-01', 'day', 'Source-backed event', 'candidate', ?, ?, ?)
            """,
            (evidence_artifact, now, now),
        )
        _ = repo.add_citation_span(
            conn,
            target_type="chronology_event",
            target_id="chrono-a",
            source_id=source_id,
            quoted_text="source text",
            locator="p.1",
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="chronology-task",
                title="Build baseline chronology",
                task_type="chronology",
                stage=LegalStage.S4_BASELINE_CHRONOLOGY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )

        result = reconcile_foundation(conn, matter_scope="atticus", dry_run=False)
        certifications = {
            row["certification_type"]
            for row in conn.execute("SELECT certification_type FROM certifications WHERE subject_type = 'matter' AND subject_id = 'atticus'")
        }
        runnable = select_runnable_tasks(conn, capacity=15)

    assert result["ready_for_live_resume"]
    assert result["passed"] == ["source_inventory", "extraction_coverage", "evidence_registry", "production_mapping", "chronology_citations"]
    assert certifications >= set(_strings(result["passed"]))
    assert [task["task_id"] for task in runnable] == ["chronology-task"]


def test_reconcile_freezes_later_tasks_when_foundation_is_missing(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="premature-draft",
                title="Premature draft",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        result = reconcile_foundation(conn, matter_scope="atticus", dry_run=False)
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'premature-draft'").fetchone())
    reasons = _strings(json.loads(str(task["blocked_reasons_json"])))
    assert not result["ready_for_live_resume"]
    assert task["status"] == "blocked"
    assert any("foundation reconciliation" in reason for reason in reasons)


def test_reconcile_unfreezes_tasks_after_foundation_certifies(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="previously-frozen-draft",
                title="Previously frozen draft",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        first = reconcile_foundation(conn, matter_scope="atticus", dry_run=False)
        for gate in ["source_inventory", "extraction_coverage", "evidence_registry", "production_mapping", "chronology_citations"]:
            validation_id = repo.record_validation(
                conn,
                target_type="matter",
                target_id="atticus",
                gate_name=gate,
                passed=True,
                details={"test": "precertified"},
            )
            _ = repo.add_certification(
                conn,
                subject_type="matter",
                subject_id="atticus",
                certification_type=gate,
                validator="test",
                validation_result_id=validation_id,
            )
        second = reconcile_foundation(conn, matter_scope="atticus", dry_run=False)
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'previously-frozen-draft'").fetchone())
    assert first["frozen_tasks"] == ["previously-frozen-draft"]
    assert second["ready_for_live_resume"]
    assert second["unfrozen_tasks"] == ["previously-frozen-draft"]
    assert task["status"] == "queued"
    assert json.loads(str(task["blocked_reasons_json"])) == []


def test_reconcile_unfreeze_skips_malformed_blocked_reason_and_continues(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="malformed-frozen", title="Malformed frozen", task_type="draft", status=TaskStatus.BLOCKED))
        repo.add_task(conn, TaskSpec(task_id="valid-frozen", title="Valid frozen", task_type="draft", status=TaskStatus.BLOCKED))
        _ = conn.execute("PRAGMA ignore_check_constraints = ON")
        _ = conn.execute("UPDATE tasks SET blocked_reasons_json = ? WHERE task_id = ?", ("{not valid json", "malformed-frozen"))
        _ = conn.execute(
            "UPDATE tasks SET blocked_reasons_json = ? WHERE task_id = ?",
            (json.dumps(["foundation reconciliation incomplete before live resume: source_inventory"]), "valid-frozen"),
        )
        for gate in ["source_inventory", "extraction_coverage", "evidence_registry", "production_mapping", "chronology_citations"]:
            validation_id = repo.record_validation(conn, target_type="matter", target_id="atticus", gate_name=gate, passed=True, details={})
            _ = repo.add_certification(
                conn,
                subject_type="matter",
                subject_id="atticus",
                certification_type=gate,
                validator="test",
                validation_result_id=validation_id,
            )
        result = reconcile_foundation(conn, matter_scope="atticus", dry_run=False)
        malformed = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'malformed-frozen'").fetchone())
        valid = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'valid-frozen'").fetchone())
        attention = cast(Mapping[str, object], conn.execute("SELECT reason FROM human_attention WHERE target_id = 'malformed-frozen'").fetchone())
    assert result["ready_for_live_resume"]
    assert result["unfrozen_tasks"] == ["valid-frozen"]
    assert malformed["status"] == TaskStatus.BLOCKED
    assert valid["status"] == TaskStatus.QUEUED
    assert json.loads(str(valid["blocked_reasons_json"])) == []
    assert "malformed" in str(attention["reason"])
