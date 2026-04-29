"""Repository helpers around the Atticus SQLite ledger."""

from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import sqlite3
from uuid import uuid4

from atticus.core.events import Event, utc_now
from atticus.core.policies import LegalStage, TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db.schema import DDL, SCHEMA_VERSION
from atticus.memory.types import LEGAL_MEMORY_TYPES, SOURCE_REQUIRED_MEMORY_TYPES


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
def db_connection(db_path: str | Path, *, read_only: bool = False) -> Generator[sqlite3.Connection, None, None]:
    conn = connect(db_path, read_only=read_only)
    try:
        yield conn
        if not read_only:
            conn.commit()
    finally:
        conn.close()


def initialize_database(db_path: str | Path) -> None:
    with db_connection(db_path) as conn:
        _ = conn.executescript(DDL)
        _ensure_columns(conn)
        _ensure_indexes(conn)
        _ = conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        ensure_matter(conn, "atticus", "Default Atticus matter")


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
        },
        "human_attention": {
            "matter_scope": "TEXT NOT NULL DEFAULT 'unknown'",
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


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    _ = conn.execute("DROP INDEX IF EXISTS validation_target_idx")
    _ = conn.execute(
        "CREATE INDEX IF NOT EXISTS validation_target_idx ON validation_results(matter_scope, target_type, target_id, gate_name, passed)"
    )
    _ = conn.execute(
        "CREATE INDEX IF NOT EXISTS human_attention_scope_status_idx ON human_attention(matter_scope, status, severity, created_at)"
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
                SELECT COALESCE(t.matter_scope, r.matter_scope) AS matter_scope
                FROM provider_runs pr
                LEFT JOIN tasks t ON t.task_id = pr.task_id
                LEFT JOIN runs r ON r.run_id = pr.run_id
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


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


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


def update_task_blocked(conn: sqlite3.Connection, task_id: str, reasons: list[str]) -> None:
    matter_scope = _matter_scope_for_target(conn, target_type="task", target_id=task_id) or "unknown"
    _ = conn.execute(
        """
        UPDATE tasks
        SET status = ?, blocked_reasons_json = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (TaskStatus.BLOCKED, _json(reasons), utc_now(), task_id),
    )
    _ = record_human_attention(
        conn,
        target_type="task",
        target_id=task_id,
        severity="blocker",
        reason="; ".join(reasons),
    )
    _ = emit_event(conn, "task.blocked", matter_scope=matter_scope, payload={"task_id": task_id, "reasons": reasons})


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
    estimated_cost_usd: float = 0.0,
    actual_cost_usd: float | None = None,
    latency_ms: int = 0,
    retries: int = 0,
    fallback_allowed: bool = False,
    fallback_policy_result: str = "not_needed",
    raw_usage: dict[str, object] | None = None,
) -> str:
    rid = provider_run_id or f"prun-{uuid4().hex}"
    matter_scope = None
    if task_id is not None:
        matter_scope = _matter_scope_for_target(conn, target_type="task", target_id=task_id)
    if matter_scope is None and run_id is not None:
        matter_scope = _matter_scope_for_target(conn, target_type="run", target_id=run_id)
    matter_scope = matter_scope or "unknown"
    _ = conn.execute(
        """
        INSERT INTO provider_runs(provider_run_id, task_id, run_id, stage, requested_provider,
          requested_model, actual_provider, actual_model, input_tokens, output_tokens,
          cache_hit_tokens, cache_miss_tokens, estimated_cost_usd, actual_cost_usd, latency_ms,
          retries, fallback_allowed, fallback_policy_result, raw_usage_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            estimated_cost_usd,
            actual_cost_usd,
            latency_ms,
            retries,
            1 if fallback_allowed else 0,
            fallback_policy_result,
            _json(raw_usage or {}),
            utc_now(),
        ),
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


def record_human_attention(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    severity: str,
    reason: str,
    status: str = "open",
    matter_scope: str | None = None,
) -> int:
    target_matter_scope = _matter_scope_for_target(conn, target_type=target_type, target_id=target_id)
    if matter_scope is not None and target_matter_scope is not None and matter_scope != target_matter_scope:
        raise ValueError(f"human attention matter_scope {matter_scope!r} does not match target matter {target_matter_scope!r}")
    resolved_matter_scope = matter_scope or target_matter_scope or "unknown"
    cur = conn.execute(
        """
        INSERT INTO human_attention(matter_scope, target_type, target_id, severity, reason, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (resolved_matter_scope, target_type, target_id, severity, reason, status, utc_now()),
    )
    lastrowid = cur.lastrowid
    if lastrowid is None:
        raise RuntimeError("human attention insert did not produce a row id")
    return int(lastrowid)


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


def export_session(conn: sqlite3.Connection, *, session_id: str) -> dict[str, object]:
    session = get_session(conn, session_id=session_id)
    if session is None:
        raise ValueError(f"session not found: {session_id}")
    return {"session": session, "messages": list_session_messages(conn, session_id=session_id)}


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
