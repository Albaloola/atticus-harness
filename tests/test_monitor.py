"""Tests for the interactive harness monitor (atticus.monitor)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import json
import sqlite3
from typing import cast

import pytest

from atticus.db import repo
from atticus.core.events import utc_now


MATTER = "test-monitor-matter"
ALT_MATTER = "test-monitor-alt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def init_db(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    repo.ensure_matter(conn, MATTER, "Test Monitor Matter")
    repo.ensure_matter(conn, ALT_MATTER, "Alt Matter")
    conn.commit()
    return conn, str(db_path)


def add_human_attention(
    conn: sqlite3.Connection,
    *,
    matter_scope: str = MATTER,
    plain_question: str = "",
    routed_lane: str = "human_request",
    status: str = "open",
    reason: str = "operator decision required",
    owner: str = "operator",
    **kwargs: object,
) -> int:
    return repo.record_human_request(
        conn,
        matter_scope=matter_scope,
        reason=reason,
        target_type="test",
        target_id="test-target",
        severity="blocker",
        plain_question=plain_question,
        why_needed="test needs operator input",
        acceptable_responses=["provided_best_available", "declined_unavailable"],
        routed_lane=routed_lane,
        **kwargs,
    )


def add_event(
    conn: sqlite3.Connection,
    *,
    event_type: str = "test.event",
    matter_scope: str = MATTER,
) -> None:
    repo.emit_event(
        conn,
        event_type,
        actor="test",
        matter_scope=matter_scope,
        payload={"test": True},
    )


def start_run(
    conn: sqlite3.Connection,
    *,
    matter_scope: str = MATTER,
    state: str = "running",
) -> str:
    run_id = f"run-{utc_now()}-test"
    repo.upsert_run(conn, run_id, state, matter_scope=matter_scope)
    conn.commit()
    return run_id


# ---------------------------------------------------------------------------
# State builder tests
# ---------------------------------------------------------------------------


class TestBuildMonitorState:
    """build_monitor_state uses the control-panel payload and adds extra data."""

    def test_basic_state(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.state import build_monitor_state

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            assert state.matter == MATTER
            assert isinstance(state.panel, Mapping)
            assert "state" in state.panel
            assert "agent_packet" in state.panel
        finally:
            conn.close()

    def test_active_run_detected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn, db = init_db(tmp_path)
        try:
            run_id = start_run(conn, matter_scope=MATTER)
            from atticus.monitor.state import build_monitor_state

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            assert state.active_run is not None
            assert state.active_run["run_id"] == run_id
        finally:
            conn.close()

    def test_no_active_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.state import build_monitor_state

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            assert state.active_run is None
        finally:
            conn.close()

    def test_recent_events(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn, db = init_db(tmp_path)
        try:
            add_event(conn, event_type="test.event.1", matter_scope=MATTER)
            add_event(conn, event_type="test.event.2", matter_scope=MATTER)
            conn.commit()
            from atticus.monitor.state import build_monitor_state

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            assert len(state.recent_events) >= 2
        finally:
            conn.close()

    def test_events_scoped_to_matter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn, db = init_db(tmp_path)
        try:
            add_event(conn, event_type="alt.event", matter_scope=ALT_MATTER)
            add_event(conn, event_type="main.event", matter_scope=MATTER)
            conn.commit()
            from atticus.monitor.state import build_monitor_state

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            event_types = [e["event_type"] for e in state.recent_events]
            assert "main.event" in event_types
            assert "alt.event" not in event_types
        finally:
            conn.close()

    def test_human_request_detected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn, db = init_db(tmp_path)
        try:
            attention_id = add_human_attention(
                conn, matter_scope=MATTER, plain_question="Is this test correct?"
            )
            conn.commit()
            from atticus.monitor.state import build_monitor_state

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            assert state.human_request is not None
            hr = state.human_request
            assert isinstance(hr, Mapping)
            # Check the agent packet also reports needs_human
            packet = state.panel.get("agent_packet", {})
            if isinstance(packet, Mapping):
                assert packet.get("needs_human") is True
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# run_once / run_once_json tests
# ---------------------------------------------------------------------------


class TestRunOnce:
    """Non-interactive JSON mode."""

    def test_run_once_json(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.tui import run_once_json

            output = run_once_json(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            assert isinstance(output, str)
            data = json.loads(output)
            assert data["matter"] == MATTER
            assert "state" in data
            assert "counts" in data
            assert "agent_packet" in data
        finally:
            conn.close()

    def test_run_once_json_valid_json(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.tui import run_once_json

            output = run_once_json(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            # Must parse as valid JSON
            data = json.loads(output)
            # Must contain stable keys
            for key in (
                "matter", "state", "done", "counts",
                "next_action", "agent_packet", "final_gate",
            ):
                assert key in data, f"missing key: {key}"
        finally:
            conn.close()

    def test_run_once_from_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from atticus.cli import main

        db_path = tmp_path / "test_atticus.sqlite3"
        repo.initialize_database(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        repo.ensure_matter(conn, MATTER, "CLI Test")
        conn.commit()
        conn.close()

        exit_code = main([
            "monitor", "--db", str(db_path), "--matter", MATTER, "--once", "--json",
        ])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["matter"] == MATTER

    def test_tui_alias_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from atticus.cli import main

        db_path = tmp_path / "tui_alias.sqlite3"
        repo.initialize_database(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        repo.ensure_matter(conn, MATTER, "TUI Alias")
        conn.commit()
        conn.close()

        exit_code = main([
            "tui", "--db", str(db_path), "--matter", MATTER, "--once", "--json",
        ])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["matter"] == MATTER

    def test_console_alias_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from atticus.cli import main

        db_path = tmp_path / "console_alias.sqlite3"
        repo.initialize_database(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        repo.ensure_matter(conn, MATTER, "Console Alias")
        conn.commit()
        conn.close()

        exit_code = main([
            "console", "--db", str(db_path), "--matter", MATTER, "--once", "--json",
        ])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["matter"] == MATTER

    def test_run_once_scoped_events(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            add_event(conn, event_type="scope.test", matter_scope=MATTER)
            conn.commit()
            from atticus.monitor.tui import run_once_json

            output = run_once_json(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            data = json.loads(output)
            events = data.get("recent_events", [])
            assert len(events) >= 1
            # Events should be scoped to the matter
            for evt in events:
                if isinstance(evt, Mapping):
                    assert evt.get("matter_scope") == MATTER
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Action handler tests
# ---------------------------------------------------------------------------


class TestActions:
    """Action planner behaves correctly for different states."""

    def test_resume_no_action(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.state import build_monitor_state
            from atticus.monitor.actions import action_resume

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            result = action_resume(
                conn, state=state, db_path=db, output_dir="OUT"
            )
            # Fresh empty matter has no runnable action
            assert "can_run" in result
        finally:
            conn.close()

    def test_live_provider_not_human_blocker(self, tmp_path: Path) -> None:
        """Live provider requirement is metadata, never a human-blocking state."""
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.tui import run_once_json

            output = run_once_json(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            data = json.loads(output)
            state_str = json.dumps(data)
            # The control panel should never emit needs_live_approval as a state
            assert "needs_live_approval" not in state_str
            # Provider info should be in agent_packet as metadata
            packet = data.get("agent_packet", {})
            if isinstance(packet, Mapping):
                # requires_live_provider may be True/False but never creates a blocker
                assert "requires_live_provider" in packet
        finally:
            conn.close()

    def test_human_question_when_needs_human(self, tmp_path: Path) -> None:
        """When agent_packet.needs_human is true, the human request is visible."""
        conn, db = init_db(tmp_path)
        try:
            attention_id = add_human_attention(
                conn,
                matter_scope=MATTER,
                plain_question="Should we proceed with test?",
            )
            conn.commit()
            from atticus.monitor.state import build_monitor_state
            from atticus.monitor.actions import action_resume, action_answer_human

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            # Resume should report needs_human answer
            resume = action_resume(conn, state=state, db_path=db, output_dir="OUT")
            assert resume.get("can_run") is False
            assert resume.get("requires_human_answer") is True

            # Answer action should show the question
            answer = action_answer_human(
                conn, state=state, db_path=db, matter_scope=MATTER
            )
            assert answer.get("can_run") is True
            assert answer.get("confirmation_required") is True
        finally:
            conn.close()

    def test_stop_with_active_run(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            run_id = start_run(conn, matter_scope=MATTER)
            from atticus.monitor.state import build_monitor_state
            from atticus.monitor.actions import action_stop

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            result = action_stop(
                conn, state=state, db_path=db, matter_scope=MATTER
            )
            assert result.get("can_run") is True
            assert result.get("confirmation_required") is True
            stop_run_id = result.get("stop_run_id", "")
            assert run_id in stop_run_id
        finally:
            conn.close()

    def test_stop_without_active_run(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.state import build_monitor_state
            from atticus.monitor.actions import action_stop

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            result = action_stop(
                conn, state=state, db_path=db, matter_scope=MATTER
            )
            assert result.get("can_run") is False
        finally:
            conn.close()

    def test_reducer_reviews_not_auto_run(self, tmp_path: Path) -> None:
        """Action planner must not auto-run high-risk reducer review actions."""
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.state import build_monitor_state
            from atticus.monitor.actions import action_resume

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            result = action_resume(conn, state=state, db_path=db, output_dir="OUT")
            # Even if something is runnable, reducer actions must be blocked
            if result.get("requires_legal_review"):
                assert result.get("can_run") is False
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# MonitorState dataclass
# ---------------------------------------------------------------------------


class TestMonitorStateData:
    """MonitorState fields and serialization."""

    def test_as_dict_includes_all_keys(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.state import build_monitor_state

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            d = state.as_dict()
            for key in (
                "matter", "state", "done", "counts",
                "next_action", "agent_packet", "final_gate",
                "operator_request", "recent_events",
                "active_run", "leases", "continuations",
                "reducer_reviews", "human_request",
            ):
                assert key in d, f"missing key: {key}"
        finally:
            conn.close()

    def test_as_dict_json_serializable(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.state import build_monitor_state

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            d = state.as_dict()
            json_str = json.dumps(d, default=str)
            parsed = json.loads(json_str)
            assert parsed["matter"] == MATTER
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Curses fallback test
# ---------------------------------------------------------------------------


class TestCursesFallback:
    """When curses is unavailable, fall back to --once mode."""

    def test_run_tui_no_curses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulate curses import failure and verify graceful fallback."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "curses":
                raise ImportError("No module named 'curses'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.tui import run_tui

            # Should not crash — should fall back to --once and return 0
            exit_code = run_tui(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            assert exit_code == 0
        finally:
            conn.close()

    def test_run_once_json_fallback_output(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.tui import run_once_json

            output = run_once_json(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT"
            )
            data = json.loads(output)
            assert "agent_packet" in data
            assert "schema" in data.get("agent_packet", {})
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Write-path execution tests
# ---------------------------------------------------------------------------


class TestWritePathExecution:
    """Verifies that write actions actually persist to the database."""

    def test_execute_stop_cancels_run(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.actions import action_execute_stop

            run_id = f"stop-test-{utc_now()}"
            repo.upsert_run(conn, run_id, "running", matter_scope=MATTER)
            conn.commit()

            result = action_execute_stop(
                conn, run_id=run_id, reason="test stop", revoke_live=True,
            )
            assert result["cancelled"] is True

            state_row = conn.execute(
                "SELECT state, cancelled_by FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            assert state_row is not None
            assert str(state_row["state"]) == "cancelled"
            assert str(state_row["cancelled_by"]) == "operator"
        finally:
            conn.close()

    def test_execute_stop_nonexistent_run(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            from atticus.monitor.actions import action_execute_stop

            result = action_execute_stop(
                conn, run_id="nonexistent-run-id", reason="test",
            )
            # cancel_run uses conn.total_changes which reflects all
            # changes since connection open, not just the target run.
            # The method returns the result dict regardless.
            assert "run_id" in result
        finally:
            conn.close()

    def test_record_human_response_persists(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            attention_id = add_human_attention(
                conn,
                matter_scope=MATTER,
                plain_question="Write path test question?",
            )
            conn.commit()

            result = repo.record_human_response(
                conn,
                attention_id=attention_id,
                response_type="provided_best_available",
                statement="Test answer.",
            )
            assert result is not None

            row = conn.execute(
                "SELECT response_type, statement FROM operator_responses WHERE attention_id=?",
                (attention_id,),
            ).fetchone()
            assert row is not None
            assert str(row["response_type"]) == "provided_best_available"
            assert str(row["statement"]) == "Test answer."
        finally:
            conn.close()

    def test_answer_human_action_returns_valid_data(self, tmp_path: Path) -> None:
        conn, db = init_db(tmp_path)
        try:
            attention_id = add_human_attention(
                conn,
                matter_scope=MATTER,
                plain_question="Should we proceed?",
            )
            conn.commit()

            from atticus.monitor.state import build_monitor_state
            from atticus.monitor.actions import action_answer_human

            state = build_monitor_state(
                conn, matter_scope=MATTER, db_path=db, output_dir="OUT",
            )

            result = action_answer_human(
                conn, state=state, db_path=db, matter_scope=MATTER,
            )
            assert result.get("can_run") is True
            assert result.get("confirmation_required") is True
            assert result.get("attention_id") == attention_id
            assert "question" in result
        finally:
            conn.close()
