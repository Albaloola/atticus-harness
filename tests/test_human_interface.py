

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from atticus.core.events import utc_now
from atticus.db import repo
from atticus.operator_control import build_agent_handoff_packet, build_operator_control_panel, render_operator_control_panel
from atticus.status.completion import (
    ROUTE_MAP,
    build_matter_completion_report,
    next_resume_action,
    route_human_attention,
    triage_human_attention,
)
from atticus.workflows.final_gate import final_gate_readiness




MATTER = "test-matter"


def init_db(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    repo.ensure_matter(conn, MATTER, "Test Matter")
    return conn, str(db_path)


def add_human_attention(conn: sqlite3.Connection, **kwargs: object) -> int:
    defaults: dict[str, object] = dict(
        target_type="task",
        target_id="test-task",
        severity="blocker",
        reason="operator decision required: test reason",
        owner="operator",
        matter_scope=MATTER,
        plain_question="",
        why_needed="",
        acceptable_responses=(),
        routed_lane="human_request",
    )
    defaults.update(kwargs)
    return repo.record_human_request(conn, **defaults)  # type: ignore[arg-type]




class TestTriageAndRouting:

    def test_proof_citation_repair_classification(self) -> None:
        item: dict[str, object] = {"reason": "proof_citation: worker output cited impermissible material", "status": "open"}
        assert triage_human_attention(item) == "proof_citation_repair"

    def test_proof_citation_repair_routing(self) -> None:
        item: dict[str, object] = {"reason": "impermissible citation: findings cited derivative material", "status": "open"}
        route = route_human_attention(item)
        assert route.get("classification") == "proof_citation_repair"
        assert route.get("routed_owner") == "scheduler"
        assert "repair-tick" in str(route.get("routed_command", ""))

    def test_validation_failure_classification(self) -> None:
        item: dict[str, object] = {"reason": "validation_failure: gate failed on citation check", "status": "open"}
        assert triage_human_attention(item) == "validation_failure"

    def test_validation_failure_routes_to_scheduler(self) -> None:
        item: dict[str, object] = {"reason": "gate failed: extraction_coverage check", "status": "open"}
        route = route_human_attention(item)
        assert route.get("classification") == "validation_failure"
        assert route.get("routed_owner") == "scheduler"

    def test_routed_lane_in_route(self, tmp_path: Path) -> None:
        conn, _ = init_db(tmp_path)
        try:
            attention_id = add_human_attention(conn, routed_lane="orchestrator_attention", reason="incomplete task dependency: test", plain_question="internal routing test")
            item = repo.get_human_request(conn, attention_id=attention_id)
            assert item is not None
            route = route_human_attention(item, matter_scope=MATTER)
            assert route.get("routed_lane") == "orchestrator_attention"
        finally:
            conn.close()

    def test_human_request_lane_operator_blocked(self, tmp_path: Path) -> None:
        conn, db_path = init_db(tmp_path)
        try:
            now = utc_now()
            for cert in ("source_inventory", "extraction_coverage", "evidence_registry",
                         "production_mapping", "chronology_citations", "issue_route_map",
                         "authority_map", "hostile_review", "draft_preparation",
                         "privacy_redaction_audit", "citation_audit"):
                conn.execute(
                    "INSERT OR IGNORE INTO certifications (certification_id, subject_type, subject_id, certification_type, status, validator, validation_result_id, evidence_json, created_at) VALUES (?, 'matter', ?, ?, 'active', 'test', 0, '{}', ?)",
                    (f"cert-{cert}-auto", MATTER, cert, now),
                )
            conn.commit()

            attention_id = add_human_attention(
                conn,
                target_type="matter",
                target_id=MATTER,
                reason="operator decision required for final quality gate",
                plain_question="Should we proceed with the current draft?",
                why_needed="Final gate requires operator sign-off on legal risk",
            )
            conn.commit()

            readiness = final_gate_readiness(conn, MATTER)
            assert readiness["can_create_final_gate"] is False, "operator attention should block final gate creation"
            blocked_types = [r["type"] for r in readiness.get("blocked_reasons", [])]
            assert "open_human_attention" in blocked_types
        finally:
            conn.close()


class TestHumanRequest:

    def test_get_human_request_new_fields(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            attention_id = add_human_attention(
                conn,
                reason="Obtain clearer copy of NAP-SRC-0020",
                plain_question="Please provide a clearer copy of the NTQ or authorise use of best available",
                why_needed="The source is needed for reliable NTQ/date support at final quality gate",
                acceptable_responses=["upload_document", "authorise_existing_source", "proceed_with_best_available", "proceed_without_source"],
            )
            item = repo.get_human_request(conn, attention_id=attention_id)
            assert item is not None
            assert item["plain_question"] == "Please provide a clearer copy of the NTQ or authorise use of best available"
            assert item["why_needed"] == "The source is needed for reliable NTQ/date support at final quality gate"
            responses = item.get("acceptable_responses")
            assert isinstance(responses, list)
            assert "upload_document" in responses
            assert item["routed_lane"] == "human_request"
        finally:
            conn.close()

    def test_get_human_requests_filters_by_lane(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            id1 = add_human_attention(conn, reason="operator decision 1", routed_lane="human_request")
            id2 = add_human_attention(conn, reason="orchestrator issue", routed_lane="orchestrator_attention")

            human_items = repo.get_human_requests_for_matter(conn, matter_scope=MATTER, lane="human_request")
            orch_items = repo.get_human_requests_for_matter(conn, matter_scope=MATTER, lane="orchestrator_attention")

            assert len(human_items) >= 1
            assert len(orch_items) >= 1
            assert any(str(i.get("attention_id")) == str(id1) for i in human_items)
            assert any(str(i.get("attention_id")) == str(id2) for i in orch_items)
        finally:
            conn.close()

    def test_human_request_excludes_internal_lanes(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            add_human_attention(conn, reason="scheduler issue", routed_lane="scheduler_action")
            add_human_attention(conn, reason="provider issue", routed_lane="provider_control_plane")
            add_human_attention(conn, reason="proof citation issue", routed_lane="proof_citation_repair")

            human_items = repo.get_human_requests_for_matter(conn, matter_scope=MATTER, lane="human_request")
            lanes_found = {str(i.get("routed_lane")) for i in human_items}
            assert "scheduler_action" not in lanes_found
            assert "provider_control_plane" not in lanes_found
            assert "proof_citation_repair" not in lanes_found
        finally:
            conn.close()


class TestHumanResponse:

    def test_record_human_response_marks_attention_handled(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            attention_id = add_human_attention(conn, reason="test response")
            result = repo.record_human_response(
                conn,
                attention_id=attention_id,
                response_type="proceed_without_source",
                statement="Operator says proceed without this source",
            )
            assert result is not None
            assert result.get("response_id") is not None

            item = repo.get_human_request(conn, attention_id=attention_id)
            assert item is not None
            assert item.get("response_type") == "proceed_without_source"
            assert "proceed without" in str(item.get("response_caveat", "")).lower()
        finally:
            conn.close()

    def test_record_human_response_best_available_caveat(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            attention_id = add_human_attention(conn, reason="need clearer NTQ")
            result = repo.record_human_response(
                conn,
                attention_id=attention_id,
                response_type="provided_best_available",
                statement="Omer says this is as good as available; OCR and use with caveat",
            )
            assert result is not None

            item = repo.get_human_request(conn, attention_id=attention_id)
            assert item is not None
            assert item.get("response_type") == "provided_best_available"
            assert "best available" in str(item.get("response_caveat", "")).lower()
        finally:
            conn.close()

    def test_response_creates_operator_response_record(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            attention_id = add_human_attention(conn, reason="test")
            repo.record_human_response(
                conn,
                attention_id=attention_id,
                response_type="authorised_existing",
                statement="Authorised",
                source_ids=["SRC-001"],
            )

            row = conn.execute(
                "SELECT * FROM operator_responses WHERE attention_id=?",
                (attention_id,),
            ).fetchone()
            assert row is not None
            assert str(row["response_type"]) == "authorised_existing"
            assert "SRC-001" in str(row["source_ids"])
        finally:
            conn.close()


class TestRunStop:

    def test_cancel_run_marks_cancelled(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            run_id = "test-run-001"
            repo.upsert_run(conn, run_id, "running", "test run")

            result = repo.cancel_run(conn, run_id=run_id, cancelled_by="operator", cancel_reason="test stop")
            assert result.get("cancelled") is True

            run = conn.execute("SELECT state, cancelled_by, cancel_reason FROM runs WHERE run_id=?", (run_id,)).fetchone()
            assert run is not None
            assert run["state"] == "cancelled"
            assert run["cancelled_by"] == "operator"
        finally:
            conn.close()

    def test_cancel_run_revokes_live(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            run_id = "test-run-002"
            repo.upsert_run(conn, run_id, "running", "test run")

            repo.cancel_run(conn, run_id=run_id, cancelled_by="operator", cancel_reason="revoke live", revoke_live=True)

            run = conn.execute("SELECT live_provider_permission_revoked FROM runs WHERE run_id=?", (run_id,)).fetchone()
            assert run is not None
            assert run["live_provider_permission_revoked"] == 1
        finally:
            conn.close()

    def test_cancel_run_does_not_affect_other_runs(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            repo.upsert_run(conn, "run-a", "running", "run a")
            repo.upsert_run(conn, "run-b", "running", "run b")

            repo.cancel_run(conn, run_id="run-a", cancelled_by="operator", cancel_reason="stop a")

            run_b = conn.execute("SELECT state FROM runs WHERE run_id='run-b'").fetchone()
            assert run_b is not None
            assert run_b["state"] == "running"
        finally:
            conn.close()

    def test_cancel_non_existent_run(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            result = repo.cancel_run(conn, run_id="non-existent", cancelled_by="operator", cancel_reason="test")
            assert result is not None
        finally:
            conn.close()

    def test_check_run_cancelled(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            repo.upsert_run(conn, "run-1", "cancelled", "cancelled run")
            repo.upsert_run(conn, "run-2", "running", "running run")

            result1 = repo.check_run_cancelled(conn, run_id="run-1")
            assert result1 is not None

            result2 = repo.check_run_cancelled(conn, run_id="run-2")
            assert result2 is None
        finally:
            conn.close()

    def test_check_run_live_allowed(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            repo.upsert_run(conn, "run-1", "running", "ok")
            repo.upsert_run(conn, "run-2", "cancelled", "cancelled", matter_scope=MATTER)

            allowed = repo.check_run_live_allowed(conn, run_id="run-1")
            assert allowed is True

            not_allowed = repo.check_run_live_allowed(conn, run_id="run-2")
            assert not_allowed is False
        finally:
            conn.close()


class TestContinuationManagement:

    def test_register_and_cancel_continuation(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            repo.upsert_run(conn, "run-cont-1", "running", "test")

            cid = repo.register_continuation(
                conn,
                run_id="run-cont-1",
                matter_scope=MATTER,
                command="python -m atticus.cli run-free-loop --db test.db",
                wake_at="2099-01-01T00:00:00Z",
            )
            assert cid is not None
            assert cid.startswith("cnt-")

            cancelled = repo.cancel_continuation(conn, continuation_id=cid)
            assert cancelled is True

            row = conn.execute("SELECT status FROM continued_commands WHERE continuation_id=?", (cid,)).fetchone()
            assert row is not None
            assert row["status"] == "cancelled"
        finally:
            conn.close()

    def test_cancel_continuations_for_run(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            repo.upsert_run(conn, "run-cont-2", "running", "test")

            cid1 = repo.register_continuation(conn, run_id="run-cont-2", matter_scope=MATTER, command="cmd1", wake_at="2099-01-01T00:00:00Z")
            cid2 = repo.register_continuation(conn, run_id="run-cont-2", matter_scope=MATTER, command="cmd2", wake_at="2099-01-01T00:00:00Z")

            cancelled = repo.cancel_continuations_for_run(conn, run_id="run-cont-2")
            assert len(cancelled) == 2

            rows = conn.execute(
                "SELECT status FROM continued_commands WHERE run_id='run-cont-2'"
            ).fetchall()
            for row in rows:
                assert row["status"] == "cancelled"
        finally:
            conn.close()


class TestProgressSignatures:

    def test_record_new_progress_signature(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            repo.upsert_run(conn, "run-prog-1", "running", "test")
            result = repo.record_progress_signature(
                conn,
                run_id="run-prog-1",
                signature="missing_final_quality_gate|existing_task|repair_tick_continue|next_action_planning",
            )
            assert result.get("new") is True
            assert result.get("attempt_count") == 1
        finally:
            conn.close()

    def test_increment_existing_progress_signature(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            repo.upsert_run(conn, "run-prog-2", "running", "test")
            sig = "missing_final_quality_gate|no_progress"

            result1 = repo.record_progress_signature(conn, run_id="run-prog-2", signature=sig)
            assert result1.get("attempt_count") == 1

            result2 = repo.record_progress_signature(conn, run_id="run-prog-2", signature=sig)
            assert result2.get("attempt_count") == 2
            assert result2.get("new") is False
        finally:
            conn.close()


class TestNextActionRouting:

    def test_proof_citation_repair_routes_to_scheduler(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            # Add all prerequisite certifications so next-resume-action can reach attention routing
            for cert in ["source_inventory", "extraction_coverage", "evidence_registry", 
                         "production_mapping", "chronology_citations", "issue_route_map",
                         "authority_map", "draft_preparation", "hostile_review",
                         "privacy_redaction_audit", "citation_audit", "final_quality_gate"]:
                conn.execute(
                    "INSERT OR IGNORE INTO certifications (certification_id, subject_type, subject_id, certification_type, status, validator, validation_result_id, evidence_json, created_at) VALUES (?, 'matter', ?, ?, 'active', 'test', 0, '{}', ?)",
                    (f"cert-{cert}", MATTER, cert, utc_now()),
                )
            conn.commit()
            
            add_human_attention(
                conn,
                routed_lane="proof_citation_repair",
                reason="proof_citation_repair: worker output cited impermissible material",
                plain_question="",
            )
            next_action = next_resume_action(conn, MATTER)
            # Proof citation repair should route to scheduler, not operator
            assert next_action.get("owner") not in ("operator",), \
                f"Got owner={next_action.get('owner')}. Proof citation should not route to operator"
        finally:
            conn.close()

    def test_validation_failure_routes_to_scheduler(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            for cert in ["source_inventory", "extraction_coverage", "evidence_registry", 
                         "production_mapping", "chronology_citations", "issue_route_map",
                         "authority_map", "draft_preparation", "hostile_review",
                         "privacy_redaction_audit", "citation_audit", "final_quality_gate"]:
                conn.execute(
                    "INSERT OR IGNORE INTO certifications (certification_id, subject_type, subject_id, certification_type, status, validator, validation_result_id, evidence_json, created_at) VALUES (?, 'matter', ?, ?, 'active', 'test', 0, '{}', ?)",
                    (f"cert-{cert}", MATTER, cert, utc_now()),
                )
            conn.commit()
            add_human_attention(
                conn,
                routed_lane="validation_failure",
                reason="validation_failure: gate failed on citation check",
                plain_question="",
            )
            next_action = next_resume_action(conn, MATTER)
            assert next_action.get("owner") not in ("operator",), \
                f"Got owner={next_action.get('owner')}. Validation failure should not route to operator"
        finally:
            conn.close()

    def test_human_request_operator_blocked(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            for cert in ["source_inventory", "extraction_coverage", "evidence_registry", 
                         "production_mapping", "chronology_citations", "issue_route_map",
                         "authority_map", "draft_preparation", "hostile_review",
                         "privacy_redaction_audit", "citation_audit", "final_quality_gate"]:
                conn.execute(
                    "INSERT OR IGNORE INTO certifications (certification_id, subject_type, subject_id, certification_type, status, validator, validation_result_id, evidence_json, created_at) VALUES (?, 'matter', ?, ?, 'active', 'test', 0, '{}', ?)",
                    (f"cert-{cert}", MATTER, cert, utc_now()),
                )
            conn.commit()
            add_human_attention(
                conn,
                routed_lane="human_request",
                reason="operator decision: NTQ unclear",
                plain_question="Please provide clearer NTQ",
                why_needed="NTQ needed for date evidence",
            )
            next_action = next_resume_action(conn, MATTER)
            assert next_action.get("type") in ("human_attention",), \
                f"Expected human_attention, got {next_action.get('type')}"
        finally:
            conn.close()

    def test_final_gate_operator_blocked_excludes_internal(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            for cert in ["source_inventory", "extraction_coverage", "evidence_registry",
                         "production_mapping", "chronology_citations", "issue_route_map",
                         "authority_map", "draft_preparation", "hostile_review",
                         "privacy_redaction_audit", "citation_audit"]:
                conn.execute(
                    """INSERT OR IGNORE INTO certifications (certification_id, subject_type, subject_id, certification_type, status, validator, created_at)
                       VALUES (?, 'matter', ?, ?, 'active', 'test', ?)""",
                    (f"cert-{cert}", MATTER, cert, utc_now()),
                )

            add_human_attention(
                conn,
                routed_lane="orchestrator_attention",
                reason="incomplete task dependency: test",
                plain_question="",
            )

            conn.commit()
            readiness = final_gate_readiness(conn, MATTER)
            assert not any(
                str(b.get("routed_lane", "")).startswith("human_request")
                for b in (readiness.get("blocked_reasons") or [])
                if str(b.get("type", "")) == "open_human_attention"
            ), "orchestrator-routed attention should not appear as human_request lane in blocked_reasons"
        finally:
            conn.close()


class TestOperatorControlPanel:

    def test_control_panel_routes_real_human_question_to_agent_packet(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            for cert in ["source_inventory", "extraction_coverage", "evidence_registry",
                         "production_mapping", "chronology_citations", "issue_route_map",
                         "authority_map", "draft_preparation", "hostile_review",
                         "privacy_redaction_audit", "citation_audit", "final_quality_gate"]:
                conn.execute(
                    "INSERT OR IGNORE INTO certifications (certification_id, subject_type, subject_id, certification_type, status, validator, validation_result_id, evidence_json, created_at) VALUES (?, 'matter', ?, ?, 'active', 'test', 0, '{}', ?)",
                    (f"cert-panel-{cert}", MATTER, cert, utc_now()),
                )
            conn.commit()
            attention_id = add_human_attention(
                conn,
                routed_lane="human_request",
                reason="operator decision: clearer tenancy agreement needed",
                plain_question="Please upload the tenancy agreement or confirm best available evidence is enough.",
                why_needed="The harness needs source-backed tenancy terms before final use.",
                acceptable_responses=["upload_document", "provided_best_available", "proceed_without_source"],
            )

            panel = build_operator_control_panel(conn, db_path=db, matter_scope=MATTER, output_dir=str(tmp_path), live_approved=False)
            packet = panel["agent_packet"]

            assert panel["state"] == "needs_human_answer"
            assert packet["needs_human"] is True
            assert packet["may_run_without_asking_human"] is False
            assert packet["question"]["attention_id"] == attention_id
            assert "Please upload" in str(packet["question"]["question"])
        finally:
            conn.close()

    def test_control_panel_materializes_agent_continuation_when_safe(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            panel = build_operator_control_panel(conn, db_path=db, matter_scope=MATTER, output_dir=str(tmp_path), live_approved=True)
            packet = panel["agent_packet"]

            assert packet["needs_human"] is False
            assert str(panel["next_action"]["resume_command"]).startswith("python -m atticus.cli coordinator")
            assert db in str(panel["next_action"]["resume_command"])
        finally:
            conn.close()

    def test_render_control_panel_is_human_readable(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            panel = build_operator_control_panel(conn, db_path=db, matter_scope=MATTER)
            rendered = render_operator_control_panel(panel)
            assert "Atticus Control Panel" in rendered
            assert "Next action:" in rendered
        finally:
            conn.close()

    def test_live_provider_requirement_is_not_human_interruption_gate(self) -> None:
        packet = build_agent_handoff_packet(
            matter_scope=MATTER,
            next_action={
                "type": "supervisor_tick",
                "owner": "scheduler",
                "reason": "runnable tasks remain",
                "resume_command": "ATTICUS_ENABLE_LIVE_OPENROUTER=1 python -m atticus.cli run-free-loop --db DB --matter test-matter --runtime openrouter --allow-live",
            },
            operator_request=None,
            live_approved=False,
        )

        assert packet["requires_live_provider"] is True
        assert packet["live_provider_gate"] == "not_a_human_blocker"
        assert packet["may_run_without_asking_human"] is True


class TestCLICommands:

    def test_human_request_show_empty(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from atticus.cli import main
        db = str(tmp_path / "atticus.sqlite3")
        repo.initialize_database(db)
        repo.ensure_matter(sqlite3.connect(db), MATTER, "Test")

        exit_code = main(["human-request", "show", "--db", db, "--matter", MATTER, "--json"])
        assert exit_code == 0

    def test_human_request_next_none(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from atticus.cli import main
        db = str(tmp_path / "atticus.sqlite3")
        repo.initialize_database(db)
        repo.ensure_matter(sqlite3.connect(db), MATTER, "Test")

        exit_code = main(["human-request", "next", "--db", db, "--matter", MATTER, "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "none" in captured.out or "no human requests" in captured.out.lower()

    def test_run_stop_dry_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from atticus.cli import main
        db = str(tmp_path / "atticus.sqlite3")
        repo.initialize_database(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        repo.upsert_run(conn, "test-run-stop", "running", "test run for stop")
        conn.close()

        exit_code = main(["run", "stop", "--db", db, "--run-id", "test-run-stop", "--json"])
        assert exit_code in (0, 2)

    def test_run_stop_current_no_active_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from atticus.cli import main
        db = str(tmp_path / "atticus.sqlite3")
        repo.initialize_database(db)
        repo.ensure_matter(sqlite3.connect(db), MATTER, "Test")

        exit_code = main(["run", "stop-current", "--db", db, "--matter", MATTER, "--json"])
        assert exit_code == 2
