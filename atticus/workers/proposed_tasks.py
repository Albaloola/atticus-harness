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

SCOPED_SEARCH_TASK_TYPES = {
    "evidence_acquisition",
    "evidence_collection",
    "evidence_gathering",
    "evidence_reconciliation",
    "evidence_search",
    "privacy_review",
    "privacy_redaction_review",
    "privacy_redaction_verification",
    "redaction_fix",
    "redaction_review",
    "redaction_verification",
    "source_acquisition",
    "source_discovery",
    "source_search",
    "source_verification",
}

MAX_CONSECUTIVE_SCOPED_FOLLOWUPS = 5


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
        unsupported_reason = _unsupported_proposed_task_reason(task_map)
        if unsupported_reason:
            _record_rejected_proposed_task(conn, task_id=task_id, matter_scope=parent_matter_scope, reason=unsupported_reason)
            continue
        if proposed_matter_scope != parent_matter_scope:
            _record_rejected_proposed_task(
                conn,
                task_id=task_id,
                matter_scope=parent_matter_scope,
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
            _record_rejected_proposed_task(conn, task_id=task_id, matter_scope=matter_scope, reason=dependency_error)
            continue
        scope_error = _scope_required_error(
            task_map,
            source_dependencies=source_dependencies,
            artifact_dependencies=artifact_dependencies,
            task_dependencies=task_dependencies,
        )
        if scope_error:
            _record_rejected_proposed_task(conn, task_id=task_id, matter_scope=matter_scope, reason=scope_error)
            continue
        loop_error = _scoped_followup_loop_error(
            conn,
            parent_task_id=parent_task_id,
            task_map=task_map,
        )
        if loop_error:
            _record_rejected_proposed_task(conn, task_id=task_id, matter_scope=matter_scope, reason=loop_error)
            _ = repo.emit_event(
                conn,
                "proposed_task.loop_guard_rejected",
                matter_scope=matter_scope,
                payload={
                    "task_id": task_id,
                    "parent_task_id": parent_task_id,
                    "imported_from_candidate_id": candidate_id,
                    "reason": loop_error,
                },
            )
            continue
        if _task_exists(conn, task_id):
            collision = _resolve_task_id_collision(conn, task_id=task_id, matter_scope=matter_scope, task_map=task_map, candidate_id=candidate_id)
            if collision["decision"] in {"identical_existing", "same_import"}:
                imported.append(str(collision["task_id"]))
                continue
            if collision["decision"] == "use_suffixed_id":
                task_id = str(collision["task_id"])
            else:
                _record_rejected_proposed_task(conn, task_id=task_id, matter_scope=matter_scope, reason=str(collision["reason"]))
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
                matter_scope=matter_scope,
            )
            continue
        raw_cost_limit_usd = _optional_float(task_map.get("cost_limit_usd"))
        cost_limit_usd = _normalized_cost_limit(raw_cost_limit_usd, provider_policy=provider_policy)
        if raw_cost_limit_usd is not None and cost_limit_usd != raw_cost_limit_usd:
            _ = repo.emit_event(
                conn,
                "proposed_task.cost_limit_normalized",
                matter_scope=matter_scope,
                payload={
                    "task_id": task_id,
                    "parent_task_id": parent_task_id,
                    "imported_from_candidate_id": candidate_id,
                    "raw_cost_limit_usd": raw_cost_limit_usd,
                    "normalized_cost_limit_usd": cost_limit_usd,
                    "estimated_cost_usd": _optional_float(provider_policy.get("estimated_cost_usd")) or 0.0,
                    "reason": "worker-proposed cost limit was below provider policy estimate",
                },
            )
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
                cost_limit_usd=cost_limit_usd,
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


def _unsupported_proposed_task_reason(task_map: Mapping[object, object]) -> str:
    """Reject worker-proposed work that Atticus cannot safely execute.

    Proposed tasks are untrusted candidate output. A worker may correctly spot a
    need for better OCR, but it must not turn that into a runnable "use Google
    Cloud Vision/AWS Textract" task when no such bounded tool is configured.
    """

    policy = task_map.get("provider_policy")
    capabilities: set[str] = set()
    if isinstance(policy, Mapping):
        raw_capabilities = policy.get("capabilities")
        if isinstance(raw_capabilities, list):
            capabilities.update(str(item).strip().lower() for item in raw_capabilities if str(item).strip())
    task_type = str(task_map.get("task_type") or "").strip().lower()
    text = " ".join(
        str(task_map.get(key) or "")
        for key in ("task_id", "title", "task_type", "stage", "instructions")
    ).lower()
    external_ocr_terms = (
        "cloud-based ocr",
        "cloud based ocr",
        "google cloud vision",
        "aws textract",
        "azure vision",
        "azure ai vision",
        "external ocr service",
        "third-party ocr",
        "third party ocr",
    )
    if any(term in text for term in external_ocr_terms):
        return "external/cloud OCR is not a configured Atticus execution capability; use local extraction/OCR repair or request human/tool setup"
    if "ocr" in capabilities and task_type in {"ocr_enhancement", "structured_extraction"}:
        if not any(term in text for term in ("local extraction", "local ocr", "tesseract", "atticus.local_extraction")):
            return "proposed OCR task requested an unconfigured OCR capability instead of a bounded local Atticus OCR repair"
    external_action_task_types = {
        "evidence_acquisition",
        "source_acquisition",
        "source_collection",
        "external_request",
        "human_review",
        "manual_review",
    }
    external_action_terms = (
        "obtain clearer copy",
        "obtain a clearer copy",
        "obtain certified",
        "obtain and review",
        "certified notice",
        "manual verification",
        "human verification",
        "operator verification",
        "human review required",
        "request from the university",
        "request from university",
        "contact the university",
        "email the university",
        "ask the university",
        "send email",
        "send a letter",
        "file with",
        "serve on",
    )
    if task_type in external_action_task_types or any(term in text for term in external_action_terms):
        return "proposed task requests an external or human-only action; Atticus must record human attention instead of executing it"
    return ""


def _task_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is not None


def _existing_task_is_same_import(conn: sqlite3.Connection, *, task_id: str, matter_scope: str, candidate_id: str) -> bool:
    row = cast(Mapping[str, object] | None, conn.execute("SELECT matter_scope, imported_from_candidate_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if row is None:
        return False
    return str(row["matter_scope"]) == matter_scope and str(row["imported_from_candidate_id"] or "") == candidate_id


def _record_rejected_proposed_task(conn: sqlite3.Connection, *, task_id: str, matter_scope: str, reason: str) -> None:
    attention_id = repo.record_human_attention_once(
        conn,
        target_type="proposed_task",
        target_id=task_id,
        severity="blocker",
        reason=f"proposed task rejected: {reason}",
        matter_scope=matter_scope,
    )
    if attention_id is None:
        _ = repo.emit_event(
            conn,
            "proposed_task.rejection_duplicate_seen",
            matter_scope=matter_scope,
            payload={"task_id": task_id, "reason": reason},
        )


def _scoped_followup_loop_error(
    conn: sqlite3.Connection,
    *,
    parent_task_id: str,
    task_map: Mapping[object, object],
) -> str:
    task_type = str(task_map.get("task_type") or "").strip().lower()
    if task_type not in SCOPED_SEARCH_TASK_TYPES:
        return ""
    chain_count = _same_type_parent_chain_count(conn, parent_task_id=parent_task_id, task_type=task_type)
    if chain_count < MAX_CONSECUTIVE_SCOPED_FOLLOWUPS:
        return ""
    return (
        f"scoped {task_type} follow-up loop reached hard limit "
        f"{MAX_CONSECUTIVE_SCOPED_FOLLOWUPS}; summarize the gap and request human/orchestrator direction instead of spawning another search task"
    )


def _same_type_parent_chain_count(conn: sqlite3.Connection, *, parent_task_id: str, task_type: str) -> int:
    count = 0
    seen: set[str] = set()
    current = parent_task_id
    while current and current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT task_type, parent_task_id FROM tasks WHERE task_id = ?",
            (current,),
        ).fetchone()
        if row is None:
            break
        if str(row["task_type"] or "").strip().lower() != task_type:
            break
        count += 1
        current = str(row["parent_task_id"] or "")
    return count


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


def _scope_required_error(
    task_map: Mapping[object, object],
    *,
    source_dependencies: list[str],
    artifact_dependencies: list[str],
    task_dependencies: list[str],
) -> str:
    task_type = str(task_map.get("task_type") or "").strip().lower()
    if task_type not in SCOPED_SEARCH_TASK_TYPES:
        return ""
    if source_dependencies or artifact_dependencies or task_dependencies:
        return ""
    return (
        "proposed source/evidence search or review has no source, artifact, or task scope; "
        "name the specific matter sources/artifacts to inspect or request human source identification"
    )


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
    VALID_TABLES = {"sources", "artifacts", "tasks", "legal_memories", "authorities"}
    if table not in VALID_TABLES:
        raise ValueError(f"invalid table: {table}")
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


def _normalized_cost_limit(value: float | None, *, provider_policy: Mapping[str, object]) -> float | None:
    if value is None:
        return None
    estimated = _optional_float(provider_policy.get("estimated_cost_usd")) or 0.0
    return max(value, estimated)


def _resolve_task_id_collision(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    matter_scope: str,
    task_map: Mapping[object, object],
    candidate_id: str,
) -> dict[str, object]:
    if _existing_task_is_same_import(conn, task_id=task_id, matter_scope=matter_scope, candidate_id=candidate_id):
        return {"decision": "same_import", "task_id": task_id}
    row = conn.execute(
        """
        SELECT task_id, matter_scope, title, task_type, stage, instructions,
               source_dependencies_json, artifact_dependencies_json, task_dependencies_json,
               required_certifications_json, validation_gates_json
        FROM tasks
        WHERE task_id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return {"decision": "missing", "task_id": task_id}
    if _existing_task_semantically_matches(row, matter_scope=matter_scope, task_map=task_map):
        _ = repo.emit_event(
            conn,
            "proposed_task.collision_identical_skipped",
            matter_scope=matter_scope,
            payload={"task_id": task_id, "imported_from_candidate_id": candidate_id},
        )
        return {"decision": "identical_existing", "task_id": task_id}
    if not _collision_safe_to_suffix(row, task_map=task_map):
        return {"decision": "reject", "task_id": task_id, "reason": "proposed task id collides with an existing task"}
    for suffix in range(2, 100):
        candidate = f"{task_id}-{suffix}"
        if _existing_task_is_same_import(conn, task_id=candidate, matter_scope=matter_scope, candidate_id=candidate_id):
            return {"decision": "same_import", "task_id": candidate}
        if not _task_exists(conn, candidate):
            _ = repo.emit_event(
                conn,
                "proposed_task.id_collision_suffixed",
                matter_scope=matter_scope,
                payload={"original_task_id": task_id, "new_task_id": candidate, "imported_from_candidate_id": candidate_id},
            )
            return {"decision": "use_suffixed_id", "task_id": candidate}
    return {"decision": "reject", "task_id": task_id, "reason": "proposed task id collides with existing tasks and no stable suffix is available"}


def _existing_task_semantically_matches(row: sqlite3.Row, *, matter_scope: str, task_map: Mapping[object, object]) -> bool:
    if str(row["matter_scope"]) != matter_scope:
        return False
    checks = {
        "title": str(task_map.get("title") or str(row["task_id"])),
        "task_type": str(task_map.get("task_type") or "followup"),
        "stage": str(task_map.get("stage") or LegalStage.S0_SOURCE_INVENTORY),
        "instructions": str(task_map.get("instructions") or ""),
    }
    for key, expected in checks.items():
        if str(row[key] or "") != expected:
            return False
    json_checks = {
        "source_dependencies_json": _string_list(task_map.get("source_dependencies")),
        "artifact_dependencies_json": _string_list(task_map.get("artifact_dependencies")),
        "task_dependencies_json": _string_list(task_map.get("task_dependencies")),
        "required_certifications_json": _mapping_list(task_map.get("required_certifications")),
        "validation_gates_json": _string_list(task_map.get("validation_gates")),
    }
    for column, expected in json_checks.items():
        try:
            current = json.loads(str(row[column] or "[]"))
        except json.JSONDecodeError:
            return False
        if current != expected:
            return False
    return True


def _collision_safe_to_suffix(row: sqlite3.Row, *, task_map: Mapping[object, object]) -> bool:
    """Return true when only the requested id collides, not semantics.

    Unsafe ambiguity remains rejected.  A suffix is allowed only when the
    proposed task has the same title/type/stage/instructions as the existing
    task but different dependencies/provenance, which represents a deterministic
    id collision rather than an attempt to overwrite a different named task.
    """

    return (
        str(row["title"] or "") == str(task_map.get("title") or str(row["task_id"]))
        and str(row["task_type"] or "") == str(task_map.get("task_type") or "followup")
        and str(row["stage"] or "") == str(task_map.get("stage") or LegalStage.S0_SOURCE_INVENTORY)
        and str(row["instructions"] or "") == str(task_map.get("instructions") or "")
    )
