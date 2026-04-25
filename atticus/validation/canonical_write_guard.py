"""Reducer-only canonical write guard."""

from __future__ import annotations

import sqlite3

from atticus.scheduler.lease import LeaseError, require_active_lease


class CanonicalWriteDenied(PermissionError):
    """Raised when a non-reducer attempts a canonical write."""


REDUCER_ROLES = {"reducer", "canonical_writer"}
REDUCER_WORKER_PREFIXES = ("reducer", "atticus-reducer")


def assert_canonical_write_allowed(
    *,
    writer_role: str,
    target_path: str,
    conn: sqlite3.Connection | None = None,
    lease_id: str | None = None,
    task_id: str | None = None,
) -> None:
    if writer_role not in REDUCER_ROLES:
        raise CanonicalWriteDenied(
            f"canonical write denied for role {writer_role!r} to {target_path!r}; reducer role required"
        )
    if conn is None or lease_id is None or task_id is None:
        raise CanonicalWriteDenied("canonical write denied: active reducer lease context required")
    try:
        lease = require_active_lease(conn, lease_id=lease_id, task_id=task_id)
    except LeaseError as exc:
        raise CanonicalWriteDenied(f"canonical write denied: {exc}") from exc
    worker_id = str(lease["worker_id"] or "")
    if not worker_id.startswith(REDUCER_WORKER_PREFIXES):
        raise CanonicalWriteDenied(f"canonical write denied: lease {lease_id} is not held by a reducer worker")
