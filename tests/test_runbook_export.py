from __future__ import annotations

from pathlib import Path
import json

from atticus.agents.repair_planner import ensure_repair_plan_for_blocker
from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.reducer.review_queue import enqueue_reducer_review
from atticus.status.completion import FINAL_LEGAL_DRAFT_CERTIFICATIONS
from atticus.status.runbook import build_runbook, render_runbook_markdown


MATTER = "napier-accommodation-arrears"


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _certify(conn, certification_type: str) -> None:
    validation_id = repo.record_validation(
        conn,
        matter_scope=MATTER,
        target_type="matter",
        target_id=MATTER,
        gate_name=certification_type,
        passed=True,
        details={"test": True},
    )
    repo.add_certification(
        conn,
        subject_type="matter",
        subject_id=MATTER,
        certification_type=certification_type,
        validator="test",
        validation_result_id=validation_id,
        evidence={"test": True},
    )


def _seed_incomplete_matter(conn) -> str:
    repo.add_task(
        conn,
        TaskSpec(
            task_id="napier-final-quality",
            title="Napier final quality",
            task_type="final_quality_gate",
            stage=LegalStage.S9_FINAL_QUALITY_GATE,
            matter_scope=MATTER,
            status=TaskStatus.COMPLETE,
        ),
    )
    repo.add_task(
        conn,
        TaskSpec(
            task_id="napier-citation-repair",
            title="Napier citation repair",
            task_type="citation_repair",
            stage=LegalStage.S7_HOSTILE_REVIEW,
            matter_scope=MATTER,
            status=TaskStatus.REDUCER_PENDING,
        ),
    )
    candidate_id = repo.record_candidate_output(
        conn,
        task_id="napier-citation-repair",
        lease_id=None,
        worker_id="worker",
        output_type="result_packet",
        payload={"summary": "repair candidate"},
        status="candidate",
    )
    _ = enqueue_reducer_review(conn, candidate_id=candidate_id, reason="high-risk citation repair requires manual review")
    for certification_type in FINAL_LEGAL_DRAFT_CERTIFICATIONS:
        if certification_type not in {"citation_audit", "final_quality_gate"}:
            _certify(conn, certification_type)
    _ = ensure_repair_plan_for_blocker(
        conn,
        matter_scope=MATTER,
        target_type="matter",
        target_id=MATTER,
        reason=f"missing certification: matter:{MATTER}:citation_audit",
    )
    _ = repo.record_loop_guard_failure(
        conn,
        matter_scope=MATTER,
        target_type="task",
        target_id="napier-citation-repair",
        error_type="provider_preflight_failed",
        message="OpenRouter HTTP 401 unauthorized",
        source="test",
        payload={"provider_failure_class": "auth", "requires_user_intervention": True},
    )
    _ = repo.record_human_attention_once(
        conn,
        matter_scope=MATTER,
        target_type="candidate",
        target_id=candidate_id,
        severity="blocker",
        reason="manual reducer review required",
    )
    _ = repo.record_provider_run(
        conn,
        task_id="napier-citation-repair",
        stage="S7",
        requested_provider="openrouter",
        requested_model="deepseek/deepseek-v4-pro",
        actual_provider="DeepSeek",
        actual_model="deepseek/deepseek-v4-pro-20260423",
        input_tokens=100,
        output_tokens=25,
        cache_hit_tokens=10,
        fallback_allowed=False,
        fallback_policy_result="openrouter_endpoint_provenance",
    )
    return candidate_id


def test_runbook_export_includes_missing_certifications_reducer_queue_and_next_command(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        candidate_id = _seed_incomplete_matter(conn)
        runbook = build_runbook(conn, matter_scope=MATTER, db_path=str(db_path))
        rendered = render_runbook_markdown(runbook)

    completion = runbook["completion"]
    assert isinstance(completion, dict)
    assert "citation_audit" in completion["missing_certifications"]
    assert "final_quality_gate" in completion["missing_certifications"]
    assert runbook["reducer_review_queue"]
    assert candidate_id in rendered
    assert runbook["provider_failure_groups"]
    assert runbook["provider_taxonomy"]
    assert "openrouter_endpoint_provenance" in rendered
    assert "OpenRouter HTTP 401 unauthorized" in rendered
    assert "manual_reducer_review" in rendered
    assert "Blocker Ownership" in rendered
    assert "signature" in rendered
    assert isinstance(runbook["exact_next_action"], dict)
    assert str(db_path) in str(runbook["exact_next_action"].get("resume_command"))
    assert str(db_path) in str(runbook["exact_resume_command"])
    assert "Exact next action" in rendered
    assert "## Exact Resume Command" in rendered


def test_runbook_export_cli_writes_markdown_and_json_payload(tmp_path: Path, capsys) -> None:
    db_path = init_db(tmp_path)
    out_path = tmp_path / "runbook.md"
    with repo.db_connection(db_path) as conn:
        _ = _seed_incomplete_matter(conn)

    exit_code = cli_main(["runbook", "export", "--db", str(db_path), "--matter", MATTER, "--out", str(out_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["out"] == str(out_path)
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "Atticus Matter Runbook" in content
    assert "citation_audit" in content
    assert "reducer-review show" in content
    assert "Provider Taxonomy" in content
    assert "Reducer Review Commands" in content
