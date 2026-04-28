"""Matter seeding and task provider-policy update helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import LegalStage, TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.policy import canonical_provider_policy
from atticus.workers.contracts import safe_path_component


@dataclass(frozen=True)
class MatterSeedResult:
    dry_run: bool
    matter_scope: str
    workspace: str
    inventory: str
    matter_created: int = 0
    matter_updated: int = 0
    sources_created: int = 0
    sources_updated: int = 0
    sources_skipped: int = 0
    source_snapshots_created: int = 0
    tracked_files_created: int = 0
    tracked_files_updated: int = 0
    tasks_created: int = 0
    missing_files: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "matter_scope": self.matter_scope,
            "workspace": self.workspace,
            "inventory": self.inventory,
            "matter_created": self.matter_created,
            "matter_updated": self.matter_updated,
            "sources_created": self.sources_created,
            "sources_updated": self.sources_updated,
            "sources_skipped": self.sources_skipped,
            "source_snapshots_created": self.source_snapshots_created,
            "tracked_files_created": self.tracked_files_created,
            "tracked_files_updated": self.tracked_files_updated,
            "tasks_created": self.tasks_created,
            "missing_files": self.missing_files,
        }


@dataclass(frozen=True)
class ProviderPolicySetResult:
    dry_run: bool
    matter_scope: str
    provider_policy: dict[str, object]
    tasks_matched: int
    tasks_updated: int
    task_ids: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "matter_scope": self.matter_scope,
            "provider_policy": self.provider_policy,
            "tasks_matched": self.tasks_matched,
            "tasks_updated": self.tasks_updated,
            "task_ids": self.task_ids,
        }


def seed_matter_from_inventory(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    workspace: str | Path,
    inventory: str | Path,
    provider: str,
    model: str,
    allow_fallback: bool = False,
    estimated_cost_usd: float = 0.0,
    dry_run: bool = True,
) -> MatterSeedResult:
    """Seed or repair a matter from a local CSV inventory without calling providers."""

    matter_scope = _required_text(matter_scope, "matter_scope")
    workspace_path = Path(workspace).expanduser().resolve()
    inventory_path = Path(inventory).expanduser().resolve()
    if not inventory_path.exists():
        raise ValueError(f"inventory does not exist: {inventory_path}")
    if not workspace_path.exists():
        raise ValueError(f"workspace does not exist: {workspace_path}")

    provider_policy = canonical_provider_policy(
        provider=provider,
        model=model,
        allow_fallback=allow_fallback,
        estimated_cost_usd=estimated_cost_usd,
    )
    rows = _read_inventory(inventory_path)
    matter_exists = _matter_exists(conn, matter_scope)
    matter_created = 0 if matter_exists else 1
    matter_updated = 1 if matter_exists else 0
    sources_created = 0
    sources_updated = 0
    sources_skipped = 0
    snapshots_created = 0
    tracked_created = 0
    tracked_updated = 0
    missing_files: list[dict[str, object]] = []

    if not dry_run:
        repo.ensure_matter(conn, matter_scope, _title_from_scope(matter_scope))

    for index, row in enumerate(rows, start=2):
        record = _inventory_record(row, workspace_path=workspace_path, inventory_path=inventory_path, line_number=index)
        if record is None:
            sources_skipped += 1
            missing_files.append({"line": index, "path": _row_path(row), "reason": "missing file"})
            continue
        existing = _existing_source(conn, source_id=record.source_id, matter_scope=matter_scope, source_path=record.source_path)
        if existing is None:
            sources_created += 1
            if not dry_run:
                _insert_source(conn, matter_scope=matter_scope, record=record)
                snapshots_created += _ensure_source_snapshot(conn, source_id=record.source_id, record=record)
            else:
                snapshots_created += 1
        else:
            existing_source_id = str(existing["source_id"])
            changed = _source_changed(existing, record)
            if changed:
                sources_updated += 1
                if not dry_run:
                    _update_source(conn, source_id=existing_source_id, matter_scope=matter_scope, record=record)
            if dry_run:
                snapshots_created += 0 if _source_snapshot_exists(conn, source_id=existing_source_id, sha256=record.sha256) else 1
            else:
                snapshots_created += _ensure_source_snapshot(conn, source_id=existing_source_id, record=record)
        tracked_state = _tracked_file_state(conn, matter_scope=matter_scope, absolute_path=str(record.absolute_path))
        if tracked_state is None:
            tracked_created += 1
        elif _tracked_file_changed(tracked_state, record):
            tracked_updated += 1
        if not dry_run:
            _upsert_tracked_file(conn, matter_scope=matter_scope, record=record)

    tasks_created = 0
    if _task_count_for_matter(conn, matter_scope) == 0:
        tasks_created = 1
        if not dry_run:
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=f"{safe_path_component(matter_scope)}-foundation-source-inventory",
                    title=f"{_title_from_scope(matter_scope)} source inventory foundation",
                    task_type="source_inventory",
                    matter_scope=matter_scope,
                    stage=LegalStage.S0_SOURCE_INVENTORY,
                    status=TaskStatus.QUEUED,
                    provider_policy=provider_policy,
                    expected_value=10.0,
                ),
            )
    if not dry_run:
        _ = repo.emit_event(
            conn,
            "matter.seeded",
            matter_scope=matter_scope,
            payload={
                "inventory": str(inventory_path),
                "workspace": str(workspace_path),
                "sources_created": sources_created,
                "sources_updated": sources_updated,
                "sources_skipped": sources_skipped,
                "tasks_created": tasks_created,
            },
        )

    return MatterSeedResult(
        dry_run=dry_run,
        matter_scope=matter_scope,
        workspace=str(workspace_path),
        inventory=str(inventory_path),
        matter_created=matter_created,
        matter_updated=matter_updated,
        sources_created=sources_created,
        sources_updated=sources_updated,
        sources_skipped=sources_skipped,
        source_snapshots_created=snapshots_created,
        tracked_files_created=tracked_created,
        tracked_files_updated=tracked_updated,
        tasks_created=tasks_created,
        missing_files=missing_files,
    )


def set_provider_policy_for_matter(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    provider: str,
    model: str,
    allow_fallback: bool = False,
    estimated_cost_usd: float = 0.0,
    statuses: Iterable[str] = (TaskStatus.QUEUED,),
    dry_run: bool = True,
) -> ProviderPolicySetResult:
    """Set a normalized provider policy on matter-scoped tasks."""

    matter_scope = _required_text(matter_scope, "matter_scope")
    policy = canonical_provider_policy(
        provider=provider,
        model=model,
        allow_fallback=allow_fallback,
        estimated_cost_usd=estimated_cost_usd,
    )
    status_values = tuple(str(status) for status in statuses)
    if not status_values:
        raise ValueError("at least one task status is required")
    placeholders = ",".join("?" for _ in status_values)
    rows = [
        cast(Mapping[str, object], row)
        for row in conn.execute(
            f"""
            SELECT task_id, provider_policy_json
            FROM tasks
            WHERE matter_scope = ? AND status IN ({placeholders})
            ORDER BY task_id
            """,
            (matter_scope, *status_values),
        )
    ]
    policy_json = _json(policy)
    changed_ids = [str(row["task_id"]) for row in rows if str(row["provider_policy_json"] or "{}") != policy_json]
    if not dry_run and changed_ids:
        _ = conn.execute(
            f"""
            UPDATE tasks
            SET provider_policy_json = ?, updated_at = ?
            WHERE matter_scope = ? AND status IN ({placeholders})
            """,
            (policy_json, utc_now(), matter_scope, *status_values),
        )
        _ = repo.emit_event(
            conn,
            "provider_policy.set",
            matter_scope=matter_scope,
            payload={"task_ids": [str(row["task_id"]) for row in rows], "provider_policy": policy},
        )
    return ProviderPolicySetResult(
        dry_run=dry_run,
        matter_scope=matter_scope,
        provider_policy=policy,
        tasks_matched=len(rows),
        tasks_updated=len(changed_ids) if not dry_run else 0,
        task_ids=[str(row["task_id"]) for row in rows],
    )


@dataclass(frozen=True)
class _InventoryRecord:
    source_id: str
    source_path: str
    absolute_path: Path
    relative_path: str
    source_type: str
    sha256: str
    size_bytes: int
    metadata: dict[str, object]


def _read_inventory(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [{str(key): str(value or "") for key, value in row.items()} for row in reader]


def _inventory_record(
    row: Mapping[str, str],
    *,
    workspace_path: Path,
    inventory_path: Path,
    line_number: int,
) -> _InventoryRecord | None:
    row_path = _row_path(row)
    if not row_path:
        return None
    absolute_path = _resolve_inventory_file(workspace_path, row_path)
    if not absolute_path.exists():
        original = row.get("original_relative_path", "").strip()
        alternate = _resolve_inventory_file(workspace_path, original) if original else absolute_path
        absolute_path = alternate if alternate.exists() else absolute_path
    if not absolute_path.exists() or not absolute_path.is_file():
        return None
    sha256 = row.get("sha256", "").strip().lower()
    if not _looks_like_sha256(sha256):
        sha256 = _sha256_file(absolute_path)
    size_bytes = _int_value(row.get("size_bytes"), default=absolute_path.stat().st_size)
    if size_bytes <= 0:
        size_bytes = absolute_path.stat().st_size
    source_path = row_path
    source_id = row.get("source_id", "").strip() or _stable_id("src", source_path, sha256)
    source_type = row.get("category", "").strip() or absolute_path.suffix.lstrip(".") or "file"
    relative_path = _relative_to_workspace(absolute_path, workspace_path) or source_path
    metadata: dict[str, object] = {
        "inventory": str(inventory_path),
        "inventory_line": line_number,
        "original_relative_path": row.get("original_relative_path", "").strip(),
        "stored_path": row.get("stored_path", "").strip(),
        "category": row.get("category", "").strip(),
        "urgent_flag": row.get("urgent_flag", "").strip(),
        "notes": row.get("notes", "").strip(),
    }
    return _InventoryRecord(
        source_id=source_id,
        source_path=source_path,
        absolute_path=absolute_path.resolve(),
        relative_path=relative_path,
        source_type=source_type,
        sha256=sha256,
        size_bytes=size_bytes,
        metadata=metadata,
    )


def _row_path(row: Mapping[str, str]) -> str:
    for key in ("stored_path", "path", "relative_path", "original_relative_path"):
        value = row.get(key, "").strip()
        if value:
            return value
    return ""


def _resolve_inventory_file(workspace_path: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workspace_path / path).resolve()


def _insert_source(conn: sqlite3.Connection, *, matter_scope: str, record: _InventoryRecord) -> None:
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO sources(source_id, matter_scope, path, source_type, sha256, size_bytes,
          trust_status, stage, imported_from, chain_of_custody_json, stale, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            record.source_id,
            matter_scope,
            record.source_path,
            record.source_type,
            record.sha256,
            record.size_bytes,
            TrustStatus.CANDIDATE,
            LegalStage.S0_SOURCE_INVENTORY,
            str(record.metadata["inventory"]),
            _json(record.metadata),
            now,
            now,
        ),
    )
    _ = repo.emit_event(
        conn,
        "source.registered",
        matter_scope=matter_scope,
        payload={"source_id": record.source_id, "path": record.source_path, "sha256": record.sha256},
    )


def _update_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    matter_scope: str,
    record: _InventoryRecord,
) -> None:
    _ = conn.execute(
        """
        UPDATE sources
        SET matter_scope = ?, path = ?, source_type = ?, sha256 = ?, size_bytes = ?,
          trust_status = ?, stage = ?, imported_from = ?, chain_of_custody_json = ?,
          stale = 0, updated_at = ?
        WHERE source_id = ?
        """,
        (
            matter_scope,
            record.source_path,
            record.source_type,
            record.sha256,
            record.size_bytes,
            TrustStatus.CANDIDATE,
            LegalStage.S0_SOURCE_INVENTORY,
            str(record.metadata["inventory"]),
            _json(record.metadata),
            utc_now(),
            source_id,
        ),
    )


def _ensure_source_snapshot(conn: sqlite3.Connection, *, source_id: str, record: _InventoryRecord) -> int:
    if _source_snapshot_exists(conn, source_id=source_id, sha256=record.sha256):
        return 0
    _ = repo.add_source_snapshot(
        conn,
        source_id=source_id,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
        captured_by="matter-seed",
        custody_note=f"seeded from {record.metadata['inventory']}",
        metadata=record.metadata,
    )
    return 1


def _source_snapshot_exists(conn: sqlite3.Connection, *, source_id: str, sha256: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM source_snapshots WHERE source_id = ? AND sha256 = ? LIMIT 1",
        (source_id, sha256),
    ).fetchone() is not None


def _upsert_tracked_file(conn: sqlite3.Connection, *, matter_scope: str, record: _InventoryRecord) -> None:
    tracked_id = _stable_id("tfile", matter_scope, str(record.absolute_path))
    now = utc_now()
    _ = conn.execute(
        """
        INSERT INTO tracked_files(tracked_file_id, matter_scope, absolute_path, relative_path,
          sha256, size_bytes, file_kind, status, provenance, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?, ?, ?)
        ON CONFLICT(matter_scope, absolute_path) DO UPDATE SET
          relative_path = excluded.relative_path,
          sha256 = excluded.sha256,
          size_bytes = excluded.size_bytes,
          file_kind = excluded.file_kind,
          status = excluded.status,
          provenance = excluded.provenance,
          metadata_json = excluded.metadata_json,
          updated_at = excluded.updated_at
        """,
        (
            tracked_id,
            matter_scope,
            str(record.absolute_path),
            record.relative_path,
            record.sha256,
            record.size_bytes,
            record.source_type,
            str(record.metadata["inventory"]),
            _json(record.metadata),
            now,
            now,
        ),
    )


def _existing_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    matter_scope: str,
    source_path: str,
) -> Mapping[str, object] | None:
    row = conn.execute(
        """
        SELECT *
        FROM sources
        WHERE source_id = ? OR (matter_scope = ? AND path = ?)
        ORDER BY CASE WHEN source_id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (source_id, matter_scope, source_path, source_id),
    ).fetchone()
    return cast(Mapping[str, object] | None, row)


def _tracked_file_state(conn: sqlite3.Connection, *, matter_scope: str, absolute_path: str) -> Mapping[str, object] | None:
    row = conn.execute(
        "SELECT * FROM tracked_files WHERE matter_scope = ? AND absolute_path = ?",
        (matter_scope, absolute_path),
    ).fetchone()
    return cast(Mapping[str, object] | None, row)


def _source_changed(row: Mapping[str, object], record: _InventoryRecord) -> bool:
    return any(
        (
            str(row["path"]) != record.source_path,
            str(row["source_type"]) != record.source_type,
            str(row["sha256"]) != record.sha256,
            int(str(row["size_bytes"])) != record.size_bytes,
            str(row["chain_of_custody_json"]) != _json(record.metadata),
        )
    )


def _tracked_file_changed(row: Mapping[str, object], record: _InventoryRecord) -> bool:
    return any(
        (
            str(row["relative_path"]) != record.relative_path,
            str(row["sha256"]) != record.sha256,
            int(str(row["size_bytes"])) != record.size_bytes,
            str(row["file_kind"]) != record.source_type,
            str(row["metadata_json"]) != _json(record.metadata),
        )
    )


def _matter_exists(conn: sqlite3.Connection, matter_scope: str) -> bool:
    return conn.execute("SELECT 1 FROM matters WHERE matter_scope = ?", (matter_scope,)).fetchone() is not None


def _task_count_for_matter(conn: sqlite3.Connection, matter_scope: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE matter_scope = ?", (matter_scope,)).fetchone()
    return int(str(row["n"] if row is not None else 0))


def _title_from_scope(matter_scope: str) -> str:
    return matter_scope.replace("-", " ").strip().title()


def _relative_to_workspace(path: Path, workspace_path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace_path.resolve()))
    except ValueError:
        return ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _looks_like_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _int_value(value: object, *, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return default
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _required_text(value: str, name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
