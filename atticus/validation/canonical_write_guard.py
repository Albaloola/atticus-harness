"""Reducer-only canonical write guard."""

from __future__ import annotations

from pathlib import Path
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
    if str(lease["lease_role"] or "") != "reducer":
        raise CanonicalWriteDenied(f"canonical write denied: lease {lease_id} was not issued for reducer work")
    worker_id = str(lease["worker_id"] or "")
    if not worker_id.startswith(REDUCER_WORKER_PREFIXES):
        raise CanonicalWriteDenied(f"canonical write denied: lease {lease_id} is not held by a reducer worker")
    _assert_canonical_target_shape(target_path)


def resolve_canonical_filesystem_path(conn: sqlite3.Connection, *, target_path: str) -> Path:
    if target_path.startswith("canonical://"):
        raise CanonicalWriteDenied("canonical write denied: canonical:// targets are logical artifact URIs, not filesystem paths")
    _assert_canonical_target_shape(target_path)
    root = _canonical_root(conn)
    raw_path = Path(target_path)
    if raw_path.parts and raw_path.parts[0] == "canonical":
        relative = Path(*raw_path.parts[1:])
    else:
        relative = raw_path
    if str(relative) in {"", "."}:
        raise CanonicalWriteDenied("canonical write denied: filesystem target must be inside the canonical workspace")
    candidate = root / relative
    if candidate.is_symlink():
        raise CanonicalWriteDenied(f"canonical write denied: target is a symlink: {target_path!r}")
    target = candidate.resolve(strict=False)
    try:
        _ = target.relative_to(root)
    except ValueError as exc:
        raise CanonicalWriteDenied(f"canonical write denied: target escapes canonical workspace: {target_path!r}") from exc
    parent = target.parent.resolve(strict=False)
    try:
        _ = parent.relative_to(root)
    except ValueError as exc:
        raise CanonicalWriteDenied(f"canonical write denied: target parent escapes canonical workspace: {target_path!r}") from exc
    return target


def _assert_canonical_target_shape(target_path: str) -> None:
    if target_path.startswith("canonical://"):
        return
    path = Path(target_path)
    if path.is_absolute():
        raise CanonicalWriteDenied(f"canonical write denied: filesystem target must be relative: {target_path!r}")
    if ".." in path.parts:
        raise CanonicalWriteDenied(f"canonical write denied: filesystem target must not contain '..': {target_path!r}")
    if not str(target_path).strip():
        raise CanonicalWriteDenied("canonical write denied: target path is required")


def _canonical_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(str(row["file"] if row is not None and row["file"] else ".")).resolve()
    canonical_dir = db_path.parent.resolve(strict=False) / "canonical"
    if canonical_dir.exists() and canonical_dir.is_symlink():
        raise CanonicalWriteDenied("canonical write denied: canonical workspace root is a symlink")
    return canonical_dir.resolve(strict=False)
