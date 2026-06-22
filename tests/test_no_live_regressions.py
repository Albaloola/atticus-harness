from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from typing import cast

import pytest

from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.scheduler.supervisor_invariants import evaluate_no_silent_idle
from atticus.status.completion import FINAL_LEGAL_DRAFT_CERTIFICATIONS, build_matter_completion_report, next_resume_action
from atticus.workflows.final_gate import final_gate_readiness


MATTER = "synthetic-final-matter"


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _json_output(text: str) -> dict[str, object]:
    value = json.loads(text)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _certify(conn: sqlite3.Connection, certification_type: str) -> None:
    validation_id = repo.record_validation(
        conn,
        matter_scope=MATTER,
        target_type="matter",
        target_id=MATTER,
        gate_name=certification_type,
        passed=True,
        details={"synthetic": True},
    )
    repo.add_certification(
        conn,
        subject_type="matter",
        subject_id=MATTER,
        certification_type=certification_type,
        validator="synthetic-test",
        validation_result_id=validation_id,
        evidence={"synthetic": True},
    )


def _certify_all_except(conn: sqlite3.Connection, *excluded: str) -> None:
    excluded_set = set(excluded)
    for certification_type in FINAL_LEGAL_DRAFT_CERTIFICATIONS:
        if certification_type not in excluded_set:
            _certify(conn, certification_type)


def _add_final_task(conn: sqlite3.Connection, *, status: TaskStatus = TaskStatus.COMPLETE) -> None:
    repo.add_task(
        conn,
        TaskSpec(
            task_id="synthetic-final-quality-task",
            title="Synthetic final quality gate",
            task_type="final_quality_gate",
            stage=LegalStage.S9_FINAL_QUALITY_GATE,
            matter_scope=MATTER,
            status=status,
        ),
    )


def _drop_control_table_and_claim_current(db_path: Path, table: str) -> None:
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(f"DROP TABLE {table}")
        raw.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
        raw.commit()
    finally:
        raw.close()


def test_synthetic_matter_final_gate_completes_only_with_final_certification(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_task(conn)
        _certify_all_except(conn, "final_quality_gate")
        before = final_gate_readiness(conn, MATTER)
        _certify(conn, "final_quality_gate")
        after = final_gate_readiness(conn, MATTER)
        completion = build_matter_completion_report(conn, MATTER)

    assert before["ready"] is False
    assert before["can_create_final_gate"] is True
    assert before["next_action"]["type"] == "create_missing_certification_work"
    assert after["ready"] is True
    assert after["complete"] is True
    assert completion.done is True
    assert completion.safe_to_finalize is True


def test_no_silent_idle_records_next_action_without_provider_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingProvider:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("no-silent-idle regression must not construct provider clients")

    monkeypatch.setattr("atticus.providers.live_readiness.OpenRouterClient", ExplodingProvider)
    db_path = init_db(tmp_path)
    tick_result: dict[str, object] = {
        "leased_tasks": [],
        "executed_tasks": [],
        "imported_tasks": [],
        "reduced_candidates": [],
        "applied_actions": [],
        "routed_operator_signals": [],
        "worker_errors": [],
        "preflight_groups": [],
        "blocked_repairs": [],
        "terminal_blocks": [],
    }
    with repo.db_connection(db_path) as conn:
        _add_final_task(conn)
        _certify_all_except(conn, "citation_audit", "final_quality_gate")

        result = evaluate_no_silent_idle(conn, MATTER, tick_result, write=True)
        event_count = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type = 'supervisor.no_progress_detected'").fetchone()
        attention = conn.execute(
            "SELECT reason FROM human_attention WHERE matter_scope = ? AND target_type = 'matter' AND target_id = ? AND status = 'open'",
            (MATTER, MATTER),
        ).fetchone()

    assert result["ok"] is False
    assert result["reason"] == "no_progress_with_incomplete_matter"
    assert cast(dict[str, object], result["next_action"])["type"] == "missing_certification"
    assert "citation_audit" in result["missing_certifications"]
    assert event_count is not None and int(str(event_count["n"])) == 1
    assert attention is not None


def test_human_decision_resume_blocks_finalized_matter_until_attention_closed(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _add_final_task(conn)
        _certify_all_except(conn)
        attention_id = repo.record_human_attention(
            conn,
            matter_scope=MATTER,
            target_type="matter",
            target_id=MATTER,
            severity="blocker",
            reason="operator must choose whether to file the synthetic final draft",
        )

        blocked_report = build_matter_completion_report(conn, MATTER)
        blocked_action = next_resume_action(conn, MATTER)
        conn.execute("UPDATE human_attention SET status = 'closed' WHERE attention_id = ?", (attention_id,))
        resumed_report = build_matter_completion_report(conn, MATTER)
        resumed_action = next_resume_action(conn, MATTER)

    assert blocked_report.done is False
    assert blocked_report.safe_to_finalize is False
    assert blocked_action["type"] == "human_attention"
    assert blocked_action["attention_id"] == attention_id
    assert "human-attention" in str(blocked_action["resume_command"])
    assert resumed_report.done is True
    assert resumed_report.safe_to_finalize is True
    assert resumed_action["type"] == "complete"


@pytest.mark.parametrize(
    ("argv", "missing_table"),
    [
        (["matter-health", "--matter", MATTER, "--why-not-done", "--json"], "error_logs"),
        (["next-action", "--matter", MATTER, "--json"], "error_logs"),
        (["final-gate", "readiness", "--matter", MATTER, "--json"], "error_logs"),
    ],
)
def test_readonly_matter_commands_report_stale_schema_without_mutating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    missing_table: str,
) -> None:
    db_path = init_db(tmp_path)
    _drop_control_table_and_claim_current(db_path, missing_table)

    code = cli_main([argv[0], "--db", str(db_path), *argv[1:]])
    output = _json_output(capsys.readouterr().out)

    assert code == 2
    assert output["ok"] is False
    assert output["reason"] == "schema_mismatch"
    assert output["schema_meta_version"] == "6"
    assert missing_table in cast(list[object], output["missing_tables"])
    raw = sqlite3.connect(db_path)
    try:
        still_missing = raw.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (missing_table,)).fetchone()
    finally:
        raw.close()
    assert still_missing is None
