"""Schema verification helpers for long-lived Atticus ledgers."""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3

from atticus.db.schema import DDL, SCHEMA_VERSION


_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_INDEX_RE = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


REQUIRED_TABLES = frozenset(_TABLE_RE.findall(DDL))
REQUIRED_INDEXES = frozenset(_INDEX_RE.findall(DDL)) | {
    "validation_target_idx",
    "human_attention_scope_status_idx",
}

REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "schema_meta": frozenset({"key", "value"}),
    "tasks": frozenset(
        {
            "task_id",
            "matter_scope",
            "status",
            "stage",
            "task_type",
            "source_dependencies_json",
            "artifact_dependencies_json",
            "task_dependencies_json",
            "required_certifications_json",
            "blocked_reasons_json",
            "provider_policy_json",
            "context_pack_id",
            "task_provenance_json",
        }
    ),
    "provider_runs": frozenset(
        {
            "provider_run_id",
            "task_id",
            "run_id",
            "stage",
            "requested_provider",
            "requested_model",
            "actual_provider",
            "actual_model",
            "context_pack_id",
            "context_fingerprint",
            "provider_policy_fingerprint",
            "configured_models_json",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "cache_write_tokens",
            "failover_events_json",
            "cache_telemetry_source",
            "raw_usage_json",
        }
    ),
    "prompt_cache_observations": frozenset(
        {
            "prompt_cache_observation_id",
            "matter_scope",
            "provider_run_id",
            "task_id",
            "context_pack_id",
            "model",
            "system_fingerprint",
            "tools_fingerprint",
            "context_fingerprint",
            "policy_fingerprint",
            "cache_hit_tokens",
            "cache_write_tokens",
            "cache_miss_tokens",
            "possible_cache_break",
            "reason",
        }
    ),
    "human_attention": frozenset(
        {"attention_id", "matter_scope", "target_type", "target_id", "severity", "reason", "status", "created_at"}
    ),
    "error_logs": frozenset(
        {
            "error_log_id",
            "matter_scope",
            "target_type",
            "target_id",
            "error_type",
            "error_signature",
            "message",
            "severity",
            "escalation_level",
            "occurrence_count",
            "consecutive_count",
            "terminal",
            "payload_json",
            "created_at",
        }
    ),
    "maintenance_runs": frozenset(
        {
            "maintenance_run_id",
            "matter_scope",
            "status",
            "trigger_reason",
            "triggered_by",
            "isolation_level",
            "started_at",
            "updated_at",
        }
    ),
    "maintenance_reports": frozenset(
        {
            "maintenance_report_id",
            "maintenance_run_id",
            "matter_scope",
            "summary",
            "diagnostics_json",
            "actions_json",
            "resume_signal_json",
            "created_at",
        }
    ),
    "matter_orchestrators": frozenset(
        {
            "orchestrator_id",
            "matter_scope",
            "status",
            "model_decision_json",
            "last_tick_at",
            "current_goal",
            "failure_count",
            "created_at",
            "updated_at",
        }
    ),
    "orchestrator_events": frozenset(
        {
            "orchestrator_event_id",
            "orchestrator_id",
            "matter_scope",
            "event_type",
            "payload_json",
            "created_at",
        }
    ),
    "work_runs": frozenset(
        {
            "work_run_id",
            "matter_scope",
            "goal",
            "status",
            "active_profile_id",
            "started_at",
            "updated_at",
            "completed_at",
            "resume_token",
            "metadata_json",
        }
    ),
    "work_run_steps": frozenset(
        {
            "work_run_step_id",
            "work_run_id",
            "matter_scope",
            "step_type",
            "task_id",
            "candidate_id",
            "artifact_id",
            "context_pack_id",
            "provider_run_id",
            "status",
            "input_fingerprint",
            "output_fingerprint",
            "metadata_json",
        }
    ),
    "work_reuse_records": frozenset(
        {
            "reuse_record_id",
            "matter_scope",
            "reused_from_step_id",
            "reused_by_step_id",
            "reuse_type",
            "valid",
            "invalidation_reason",
            "created_at",
        }
    ),
    "repair_plans": frozenset(
        {
            "repair_plan_id",
            "matter_scope",
            "target_type",
            "target_id",
            "blocker_signature",
            "blocker_type",
            "severity",
            "status",
            "actions_json",
            "attempts_so_far",
            "max_attempts",
            "created_at",
            "updated_at",
        }
    ),
    "repair_attempts": frozenset(
        {
            "repair_attempt_id",
            "repair_plan_id",
            "matter_scope",
            "action_type",
            "status",
            "result_json",
            "created_at",
        }
    ),
    "reducer_review_queue": frozenset(
        {
            "reducer_review_id",
            "matter_scope",
            "candidate_id",
            "task_id",
            "stage",
            "task_type",
            "priority",
            "status",
            "reason",
            "recommended_action",
            "created_at",
            "updated_at",
        }
    ),
    "citation_support_results": frozenset(
        {
            "citation_support_result_id",
            "matter_scope",
            "candidate_id",
            "artifact_id",
            "finding_id",
            "citation_id",
            "target_type",
            "target_id",
            "quote_text",
            "quote_hash",
            "support_status",
            "support_level",
            "reason",
            "created_at",
        }
    ),
    "source_chunks": frozenset(
        {
            "chunk_id",
            "matter_scope",
            "source_id",
            "source_snapshot_id",
            "extraction_id",
            "artifact_id",
            "page_number",
            "start_offset",
            "end_offset",
            "text_hash",
            "text",
            "confidence",
            "metadata_json",
            "created_at",
        }
    ),
    "work_step_source_links": frozenset(
        {
            "work_run_step_id",
            "matter_scope",
            "source_id",
            "source_snapshot_id",
            "source_sha256",
            "extraction_artifact_id",
            "extraction_text_sha256",
            "created_at",
        }
    ),
    "context_pack_sources": frozenset(
        {
            "context_pack_id",
            "matter_scope",
            "source_id",
            "source_snapshot_id",
            "source_sha256",
            "extraction_artifact_id",
            "extraction_text_sha256",
        }
    ),
}


@dataclass(frozen=True)
class SchemaCheck:
    ok: bool
    schema_meta_version: str
    expected_version: int
    missing_tables: list[str]
    missing_columns: dict[str, list[str]]
    missing_indexes: list[str]
    dangerous: bool

    def as_dict(self, *, db_path: str | None = None) -> dict[str, object]:
        repair_command = "atticus init --db <db> or atticus doctor --repair --write"
        if db_path:
            repair_command = f"atticus doctor --db {db_path} --repair --write"
        return {
            "ok": self.ok,
            "reason": "ok" if self.ok else "schema_mismatch",
            "schema_meta_version": self.schema_meta_version,
            "expected_version": self.expected_version,
            "missing_tables": self.missing_tables,
            "missing_columns": self.missing_columns,
            "missing_indexes": self.missing_indexes,
            "dangerous": self.dangerous,
            "repair_command": repair_command,
        }


class SchemaMismatchError(RuntimeError):
    """Raised when an Atticus DB claims or requires a schema it does not have."""

    def __init__(self, check: SchemaCheck):
        super().__init__(
            "Atticus schema mismatch: "
            f"missing_tables={check.missing_tables}, "
            f"missing_columns={check.missing_columns}, "
            f"missing_indexes={check.missing_indexes}"
        )
        self.check = check


def verify_schema(conn: sqlite3.Connection) -> SchemaCheck:
    """Return a structural schema check without mutating the database."""

    tables = _table_names(conn)
    indexes = _index_names(conn)
    missing_tables = sorted(REQUIRED_TABLES - tables)
    missing_indexes = sorted(REQUIRED_INDEXES - indexes)
    missing_columns: dict[str, list[str]] = {}
    for table, required_columns in REQUIRED_COLUMNS.items():
        if table in missing_tables:
            continue
        existing_columns = _column_names(conn, table)
        missing = sorted(required_columns - existing_columns)
        if missing:
            missing_columns[table] = missing
    schema_meta_version = _schema_meta_version(conn)
    ok = not missing_tables and not missing_columns and not missing_indexes
    dangerous = (
        not ok
        and schema_meta_version == str(SCHEMA_VERSION)
        or any(table in missing_tables for table in ("error_logs", "maintenance_runs", "maintenance_reports", "orchestrator_events"))
    )
    return SchemaCheck(
        ok=ok,
        schema_meta_version=schema_meta_version,
        expected_version=SCHEMA_VERSION,
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        missing_indexes=missing_indexes,
        dangerous=bool(dangerous),
    )


def require_schema_current(conn: sqlite3.Connection) -> None:
    check = verify_schema(conn)
    if not check.ok:
        raise SchemaMismatchError(check)


def schema_check_json(conn: sqlite3.Connection, *, db_path: str | None = None) -> dict[str, object]:
    return verify_schema(conn).as_dict(db_path=db_path)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    except sqlite3.DatabaseError:
        return set()


def _index_names(conn: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    except sqlite3.DatabaseError:
        return set()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def _schema_meta_version(conn: sqlite3.Connection) -> str:
    if "schema_meta" not in _table_names(conn):
        return ""
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return ""
    if row is None:
        return ""
    return str(row["value"] if isinstance(row, sqlite3.Row) else row[0])
