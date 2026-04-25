"""Reducer-only canonical write guard."""

from __future__ import annotations

import sqlite3

from atticus.scheduler.lease import LeaseError, require_active_lease


class CanonicalWriteDenied(PermissionError):
    """Raised when a non-reducer attempts a canonical write."""


REDUCER_ROLES = {"reducer", "canonical_writer"}


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
    if conn is not None:
        if lease_id is None:
            raise CanonicalWriteDenied("canonical write denied: active reducer lease required")
        try:
            require_active_lease(conn, lease_id=lease_id, task_id=task_id)
        except LeaseError as exc:
            raise CanonicalWriteDenied(f"canonical write denied: {exc}") from exc
