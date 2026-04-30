"""Repository helpers around the Atticus SQLite ledger."""

from __future__ import annotations

from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import sqlite3
from typing import cast
from uuid import uuid4

from atticus.core.events import Event, utc_now
from atticus.core.policies import LegalStage, TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db.schema import DDL, SCHEMA_VERSION
from atticus.memory.types import LEGAL_MEMORY_TYPES, SOURCE_REQUIRED_MEMORY_TYPES


LOOP_GUARD_REPEATS_PER_ESCALATION = 5
LOOP_GUARD_MAX_AUTO_ESCALATION_LEVEL = 3
ORCHESTRATOR_REPAIR_ATTEMPT_LIMIT = LOOP_GUARD_REPEATS_PER_ESCALATION * LOOP_GUARD_MAX_AUTO_ESCALATION_LEVEL
ORCHESTRATOR_TERMINAL_STATUS = "user_intervention_required"
SYSTEM_TASK_ATTENTION_PREFIXES = (
    "budget blocked for ",
    "cross-matter artifact dependency:",
    "cross-matter source dependency:",
    "cross-matter task dependency:",
    "free loop reduction failed:",
    "free loop worker failed:",
    "incomplete task dependency:",
    "inactive matter dependency:",
    "lease expired:",
    "live Codex execution requires ",
    "live OpenRouter execution requires ",
    "malformed certification requirement",
    "malformed provider policy",
    "malformed task gate metadata",
    "missing artifact dependency:",
    "missing certification:",
    "missing matter dependency:",
    "missing source dependency:",
    "missing task dependency:",
    "OpenRouter preflight failed before leasing",
    "OpenRouter provider call failed after dispatch",
    "orchestrator repair limit reached",
    "provider policy for task ",
    "stale artifact dependency:",
    "stale source dependency:",
    "task estimated cost ",
    "validation failed:",
    "completion certification ",
    "OpenRouter provider call exceeded hard supervision limit",
    "worker output quarantined:",
    "worker failure reported to orchestrator:",
)
PROVIDER_USER_INTERVENTION_PATTERNS = (
    "openrouter http 401",
    "openrouter http 402",
    "openrouter http 403",
    "openrouter_api_key is required",
    "api key is required",
    "invalid api key",
    "missing api key",
    "unauthorized",
    "forbidden",
    "insufficient credits",
)
PROVIDER_CONTROL_PLANE_ATTENTION_PREFIXES = (
    "provider preflight requires user intervention:",
    "provider preflight failed:",
    "provider runtime requires user intervention:",
    "provider runtime failed:",
)


def connect(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if read_only:
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _ = conn.execute("PRAGMA foreign_keys = ON")
    _ = conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db_connection(
    db_path: str | Path,
    *,
    read_only: bool = False,
    apply_schema: bool = True,
) -> Generator[sqlite3.Connection, None, None]:
    conn = connect(db_path, read_only=read_only)
    try:
        if apply_schema and not read_only:
            ensure_schema_current(conn)
        yield conn
        if not read_only:
            conn.commit()
    finally:
        conn.close()


def initialize_database(db_path: str | Path) -> None:
    with db_connection(db_path, apply_schema=False) as conn:
        ensure_schema_current(conn)
        ensure_matter(conn, "atticus", "Default Atticus matter")


def ensure_schema_current(conn: sqlite3.Connection) -> None:
    """Apply the current additive schema to an existing writable connection.

    Legal matter databases are long-lived, and operators may resume work after
    the harness code has advanced. Every write entry point therefore needs the
    same fail-closed additive guard as ``init`` so new control-plane tables do
    not appear only in freshly created databases.
    """

    _ = conn.executescript(DDL)
    _ensure_columns(conn)
    _ensure_indexes(conn)
    from atticus.db.doctor import require_schema_current

    require_schema_current(conn)
    _ = conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add missing additive columns when initializing older databases.

    SQLite cannot express all additive migrations through CREATE IF NOT EXISTS.
    This lightweight migrator keeps the prototype databases readable without
    destructive rewrites.
    """

    additions: dict[str, dict[str, str]] = {
        "runs": {
            "matter_scope": "TEXT NOT NULL DEFAULT 'atticus'",
            "budget_limit_usd": "REAL",
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
    for table, columns in additions.items():
        try:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.OperationalError:
            continue
        for name, ddl in columns.items():
            if name not in existing:
                _ = conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    _backfill_validation_matter_scope(conn)
    _backfill_human_attention_matter_scope(conn)
    _backfill_human_attention_lifecycle(conn)


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    _ = conn.execute("DROP INDEX IF EXISTS validation_target_idx")
    _ = conn.execute(
        "CREATE INDEX IF NOT EXISTS validation_target_idx ON validation_results(matter_scope, target_type, target_id, gate_name, passed)"
    )
    _ = conn.execute(
        "CREATE INDEX IF NOT EXISTS human_attention_scope_status_idx ON human_attention(matter_scope, status, severity, created_at)"
    )
    _ = conn.execute(
        "CREATE INDEX IF NOT EXISTS human_attention_signature_idx ON human_attention(matter_scope, signature, status)"
    )


def _backfill_validation_matter_scope(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            "SELECT validation_result_id, target_type, target_id FROM validation_results WHERE matter_scope = 'unknown'"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        matter_scope = _matter_scope_for_target(conn, target_type=str(row["target_type"]), target_id=str(row["target_id"]))
        if matter_scope is None:
            continue
        _ = conn.execute(
            "UPDATE validation_results SET matter_scope = ? WHERE validation_result_id = ? AND matter_scope = 'unknown'",
            (matter_scope, row["validation_result_id"]),
        )


def _backfill_human_attention_matter_scope(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            "SELECT attention_id, target_type, target_id FROM human_attention WHERE matter_scope = 'unknown'"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        matter_scope = _matter_scope_for_target(conn, target_type=str(row["target_type"]), target_id=str(row["target_id"]))
        if matter_scope is None:
            continue
        _ = conn.execute(
            "UPDATE human_attention SET matter_scope = ? WHERE attention_id = ? AND matter_scope = 'unknown'",
            (matter_scope, row["attention_id"]),
        )


def _backfill_human_attention_lifecycle(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            """
            SELECT attention_id, matter_scope, target_type, target_id, severity, reason
            FROM human_attention
            WHERE signature = '' OR owner = ''
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        signature = _human_attention_signature(
            matter_scope=str(row["matter_scope"]),
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            severity=str(row["severity"]),
            reason=str(row["reason"]),
        )
        _ = conn.execute(
            """
            UPDATE human_attention
            SET owner = CASE WHEN owner = '' THEN 'operator' ELSE owner END,
                signature = CASE WHEN signature = '' THEN ? ELSE signature END
            WHERE attention_id = ?
            """,
            (signature, row["attention_id"]),
        )


def _matter_scope_for_target(conn: sqlite3.Connection, *, target_type: str, target_id: str | None) -> str | None:
    if not target_id:
        return None
    if target_type == "matter":
        return target_id
    if target_type == "provider_policy":
        target_type = "task"
    table_column = {
        "task": ("tasks", "task_id"),
        "source": ("sources", "source_id"),
        "artifact": ("artifacts", "artifact_id"),
        "authority": ("legal_authorities", "authority_id"),
        "chronology_event": ("chronology_events", "chronology_event_id"),
        "claim": ("claims", "claim_id"),
        "memory": ("legal_memories", "memory_id"),
        "session": ("sessions", "session_id"),
        "run": ("runs", "run_id"),
        "context_pack": ("context_packs", "context_pack_id"),
        "matter_profile": ("matter_profiles", "matter_profile_id"),
        "work_run": ("work_runs", "work_run_id"),
        "work_run_step": ("work_run_steps", "work_run_step_id"),
    }.get(target_type)
    try:
        if table_column is not None:
            table, column = table_column
            if not _table_exists(conn, table):
                return None
            row = conn.execute(f"SELECT matter_scope FROM {table} WHERE {column} = ?", (target_id,)).fetchone()
            return str(row["matter_scope"]) if row is not None else None
        if target_type == "candidate":
            row = conn.execute(
                """
                SELECT t.matter_scope
                FROM candidate_outputs co
                JOIN tasks t ON t.task_id = co.task_id
                WHERE co.candidate_id = ?
                """,
                (target_id,),
            ).fetchone()
            return str(row["matter_scope"]) if row is not None else None
        if target_type == "reducer_packet":
            row = conn.execute(
                """
                SELECT t.matter_scope
                FROM reducer_packets rp
                JOIN candidate_outputs co ON co.candidate_id = rp.candidate_id
                JOIN tasks t ON t.task_id = co.task_id
                WHERE rp.reducer_packet_id = ?
                """,
                (target_id,),
            ).fetchone()
            return str(row["matter_scope"]) if row is not None else None
        if target_type == "provider_run":
            row = conn.execute(
                """
                SELECT COALESCE(t.matter_scope, r.matter_scope, cp.matter_scope) AS matter_scope
                FROM provider_runs pr
                LEFT JOIN tasks t ON t.task_id = pr.task_id
                LEFT JOIN runs r ON r.run_id = pr.run_id
                LEFT JOIN context_packs cp ON cp.context_pack_id = pr.context_pack_id
                WHERE pr.provider_run_id = ?
                """,
                (target_id,),
            ).fetchone()
            return str(row["matter_scope"]) if row is not None and row["matter_scope"] is not None else None
    except sqlite3.OperationalError:
        return None
    return None


def matter_scope_for_target(conn: sqlite3.Connection, *, target_type: str, target_id: str | None) -> str | None:
    return _matter_scope_for_target(conn, target_type=target_type, target_id=target_id)


def source_material_derivatives(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_ids: Iterable[str] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Return OCR/extracted text derivatives attached to matter sources.

    The source remains the evidence target. These rows are discoverable
    source-attached text views that tell workers where the OCR/extraction lives
    and how to cite it safely.
    """

    requested = tuple(str(source_id) for source_id in (source_ids or ()) if str(source_id))
    if requested:
        placeholders = ",".join("?" for _ in requested)
        source_rows = conn.execute(
            f"SELECT source_id FROM sources WHERE matter_scope = ? AND source_id IN ({placeholders}) ORDER BY source_id",
            (matter_scope, *requested),
        ).fetchall()
    else:
        source_rows = conn.execute(
            "SELECT source_id FROM sources WHERE matter_scope = ? ORDER BY source_id",
            (matter_scope,),
        ).fetchall()
    found_source_ids = [str(row["source_id"]) for row in source_rows]
    derivatives: dict[str, list[dict[str, object]]] = {source_id: [] for source_id in found_source_ids}
    if not found_source_ids:
        return derivatives
    placeholders = ",".join("?" for _ in found_source_ids)
    rows = conn.execute(
        f"""
        SELECT
          s.source_id,
          s.sha256 AS source_sha256,
          s.stale AS source_stale,
          (
            SELECT ss.snapshot_id
            FROM source_snapshots ss
            WHERE ss.source_id = s.source_id AND ss.sha256 = s.sha256
            ORDER BY ss.created_at DESC, ss.snapshot_id DESC
            LIMIT 1
          ) AS current_source_snapshot_id,
          a.artifact_id,
          a.path,
          a.artifact_type,
          a.trust_status,
          a.sha256,
          a.title,
          a.stale,
          er.extraction_id,
          er.method AS extraction_method,
          er.coverage_status AS extraction_coverage_status,
          er.confidence,
          er.metadata_json AS extraction_metadata_json,
          er.created_at AS extraction_created_at,
          ocr.ocr_id,
          ocr.engine AS ocr_engine,
          ocr.page_count AS ocr_page_count,
          ocr.coverage_status AS ocr_coverage_status,
          ocr.metadata_json AS ocr_metadata_json,
          ocr.created_at AS ocr_created_at
        FROM sources s
        JOIN artifact_sources af ON af.source_id = s.source_id
        JOIN artifacts a ON a.artifact_id = af.artifact_id
        LEFT JOIN extraction_records er ON er.source_id = s.source_id AND er.artifact_id = a.artifact_id
        LEFT JOIN ocr_records ocr ON ocr.source_id = s.source_id AND ocr.artifact_id = a.artifact_id
        WHERE s.matter_scope = ?
          AND s.source_id IN ({placeholders})
          AND a.matter_scope = s.matter_scope
          AND a.artifact_type IN (
            'extracted_text',
            'extraction_record',
            'ocr_extract',
            'ocr_text',
            'transcription_record',
            'transcript'
          )
        ORDER BY
          s.source_id,
          CASE a.artifact_type
            WHEN 'extracted_text' THEN 0
            WHEN 'ocr_text' THEN 1
            WHEN 'ocr_extract' THEN 2
            WHEN 'transcript' THEN 3
            ELSE 4
          END,
          a.created_at DESC,
          a.artifact_id
        """,
        (matter_scope, *found_source_ids),
    ).fetchall()
    for row in rows:
        source_id = str(row["source_id"])
        extraction_metadata = _json_dict(str(row["extraction_metadata_json"] or "{}"))
        ocr_metadata = _json_dict(str(row["ocr_metadata_json"] or "{}"))
        method = str(row["extraction_method"] or extraction_metadata.get("extractor") or "artifact_text")
        artifact_type = str(row["artifact_type"])
        derivative_role = "ocr_text" if row["ocr_id"] or "ocr" in method or "ocr" in artifact_type else "extracted_text"
        source_sha256 = str(row["source_sha256"] or "")
        extraction_source_sha256 = str(extraction_metadata.get("source_sha256") or "")
        ocr_source_sha256 = str(ocr_metadata.get("source_sha256") or "")
        current, stale_reasons = _source_derivative_currentness(
            source_stale=bool(row["source_stale"]),
            artifact_stale=bool(row["stale"]),
            source_sha256=source_sha256,
            extraction_source_sha256=extraction_source_sha256,
            ocr_source_sha256=ocr_source_sha256,
            extraction_coverage=str(row["extraction_coverage_status"] or ""),
            ocr_coverage=str(row["ocr_coverage_status"] or ""),
            has_ocr=bool(row["ocr_id"] or row["ocr_engine"]),
        )
        derivatives.setdefault(source_id, []).append(
            {
                "source_id": source_id,
                "artifact_id": str(row["artifact_id"]),
                "artifact_type": artifact_type,
                "derivative_role": derivative_role,
                "evidence_role": "source_attached_text_derivative_not_independent_evidence",
                "citation_target": {"target_type": "source", "target_id": source_id},
                "artifact_citation_rule": "cite the source_id for facts found in this derivative unless the work order explicitly allows artifact citation",
                "source_material_state": "current" if current else "stale",
                "current": current,
                "stale_reasons": stale_reasons,
                "source_sha256": source_sha256,
                "source_snapshot_id": row["current_source_snapshot_id"] or "",
                "path": row["path"],
                "title": row["title"],
                "trust_status": row["trust_status"],
                "stale": bool(row["stale"]),
                "text_sha256": str(extraction_metadata.get("text_sha256") or row["sha256"] or ""),
                "extraction": {
                    "extraction_id": row["extraction_id"] or "",
                    "method": method,
                    "coverage_status": row["extraction_coverage_status"] or "",
                    "confidence": row["confidence"] if row["confidence"] is not None else None,
                    "created_at": row["extraction_created_at"] or "",
                    "performed_by": str(extraction_metadata.get("extracted_by") or "atticus.local_extraction"),
                    "tool": str(extraction_metadata.get("extractor_tool") or extraction_metadata.get("extractor") or method),
                    "source_path": str(extraction_metadata.get("source_path") or ""),
                    "output_path": str(extraction_metadata.get("output_path") or row["path"] or ""),
                },
                "ocr": {
                    "ocr_id": row["ocr_id"] or "",
                    "engine": row["ocr_engine"] or "",
                    "page_count": row["ocr_page_count"] if row["ocr_page_count"] is not None else 0,
                    "coverage_status": row["ocr_coverage_status"] or "",
                    "created_at": row["ocr_created_at"] or "",
                    "performed_by": str(ocr_metadata.get("extracted_by") or "atticus.local_extraction"),
                    "tool": str(ocr_metadata.get("extractor_tool") or row["ocr_engine"] or ""),
                } if row["ocr_id"] or row["ocr_engine"] else None,
            }
        )
    return derivatives


def _source_derivative_currentness(
    *,
    source_stale: bool,
    artifact_stale: bool,
    source_sha256: str,
    extraction_source_sha256: str,
    ocr_source_sha256: str,
    extraction_coverage: str,
    ocr_coverage: str,
    has_ocr: bool,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if source_stale:
        reasons.append("source_stale")
    if artifact_stale:
        reasons.append("artifact_stale")
    if extraction_coverage and extraction_coverage != "complete":
        reasons.append(f"extraction_coverage_{extraction_coverage}")
    if has_ocr and ocr_coverage and ocr_coverage != "complete":
        reasons.append(f"ocr_coverage_{ocr_coverage}")
    provenance_hashes = [value for value in (extraction_source_sha256, ocr_source_sha256) if value]
    if source_sha256 and provenance_hashes and all(value != source_sha256 for value in provenance_hashes):
        reasons.append("source_sha256_mismatch")
    return not reasons, reasons


def _require_target_in_matter(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    target_type: str,
    target_id: str | None,
    field_name: str,
) -> None:
    if not target_id:
        return
    target_matter = _matter_scope_for_target(conn, target_type=target_type, target_id=target_id)
    if target_matter is None:
        if matter_scope == "unknown":
            return
        raise ValueError(f"{field_name} not found or has no matter scope: {target_id}")
    if target_matter != matter_scope:
        raise ValueError(f"{field_name} {target_id} belongs to matter {target_matter}, outside matter {matter_scope}")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json_list_or_empty(text: str) -> list[object]:
    try:
        value = json.loads(text or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return cast(list[object], value) if isinstance(value, list) else []


def _json_dict(text: str) -> dict[str, object]:
    try:
        value = json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()} if isinstance(value, Mapping) else {}


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _system_attention_reason_variants(reasons: Iterable[str]) -> tuple[str, ...]:
    variants: set[str] = set()
    for raw in reasons:
        reason = " ".join(str(raw).strip().split())
        if not reason:
            continue
        variants.add(reason)
        variants.add(f"free loop worker failed: {reason}")
        variants.add(f"worker failure reported to orchestrator: {reason}")
        variants.add(f"worker failure reported to orchestrator: free loop worker failed: {reason}")
    return tuple(sorted(variants))


def _provider_failure_requires_user_intervention(message: str) -> bool:
    normalized = " ".join(message.lower().split())
    return any(pattern in normalized for pattern in PROVIDER_USER_INTERVENTION_PATTERNS)


def provider_failure_requires_user_intervention(message: str) -> bool:
    """Return whether a provider failure needs operator action instead of worker repair."""

    return _provider_failure_requires_user_intervention(message)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def add_event(conn: sqlite3.Connection, event: Event) -> str:
    previous = conn.execute(
        "SELECT event_hash FROM events ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    previous_hash = str(previous["event_hash"]) if previous else ""
    event_hash = event.hash(previous_hash)
    _ = conn.execute(
        """
        INSERT INTO events(event_type, actor, matter_scope, payload_json, previous_hash, event_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_type,
            event.actor,
            event.matter_scope,
            event.canonical_payload(),
            previous_hash,
            event_hash,
            utc_now(),
        ),
    )
    return event_hash


def emit_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    actor: str = "atticus",
    matter_scope: str = "atticus",
    payload: dict[str, object] | None = None,
) -> str:
    return add_event(conn, Event(event_type=event_type, actor=actor, matter_scope=matter_scope, payload=payload or {}))


def ensure_matter(conn: sqlite3.Connection, matter_scope: str, title: str = "") -> None:
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO matters(matter_scope, title, status, created_at, updated_at)
        VALUES (?, ?, 'active', ?, ?)
        ON CONFLICT(matter_scope) DO UPDATE SET
          title=CASE WHEN excluded.title != '' THEN excluded.title ELSE matters.title END,
          updated_at=excluded.updated_at
        """,
        (matter_scope, title, now, now),
    )


def create_matter_profile(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    profile_name: str,
    stages: Iterable[dict[str, object]] | None = None,
    base_template: str = "default_s0_s9",
    reason: str = "initial profile",
    requested_by: str = "operator",
    created_by: str = "atticus",
    matter_profile_id: str | None = None,
) -> str:
    ensure_matter(conn, matter_scope)
    stage_rows = _normalized_profile_stages(stages)
    fingerprint = _hash_text(_json({"matter_scope": matter_scope, "base_template": base_template, "stages": stage_rows}))
    old = conn.execute("SELECT matter_profile_id FROM matter_profiles WHERE matter_scope = ? AND status = 'active'", (matter_scope,)).fetchone()
    old_profile_id = str(old["matter_profile_id"]) if old is not None else None
    pid = matter_profile_id or f"mprof-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute("UPDATE matter_profiles SET status = 'superseded', updated_at = ? WHERE matter_scope = ? AND status = 'active'", (now, matter_scope))
    _ = conn.execute(
        """
        INSERT INTO matter_profiles(matter_profile_id, matter_scope, profile_name, status,
          base_template, profile_version, fingerprint, created_by, created_at, updated_at)
        VALUES (?, ?, ?, 'active', ?, COALESCE((SELECT MAX(profile_version) + 1 FROM matter_profiles WHERE matter_scope = ?), 1), ?, ?, ?, ?)
        """,
        (pid, matter_scope, profile_name, base_template, matter_scope, fingerprint, created_by, now, now),
    )
    for stage_row in stage_rows:
        _ = conn.execute(
            """
            INSERT INTO matter_profile_stages(profile_stage_id, matter_profile_id, stage, enabled,
              gate_policy_json, worker_policy_json, model_policy_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"mprofstage-{uuid4().hex}",
                pid,
                stage_row["stage"],
                1 if stage_row["enabled"] else 0,
                _json(stage_row["gate_policy"]),
                _json(stage_row["worker_policy"]),
                _json(stage_row["model_policy"]),
                now,
            ),
        )
    change_id = f"mprofchg-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO matter_profile_changes(matter_profile_change_id, matter_scope, old_profile_id,
          new_profile_id, reason, requested_by, diff_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            change_id,
            matter_scope,
            old_profile_id,
            pid,
            reason,
            requested_by,
            _json({"old_profile_id": old_profile_id or "", "new_fingerprint": fingerprint, "base_template": base_template}),
            now,
        ),
    )
    _ = emit_event(conn, "matter_profile.activated", matter_scope=matter_scope, payload={"matter_profile_id": pid, "old_profile_id": old_profile_id or "", "reason": reason})
    return pid


def get_active_matter_profile(conn: sqlite3.Connection, *, matter_scope: str) -> dict[str, object] | None:
    row = conn.execute("SELECT * FROM matter_profiles WHERE matter_scope = ? AND status = 'active'", (matter_scope,)).fetchone()
    if row is None:
        return None
    result = _row_to_plain_dict(row)
    stage_rows = conn.execute("SELECT * FROM matter_profile_stages WHERE matter_profile_id = ? ORDER BY stage", (row["matter_profile_id"],)).fetchall()
    result["stages"] = [_profile_stage_row_to_dict(stage_row) for stage_row in stage_rows]
    return result


def _normalized_profile_stages(stages: Iterable[dict[str, object]] | None) -> list[dict[str, object]]:
    raw_stages = list(stages) if stages is not None else [{"stage": str(stage), "enabled": True} for stage in LegalStage]
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in raw_stages:
        stage = str(raw.get("stage") or "").strip()
        if not stage:
            raise ValueError("matter profile stage is required")
        if stage in seen:
            raise ValueError(f"duplicate matter profile stage: {stage}")
        seen.add(stage)
        normalized.append(
            {
                "stage": stage,
                "enabled": bool(raw.get("enabled", True)),
                "gate_policy": dict(cast(dict[str, object], raw.get("gate_policy") or raw.get("gate_policy_json") or {})),
                "worker_policy": dict(cast(dict[str, object], raw.get("worker_policy") or raw.get("worker_policy_json") or {})),
                "model_policy": dict(cast(dict[str, object], raw.get("model_policy") or raw.get("model_policy_json") or {})),
            }
        )
    return normalized


def _profile_stage_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "profile_stage_id": row["profile_stage_id"],
        "stage": row["stage"],
        "enabled": bool(row["enabled"]),
        "gate_policy": json.loads(str(row["gate_policy_json"] or "{}")),
        "worker_policy": json.loads(str(row["worker_policy_json"] or "{}")),
        "model_policy": json.loads(str(row["model_policy_json"] or "{}")),
        "created_at": row["created_at"],
    }


def upsert_run(
    conn: sqlite3.Connection,
    run_id: str,
    state: str,
    reason: str = "",
    *,
    matter_scope: str = "atticus",
    budget_limit_usd: float | None = None,
) -> None:
    ensure_matter(conn, matter_scope)
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO runs(run_id, matter_scope, state, reason, budget_limit_usd, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
          matter_scope=excluded.matter_scope,
          state=excluded.state,
          reason=excluded.reason,
          budget_limit_usd=COALESCE(excluded.budget_limit_usd, runs.budget_limit_usd),
          updated_at=excluded.updated_at
        """,
        (run_id, matter_scope, state, reason, budget_limit_usd, now, now),
    )
    _ = emit_event(
        conn,
        "run.upserted",
        matter_scope=matter_scope,
        payload={"run_id": run_id, "state": state, "reason": reason},
    )


def add_source(
    conn: sqlite3.Connection,
    *,
    source_id: str | None = None,
    matter_scope: str = "atticus",
    path: str,
    source_type: str = "file",
    sha256: str,
    size_bytes: int = 0,
    trust_status: str = TrustStatus.CANDIDATE,
    stage: str = LegalStage.S0_SOURCE_INVENTORY,
    imported_from: str | None = None,
    stale: bool = False,
    chain_of_custody: dict[str, object] | None = None,
) -> str:
    ensure_matter(conn, matter_scope)
    sid = source_id or f"src-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO sources(source_id, matter_scope, path, source_type, sha256, size_bytes,
          trust_status, stage, imported_from, chain_of_custody_json, stale, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            matter_scope,
            path,
            source_type,
            sha256,
            size_bytes,
            str(trust_status),
            str(stage),
            imported_from,
            _json(chain_of_custody or {}),
            1 if stale else 0,
            now,
            now,
        ),
    )
    _ = add_source_snapshot(
        conn,
        source_id=sid,
        sha256=sha256,
        size_bytes=size_bytes,
        captured_by="importer" if imported_from else "atticus",
        custody_note=f"initial registration from {imported_from}" if imported_from else "initial registration",
    )
    _ = emit_event(
        conn,
        "source.registered",
        matter_scope=matter_scope,
        payload={"source_id": sid, "path": path, "sha256": sha256, "trust_status": str(trust_status)},
    )
    return sid


def add_source_snapshot(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    sha256: str,
    size_bytes: int = 0,
    captured_by: str,
    custody_note: str = "",
    metadata: dict[str, object] | None = None,
    snapshot_id: str | None = None,
) -> str:
    snap_id = snapshot_id or f"snap-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO source_snapshots(snapshot_id, source_id, sha256, size_bytes, captured_by,
          custody_note, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (snap_id, source_id, sha256, size_bytes, captured_by, custody_note, _json(metadata or {}), utc_now()),
    )
    return snap_id


def add_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_id: str | None = None,
    matter_scope: str = "atticus",
    path: str,
    artifact_type: str,
    stage: str = LegalStage.S0_SOURCE_INVENTORY,
    trust_status: str = TrustStatus.CANDIDATE,
    sha256: str | None = None,
    title: str = "",
    content: str = "",
    imported_from: str | None = None,
    source_ids: Iterable[str] = (),
    artifact_dependency_ids: Iterable[str] = (),
    produced_by_task_id: str | None = None,
    stale: bool = False,
) -> str:
    ensure_matter(conn, matter_scope)
    aid = artifact_id or f"art-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO artifacts(artifact_id, matter_scope, path, artifact_type, stage, trust_status,
          sha256, title, content, imported_from, produced_by_task_id, stale, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            aid,
            matter_scope,
            path,
            artifact_type,
            str(stage),
            str(trust_status),
            sha256,
            title,
            content,
            imported_from,
            produced_by_task_id,
            1 if stale else 0,
            now,
            now,
        ),
    )
    for source_id in source_ids:
        _ = conn.execute(
            "INSERT OR IGNORE INTO artifact_sources(artifact_id, source_id, dependency_type) VALUES (?, ?, 'supports')",
            (aid, source_id),
        )
    for dep_id in artifact_dependency_ids:
        _ = conn.execute(
            """
            INSERT OR IGNORE INTO artifact_dependencies(artifact_id, dependency_artifact_id, dependency_type, created_at)
            VALUES (?, ?, 'derived_from', ?)
            """,
            (aid, dep_id, now),
        )
    _ = add_artifact_version(
        conn,
        artifact_id=aid,
        version_number=1,
        sha256=sha256,
        content=content,
        status=str(trust_status),
        created_by_task_id=produced_by_task_id,
        created_by_role="importer" if imported_from else "atticus",
    )
    _ = emit_event(
        conn,
        "artifact.registered",
        matter_scope=matter_scope,
        payload={"artifact_id": aid, "path": path, "artifact_type": artifact_type, "trust_status": str(trust_status)},
    )
    return aid


def add_artifact_version(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    version_number: int,
    sha256: str | None,
    content: str,
    status: str,
    created_by_task_id: str | None = None,
    created_by_role: str = "",
    artifact_version_id: str | None = None,
) -> str:
    version_id = artifact_version_id or f"aver-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO artifact_versions(artifact_version_id, artifact_id, version_number, sha256,
          content_hash, status, created_by_task_id, created_by_role, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            artifact_id,
            version_number,
            sha256,
            _hash_text(content),
            status,
            created_by_task_id,
            created_by_role,
            utc_now(),
        ),
    )
    return version_id


def add_artifact_from_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    artifact_type: str,
    stage: str = LegalStage.S0_SOURCE_INVENTORY,
    trust_status: str = TrustStatus.CANDIDATE,
    imported_from: str | None = None,
) -> str:
    sha256, _size = _hash_file(path)
    try:
        content = path.read_text(encoding="utf-8")[:200_000]
    except UnicodeDecodeError:
        content = ""
    return add_artifact(
        conn,
        path=str(path),
        artifact_type=artifact_type,
        stage=stage,
        trust_status=trust_status,
        sha256=sha256,
        title=path.name,
        content=content,
        imported_from=imported_from,
    )


def add_task(conn: sqlite3.Connection, task: TaskSpec) -> None:
    _ensure_columns(conn)
    ensure_matter(conn, task.matter_scope)
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO tasks(task_id, matter_scope, stage, status, task_type, title, instructions,
          source_dependencies_json, artifact_dependencies_json, task_dependencies_json,
          matter_dependencies_json, required_certifications_json, validation_gates_json,
          staleness_rules_json, provider_policy_json, cost_limit_usd, expected_value,
          human_attention_flags_json, blocked_reasons_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.task_id,
            task.matter_scope,
            str(task.stage),
            str(task.status),
            task.task_type,
            task.title,
            task.instructions,
            _json(task.source_dependencies),
            _json(task.artifact_dependencies),
            _json(task.task_dependencies),
            _json(task.matter_dependencies),
            _json(task.required_certifications),
            _json(task.validation_gates),
            _json(task.staleness_rules),
            _json(task.provider_policy),
            task.cost_limit_usd,
            task.expected_value,
            _json([]),
            _json([]),
            now,
            now,
        ),
    )
    _ = emit_event(
        conn,
        "task.created",
        matter_scope=task.matter_scope,
        payload={"task_id": task.task_id, "stage": str(task.stage), "task_type": task.task_type},
    )


def update_task_status(conn: sqlite3.Connection, task_id: str, status: str, reason: str = "") -> None:
    matter_scope = _matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown"
    _ = conn.execute(
        """
        UPDATE tasks
        SET status = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (str(status), utc_now(), task_id),
    )
    _ = emit_event(conn, "task.status_changed", matter_scope=matter_scope, payload={"task_id": task_id, "status": str(status), "reason": reason})
    if str(status) in {
        str(TaskStatus.QUEUED),
        str(TaskStatus.READY),
        str(TaskStatus.LEASED),
        str(TaskStatus.RUNNING),
        str(TaskStatus.REDUCER_PENDING),
        str(TaskStatus.COMPLETE),
    }:
        _ = resolve_system_task_attention(
            conn,
            task_id=task_id,
            matter_scope=matter_scope,
            resolution_source="task.status_changed",
        )


def update_task_blocked(conn: sqlite3.Connection, task_id: str, reasons: list[str]) -> None:
    task_row = conn.execute(
        "SELECT matter_scope, status, blocked_reasons_json FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    matter_scope = str(task_row["matter_scope"]) if task_row is not None else _matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown"
    existing_reasons = _json_list_or_empty(str(task_row["blocked_reasons_json"] or "[]")) if task_row is not None else []
    if _repair_limit_event_exists(conn, task_id=task_id):
        terminal_reason = "orchestrator repair limit reached: user intervention required"
        terminal_reasons = [*reasons]
        if terminal_reason not in terminal_reasons:
            terminal_reasons.append(terminal_reason)
        if existing_reasons != terminal_reasons:
            _ = conn.execute(
                """
                UPDATE tasks
                SET status = ?, blocked_reasons_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskStatus.BLOCKED, _json(terminal_reasons), utc_now(), task_id),
            )
        attention = conn.execute(
            """
            SELECT 1
            FROM human_attention
            WHERE matter_scope = ? AND target_type = 'task' AND target_id = ?
              AND severity = 'blocker' AND status = 'open'
              AND reason LIKE 'orchestrator repair limit reached%'
            LIMIT 1
            """,
            (matter_scope, task_id),
        ).fetchone()
        if attention is None:
            _ = record_human_attention_once(
                conn,
                target_type="task",
                target_id=task_id,
                severity="blocker",
                reason=terminal_reason,
                matter_scope=matter_scope,
            )
        return
    if task_row is not None and str(task_row["status"]) == str(TaskStatus.BLOCKED) and existing_reasons == reasons:
        if _orchestrator_signal_count_for_task(conn, task_id=task_id) == 0:
            _ = record_orchestrator_task_blocked(conn, task_id=task_id, reasons=reasons, matter_scope=matter_scope)
        return
    _ = conn.execute(
        """
        UPDATE tasks
        SET status = ?, blocked_reasons_json = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (TaskStatus.BLOCKED, _json(reasons), utc_now(), task_id),
    )
    _ = record_human_attention_once(
        conn,
        target_type="task",
        target_id=task_id,
        severity="blocker",
        reason="; ".join(reasons),
    )
    from atticus.agents.repair_planner import ensure_repair_plan_for_blocker

    for reason in reasons:
        _ = ensure_repair_plan_for_blocker(
            conn,
            matter_scope=matter_scope,
            target_type="task",
            target_id=task_id,
            reason=reason,
        )
    _ = emit_event(conn, "task.blocked", matter_scope=matter_scope, payload={"task_id": task_id, "reasons": reasons})
    _ = record_orchestrator_task_blocked(conn, task_id=task_id, reasons=reasons, matter_scope=matter_scope)


def record_validation(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    gate_name: str,
    passed: bool,
    details: dict[str, object] | None = None,
    severity: str = "info",
    matter_scope: str | None = None,
) -> int:
    resolved_matter_scope = matter_scope or _matter_scope_for_target(conn, target_type=target_type, target_id=target_id) or "unknown"
    cur = conn.execute(
        """
        INSERT INTO validation_results(matter_scope, target_type, target_id, gate_name, passed, severity, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resolved_matter_scope,
            target_type,
            target_id,
            gate_name,
            1 if passed else 0,
            severity,
            _json(details or {}),
            utc_now(),
        ),
    )
    lastrowid = cur.lastrowid
    if lastrowid is None:
        raise RuntimeError("validation insert did not produce a row id")
    validation_id = int(lastrowid)
    _ = emit_event(
        conn,
        "validation.recorded",
        matter_scope=resolved_matter_scope,
        payload={
            "validation_result_id": validation_id,
            "matter_scope": resolved_matter_scope,
            "target_type": target_type,
            "target_id": target_id,
            "gate_name": gate_name,
            "passed": passed,
        },
    )
    if not passed:
        _ = record_human_attention(
            conn,
            target_type=target_type,
            target_id=target_id,
            severity="blocker" if severity == "error" else "warning",
            reason=f"validation failed: {gate_name}",
        )
    return validation_id


def add_certification(
    conn: sqlite3.Connection,
    *,
    subject_type: str,
    subject_id: str,
    certification_type: str,
    validator: str,
    validation_result_id: int,
    evidence: dict[str, object] | None = None,
    certification_id: str | None = None,
) -> str:
    cid = certification_id or f"cert-{uuid4().hex}"
    matter_scope = _matter_scope_for_target(conn, target_type=subject_type, target_id=subject_id)
    if matter_scope is None:
        row = conn.execute(
            "SELECT matter_scope FROM validation_results WHERE validation_result_id = ?",
            (validation_result_id,),
        ).fetchone()
        matter_scope = str(row["matter_scope"]) if row is not None else "unknown"
    _ = conn.execute(
        """
        INSERT INTO certifications(certification_id, subject_type, subject_id, certification_type,
          status, validator, validation_result_id, evidence_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid,
            subject_type,
            subject_id,
            certification_type,
            "active",
            validator,
            validation_result_id,
            _json(evidence or {}),
            utc_now(),
        ),
    )
    _ = emit_event(
        conn,
        "certification.issued",
        matter_scope=matter_scope,
        payload={
            "certification_id": cid,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "certification_type": certification_type,
        },
    )
    return cid


def record_provider_run(
    conn: sqlite3.Connection,
    *,
    provider_run_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    stage: str = "",
    requested_provider: str,
    requested_model: str,
    actual_provider: str,
    actual_model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
    cache_write_tokens: int = 0,
    context_pack_id: str | None = None,
    context_fingerprint: str = "",
    provider_policy_fingerprint: str = "",
    configured_models: Iterable[str] = (),
    failover_events: Iterable[dict[str, object]] = (),
    cache_telemetry_source: str = "provider_reported",
    estimated_cost_usd: float = 0.0,
    actual_cost_usd: float | None = None,
    latency_ms: int = 0,
    retries: int = 0,
    fallback_allowed: bool = False,
    fallback_policy_result: str = "not_needed",
    raw_usage: dict[str, object] | None = None,
) -> str:
    rid = provider_run_id or f"prun-{uuid4().hex}"
    raw_usage_payload = raw_usage or {}
    matter_scope = None
    if task_id is not None:
        matter_scope = _matter_scope_for_target(conn, target_type="task", target_id=task_id)
    if matter_scope is None and run_id is not None:
        matter_scope = _matter_scope_for_target(conn, target_type="run", target_id=run_id)
    elif run_id is not None:
        _require_target_in_matter(conn, matter_scope=matter_scope, target_type="run", target_id=run_id, field_name="run_id")
    resolved_context_pack_id = context_pack_id or _context_pack_id_for_task(conn, task_id=task_id)
    if matter_scope is None and resolved_context_pack_id is not None:
        matter_scope = _matter_scope_for_target(conn, target_type="context_pack", target_id=resolved_context_pack_id)
    elif resolved_context_pack_id is not None:
        _require_target_in_matter(
            conn,
            matter_scope=matter_scope,
            target_type="context_pack",
            target_id=resolved_context_pack_id,
            field_name="context_pack_id",
        )
    matter_scope = matter_scope or "unknown"
    resolved_context_fingerprint = context_fingerprint or _context_fingerprint(conn, resolved_context_pack_id)
    resolved_provider_policy_fingerprint = provider_policy_fingerprint or _provider_policy_fingerprint_from_usage(raw_usage_payload)
    configured_model_values = tuple(configured_models) or _strings_from_usage(raw_usage_payload.get("configured_models"))
    failover_event_values = tuple(failover_events) or _dicts_from_usage(raw_usage_payload.get("openrouter_failover_events"))
    _ = conn.execute(
        """
        INSERT INTO provider_runs(provider_run_id, task_id, run_id, stage, requested_provider,
          requested_model, actual_provider, actual_model, input_tokens, output_tokens,
          cache_hit_tokens, cache_miss_tokens, context_pack_id, context_fingerprint,
          provider_policy_fingerprint, configured_models_json, cache_write_tokens,
          failover_events_json, cache_telemetry_source, estimated_cost_usd, actual_cost_usd,
          latency_ms, retries, fallback_allowed, fallback_policy_result, raw_usage_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rid,
            task_id,
            run_id,
            stage,
            requested_provider,
            requested_model,
            actual_provider,
            actual_model,
            input_tokens,
            output_tokens,
            cache_hit_tokens,
            cache_miss_tokens,
            resolved_context_pack_id,
            resolved_context_fingerprint,
            resolved_provider_policy_fingerprint,
            _json(list(configured_model_values)),
            cache_write_tokens,
            _json(list(failover_event_values)),
            cache_telemetry_source,
            estimated_cost_usd,
            actual_cost_usd,
            latency_ms,
            retries,
            1 if fallback_allowed else 0,
            fallback_policy_result,
            _json(raw_usage_payload),
            utc_now(),
        ),
    )
    if cache_hit_tokens or cache_write_tokens or cache_miss_tokens or resolved_context_fingerprint or resolved_provider_policy_fingerprint:
        _ = record_prompt_cache_observation(
            conn,
            matter_scope=matter_scope,
            provider_run_id=rid,
            task_id=task_id,
            context_pack_id=resolved_context_pack_id,
            query_source="provider_run",
            model=actual_model or requested_model,
            context_fingerprint=resolved_context_fingerprint,
            policy_fingerprint=resolved_provider_policy_fingerprint,
            cache_hit_tokens=cache_hit_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_miss_tokens=cache_miss_tokens,
            reason="provider cache telemetry only; cache hits are not evidence correctness",
        )
    _ = emit_event(
        conn,
        "provider.run_recorded",
        matter_scope=matter_scope,
        payload={
            "provider_run_id": rid,
            "task_id": task_id,
            "requested": [requested_provider, requested_model],
            "actual": [actual_provider, actual_model],
            "estimated_cost_usd": estimated_cost_usd,
            "fallback_policy_result": fallback_policy_result,
        },
    )
    return rid


def record_prompt_cache_observation(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    provider_run_id: str | None = None,
    task_id: str | None = None,
    context_pack_id: str | None = None,
    query_source: str = "",
    model: str = "",
    system_fingerprint: str = "",
    tools_fingerprint: str = "",
    context_fingerprint: str = "",
    policy_fingerprint: str = "",
    cache_hit_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_miss_tokens: int = 0,
    possible_cache_break: bool | None = None,
    reason: str = "",
    prompt_cache_observation_id: str | None = None,
) -> str:
    oid = prompt_cache_observation_id or f"pcache-{uuid4().hex}"
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="provider_run", target_id=provider_run_id, field_name="provider_run_id")
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="task", target_id=task_id, field_name="task_id")
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="context_pack", target_id=context_pack_id, field_name="context_pack_id")
    if possible_cache_break is None:
        possible_cache_break = _looks_like_cache_break(
            conn,
            matter_scope=matter_scope,
            model=model,
            system_fingerprint=system_fingerprint,
            tools_fingerprint=tools_fingerprint,
            context_fingerprint=context_fingerprint,
            policy_fingerprint=policy_fingerprint,
            cache_hit_tokens=cache_hit_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )
    _ = conn.execute(
        """
        INSERT INTO prompt_cache_observations(prompt_cache_observation_id, matter_scope,
          provider_run_id, task_id, context_pack_id, query_source, model,
          system_fingerprint, tools_fingerprint, context_fingerprint, policy_fingerprint,
          cache_hit_tokens, cache_write_tokens, cache_miss_tokens, possible_cache_break,
          reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            oid,
            matter_scope,
            provider_run_id,
            task_id,
            context_pack_id,
            query_source,
            model,
            system_fingerprint,
            tools_fingerprint,
            context_fingerprint,
            policy_fingerprint,
            cache_hit_tokens,
            cache_write_tokens,
            cache_miss_tokens,
            1 if possible_cache_break else 0,
            reason,
            utc_now(),
        ),
    )
    _ = emit_event(
        conn,
        "prompt_cache.observed",
        matter_scope=matter_scope,
        payload={
            "prompt_cache_observation_id": oid,
            "provider_run_id": provider_run_id or "",
            "cache_hit_tokens": cache_hit_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_miss_tokens": cache_miss_tokens,
            "possible_cache_break": possible_cache_break,
            "not_evidence_correctness": True,
        },
    )
    return oid


def _looks_like_cache_break(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    model: str,
    system_fingerprint: str,
    tools_fingerprint: str,
    context_fingerprint: str,
    policy_fingerprint: str,
    cache_hit_tokens: int,
    cache_write_tokens: int,
    cache_miss_tokens: int,
) -> bool:
    if cache_hit_tokens > 0 or cache_miss_tokens <= 0 or not (model and context_fingerprint and policy_fingerprint):
        return False
    row = conn.execute(
        """
        SELECT 1 FROM prompt_cache_observations
        WHERE matter_scope = ? AND model = ? AND system_fingerprint = ? AND tools_fingerprint = ?
          AND context_fingerprint = ? AND policy_fingerprint = ?
          AND cache_hit_tokens > 0 AND possible_cache_break = 0
        LIMIT 1
        """,
        (matter_scope, model, system_fingerprint, tools_fingerprint, context_fingerprint, policy_fingerprint),
    ).fetchone()
    return row is not None and cache_write_tokens == 0


def _context_pack_id_for_task(conn: sqlite3.Connection, *, task_id: str | None) -> str | None:
    if not task_id:
        return None
    row = conn.execute("SELECT context_pack_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None or row["context_pack_id"] is None:
        return None
    return str(row["context_pack_id"])


def _context_fingerprint(conn: sqlite3.Connection, context_pack_id: str | None) -> str:
    if not context_pack_id:
        return ""
    row = conn.execute("SELECT fingerprint FROM context_packs WHERE context_pack_id = ?", (context_pack_id,)).fetchone()
    return str(row["fingerprint"]) if row is not None and row["fingerprint"] is not None else ""


def _provider_policy_fingerprint_from_usage(raw_usage: dict[str, object]) -> str:
    provider_policy = raw_usage.get("provider_policy")
    if isinstance(provider_policy, Mapping):
        provider_policy_map = cast(Mapping[object, object], provider_policy)
        return _hash_text(_json({str(key): item for key, item in provider_policy_map.items()}))
    return ""


def _strings_from_usage(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    items = cast(Iterable[object], value)
    return tuple(str(item) for item in items if str(item))


def _dicts_from_usage(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    items = cast(Iterable[object], value)
    return tuple({str(key): val for key, val in cast(Mapping[object, object], item).items()} for item in items if isinstance(item, Mapping))


def record_human_attention(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    severity: str,
    reason: str,
    status: str = "open",
    matter_scope: str | None = None,
    owner: str = "operator",
    signature: str | None = None,
    superseded_by: str | None = None,
) -> int:
    target_matter_scope = _matter_scope_for_target(conn, target_type=target_type, target_id=target_id)
    if matter_scope is not None and target_matter_scope is not None and matter_scope != target_matter_scope:
        raise ValueError(f"human attention matter_scope {matter_scope!r} does not match target matter {target_matter_scope!r}")
    resolved_matter_scope = matter_scope or target_matter_scope or "unknown"
    resolved_signature = signature or _human_attention_signature(
        matter_scope=resolved_matter_scope,
        target_type=target_type,
        target_id=target_id,
        severity=severity,
        reason=reason,
    )
    cur = conn.execute(
        """
        INSERT INTO human_attention(
          matter_scope, target_type, target_id, severity, reason, status,
          owner, signature, superseded_by, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resolved_matter_scope,
            target_type,
            target_id,
            severity,
            reason,
            status,
            owner,
            resolved_signature,
            superseded_by,
            utc_now(),
        ),
    )
    lastrowid = cur.lastrowid
    if lastrowid is None:
        raise RuntimeError("human attention insert did not produce a row id")
    return int(lastrowid)


def record_human_attention_once(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    severity: str,
    reason: str,
    status: str = "open",
    matter_scope: str | None = None,
    owner: str = "operator",
    signature: str | None = None,
) -> int | None:
    target_matter_scope = _matter_scope_for_target(conn, target_type=target_type, target_id=target_id)
    if matter_scope is not None and target_matter_scope is not None and matter_scope != target_matter_scope:
        raise ValueError(f"human attention matter_scope {matter_scope!r} does not match target matter {target_matter_scope!r}")
    resolved_matter_scope = matter_scope or target_matter_scope or "unknown"
    resolved_signature = signature or _human_attention_signature(
        matter_scope=resolved_matter_scope,
        target_type=target_type,
        target_id=target_id,
        severity=severity,
        reason=reason,
    )
    row = conn.execute(
        """
        SELECT attention_id
        FROM human_attention
        WHERE matter_scope = ? AND signature = ? AND status = ?
        ORDER BY attention_id DESC
        LIMIT 1
        """,
        (resolved_matter_scope, resolved_signature, status),
    ).fetchone()
    if row is not None:
        return None
    return record_human_attention(
        conn,
        target_type=target_type,
        target_id=target_id,
        severity=severity,
        reason=reason,
        status=status,
        matter_scope=resolved_matter_scope,
        owner=owner,
        signature=resolved_signature,
    )


def _human_attention_signature(
    *,
    matter_scope: str,
    target_type: str,
    target_id: str,
    severity: str,
    reason: str,
) -> str:
    return "|".join((matter_scope, target_type, target_id, severity, reason))


def resolve_attention_by_signature(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    signature: str,
    resolution_source: str = "system",
) -> int:
    cur = conn.execute(
        """
        UPDATE human_attention
        SET status = 'closed'
        WHERE matter_scope = ? AND signature = ? AND status = 'open'
        """,
        (matter_scope, signature),
    )
    changed = int(cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0)
    if changed:
        _ = emit_event(
            conn,
            "human_attention.resolved",
            matter_scope=matter_scope,
            payload={
                "signature": signature,
                "resolved_count": changed,
                "resolution_source": resolution_source,
            },
        )
    return changed


def supersede_attention(
    conn: sqlite3.Connection,
    *,
    attention_id: int,
    superseded_by: str,
    resolution_source: str = "system",
) -> int:
    row = conn.execute(
        "SELECT matter_scope FROM human_attention WHERE attention_id = ?",
        (attention_id,),
    ).fetchone()
    if row is None:
        return 0
    cur = conn.execute(
        """
        UPDATE human_attention
        SET status = 'superseded', superseded_by = ?
        WHERE attention_id = ? AND status = 'open'
        """,
        (superseded_by, attention_id),
    )
    changed = int(cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0)
    if changed:
        _ = emit_event(
            conn,
            "human_attention.superseded",
            matter_scope=str(row["matter_scope"]),
            payload={
                "attention_id": attention_id,
                "superseded_by": superseded_by,
                "resolution_source": resolution_source,
            },
        )
    return changed


def dedupe_open_human_attention(
    conn: sqlite3.Connection,
    *,
    matter_scope: str | None = None,
    resolution_source: str = "maintenance",
) -> int:
    where = "WHERE status = 'open' AND signature != ''"
    params: tuple[object, ...] = ()
    if matter_scope and matter_scope != "global":
        where += " AND matter_scope = ?"
        params = (matter_scope,)
    groups = conn.execute(
        f"""
        SELECT matter_scope, signature, MAX(attention_id) AS keep_id, COUNT(*) AS n
        FROM human_attention
        {where}
        GROUP BY matter_scope, signature
        HAVING COUNT(*) > 1
        """,
        params,
    ).fetchall()
    changed = 0
    for group in groups:
        keep_id = int(group["keep_id"])
        cur = conn.execute(
            """
            UPDATE human_attention
            SET status = 'superseded', superseded_by = ?
            WHERE matter_scope = ? AND signature = ? AND status = 'open' AND attention_id != ?
            """,
            (str(keep_id), str(group["matter_scope"]), str(group["signature"]), keep_id),
        )
        changed += int(cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0)
    if changed:
        _ = emit_event(
            conn,
            "human_attention.deduped",
            matter_scope=matter_scope or "global",
            payload={
                "deduped_count": changed,
                "group_count": len(groups),
                "resolution_source": resolution_source,
            },
        )
    return changed


def resolve_provider_control_plane_attention(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    provider: str,
    resolution_source: str = "provider.control_plane_ok",
) -> int:
    """Close stale provider user-intervention attention after a provider probe succeeds."""

    params: list[object] = [matter_scope, matter_scope]
    clauses = [f"reason LIKE ? ESCAPE '\\'" for _ in PROVIDER_CONTROL_PLANE_ATTENTION_PREFIXES]
    params.extend(_like_prefix(prefix) for prefix in PROVIDER_CONTROL_PLANE_ATTENTION_PREFIXES)
    cur = conn.execute(
        f"""
        UPDATE human_attention
        SET status = 'closed'
        WHERE matter_scope = ?
          AND target_type = 'matter'
          AND target_id = ?
          AND status = 'open'
          AND ({' OR '.join(clauses)})
        """,
        tuple(params),
    )
    changed = int(cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0)
    if not changed:
        return 0
    _ = emit_event(
        conn,
        "provider.control_plane_attention_resolved",
        matter_scope=matter_scope,
        payload={
            "provider": provider,
            "resolved_count": changed,
            "resolution_source": resolution_source,
        },
    )
    still_requires_user = conn.execute(
        """
        SELECT 1
        FROM human_attention
        WHERE matter_scope = ? AND target_type = 'matter' AND target_id = ?
          AND severity = 'blocker' AND status = 'open'
          AND reason LIKE '%requires user intervention%'
        LIMIT 1
        """,
        (matter_scope, matter_scope),
    ).fetchone()
    if still_requires_user is None:
        _ = conn.execute(
            """
            UPDATE matter_orchestrators
            SET status = 'repair_required', updated_at = ?
            WHERE matter_scope = ? AND status = ?
            """,
            (utc_now(), matter_scope, ORCHESTRATOR_TERMINAL_STATUS),
        )
    return changed


def resolve_system_task_attention(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    matter_scope: str | None = None,
    reasons: Iterable[str] | None = None,
    resolution_source: str = "system",
) -> int:
    """Close stale system-generated attention after a task is unblocked.

    Operator-authored attention remains open. The closed rows are limited to
    known harness reason prefixes plus exact prior blocker reasons and their
    common wrapper forms.
    """

    resolved_matter_scope = matter_scope or _matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown"
    exact_reasons = _system_attention_reason_variants(reasons or ())
    params: list[object] = [resolved_matter_scope, task_id]
    clauses = [f"reason LIKE ? ESCAPE '\\'" for _ in SYSTEM_TASK_ATTENTION_PREFIXES]
    params.extend(_like_prefix(prefix) for prefix in SYSTEM_TASK_ATTENTION_PREFIXES)
    if exact_reasons:
        clauses.append(f"reason IN ({','.join('?' for _ in exact_reasons)})")
        params.extend(exact_reasons)
    cur = conn.execute(
        f"""
        UPDATE human_attention
        SET status = 'closed'
        WHERE matter_scope = ?
          AND target_type = 'task'
          AND target_id = ?
          AND status = 'open'
          AND ({' OR '.join(clauses)})
        """,
        tuple(params),
    )
    changed = int(cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0)
    if changed:
        _ = emit_event(
            conn,
            "human_attention.resolved",
            matter_scope=resolved_matter_scope,
            payload={
                "target_type": "task",
                "target_id": task_id,
                "resolved_count": changed,
                "resolution_source": resolution_source,
            },
        )
    return changed


def resolve_stale_system_task_attention(
    conn: sqlite3.Connection,
    *,
    matter_scope: str = "global",
) -> int:
    """Close system task blockers whose target task is no longer blocked."""

    where = ""
    params: tuple[object, ...] = ()
    if matter_scope != "global":
        where = "AND ha.matter_scope = ?"
        params = (matter_scope,)
    rows = conn.execute(
        f"""
        SELECT DISTINCT ha.target_id, ha.matter_scope
        FROM human_attention ha
        JOIN tasks t ON t.task_id = ha.target_id
        WHERE ha.status = 'open'
          AND ha.target_type = 'task'
          AND t.status NOT IN (?, ?, ?)
          {where}
        """,
        (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.QUARANTINED, *params),
    ).fetchall()
    total = 0
    for row in rows:
        total += resolve_system_task_attention(
            conn,
            task_id=str(row["target_id"]),
            matter_scope=str(row["matter_scope"]),
            resolution_source="maintenance.stale_attention_cleanup",
        )
    return total


def record_loop_guard_failure(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    target_type: str,
    target_id: str,
    error_type: str,
    message: str,
    source: str = "",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    if not _table_exists(conn, "error_logs"):
        ensure_schema_current(conn)
    clean_message = " ".join(message.strip().split()) or "unspecified failure"
    clean_error_type = error_type.strip() or "failure"
    signature = _failure_signature(
        matter_scope=matter_scope,
        target_type=target_type,
        target_id=target_id,
        error_type=clean_error_type,
        message=clean_message,
    )
    occurrence_count = _error_occurrence_count(conn, error_signature=signature) + 1
    consecutive_count = _consecutive_error_count(
        conn,
        target_type=target_type,
        target_id=target_id,
        error_type=clean_error_type,
        error_signature=signature,
    ) + 1
    raw_payload = payload or {}
    escalation = _escalation_payload_for_signal(consecutive_count)
    if bool(raw_payload.get("requires_user_intervention")) or bool(raw_payload.get("terminal")):
        escalation = {
            **escalation,
            "escalation_level": max(int(escalation["escalation_level"]), 4),
            "escalation_target": ORCHESTRATOR_TERMINAL_STATUS,
            "attempts_until_next_escalation": 0,
            "repair_attempts_remaining": 0,
            "retry_allowed": False,
            "terminal": True,
            "requires_user_intervention": True,
        }
    severity = "blocker" if bool(escalation["terminal"]) else "warning" if int(escalation["escalation_level"]) >= 2 else "info"
    error_log_id = f"err-{uuid4().hex}"
    now = utc_now()
    log_payload = {
        **raw_payload,
        "source": source,
        "failure_signature": signature,
        **escalation,
    }
    _ = conn.execute(
        """
        INSERT INTO error_logs(error_log_id, matter_scope, target_type, target_id,
          error_type, error_signature, message, severity, escalation_level,
          occurrence_count, consecutive_count, terminal, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            error_log_id,
            matter_scope,
            target_type,
            target_id,
            clean_error_type,
            signature,
            clean_message,
            severity,
            int(escalation["escalation_level"]),
            occurrence_count,
            consecutive_count,
            1 if bool(escalation["terminal"]) else 0,
            _json(log_payload),
            now,
        ),
    )
    result = {
        "error_log_id": error_log_id,
        "failure_signature": signature,
        "occurrence_count": occurrence_count,
        "consecutive_count": consecutive_count,
        **escalation,
    }
    if _should_emit_loop_guard_escalation(consecutive_count, bool(escalation["terminal"])):
        escalation_payload = {
            "error_log_id": error_log_id,
            "target_type": target_type,
            "target_id": target_id,
            "error_type": clean_error_type,
            "message": clean_message,
            **result,
        }
        _ = emit_event(conn, "loop_guard.escalated", matter_scope=matter_scope, payload=escalation_payload)
        level = int(escalation["escalation_level"])
        if level == 2:
            _ = emit_event(conn, "matter_orchestrator.loop_guard_escalated", matter_scope=matter_scope, payload=escalation_payload)
        elif level >= 3:
            _ = emit_event(conn, "master_orchestrator.loop_guard_escalated", matter_scope=matter_scope, payload=escalation_payload)
    return result


def record_provider_preflight_failure(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    task_id: str,
    provider: str,
    message: str,
    runnable_task_count: int,
    provider_policy_result: str = "",
) -> str | None:
    """Surface provider control-plane failures without blaming a worker task."""

    return record_provider_control_plane_failure(
        conn,
        matter_scope=matter_scope,
        task_id=task_id,
        provider=provider,
        message=message,
        runnable_task_count=runnable_task_count,
        provider_policy_result=provider_policy_result,
        source="provider.preflight",
        error_type="provider_preflight_failed",
        attention_prefix="provider preflight",
        trigger_reason_prefix="provider preflight",
        event_prefix="orchestrator.provider_preflight",
    )


def record_provider_control_plane_failure(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    task_id: str,
    provider: str,
    message: str,
    runnable_task_count: int,
    provider_policy_result: str = "",
    source: str = "provider.runtime",
    error_type: str = "provider_control_plane_failed",
    attention_prefix: str = "provider failure",
    trigger_reason_prefix: str = "provider failure",
    event_prefix: str = "orchestrator.provider_control_plane",
) -> str | None:
    """Surface provider auth/config/billing failures without entering worker retry loops."""

    clean_message = " ".join(message.strip().split()) or "provider control-plane failure"
    requires_user = _provider_failure_requires_user_intervention(clean_message)
    orchestrator_id = _ensure_signal_orchestrator(conn, matter_scope=matter_scope)
    attention_reason = (
        f"{attention_prefix} requires user intervention: {clean_message}"
        if requires_user
        else f"{attention_prefix} failed: {clean_message}"
    )
    _ = record_human_attention_once(
        conn,
        target_type="matter",
        target_id=matter_scope,
        severity="blocker" if requires_user else "warning",
        reason=attention_reason,
        matter_scope=matter_scope,
    )
    guard = record_loop_guard_failure(
        conn,
        matter_scope=matter_scope,
        target_type="matter",
        target_id=matter_scope,
        error_type=error_type,
        message=clean_message,
        source=source,
        payload={
            "task_id": task_id,
            "provider": provider,
            "runnable_task_count": runnable_task_count,
            "provider_policy_result": provider_policy_result,
            "requires_user_intervention": requires_user,
            "retry_allowed": not requires_user,
        },
    )
    if orchestrator_id is None:
        return None
    now = utc_now()
    next_status = ORCHESTRATOR_TERMINAL_STATUS if requires_user else "repair_required"
    _ = conn.execute(
        "UPDATE matter_orchestrators SET status = ?, updated_at = ? WHERE orchestrator_id = ?",
        (next_status, now, orchestrator_id),
    )
    event_payload = {
        "task_id": task_id,
        "provider": provider,
        "message": clean_message,
        "runnable_task_count": runnable_task_count,
        "provider_policy_result": provider_policy_result,
        **guard,
        "requires_user_intervention": requires_user,
        "retry_allowed": not requires_user,
        "terminal": requires_user,
        "escalation_target": ORCHESTRATOR_TERMINAL_STATUS if requires_user else guard.get("escalation_target"),
        "source": source,
    }
    event_type = f"{event_prefix}_user_intervention_required" if requires_user else f"{event_prefix}_failed"
    event_id = record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type=event_type,
        payload=event_payload,
    )
    if requires_user:
        _ = emit_event(
            conn,
            "master_orchestrator.user_intervention_required",
            matter_scope=matter_scope,
            payload={"orchestrator_event_id": event_id, **event_payload},
        )
        _ = request_maintenance_run(
            conn,
            matter_scope=matter_scope,
            trigger_reason=f"{trigger_reason_prefix} requires user intervention for {provider}",
            triggered_by="master_orchestrator",
            trigger_event_id=event_id,
            payload=event_payload,
        )
    return event_id


def record_orchestrator_task_blocked(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    reasons: list[str],
    matter_scope: str | None = None,
    source: str = "task.blocked",
) -> str | None:
    row = conn.execute("SELECT matter_scope FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task_matter_scope = str(row["matter_scope"])
    if matter_scope is not None and matter_scope not in {"", "unknown"} and matter_scope != task_matter_scope:
        raise ValueError(f"task {task_id} belongs to matter {task_matter_scope}, not {matter_scope}")
    orchestrator_id = _ensure_signal_orchestrator(conn, matter_scope=task_matter_scope)
    if orchestrator_id is None:
        return None
    guard = record_loop_guard_failure(
        conn,
        matter_scope=task_matter_scope,
        target_type="task",
        target_id=task_id,
        error_type="task_blocked",
        message="; ".join(reasons),
        source=source,
        payload={"reasons": reasons},
    )
    now = utc_now()
    _ = conn.execute(
        "UPDATE matter_orchestrators SET status = 'repair_required', updated_at = ? WHERE orchestrator_id = ?",
        (now, orchestrator_id),
    )
    event_id = record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.task_blocked",
        payload={
            "task_id": task_id,
            "reasons": reasons,
            "source": source,
            "retry_policy": "no silent infinite retry",
            **guard,
        },
    )
    _ = _record_repair_limit_if_needed(
        conn,
        orchestrator_id=orchestrator_id,
        task_id=task_id,
        matter_scope=task_matter_scope,
        reason="; ".join(reasons),
        source=source,
        related_event_id=event_id,
        escalation=guard,
    )
    return event_id


def record_orchestrator_worker_failure(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    failure_reason: str,
    matter_scope: str | None = None,
    source: str = "worker",
) -> str:
    row = conn.execute("SELECT matter_scope FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task: {task_id}")
    task_matter_scope = str(row["matter_scope"])
    if matter_scope is not None and task_matter_scope != matter_scope:
        raise ValueError(f"task {task_id} belongs to matter {task_matter_scope}, not {matter_scope}")
    orchestrator_id = _ensure_signal_orchestrator(conn, matter_scope=task_matter_scope)
    if orchestrator_id is None:
        raise ValueError("orchestrator tables are unavailable")
    _ = record_human_attention_once(
        conn,
        target_type="task",
        target_id=task_id,
        severity="warning",
        reason=f"worker failure reported to orchestrator: {failure_reason}",
    )
    guard = record_loop_guard_failure(
        conn,
        matter_scope=task_matter_scope,
        target_type="task",
        target_id=task_id,
        error_type="worker_failure",
        message=failure_reason,
        source=source,
        payload={"failure_reason": failure_reason},
    )
    _ = conn.execute(
        """
        UPDATE matter_orchestrators
        SET failure_count = failure_count + 1, status = 'repair_required', updated_at = ?
        WHERE orchestrator_id = ?
        """,
        (utc_now(), orchestrator_id),
    )
    event_id = record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.worker_failed",
        payload={
            "task_id": task_id,
            "failure_reason": failure_reason,
            "source": source,
            "retry_policy": "no silent infinite retry",
            **guard,
        },
    )
    _ = _record_repair_limit_if_needed(
        conn,
        orchestrator_id=orchestrator_id,
        task_id=task_id,
        matter_scope=task_matter_scope,
        reason=failure_reason,
        source=source,
        related_event_id=event_id,
        escalation=guard,
    )
    return event_id


def record_orchestrator_repair_proposed(
    conn: sqlite3.Connection,
    *,
    orchestrator_id: str,
    task_id: str,
    payload: dict[str, object],
) -> str | None:
    if _repair_limit_event_exists(conn, task_id=task_id):
        return None
    task_row = conn.execute("SELECT matter_scope FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task_row is None:
        raise ValueError(f"unknown task: {task_id}")
    orchestrator_row = conn.execute("SELECT matter_scope FROM matter_orchestrators WHERE orchestrator_id = ?", (orchestrator_id,)).fetchone()
    if orchestrator_row is None:
        raise ValueError(f"orchestrator not found: {orchestrator_id}")
    task_matter_scope = str(task_row["matter_scope"])
    if str(orchestrator_row["matter_scope"]) != task_matter_scope:
        raise ValueError(f"task {task_id} belongs to matter {task_matter_scope}, not orchestrator {orchestrator_id}")
    blocked_reasons = [str(item) for item in payload.get("blocked_reasons", [])] if isinstance(payload.get("blocked_reasons"), list) else []
    repair_message = "; ".join(blocked_reasons) if blocked_reasons else "repair proposal did not unblock task"
    guard = record_loop_guard_failure(
        conn,
        matter_scope=task_matter_scope,
        target_type="task",
        target_id=task_id,
        error_type="task_blocked",
        message=repair_message,
        source="orchestrator.repair_proposed",
        payload={"blocked_reasons": blocked_reasons, "proposed_actions": payload.get("proposed_actions", [])},
    )
    event_payload = {
        **payload,
        "task_id": task_id,
        "retry_policy": "hard repair signal limit",
        **guard,
    }
    event_id = record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.repair_proposed",
        payload=event_payload,
    )
    _ = _record_repair_limit_if_needed(
        conn,
        orchestrator_id=orchestrator_id,
        task_id=task_id,
        matter_scope=task_matter_scope,
        reason="repair proposal limit reached without successful unblock",
        source="orchestrator.repair_proposed",
        related_event_id=event_id,
        escalation=guard,
    )
    return event_id


def record_external_action_block(
    conn: sqlite3.Connection,
    *,
    action_type: str,
    requested_by: str = "user",
    reason: str = "external legal actions are blocked",
    payload: dict[str, object] | None = None,
    matter_scope: str | None = None,
) -> str:
    payload = payload or {}
    resolved_matter_scope = matter_scope or _matter_scope_for_target(conn, target_type="task", target_id=str(payload.get("task_id") or "")) or "unknown"
    block_id = f"block-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO external_action_blocks(block_id, action_type, requested_by, reason, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (block_id, action_type, requested_by, reason, _json(payload), utc_now()),
    )
    _ = emit_event(conn, "external_action.blocked", matter_scope=resolved_matter_scope, payload={"block_id": block_id, "action_type": action_type})
    return block_id


def upsert_matter_orchestrator(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    status: str = "idle",
    current_goal: str = "",
    model_decision: dict[str, object] | None = None,
    failure_count: int = 0,
    orchestrator_id: str | None = None,
) -> str:
    ensure_matter(conn, matter_scope)
    oid = orchestrator_id or f"orch-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO matter_orchestrators(orchestrator_id, matter_scope, status,
          model_decision_json, last_tick_at, current_goal, failure_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(matter_scope) DO UPDATE SET
          status=excluded.status,
          model_decision_json=excluded.model_decision_json,
          current_goal=excluded.current_goal,
          failure_count=excluded.failure_count,
          updated_at=excluded.updated_at
        """,
        (oid, matter_scope, status, _json(model_decision or {}), None, current_goal, failure_count, now, now),
    )
    row = conn.execute("SELECT orchestrator_id FROM matter_orchestrators WHERE matter_scope = ?", (matter_scope,)).fetchone()
    resolved_id = str(row["orchestrator_id"] if row is not None else oid)
    _ = emit_event(conn, "orchestrator.upserted", matter_scope=matter_scope, payload={"orchestrator_id": resolved_id, "status": status, "current_goal": current_goal})
    return resolved_id


def record_orchestrator_event(
    conn: sqlite3.Connection,
    *,
    orchestrator_id: str,
    event_type: str,
    payload: dict[str, object] | None = None,
    orchestrator_event_id: str | None = None,
) -> str:
    row = conn.execute("SELECT matter_scope FROM matter_orchestrators WHERE orchestrator_id = ?", (orchestrator_id,)).fetchone()
    if row is None:
        raise ValueError(f"orchestrator not found: {orchestrator_id}")
    matter_scope = str(row["matter_scope"])
    eid = orchestrator_event_id or f"orchevt-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO orchestrator_events(orchestrator_event_id, orchestrator_id, matter_scope,
          event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (eid, orchestrator_id, matter_scope, event_type, _json(payload or {}), now),
    )
    _ = conn.execute("UPDATE matter_orchestrators SET last_tick_at = ?, updated_at = ? WHERE orchestrator_id = ?", (now, now, orchestrator_id))
    _ = emit_event(conn, "orchestrator.event_recorded", matter_scope=matter_scope, payload={"orchestrator_event_id": eid, "orchestrator_id": orchestrator_id, "event_type": event_type})
    return eid


def request_maintenance_run(
    conn: sqlite3.Connection,
    *,
    matter_scope: str = "global",
    trigger_reason: str,
    triggered_by: str = "master_orchestrator",
    trigger_event_id: str = "",
    payload: dict[str, object] | None = None,
) -> str | None:
    if not _table_exists(conn, "maintenance_runs"):
        ensure_schema_current(conn)
    if not _table_exists(conn, "maintenance_runs"):
        return None
    if trigger_event_id:
        existing = conn.execute(
            """
            SELECT maintenance_run_id
            FROM maintenance_runs
            WHERE trigger_event_id = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (trigger_event_id,),
        ).fetchone()
        if existing is not None:
            return str(existing["maintenance_run_id"])
    existing_pending = conn.execute(
        """
        SELECT maintenance_run_id
        FROM maintenance_runs
        WHERE matter_scope = ? AND status IN ('pending', 'running')
          AND trigger_reason = ?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (matter_scope, trigger_reason),
    ).fetchone()
    if existing_pending is not None:
        return str(existing_pending["maintenance_run_id"])
    mid = f"maint-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO maintenance_runs(maintenance_run_id, matter_scope, status,
          triggered_by, trigger_reason, trigger_event_id, isolation_level,
          started_at, updated_at, payload_json)
        VALUES (?, ?, 'pending', ?, ?, ?, 'control_plane_only', ?, ?, ?)
        """,
        (mid, matter_scope or "global", triggered_by, trigger_reason, trigger_event_id, now, now, _json(payload or {})),
    )
    event_payload = {
        "maintenance_run_id": mid,
        "matter_scope": matter_scope or "global",
        "triggered_by": triggered_by,
        "trigger_reason": trigger_reason,
        "trigger_event_id": trigger_event_id,
        "isolation_level": "control_plane_only",
    }
    _ = emit_event(conn, "maintenance.run_requested", matter_scope=matter_scope or "global", payload=event_payload)
    if triggered_by == "master_orchestrator":
        _ = emit_event(conn, "master_orchestrator.maintenance_requested", matter_scope=matter_scope or "global", payload=event_payload)
    return mid


def record_maintenance_report(
    conn: sqlite3.Connection,
    *,
    maintenance_run_id: str,
    summary: str,
    diagnostics: dict[str, object],
    actions: list[dict[str, object]],
    resume_signal: dict[str, object],
) -> str:
    row = conn.execute("SELECT matter_scope FROM maintenance_runs WHERE maintenance_run_id = ?", (maintenance_run_id,)).fetchone()
    if row is None:
        raise ValueError(f"maintenance run not found: {maintenance_run_id}")
    matter_scope = str(row["matter_scope"])
    report_id = f"maintrep-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO maintenance_reports(maintenance_report_id, maintenance_run_id,
          matter_scope, summary, diagnostics_json, actions_json, resume_signal_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (report_id, maintenance_run_id, matter_scope, summary, _json(diagnostics), _json(actions), _json(resume_signal), now),
    )
    _ = conn.execute(
        """
        UPDATE maintenance_runs
        SET status = 'completed', updated_at = ?, completed_at = ?
        WHERE maintenance_run_id = ?
        """,
        (now, now, maintenance_run_id),
    )
    event_payload = {
        "maintenance_run_id": maintenance_run_id,
        "maintenance_report_id": report_id,
        "summary": summary,
        "resume_signal": resume_signal,
    }
    _ = emit_event(conn, "maintenance.report_ready", matter_scope=matter_scope, payload=event_payload)
    _ = emit_event(conn, "master_orchestrator.maintenance_completed", matter_scope=matter_scope, payload=event_payload)
    severity = "blocker" if str(resume_signal.get("status") or "") == "blocked_by_user_intervention" else "warning"
    _ = record_human_attention_once(
        conn,
        target_type="matter",
        target_id=matter_scope,
        severity=severity,
        reason=f"maintenance report ready: {summary}",
        matter_scope=matter_scope,
    )
    return report_id


def get_matter_orchestrator(conn: sqlite3.Connection, *, matter_scope: str) -> dict[str, object] | None:
    row = conn.execute("SELECT * FROM matter_orchestrators WHERE matter_scope = ?", (matter_scope,)).fetchone()
    if row is None:
        return None
    result = _row_to_plain_dict(row)
    result["model_decision"] = json.loads(str(result.pop("model_decision_json") or "{}"))
    return result


def _ensure_signal_orchestrator(conn: sqlite3.Connection, *, matter_scope: str) -> str | None:
    if not _table_exists(conn, "matter_orchestrators") or not _table_exists(conn, "orchestrator_events"):
        ensure_schema_current(conn)
    if not _table_exists(conn, "matter_orchestrators") or not _table_exists(conn, "orchestrator_events"):
        return None
    row = conn.execute("SELECT orchestrator_id FROM matter_orchestrators WHERE matter_scope = ?", (matter_scope,)).fetchone()
    if row is not None:
        return str(row["orchestrator_id"])
    return upsert_matter_orchestrator(conn, matter_scope=matter_scope, status="repair_required")


def _orchestrator_signal_count_for_task(conn: sqlite3.Connection, *, task_id: str) -> int:
    if not _table_exists(conn, "orchestrator_events"):
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM orchestrator_events
        WHERE event_type IN ('orchestrator.task_blocked', 'orchestrator.worker_failed', 'orchestrator.repair_proposed')
          AND json_extract(payload_json, '$.task_id') = ?
        """,
        (task_id,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def _escalation_payload(prior_signals: int) -> dict[str, object]:
    return _escalation_payload_for_signal(max(0, prior_signals) + 1)


def _escalation_payload_for_signal(signal_count: int) -> dict[str, object]:
    clean_count = max(1, signal_count)
    if clean_count >= ORCHESTRATOR_REPAIR_ATTEMPT_LIMIT:
        level = 4
    elif clean_count >= LOOP_GUARD_REPEATS_PER_ESCALATION * 2:
        level = 3
    elif clean_count >= LOOP_GUARD_REPEATS_PER_ESCALATION:
        level = 2
    else:
        level = 1
    target = {
        1: "worker_self_repair",
        2: "matter_orchestrator_repair",
        3: "master_orchestrator_attention",
        4: "user_intervention_required",
    }[level]
    terminal = clean_count >= ORCHESTRATOR_REPAIR_ATTEMPT_LIMIT
    next_escalation_at = _next_escalation_at(clean_count)
    return {
        "signal_count": clean_count,
        "escalation_level": level,
        "escalation_target": target,
        "escalation_window": LOOP_GUARD_REPEATS_PER_ESCALATION,
        "next_escalation_at": next_escalation_at,
        "attempts_until_next_escalation": max(0, next_escalation_at - clean_count) if next_escalation_at else 0,
        "repair_attempt_limit": ORCHESTRATOR_REPAIR_ATTEMPT_LIMIT,
        "repair_attempts_remaining": max(0, ORCHESTRATOR_REPAIR_ATTEMPT_LIMIT - clean_count),
        "retry_allowed": not terminal,
        "terminal": terminal,
        "requires_user_intervention": terminal,
    }


def _next_escalation_at(signal_count: int) -> int | None:
    for threshold in (
        LOOP_GUARD_REPEATS_PER_ESCALATION,
        LOOP_GUARD_REPEATS_PER_ESCALATION * 2,
        ORCHESTRATOR_REPAIR_ATTEMPT_LIMIT,
    ):
        if signal_count < threshold:
            return threshold
    return None


def _should_emit_loop_guard_escalation(consecutive_count: int, terminal: bool) -> bool:
    return terminal or consecutive_count in {
        LOOP_GUARD_REPEATS_PER_ESCALATION,
        LOOP_GUARD_REPEATS_PER_ESCALATION * 2,
    }


def _failure_signature(
    *,
    matter_scope: str,
    target_type: str,
    target_id: str,
    error_type: str,
    message: str,
) -> str:
    normalized = "|".join(
        (
            matter_scope.strip().lower(),
            target_type.strip().lower(),
            target_id.strip(),
            error_type.strip().lower(),
            " ".join(message.lower().split()),
        )
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _error_occurrence_count(conn: sqlite3.Connection, *, error_signature: str) -> int:
    if not _table_exists(conn, "error_logs"):
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM error_logs WHERE error_signature = ?",
        (error_signature,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def _consecutive_error_count(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    error_type: str,
    error_signature: str,
) -> int:
    if not _table_exists(conn, "error_logs"):
        return 0
    rows = conn.execute(
        """
        SELECT error_signature
        FROM error_logs
        WHERE target_type = ? AND target_id = ? AND error_type = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
        """,
        (target_type, target_id, error_type, ORCHESTRATOR_REPAIR_ATTEMPT_LIMIT),
    ).fetchall()
    count = 0
    for row in rows:
        if str(row["error_signature"]) != error_signature:
            break
        count += 1
    return count


def _record_repair_limit_if_needed(
    conn: sqlite3.Connection,
    *,
    orchestrator_id: str,
    task_id: str,
    matter_scope: str,
    reason: str,
    source: str,
    related_event_id: str,
    escalation: Mapping[str, object],
) -> str | None:
    if not bool(escalation.get("terminal")):
        return None
    if _repair_limit_event_exists(conn, task_id=task_id):
        return None
    attention_reason = (
        f"orchestrator repair limit reached after {ORCHESTRATOR_REPAIR_ATTEMPT_LIMIT} signals; "
        f"user intervention required: {reason}"
    )
    attention_id = record_human_attention_once(
        conn,
        target_type="task",
        target_id=task_id,
        severity="blocker",
        reason=attention_reason,
        matter_scope=matter_scope,
    )
    _mark_task_terminal_blocked(conn, task_id=task_id, reason=attention_reason)
    now = utc_now()
    _ = conn.execute(
        "UPDATE matter_orchestrators SET status = ?, updated_at = ? WHERE orchestrator_id = ?",
        (ORCHESTRATOR_TERMINAL_STATUS, now, orchestrator_id),
    )
    payload = {
        "task_id": task_id,
        "reason": reason,
        "source": source,
        "related_event_id": related_event_id,
        "attention_id": attention_id or "",
        **dict(escalation),
    }
    event_id = record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.repair_limit_reached",
        payload=payload,
    )
    _ = emit_event(
        conn,
        "master_orchestrator.user_intervention_required",
        matter_scope=matter_scope,
        payload={"orchestrator_event_id": event_id, **payload},
    )
    _ = request_maintenance_run(
        conn,
        matter_scope=matter_scope,
        trigger_reason=f"repair limit reached for task {task_id}",
        triggered_by="master_orchestrator",
        trigger_event_id=event_id,
        payload={"task_id": task_id, "reason": reason, "source": source, "related_event_id": related_event_id},
    )
    return event_id


def _repair_limit_event_exists(conn: sqlite3.Connection, *, task_id: str) -> bool:
    if not _table_exists(conn, "orchestrator_events"):
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM orchestrator_events
        WHERE event_type = 'orchestrator.repair_limit_reached'
          AND json_extract(payload_json, '$.task_id') = ?
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return row is not None


def _mark_task_terminal_blocked(conn: sqlite3.Connection, *, task_id: str, reason: str) -> None:
    row = conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None or str(row["status"]) == str(TaskStatus.COMPLETE):
        return
    existing = _json_list_or_empty(str(row["blocked_reasons_json"] or "[]"))
    terminal_reason = "orchestrator repair limit reached: user intervention required"
    reasons = [*existing]
    if terminal_reason not in reasons:
        reasons.append(terminal_reason)
    if reason and reason not in reasons:
        reasons.append(reason)
    _ = conn.execute(
        """
        UPDATE tasks
        SET status = ?, blocked_reasons_json = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (TaskStatus.BLOCKED, _json(reasons), utc_now(), task_id),
    )


def start_work_run(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    goal: str = "",
    active_profile_id: str | None = None,
    metadata: dict[str, object] | None = None,
    work_run_id: str | None = None,
) -> str:
    ensure_matter(conn, matter_scope)
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="matter_profile", target_id=active_profile_id, field_name="active_profile_id")
    wid = work_run_id or f"wrun-{uuid4().hex}"
    now = utc_now()
    resume_token = f"resume-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO work_runs(work_run_id, matter_scope, goal, status, active_profile_id,
          started_at, updated_at, completed_at, resume_token, metadata_json)
        VALUES (?, ?, ?, 'running', ?, ?, ?, NULL, ?, ?)
        """,
        (wid, matter_scope, goal, active_profile_id, now, now, resume_token, _json(metadata or {})),
    )
    _ = emit_event(conn, "work_run.started", matter_scope=matter_scope, payload={"work_run_id": wid, "goal": goal, "active_profile_id": active_profile_id or ""})
    return wid


def update_work_run_status(conn: sqlite3.Connection, *, work_run_id: str, status: str, matter_scope: str | None = None) -> None:
    row = conn.execute("SELECT matter_scope FROM work_runs WHERE work_run_id = ?", (work_run_id,)).fetchone()
    if row is None:
        raise ValueError(f"work run not found: {work_run_id}")
    run_matter_scope = str(row["matter_scope"])
    if matter_scope is not None and run_matter_scope != matter_scope:
        raise ValueError(f"work_run_id {work_run_id} belongs to matter {run_matter_scope}, outside matter {matter_scope}")
    now = utc_now()
    completed_at = now if status in {"complete", "failed", "cancelled"} else None
    _ = conn.execute(
        "UPDATE work_runs SET status = ?, updated_at = ?, completed_at = COALESCE(?, completed_at) WHERE work_run_id = ?",
        (status, now, completed_at, work_run_id),
    )
    _ = emit_event(conn, "work_run.status_updated", matter_scope=run_matter_scope, payload={"work_run_id": work_run_id, "status": status})


def record_work_run_step(
    conn: sqlite3.Connection,
    *,
    work_run_id: str,
    step_type: str,
    status: str,
    task_id: str | None = None,
    candidate_id: str | None = None,
    artifact_id: str | None = None,
    context_pack_id: str | None = None,
    provider_run_id: str | None = None,
    input_fingerprint: str = "",
    output_fingerprint: str = "",
    metadata: dict[str, object] | None = None,
    work_run_step_id: str | None = None,
    expected_matter_scope: str | None = None,
) -> str:
    run = conn.execute("SELECT matter_scope FROM work_runs WHERE work_run_id = ?", (work_run_id,)).fetchone()
    if run is None:
        raise ValueError(f"work run not found: {work_run_id}")
    sid = work_run_step_id or f"wstep-{uuid4().hex}"
    matter_scope = str(run["matter_scope"])
    if expected_matter_scope is not None and matter_scope != expected_matter_scope:
        raise ValueError(f"work_run_id {work_run_id} belongs to matter {matter_scope}, outside matter {expected_matter_scope}")
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="task", target_id=task_id, field_name="task_id")
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="candidate", target_id=candidate_id, field_name="candidate_id")
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="artifact", target_id=artifact_id, field_name="artifact_id")
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="context_pack", target_id=context_pack_id, field_name="context_pack_id")
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="provider_run", target_id=provider_run_id, field_name="provider_run_id")
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO work_run_steps(work_run_step_id, work_run_id, matter_scope, step_type,
          task_id, candidate_id, artifact_id, context_pack_id, provider_run_id, status,
          input_fingerprint, output_fingerprint, created_at, updated_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sid, work_run_id, matter_scope, step_type, task_id, candidate_id, artifact_id, context_pack_id, provider_run_id, status, input_fingerprint, output_fingerprint, now, now, _json(metadata or {})),
    )
    _ = conn.execute("UPDATE work_runs SET updated_at = ? WHERE work_run_id = ?", (now, work_run_id))
    _ = emit_event(conn, "work_run.step_recorded", matter_scope=matter_scope, payload={"work_run_id": work_run_id, "work_run_step_id": sid, "step_type": step_type, "status": status})
    return sid


def find_reusable_work_step(conn: sqlite3.Connection, *, matter_scope: str, step_type: str, input_fingerprint: str) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT * FROM work_run_steps
        WHERE matter_scope = ? AND step_type = ? AND input_fingerprint = ? AND status = 'complete'
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (matter_scope, step_type, input_fingerprint),
    ).fetchone()
    return _work_step_row_to_dict(row) if row is not None else None


def record_work_reuse(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    reused_from_step_id: str,
    reused_by_step_id: str | None = None,
    reuse_type: str = "exact_input_fingerprint",
    valid: bool = True,
    invalidation_reason: str = "",
    reuse_record_id: str | None = None,
) -> str:
    rid = reuse_record_id or f"reuse-{uuid4().hex}"
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="work_run_step", target_id=reused_from_step_id, field_name="reused_from_step_id")
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="work_run_step", target_id=reused_by_step_id, field_name="reused_by_step_id")
    _ = conn.execute(
        """
        INSERT INTO work_reuse_records(reuse_record_id, matter_scope, reused_from_step_id,
          reused_by_step_id, reuse_type, valid, invalidation_reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (rid, matter_scope, reused_from_step_id, reused_by_step_id, reuse_type, 1 if valid else 0, invalidation_reason, utc_now()),
    )
    _ = emit_event(conn, "work_reuse.recorded", matter_scope=matter_scope, payload={"reuse_record_id": rid, "reused_from_step_id": reused_from_step_id, "valid": valid})
    return rid


def _work_step_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    result = _row_to_plain_dict(row)
    result["metadata"] = json.loads(str(result.pop("metadata_json") or "{}"))
    return result


def add_citation_span(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    source_id: str | None = None,
    artifact_id: str | None = None,
    authority_id: str | None = None,
    start_offset: int | None = None,
    end_offset: int | None = None,
    quoted_text: str = "",
    locator: str = "",
    status: str = "candidate",
    citation_span_id: str | None = None,
) -> str:
    span_id = citation_span_id or f"cite-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO citation_spans(citation_span_id, target_type, target_id, source_id,
          artifact_id, authority_id, start_offset, end_offset, quoted_text_hash, locator, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            span_id,
            target_type,
            target_id,
            source_id,
            artifact_id,
            authority_id,
            start_offset,
            end_offset,
            _hash_text(quoted_text) if quoted_text else "",
            locator,
            status,
            utc_now(),
        ),
    )
    return span_id


def add_claim(
    conn: sqlite3.Connection,
    *,
    claim_text: str,
    matter_scope: str = "atticus",
    issue_id: str | None = None,
    support_status: str = "candidate",
    created_by_artifact_id: str | None = None,
    claim_id: str | None = None,
) -> str:
    cid = claim_id or f"claim-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO claims(claim_id, matter_scope, claim_text, issue_id, support_status,
          created_by_artifact_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (cid, matter_scope, claim_text, issue_id, support_status, created_by_artifact_id, now, now),
    )
    return cid


def add_context_pack(
    conn: sqlite3.Connection,
    *,
    context_pack_id: str,
    matter_scope: str,
    task_id: str | None,
    pack_type: str,
    fingerprint: str,
    token_budget: int,
    estimated_tokens: int,
    sections: list[dict[str, object]],
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
) -> str:
    _require_target_in_matter(conn, matter_scope=matter_scope, target_type="task", target_id=task_id, field_name="task_id")
    _ = conn.execute(
        """
        INSERT OR IGNORE INTO context_packs(context_pack_id, matter_scope, task_id, pack_type,
          fingerprint, token_budget, estimated_tokens, cache_hit_tokens, cache_miss_tokens,
          sections_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            context_pack_id,
            matter_scope,
            task_id,
            pack_type,
            fingerprint,
            token_budget,
            estimated_tokens,
            cache_hit_tokens,
            cache_miss_tokens,
            _json(sections),
            utc_now(),
        ),
    )
    if task_id:
        _ = conn.execute(
            "UPDATE tasks SET context_pack_id = ?, updated_at = ? WHERE task_id = ?",
            (context_pack_id, utc_now(), task_id),
        )
    _ = emit_event(
        conn,
        "context_pack.created",
        matter_scope=matter_scope,
        payload={"context_pack_id": context_pack_id, "task_id": task_id, "fingerprint": fingerprint},
    )
    return context_pack_id


def add_budget(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: str,
    limit_usd: float,
    hard_stop: bool = True,
    budget_id: str | None = None,
) -> str:
    bid = budget_id or f"budget-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO budgets(budget_id, scope_type, scope_id, limit_usd, hard_stop, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope_type, scope_id) DO UPDATE SET
          limit_usd=excluded.limit_usd,
          hard_stop=excluded.hard_stop,
          updated_at=excluded.updated_at
        """,
        (bid, scope_type, scope_id, limit_usd, 1 if hard_stop else 0, now, now),
    )
    row = conn.execute(
        "SELECT budget_id FROM budgets WHERE scope_type = ? AND scope_id = ?",
        (scope_type, scope_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"budget upsert failed for {scope_type}:{scope_id}")
    return str(row["budget_id"])


def add_budget_entry(
    conn: sqlite3.Connection,
    *,
    budget_id: str,
    amount_usd: float,
    entry_type: str = "provider_estimate",
    provider_run_id: str | None = None,
    budget_entry_id: str | None = None,
) -> str:
    entry_id = budget_entry_id or f"bent-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO budget_entries(budget_entry_id, budget_id, provider_run_id, amount_usd, entry_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entry_id, budget_id, provider_run_id, amount_usd, entry_type, utc_now()),
    )
    return entry_id


def budget_spent(conn: sqlite3.Connection, *, scope_type: str, scope_id: str) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(be.amount_usd), 0) AS spent
        FROM budgets b
        LEFT JOIN budget_entries be ON be.budget_id = b.budget_id
        WHERE b.scope_type = ? AND b.scope_id = ?
        """,
        (scope_type, scope_id),
    ).fetchone()
    return float(str(row["spent"] if row else 0))


def record_candidate_output(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str | None,
    worker_id: str,
    output_type: str,
    payload: dict[str, object],
    status: str = "candidate",
    quarantined_reason: str = "",
    candidate_id: str | None = None,
) -> str:
    cid = candidate_id or f"cand-{uuid4().hex}"
    matter_scope = _matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown"
    payload_json = _json(payload)
    _ = conn.execute(
        """
        INSERT INTO candidate_outputs(candidate_id, task_id, lease_id, worker_id, status,
          output_type, payload_json, payload_hash, created_at, quarantined_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid,
            task_id,
            lease_id,
            worker_id,
            status,
            output_type,
            payload_json,
            _hash_text(payload_json),
            utc_now(),
            quarantined_reason,
        ),
    )
    _ = emit_event(
        conn,
        "candidate_output.recorded",
        matter_scope=matter_scope,
        payload={"candidate_id": cid, "task_id": task_id, "status": status, "quarantined_reason": quarantined_reason},
    )
    return cid


def record_reducer_packet(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    decision: str,
    reducer_lease_id: str | None = None,
    validation_result_id: int | None = None,
    canonical_artifact_id: str | None = None,
    dissent: list[dict[str, object]] | None = None,
    reducer_packet_id: str | None = None,
) -> str:
    rid = reducer_packet_id or f"red-{uuid4().hex}"
    matter_scope = _matter_scope_for_target(conn, target_type="candidate", target_id=candidate_id) or "unknown"
    _ = conn.execute(
        """
        INSERT INTO reducer_packets(reducer_packet_id, candidate_id, reducer_lease_id, decision,
          validation_result_id, canonical_artifact_id, dissent_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rid,
            candidate_id,
            reducer_lease_id,
            decision,
            validation_result_id,
            canonical_artifact_id,
            _json(dissent or []),
            utc_now(),
        ),
    )
    _ = emit_event(
        conn,
        "reducer_packet.recorded",
        matter_scope=matter_scope,
        payload={"reducer_packet_id": rid, "candidate_id": candidate_id, "decision": decision},
    )
    return rid


def record_migration_report(
    conn: sqlite3.Connection,
    *,
    workspace_path: str,
    dry_run: bool,
    summary: dict[str, object],
    migration_report_id: str | None = None,
) -> str:
    mid = migration_report_id or f"mig-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO migration_reports(migration_report_id, workspace_path, dry_run, summary_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (mid, workspace_path, 1 if dry_run else 0, _json(summary), utc_now()),
    )
    return mid


def add_legal_memory(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    memory_type: str,
    name: str,
    description: str = "",
    content: str = "",
    status: str = "active",
    confidence: float = 0.0,
    source_refs: list[dict[str, object]] | None = None,
    last_verified_at: str | None = None,
    stale: bool = False,
    staleness_trigger: str = "",
    memory_id: str | None = None,
) -> str:
    ensure_matter(conn, matter_scope)
    _validate_legal_memory(
        conn,
        matter_scope=matter_scope,
        memory_type=memory_type,
        confidence=confidence,
        source_refs=source_refs or [],
    )
    mid = memory_id or f"mem-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO legal_memories(memory_id, matter_scope, type, name, description, content,
          status, confidence, source_refs_json, last_verified_at, stale, staleness_trigger, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mid,
            matter_scope,
            memory_type,
            name,
            description,
            content,
            status,
            confidence,
            _json(source_refs or []),
            last_verified_at,
            1 if stale else 0,
            staleness_trigger,
            now,
            now,
        ),
    )
    _ = emit_event(
        conn,
        "legal_memory.added",
        matter_scope=matter_scope,
        payload={"memory_id": mid, "type": memory_type, "status": status, "stale": stale},
    )
    return mid


def list_legal_memories(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    status: str | None = "active",
) -> list[dict[str, object]]:
    if status is None:
        rows = conn.execute(
            "SELECT * FROM legal_memories WHERE matter_scope = ? ORDER BY type, name, memory_id",
            (matter_scope,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM legal_memories WHERE matter_scope = ? AND status = ? ORDER BY type, name, memory_id",
            (matter_scope, status),
        ).fetchall()
    return [_memory_row_to_dict(row) for row in rows]


def get_legal_memory(conn: sqlite3.Connection, *, memory_id: str, matter_scope: str | None = None) -> dict[str, object] | None:
    if matter_scope is None:
        row = conn.execute("SELECT * FROM legal_memories WHERE memory_id = ?", (memory_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM legal_memories WHERE memory_id = ? AND matter_scope = ?", (memory_id, matter_scope)).fetchone()
    return _memory_row_to_dict(row) if row is not None else None


def mark_legal_memory_stale(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    matter_scope: str,
    reason: str,
) -> None:
    now = utc_now()
    cur = conn.execute(
        """
        UPDATE legal_memories
        SET stale = 1, staleness_trigger = ?, updated_at = ?
        WHERE memory_id = ? AND matter_scope = ?
        """,
        (reason, now, memory_id, matter_scope),
    )
    if cur.rowcount != 1:
        raise ValueError(f"memory not found for matter {matter_scope}: {memory_id}")
    _ = emit_event(
        conn,
        "legal_memory.marked_stale",
        matter_scope=matter_scope,
        payload={"memory_id": memory_id, "reason": reason},
    )


def _validate_legal_memory(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    memory_type: str,
    confidence: float,
    source_refs: list[dict[str, object]],
) -> None:
    if memory_type not in LEGAL_MEMORY_TYPES:
        raise ValueError(f"unknown legal memory type: {memory_type}")
    if confidence < 0 or confidence > 1:
        raise ValueError("memory confidence must be between 0 and 1")
    if memory_type in SOURCE_REQUIRED_MEMORY_TYPES and not source_refs:
        raise ValueError(f"{memory_type} memory requires source_refs")
    for index, ref in enumerate(source_refs):
        target_type = str(ref.get("target_type") or "")
        target_id = str(ref.get("target_id") or "")
        if not target_type or not target_id:
            raise ValueError(f"source_refs[{index}] requires target_type and target_id")
        if memory_type in SOURCE_REQUIRED_MEMORY_TYPES and target_type in {"memory", "validation_result"}:
            raise ValueError(f"source_refs[{index}] cannot use orientation-only target type for {memory_type} memory: {target_type}")
        if not _memory_ref_exists(conn, matter_scope=matter_scope, target_type=target_type, target_id=target_id):
            raise ValueError(f"source_refs[{index}] target does not exist in matter {matter_scope}: {target_type}:{target_id}")


def _memory_ref_exists(conn: sqlite3.Connection, *, matter_scope: str, target_type: str, target_id: str) -> bool:
    if target_type == "source":
        sql = "SELECT 1 FROM sources WHERE source_id = ? AND matter_scope = ? LIMIT 1"
    elif target_type == "artifact":
        sql = "SELECT 1 FROM artifacts WHERE artifact_id = ? AND matter_scope = ? LIMIT 1"
    elif target_type == "authority":
        sql = "SELECT 1 FROM legal_authorities WHERE authority_id = ? AND matter_scope = ? LIMIT 1"
    elif target_type == "claim":
        sql = "SELECT 1 FROM claims WHERE claim_id = ? AND matter_scope = ? LIMIT 1"
    elif target_type == "chronology_event":
        sql = "SELECT 1 FROM chronology_events WHERE chronology_event_id = ? AND matter_scope = ? LIMIT 1"
    elif target_type == "memory":
        sql = "SELECT 1 FROM legal_memories WHERE memory_id = ? AND matter_scope = ? LIMIT 1"
    elif target_type == "validation_result":
        return conn.execute(
            "SELECT 1 FROM validation_results WHERE validation_result_id = ? AND matter_scope = ? LIMIT 1",
            (target_id, matter_scope),
        ).fetchone() is not None
    else:
        return False
    return conn.execute(sql, (target_id, matter_scope)).fetchone() is not None


def _memory_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    result: dict[str, object] = {str(key): row[key] for key in row.keys()}
    result["source_refs"] = json.loads(str(result.pop("source_refs_json") or "[]"))
    result["stale"] = bool(result["stale"])
    return result


SESSION_ROLES = {"user", "assistant", "system", "tool", "worker", "operator"}
HOOK_SEVERITIES = {"info", "warning", "blocker"}


def create_session(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    title: str,
    status: str = "active",
    session_id: str | None = None,
) -> str:
    if status not in {"active", "paused", "closed", "archived"}:
        raise ValueError(f"unsupported session status: {status}")
    _ensure_session_hook_tables(conn)
    ensure_matter(conn, matter_scope)
    sid = session_id or f"sess-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO sessions(session_id, matter_scope, title, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (sid, matter_scope, title, status, now, now),
    )
    _ = emit_event(
        conn,
        "session.created",
        matter_scope=matter_scope,
        payload={"session_id": sid, "status": status, "title": title},
    )
    return sid


def record_session_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    content: dict[str, object],
    context_pack_id: str | None = None,
    provider_run_id: str | None = None,
    candidate_id: str | None = None,
    reducer_packet_id: str | None = None,
    session_message_id: str | None = None,
) -> str:
    _ensure_session_hook_tables(conn)
    if role not in SESSION_ROLES:
        raise ValueError(f"unsupported session message role: {role}")
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if session is None:
        raise ValueError(f"session not found: {session_id}")
    mid = session_message_id or f"smsg-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO session_messages(session_message_id, session_id, role, content_json,
          context_pack_id, provider_run_id, candidate_id, reducer_packet_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mid,
            session_id,
            role,
            _json(content),
            context_pack_id,
            provider_run_id,
            candidate_id,
            reducer_packet_id,
            now,
        ),
    )
    _ = conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))
    _ = emit_event(
        conn,
        "session.message_recorded",
        matter_scope=str(session["matter_scope"]),
        payload={
            "session_id": session_id,
            "session_message_id": mid,
            "role": role,
            "context_pack_id": context_pack_id or "",
            "provider_run_id": provider_run_id or "",
            "candidate_id": candidate_id or "",
            "reducer_packet_id": reducer_packet_id or "",
        },
    )
    return mid


def list_sessions(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    status: str | None = None,
) -> list[dict[str, object]]:
    if not _table_exists(conn, "sessions"):
        return []
    if status is None:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE matter_scope = ? ORDER BY updated_at DESC, created_at DESC",
            (matter_scope,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM sessions
            WHERE matter_scope = ? AND status = ?
            ORDER BY updated_at DESC, created_at DESC
            """,
            (matter_scope, status),
        ).fetchall()
    return [_session_row_to_dict(row) for row in rows]


def get_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    matter_scope: str | None = None,
) -> dict[str, object] | None:
    if not _table_exists(conn, "sessions"):
        return None
    if matter_scope is None:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND matter_scope = ?",
            (session_id, matter_scope),
        ).fetchone()
    return _session_row_to_dict(row) if row is not None else None


def list_session_messages(conn: sqlite3.Connection, *, session_id: str) -> list[dict[str, object]]:
    if not _table_exists(conn, "session_messages"):
        return []
    rows = conn.execute(
        "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at, session_message_id",
        (session_id,),
    ).fetchall()
    return [_session_message_row_to_dict(row) for row in rows]


def export_session(conn: sqlite3.Connection, *, session_id: str, matter_scope: str) -> dict[str, object]:
    session = get_session(conn, session_id=session_id, matter_scope=matter_scope)
    if session is None:
        raise ValueError(f"session not found: {session_id}")
    return {"session": session, "messages": list_session_messages(conn, session_id=session_id)}


def export_session_for_matter(conn: sqlite3.Connection, *, session_id: str, matter_scope: str) -> dict[str, object]:
    return export_session(conn, session_id=session_id, matter_scope=matter_scope)


def record_hook_invocation(
    conn: sqlite3.Connection,
    *,
    hook_event: str,
    matter_scope: str,
    allowed: bool,
    severity: str,
    message: str,
    details: dict[str, object] | None = None,
    hook_invocation_id: str | None = None,
) -> str:
    _ensure_session_hook_tables(conn)
    if severity not in HOOK_SEVERITIES:
        raise ValueError(f"unsupported hook severity: {severity}")
    hid = hook_invocation_id or f"hook-{uuid4().hex}"
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO hook_invocations(hook_invocation_id, hook_event, matter_scope,
          allowed, severity, message, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (hid, hook_event, matter_scope, 1 if allowed else 0, severity, message, _json(details or {}), now),
    )
    _ = emit_event(
        conn,
        "hook.evaluated",
        matter_scope=matter_scope,
        payload={
            "hook_invocation_id": hid,
            "hook_event": hook_event,
            "allowed": allowed,
            "severity": severity,
            "message": message,
        },
    )
    return hid


def _session_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {str(key): row[key] for key in row.keys()}


def _row_to_plain_dict(row: sqlite3.Row) -> dict[str, object]:
    return {str(key): row[key] for key in row.keys()}


def _session_message_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    result: dict[str, object] = {str(key): row[key] for key in row.keys()}
    result["content"] = json.loads(str(result.pop("content_json") or "{}"))
    return result


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _ensure_session_hook_tables(conn: sqlite3.Connection) -> None:
    for statement in (
        """
        CREATE TABLE IF NOT EXISTS sessions (
          session_id TEXT PRIMARY KEY,
          matter_scope TEXT NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE IF NOT EXISTS session_messages (
          session_message_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
          role TEXT NOT NULL,
          content_json TEXT NOT NULL CHECK(json_valid(content_json)),
          context_pack_id TEXT REFERENCES context_packs(context_pack_id) ON DELETE SET NULL,
          provider_run_id TEXT REFERENCES provider_runs(provider_run_id) ON DELETE SET NULL,
          candidate_id TEXT REFERENCES candidate_outputs(candidate_id) ON DELETE SET NULL,
          reducer_packet_id TEXT REFERENCES reducer_packets(reducer_packet_id) ON DELETE SET NULL,
          created_at TEXT NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE IF NOT EXISTS hook_invocations (
          hook_invocation_id TEXT PRIMARY KEY,
          hook_event TEXT NOT NULL,
          matter_scope TEXT NOT NULL,
          allowed INTEGER NOT NULL CHECK(allowed IN (0, 1)),
          severity TEXT NOT NULL,
          message TEXT NOT NULL,
          details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
          created_at TEXT NOT NULL
        ) STRICT
        """,
        "CREATE INDEX IF NOT EXISTS sessions_scope_status_idx ON sessions(matter_scope, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS session_messages_session_idx ON session_messages(session_id, created_at)",
        "CREATE INDEX IF NOT EXISTS hook_invocations_event_idx ON hook_invocations(hook_event, matter_scope, created_at)",
    ):
        _ = conn.execute(statement)


def fetch_one(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(query, params).fetchone()


def fetch_all(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(query, params))
