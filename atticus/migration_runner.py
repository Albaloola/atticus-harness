"""Version-tracked migration runner for Atticus harness data stores.

Ported from Claude Code's sync migration pattern in `main.tsx`:
    - A global ``CURRENT_MIGRATION_VERSION`` counter drives the pipeline.
    - On startup, ``run_migrations(db_path)`` inspects the DB for the
      currently-applied version and runs every pending migration in order.
    - Each migration is a ``MigrationFunc`` — a callable receiving the
      database path and mutating it as needed.
    - Completed migrations are skipped on subsequent runs.

Usage::

    from atticus.migration_runner import run_migrations

    run_migrations("path/to/atticus_state.db")
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Final

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CURRENT_MIGRATION_VERSION: Final[int] = 1
"""Global ceiling: only migrations numbered ≤ this constant are considered."""

_MIGRATIONS_TABLE: Final[str] = "_migrations"
"""Table that records which migrations have been applied."""

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

MigrationFunc = Callable[[str], None]
"""Signature of a single migration function.  Receives the database path."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

MIGRATIONS: dict[int, MigrationFunc] = {}
"""Mapping of ``version_number → migration_function``.
Populated automatically by the ``@register_migration`` decorator."""


def register_migration(version: int) -> Callable[[MigrationFunc], MigrationFunc]:
    """Decorator: register the decorated function as migration *version*.

    Example::

        @register_migration(1)
        def initial_harness_setup(db_path: str) -> None:
            ...
    """

    def _decorator(fn: MigrationFunc) -> MigrationFunc:
        if version in MIGRATIONS:
            raise ValueError(
                f"migration version {version} is already registered "
                f"(existing={MIGRATIONS[version].__name__})"
            )
        MIGRATIONS[version] = fn
        return fn

    return _decorator


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_migrations(db_path: str) -> None:
    """Run every pending migration against *db_path*, in ascending version order.

    Algorithm:
        1. Open a connection (WAL mode).
        2. Read the current applied version from ``_migrations`` (default 0).
        3. Collect registered migrations whose ``version > current`` and
           ``version ≤ CURRENT_MIGRATION_VERSION``.
        4. Apply each in its own savepoint-protected transaction.
        5. After each success, record the version in ``_migrations``.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        _ensure_migrations_table(conn)

        current = _read_current_version(conn)
        pending = sorted(
            v
            for v in MIGRATIONS
            if current < v <= CURRENT_MIGRATION_VERSION
        )

        if not pending:
            _logger.debug(
                "no pending migrations (current=%d, ceiling=%d)",
                current,
                CURRENT_MIGRATION_VERSION,
            )
            return

        conn.execute("BEGIN")

        for version in pending:
            mig_name = MIGRATIONS[version].__name__
            _logger.info(
                "applying migration %d/%d: %s",
                version,
                CURRENT_MIGRATION_VERSION,
                mig_name,
            )

            sp_name = f"_migration_{version}"
            conn.execute(f"SAVEPOINT {sp_name}")

            try:
                MIGRATIONS[version](db_path)
            except Exception:
                _logger.exception(
                    "migration %d (%s) failed — rolling back savepoint",
                    version,
                    mig_name,
                )
                conn.execute(f"ROLLBACK TO {sp_name}")
                conn.execute(f"RELEASE {sp_name}")
                conn.execute("ROLLBACK")
                raise

            conn.execute(f"RELEASE {sp_name}")
            _record_migration(conn, version)

        conn.execute("COMMIT")

        _logger.info(
            "migrations complete: applied %d (versions %s)",
            len(pending),
            ", ".join(str(v) for v in pending),
        )

    except Exception:
        # Roll back anything that wasn't committed
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_current_migration_version(db_path: str) -> int:
    """Return the last migration version applied to *db_path*, or 0."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        _ensure_migrations_table(conn)
        return _read_current_version(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_MIGRATIONS_TABLE} (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
        """
    )


def _read_current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        f"SELECT MAX(version) FROM {_MIGRATIONS_TABLE}"
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(str(row[0]))


def _record_migration(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO {_MIGRATIONS_TABLE} (version, applied_at) VALUES (?, ?)",
        (version, datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )


# ===========================================================================
# Default migrations
# ===========================================================================


@register_migration(1)
def initial_harness_setup(db_path: str) -> None:
    """Migration 1: ensure the default Atticus config exists on disk.

    Creates ``~/.atticus/config.json`` with factory defaults if the file is
    missing.  Existing configs are left untouched so operator edits are
    preserved across harness updates.
    """
    from pathlib import Path

    config_dir = Path.home() / ".atticus"
    config_path = config_dir / "config.json"

    if config_path.exists():
        _logger.debug("config already exists at %s — skipping", config_path)
        return

    config_dir.mkdir(parents=True, exist_ok=True)

    default_config: dict[str, object] = {
        "version": 2,
        "models": {
            "flash_worker": "deepseek/deepseek-v4-flash",
            "pro_orchestrator": "deepseek/deepseek-v4-pro",
            "codex_exact": "gpt-5.5",
        },
        "skills": {},
        "providers": {
            "openrouter_failover_enabled": True,
            "openrouter_max_failed_cycles": 5,
            "openrouter_cooldown_seconds": 300.0,
            "openrouter_timeout_seconds": 180.0,
            "allow_live_providers": False,
        },
        "budget": {},
    }

    config_path.write_text(
        json.dumps(default_config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    _logger.info("created default config at %s", config_path)
