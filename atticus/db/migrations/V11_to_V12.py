"""Migration from schema version 11 to 12.

Adds columns added by _ensure_columns to databases created before
each column was added to the DDL. This is the first formal migration
for databases that predate the migration framework.
"""

from __future__ import annotations

import sqlite3

from atticus.db.migrations.registry import register


_COLUMN_ADDITIONS: dict[str, dict[str, str]] = {
    "runs": {
        "cancelled_by": "TEXT",
        "cancelled_at": "TEXT",
        "cancel_reason": "TEXT NOT NULL DEFAULT ''",
        "live_provider_permission_revoked": "INTEGER NOT NULL DEFAULT 0 CHECK(live_provider_permission_revoked IN (0, 1))",
    },
    "sources": {
        "chain_of_custody_json": "TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(chain_of_custody_json))",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    },
    "artifacts": {
        "produced_by_task_id": "TEXT",
        "replaced_by_artifact_id": "TEXT",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    },
    "artifact_sources": {
        "dependency_type": "TEXT NOT NULL DEFAULT 'supports'",
    },
    "tasks": {
        "instructions": "TEXT NOT NULL DEFAULT ''",
        "task_dependencies_json": "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(task_dependencies_json))",
        "matter_dependencies_json": "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(matter_dependencies_json))",
        "context_pack_id": "TEXT",
        "parent_task_id": "TEXT",
        "imported_from_candidate_id": "TEXT",
        "task_provenance_json": "TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(task_provenance_json))",
    },
    "leases": {
        "lease_role": "TEXT NOT NULL DEFAULT 'worker'",
        "fencing_token": "INTEGER NOT NULL DEFAULT 1",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    },
    "validation_results": {
        "matter_scope": "TEXT NOT NULL DEFAULT 'unknown'",
        "severity": "TEXT NOT NULL DEFAULT 'info'",
    },
    "provider_runs": {
        "run_id": "TEXT",
        "stage": "TEXT NOT NULL DEFAULT ''",
        "latency_ms": "INTEGER NOT NULL DEFAULT 0",
        "retries": "INTEGER NOT NULL DEFAULT 0",
        "context_pack_id": "TEXT",
        "context_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "provider_policy_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "configured_models_json": "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(configured_models_json))",
        "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
        "failover_events_json": "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(failover_events_json))",
        "cache_telemetry_source": "TEXT NOT NULL DEFAULT 'provider_reported'",
    },
    "human_attention": {
        "matter_scope": "TEXT NOT NULL DEFAULT 'unknown'",
        "owner": "TEXT NOT NULL DEFAULT 'operator'",
        "signature": "TEXT NOT NULL DEFAULT ''",
        "superseded_by": "TEXT",
        "plain_question": "TEXT NOT NULL DEFAULT ''",
        "why_needed": "TEXT NOT NULL DEFAULT ''",
        "acceptable_responses": "TEXT NOT NULL DEFAULT '[]'",
        "response_type": "TEXT",
        "response_statement": "TEXT",
        "response_artifact_id": "TEXT",
        "response_caveat": "TEXT NOT NULL DEFAULT ''",
        "routed_lane": "TEXT NOT NULL DEFAULT 'human_request'",
        "continuation_id": "TEXT",
    },
    "authority_verifications": {
        "jurisdiction_status": "TEXT NOT NULL DEFAULT ''",
        "proposition_hash": "TEXT NOT NULL DEFAULT ''",
        "verification_method": "TEXT NOT NULL DEFAULT ''",
        "source_url_or_reference": "TEXT NOT NULL DEFAULT ''",
        "expires_at": "TEXT",
    },
    "citation_support_results": {
        "proposition_text": "TEXT NOT NULL DEFAULT ''",
        "semantic_support_status": "TEXT NOT NULL DEFAULT 'unchecked_requires_human'",
        "authority_support_status": "TEXT NOT NULL DEFAULT ''",
        "source_chunk_id": "TEXT",
        "start_offset": "INTEGER",
        "end_offset": "INTEGER",
        "support_confidence": "REAL",
        "requires_human_review": "INTEGER NOT NULL DEFAULT 0 CHECK(requires_human_review IN (0, 1))",
    },
    "source_chunks": {
        "chunk_kind": "TEXT NOT NULL DEFAULT 'text'",
        "page_label": "TEXT NOT NULL DEFAULT ''",
        "line_start": "INTEGER",
        "line_end": "INTEGER",
        "span_index_status": "TEXT NOT NULL DEFAULT 'indexed'",
    },
    "repair_plans": {
        "owner": "TEXT NOT NULL DEFAULT 'orchestrator'",
        "retry_after": "TEXT",
        "terminal_reason": "TEXT NOT NULL DEFAULT ''",
    },
    "repair_attempts": {
        "outcome_json": "TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(outcome_json))",
    },
    "reducer_review_queue": {
        "blocks_certification_type": "TEXT NOT NULL DEFAULT ''",
        "blocks_final_gate": "INTEGER NOT NULL DEFAULT 0 CHECK(blocks_final_gate IN (0, 1))",
        "reviewer": "TEXT NOT NULL DEFAULT ''",
    },
}


@register(
    version_from=11,
    version_to=12,
    description="Add columns from _ensure_columns to support schema v12 features",
)
def apply_v11_to_v12(conn: sqlite3.Connection) -> None:
    """Add all columns from the _ensure_columns additions dict."""
    for table, columns in _COLUMN_ADDITIONS.items():
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.OperationalError:
            continue
        for col_name, col_ddl in columns.items():
            if col_name not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_ddl}")
                except sqlite3.OperationalError:
                    pass
