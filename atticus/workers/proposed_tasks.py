"""Import reducer-approved follow-up tasks from worker candidate packets."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
import sqlite3
from typing import cast

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.model_policy import validate_proposed_task_provider_policy
from atticus.providers.policy import canonical_provider_policy


def import_proposed_tasks_from_candidate(conn: sqlite3.Connection, candidate: Mapping[str, object]) -> list[str]:
    payload = json.loads(str(candidate["payload_json"]))
    if not isinstance(payload, Mapping):
        return []
    payload_map = {str(key): value for key, value in cast(Mapping[object, object], payload).items()}
    raw_tasks = payload_map.get("proposed_tasks", [])
    if not isinstance(raw_tasks, list):
        return []
    proposed_tasks = cast(list[object], raw_tasks)
    imported: list[str] = []
    parent_task_id = str(candidate["task_id"])
    candidate_id = str(candidate["candidate_id"])
    parent_task = cast(Mapping[str, object] | None, conn.execute("SELECT provider_policy_json, matter_scope FROM tasks WHERE task_id = ?", (parent_task_id,)).fetchone())
    parent_policy = _load_parent_provider_policy(parent_task)
    parent_matter_scope = str(parent_task["matter_scope"]) if parent_task is not None else "atticus"
    for index, raw_task in enumerate(proposed_tasks, start=1):
        if not isinstance(raw_task, Mapping):
            continue
        task_map = cast(Mapping[object, object], raw_task)
        task_id = str(task_map.get("task_id") or f"{parent_task_id}-followup-{index}")
        proposed_matter_scope = str(task_map.get("matter_scope") or parent_matter_scope)
        if proposed_matter_scope != parent_matter_scope:
            _record_rejected_proposed_task(
                conn,
                task_id=task_id,
                reason=f"proposed task matter_scope {proposed_matter_scope!r} does not match parent matter {parent_matter_scope!r}",
            )
            continue
        matter_scope = parent_matter_scope
        source_dependencies = _string_list(task_map.get("source_dependencies"))
        if not source_dependencies:
            source_dependencies = _infer_source_dependencies(conn, matter_scope=matter_scope, task_map=task_map)
        artifact_dependencies = _string_list(task_map.get("artifact_dependencies"))
        task_dependencies = _string_list(task_map.get("task_dependencies"))
        matter_dependencies = _string_list(task_map.get("matter_dependencies"))
        dependency_error = _dependency_error(
            conn,
            matter_scope=matter_scope,
            source_dependencies=source_dependencies,
            artifact_dependencies=artifact_dependencies,
            task_dependencies=task_dependencies,
            matter_dependencies=matter_dependencies,
        )
        if dependency_error:
            _record_rejected_proposed_task(conn, task_id=task_id, reason=dependency_error)
            continue
        if _task_exists(conn, task_id):
            if _existing_task_is_same_import(conn, task_id=task_id, matter_scope=matter_scope, candidate_id=candidate_id):
                imported.append(task_id)
            else:
                _record_rejected_proposed_task(conn, task_id=task_id, reason="proposed task id collides with an existing task")
            continue
        stage = str(task_map.get("stage") or LegalStage.S0_SOURCE_INVENTORY)
        try:
            provider_policy = _provider_policy(task_map, parent_policy=parent_policy)
        except ValueError as exc:
            _ = repo.record_human_attention(
                conn,
                target_type="proposed_task",
                target_id=task_id,
                severity="blocker",
                reason=f"proposed task rejected: {exc}",
            )
            continue
        repo.add_task(
            conn,
            TaskSpec(
                task_id=task_id,
                title=str(task_map.get("title") or task_id),
                task_type=str(task_map.get("task_type") or "followup"),
                matter_scope=matter_scope,
                stage=cast(LegalStage, cast(object, stage)),
                status=TaskStatus.QUEUED,
                source_dependencies=source_dependencies,
                artifact_dependencies=artifact_dependencies,
                task_dependencies=task_dependencies,
                matter_dependencies=matter_dependencies,
                required_certifications=_mapping_list(task_map.get("required_certifications")),
                validation_gates=_string_list(task_map.get("validation_gates")),
                staleness_rules=_dict(task_map.get("staleness_rules")),
                provider_policy=provider_policy,
                cost_limit_usd=_optional_float(task_map.get("cost_limit_usd")),
                expected_value=_optional_float(task_map.get("expected_value")) or 0.0,
            ),
        )
        _ = conn.execute(
            """
            UPDATE tasks
            SET parent_task_id = ?, imported_from_candidate_id = ?, task_provenance_json = ?
            WHERE task_id = ?
            """,
            (
                parent_task_id,
                candidate_id,
                json.dumps(
                    {"parent_task_id": parent_task_id, "imported_from_candidate_id": candidate_id, "proposed_task_index": index},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                task_id,
            ),
        )
        imported.append(task_id)
    return imported


def _provider_policy(task_map: Mapping[object, object], *, parent_policy: Mapping[str, object] | None = None) -> dict[str, object]:
    if parent_policy:
        return validate_proposed_task_provider_policy(parent_provider_policy=parent_policy, proposed_task=task_map)
    raw = task_map.get("provider_policy")
    policy = dict(cast(Mapping[str, object], raw)) if isinstance(raw, Mapping) else {}
    provider = str(policy.get("provider") or task_map.get("provider") or "").strip()
    model = str(policy.get("model") or task_map.get("model") or "").strip()
    if not provider or not model:
        raise ValueError("proposed task provider policy is required when no parent routing policy is available")
    return canonical_provider_policy(
        provider=provider,
        model=model,
        allow_fallback=bool(policy.get("allow_fallback") or False),
        estimated_cost_usd=_optional_float(policy.get("estimated_cost_usd")) or 0.0,
    )


def _load_parent_provider_policy(parent_task: Mapping[str, object] | None) -> dict[str, object]:
    if parent_task is None:
        return {}
    try:
        raw = json.loads(str(parent_task["provider_policy_json"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): value for key, value in cast(Mapping[object, object], raw).items()}


def _task_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is not None


def _existing_task_is_same_import(conn: sqlite3.Connection, *, task_id: str, matter_scope: str, candidate_id: str) -> bool:
    row = cast(Mapping[str, object] | None, conn.execute("SELECT matter_scope, imported_from_candidate_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if row is None:
        return False
    return str(row["matter_scope"]) == matter_scope and str(row["imported_from_candidate_id"] or "") == candidate_id


def _record_rejected_proposed_task(conn: sqlite3.Connection, *, task_id: str, reason: str) -> None:
    _ = repo.record_human_attention(
        conn,
        target_type="proposed_task",
        target_id=task_id,
        severity="blocker",
        reason=f"proposed task rejected: {reason}",
    )


def _dependency_error(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_dependencies: list[str],
    artifact_dependencies: list[str],
    task_dependencies: list[str],
    matter_dependencies: list[str],
) -> str:
    bad_sources = _ids_not_in_matter(conn, table="sources", column="source_id", matter_scope=matter_scope, ids=source_dependencies)
    if bad_sources:
        return f"source dependencies outside parent matter {matter_scope}: {', '.join(bad_sources)}"
    bad_artifacts = _ids_not_in_matter(conn, table="artifacts", column="artifact_id", matter_scope=matter_scope, ids=artifact_dependencies)
    if bad_artifacts:
        return f"artifact dependencies outside parent matter {matter_scope}: {', '.join(bad_artifacts)}"
    bad_tasks = _ids_not_in_matter(conn, table="tasks", column="task_id", matter_scope=matter_scope, ids=task_dependencies)
    if bad_tasks:
        return f"task dependencies outside parent matter {matter_scope}: {', '.join(bad_tasks)}"
    bad_matters = [item for item in matter_dependencies if item != matter_scope]
    if bad_matters:
        return f"matter dependencies outside parent matter {matter_scope}: {', '.join(bad_matters)}"
    return ""


def _ids_not_in_matter(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    matter_scope: str,
    ids: list[str],
) -> list[str]:
    if not ids:
        return []
    rows = conn.execute(
        f"SELECT {column} FROM {table} WHERE matter_scope = ? AND {column} IN (%s)" % ",".join("?" for _ in ids),
        (matter_scope, *ids),
    ).fetchall()
    found = {str(row[column]) for row in rows}
    return [item for item in ids if item not in found]


def _infer_source_dependencies(conn: sqlite3.Connection, *, matter_scope: str, task_map: Mapping[object, object]) -> list[str]:
    text = " ".join(
        str(task_map.get(key) or "")
        for key in ("task_id", "title", "task_type", "stage", "instructions")
    )
    explicit = list(dict.fromkeys(re.findall(r"\b[A-Z]+-SRC-\d{4,}\b", text)))
    if explicit:
        return _existing_matter_source_ids(conn, matter_scope=matter_scope, requested=explicit)
    if str(task_map.get("task_type") or "") in {"source_inventory", "targeted_source_gap_search"}:
        return _all_matter_source_ids(conn, matter_scope=matter_scope)
    return []


def _existing_matter_source_ids(conn: sqlite3.Connection, *, matter_scope: str, requested: list[str]) -> list[str]:
    if not requested:
        return []
    rows = conn.execute(
        "SELECT source_id FROM sources WHERE matter_scope = ? AND source_id IN (%s)" % ",".join("?" for _ in requested),
        (matter_scope, *requested),
    ).fetchall()
    found = {str(row["source_id"]) for row in rows}
    return [source_id for source_id in requested if source_id in found]


def _all_matter_source_ids(conn: sqlite3.Connection, *, matter_scope: str) -> list[str]:
    return [
        str(row["source_id"])
        for row in conn.execute(
            "SELECT source_id FROM sources WHERE matter_scope = ? ORDER BY source_id",
            (matter_scope,),
        )
    ]


def _string_list(value: object) -> list[str]:
    return [str(item) for item in cast(list[object], value)] if isinstance(value, list) else []


def _mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(cast(Mapping[str, object], item)) for item in cast(list[object], value) if isinstance(item, Mapping)]


def _dict(value: object) -> dict[str, object]:
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
