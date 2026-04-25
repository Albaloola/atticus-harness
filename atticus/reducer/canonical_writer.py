"""Canonical write facade guarded to reducer roles."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from atticus.validation.canonical_write_guard import assert_canonical_write_allowed


def write_canonical_text(
    *,
    conn: sqlite3.Connection,
    lease_id: str,
    task_id: str,
    writer_role: str,
    target_path: str,
    text: str,
) -> None:
    assert_canonical_write_allowed(
        writer_role=writer_role,
        target_path=target_path,
        conn=conn,
        lease_id=lease_id,
        task_id=task_id,
    )
    Path(target_path).write_text(text, encoding="utf-8")
