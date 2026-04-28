from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
import json
import sqlite3

from atticus.cli import main as cli_main
from atticus.db import repo
from atticus.hooks import run_hooks


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_session_records_user_message_before_provider_references(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        session_id = repo.create_session(conn, matter_scope="alpha", title="Urgent chronology")
        message_id = repo.record_session_message(
            conn,
            session_id=session_id,
            role="user",
            content={"text": "Build a chronology but do not send anything."},
        )
        message = conn.execute("SELECT role, provider_run_id FROM session_messages WHERE session_message_id = ?", (message_id,)).fetchone()
        event_row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type = 'session.message_recorded'").fetchone()
        assert message is not None
        assert event_row is not None
        event_count = event_row["n"]

    assert message["role"] == "user"
    assert message["provider_run_id"] is None
    assert event_count == 1


def test_session_cli_list_show_export(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        session_id = repo.create_session(conn, matter_scope="alpha", title="Session CLI")
        _ = repo.record_session_message(conn, session_id=session_id, role="user", content={"text": "hello"})

    assert cli_main(["session", "list", "--db", str(db_path), "--matter", "alpha"]) == 0
    listed = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    sessions = cast(list[Mapping[str, object]], listed["sessions"])
    assert sessions[0]["session_id"] == session_id

    assert cli_main(["session", "show", session_id, "--db", str(db_path)]) == 0
    shown = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    shown_session = cast(Mapping[str, object], shown["session"])
    assert shown_session["title"] == "Session CLI"

    assert cli_main(["session", "export", session_id, "--db", str(db_path)]) == 0
    exported = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    messages = cast(list[Mapping[str, object]], exported["messages"])
    assert messages[0]["role"] == "user"

    assert cli_main(["session", "resume", session_id, "--db", str(db_path)]) == 0
    resumed = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    resume = cast(Mapping[str, object], resumed["resume"])
    assert resume["provider_replay"] is False


def test_session_list_is_safe_on_older_db_without_session_tables(tmp_path: Path, capsys):
    db_path = tmp_path / "old.sqlite3"
    sqlite3.connect(db_path).close()

    assert cli_main(["session", "list", "--db", str(db_path), "--matter", "alpha"]) == 0
    listed = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    assert listed["sessions"] == []


def test_internal_hooks_log_and_block_external_action_requests(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        outcomes = run_hooks(
            conn,
            event_name="ExternalActionBlocked",
            matter_scope="alpha",
            payload={"action_type": "email", "requested_by": "worker"},
        )
        event_row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type = 'hook.evaluated'").fetchone()
        assert event_row is not None
        event_count = event_row["n"]

    assert outcomes[0].allowed is False
    assert outcomes[0].severity == "blocker"
    assert "external legal actions" in outcomes[0].message
    assert event_count == 1


def test_internal_hooks_warn_on_stale_evidence_and_block_cross_matter_context(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        stale = run_hooks(
            conn,
            event_name="PostContextPack",
            matter_scope="alpha",
            payload={"stale_source_ids": ["src-1"]},
        )
        cross_matter = run_hooks(
            conn,
            event_name="PreWorkOrder",
            matter_scope="alpha",
            payload={"authorized_matter_scope": "beta"},
        )
        hooks = conn.execute("SELECT severity, allowed FROM hook_invocations").fetchall()

    assert stale[0].allowed is True
    assert stale[0].severity == "warning"
    assert cross_matter[0].allowed is False
    severities = sorted(str(row["severity"]) for row in hooks)
    assert severities == ["blocker", "warning"]


def test_internal_hooks_block_final_draft_without_hostile_review(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        outcomes = run_hooks(
            conn,
            event_name="PreReduce",
            matter_scope="alpha",
            payload={"stage": "S9", "task_type": "final_quality_gate", "required_certifications": ["hostile_review"]},
        )

    assert outcomes[0].allowed is False
    assert "hostile review" in outcomes[0].message
