"""Migration registry for Atticus ledger schema upgrades.

Each migration is a callable that takes a writable sqlite3.Connection and
transforms the database from version N to version N+1. The registry
supports ordered apply and optional rollback.
"""

from __future__ import annotations

from collections.abc import Callable
import sqlite3
import threading


MigrationFn = Callable[[sqlite3.Connection], None]
RollbackFn = Callable[[sqlite3.Connection], None] | None


class _Migration:
    def __init__(
        self,
        version_from: int,
        version_to: int,
        apply_fn: MigrationFn,
        rollback_fn: RollbackFn = None,
        description: str = "",
    ):
        self.version_from = version_from
        self.version_to = version_to
        self.apply_fn = apply_fn
        self.rollback_fn = rollback_fn
        self.description = description


_registry: list[_Migration] = []
_loaded = False
_loaded_lock = threading.Lock()


def register(
    version_from: int,
    version_to: int,
    apply_fn: MigrationFn | None = None,
    *,
    rollback_fn: RollbackFn = None,
    description: str = "",
) -> Callable[[MigrationFn], MigrationFn] | None:
    """Register a migration, optionally as a decorator.

    Usage as function:
        register(11, 12, my_apply_fn, rollback_fn=my_rollback_fn)

    Usage as decorator:
        @register(11, 12, description="Add matter_scope to runs")
        def migrate_v11_to_v12(conn):
            ...
    """

    def decorator(fn: MigrationFn) -> MigrationFn:
        _registry.append(_Migration(version_from, version_to, fn, rollback_fn, description))
        return fn

    if apply_fn is not None:
        decorator(apply_fn)
        return None
    return decorator


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _loaded_lock:
        if _loaded:
            return
        _loaded = True
        import atticus.db.migrations.V11_to_V12 as _  # noqa: F401


def pending_migrations(current_version: int, target_version: int) -> list[_Migration]:
    """Return ordered list of migrations from current_version to target_version."""
    _ensure_loaded()
    if current_version >= target_version:
        return []
    by_from: dict[int, _Migration] = {m.version_from: m for m in _registry}
    pending: list[_Migration] = []
    v = current_version
    while v < target_version:
        m = by_from.get(v)
        if m is None:
            raise ValueError(
                f"no migration registered from version {v} to {v + 1} "
                f"(target {target_version}); registered: {sorted(by_from.keys())}"
            )
        pending.append(m)
        v = m.version_to
    return pending


def apply_migrations(conn: sqlite3.Connection, target_version: int) -> list[str]:
    """Apply all pending migrations to reach target_version.

    Returns a list of migration descriptions that were applied.
    Returns empty list if already at target_version.
    """
    current = _read_schema_version(conn)
    pending = pending_migrations(current, target_version)
    applied: list[str] = []
    for migration in pending:
        _write_schema_version(conn, migration.version_from)
        migration.apply_fn(conn)
        _write_schema_version(conn, migration.version_to)
        applied.append(
            migration.description or f"V{migration.version_from}→V{migration.version_to}"
        )
    return applied


def _read_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    return int(row["value"])


def _write_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )
