"""Canonical write facade guarded to reducer roles."""

from __future__ import annotations

import os
import sqlite3
from uuid import uuid4

from atticus.validation.canonical_write_guard import assert_canonical_write_allowed, resolve_canonical_filesystem_path


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
    path = resolve_canonical_filesystem_path(conn, target_path=target_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            _ = handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()
