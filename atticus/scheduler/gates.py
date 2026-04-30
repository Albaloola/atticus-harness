"""Dependency and certification gates for active legal work."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from dataclasses import dataclass
from typing import cast

from atticus.core.policies import STAGE_FOUNDATION_REQUIREMENTS


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reasons: list[str]


AUTO_REQUEUE_BLOCKER_PREFIXES = (
    "budget blocked for ",
    "cross-matter artifact dependency:",
    "cross-matter source dependency:",
    "cross-matter task dependency:",
    "inactive matter dependency:",
    "incomplete task dependency:",
    "malformed certification requirement",
    "malformed task gate metadata",
    "missing artifact dependency:",
    "missing certification:",
    "missing matter dependency:",
    "missing source dependency:",
    "missing task dependency:",
    "provider policy for task ",
    "stale artifact dependency:",
    "stale source dependency:",
    "task estimated cost ",
)

LIVE_OPENROUTER_NOT_ENABLED_BLOCKER = "live OpenRouter execution requires allow_live=True and ATTICUS_ENABLE_LIVE_OPENROUTER=1"
LIVE_CODEX_NOT_ENABLED_BLOCKER = "live Codex execution requires allow_live=True and ATTICUS_ENABLE_LIVE_CODEX=1"


def blocked_task_auto_requeue_allowed(
    task_row: Mapping[str, object],
    *,
    resolved_transient_blocker_prefixes: tuple[str, ...] = (),
) -> bool:
    """Return true only for blocked tasks whose old blockers were gate-like.

    Provider, runtime, quarantine, parser, and context-build failures must not
    be retried merely because dependency gates pass; those need a repair event
    or explicit operator/orchestrator requeue. Runtime configuration blockers
    are only requeueable when the caller proves the configuration is now
    resolved, so a missing live flag cannot become an unbounded retry loop.
    """

    reasons = _blocked_reasons(task_row)
    if not reasons:
        return True
    allowed_prefixes = AUTO_REQUEUE_BLOCKER_PREFIXES + resolved_transient_blocker_prefixes
    return all(any(reason.startswith(prefix) for prefix in allowed_prefixes) for reason in reasons)


def evaluate_task_gates(conn: sqlite3.Connection, task_row: Mapping[str, object]) -> GateResult:
    reasons: list[str] = []
    task_id = str(task_row["task_id"])
    task_matter = str(task_row["matter_scope"])
    external_action_reason = _external_action_task_reason(task_row)
    if external_action_reason:
        reasons.append(external_action_reason)
    if _task_repair_limit_reached(conn, task_id=task_id):
        reasons.append("orchestrator repair limit reached: user intervention required")
    source_deps = _load_string_list(task_row, "source_dependencies_json", task_id, reasons)
    artifact_deps = _load_string_list(task_row, "artifact_dependencies_json", task_id, reasons)
    task_deps = _load_string_list(task_row, "task_dependencies_json", task_id, reasons) if "task_dependencies_json" in task_row.keys() else []
    matter_deps = _load_string_list(task_row, "matter_dependencies_json", task_id, reasons) if "matter_dependencies_json" in task_row.keys() else []
    required_certs = _load_certification_requirements(task_row, task_id, reasons)
    for requirement in STAGE_FOUNDATION_REQUIREMENTS.get(str(task_row["stage"]), []):
        scoped: dict[str, object] = dict(requirement)
        if scoped.get("subject_type") == "matter":
            scoped["subject_id"] = task_row["matter_scope"]
        required_certs.append(scoped)

    for source_id in source_deps:
        row = cast(sqlite3.Row | None, cast(object, conn.execute(
            "SELECT stale, matter_scope FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()))
        if row is None:
            reasons.append(f"missing source dependency: {source_id}")
        elif row["matter_scope"] != task_matter:
            reasons.append(f"cross-matter source dependency: {source_id} belongs to {row['matter_scope']}, not {task_matter}")
        elif row["stale"]:
            reasons.append(f"stale source dependency: {source_id}")

    for artifact_id in artifact_deps:
        row = cast(sqlite3.Row | None, cast(object, conn.execute(
            "SELECT stale, matter_scope FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()))
        if row is None:
            reasons.append(f"missing artifact dependency: {artifact_id}")
        elif row["matter_scope"] != task_matter:
            reasons.append(f"cross-matter artifact dependency: {artifact_id} belongs to {row['matter_scope']}, not {task_matter}")
        elif row["stale"]:
            reasons.append(f"stale artifact dependency: {artifact_id}")

    for dependency_task_id in task_deps:
        row = cast(sqlite3.Row | None, cast(object, conn.execute(
            "SELECT status, matter_scope FROM tasks WHERE task_id = ?",
            (dependency_task_id,),
        ).fetchone()))
        if row is None:
            reasons.append(f"missing task dependency: {dependency_task_id}")
        elif row["matter_scope"] != task_matter:
            reasons.append(f"cross-matter task dependency: {dependency_task_id} belongs to {row['matter_scope']}, not {task_matter}")
        elif row["status"] != "complete":
            reasons.append(f"incomplete task dependency: {dependency_task_id}")

    for matter_scope in matter_deps:
        row = cast(sqlite3.Row | None, cast(object, conn.execute(
            "SELECT status FROM matters WHERE matter_scope = ?",
            (matter_scope,),
        ).fetchone()))
        if row is None:
            reasons.append(f"missing matter dependency: {matter_scope}")
        elif row["status"] != "active":
            reasons.append(f"inactive matter dependency: {matter_scope}")

    for requirement in required_certs:
        subject_type = requirement.get("subject_type", "artifact")
        subject_id = requirement.get("subject_id")
        cert_type = requirement.get("certification_type")
        if not subject_id or not cert_type:
            reasons.append(f"malformed certification requirement: {requirement!r}")
            continue
        row = cast(sqlite3.Row | None, cast(object, conn.execute(
            """
            SELECT certification_id
            FROM certifications
            WHERE subject_type = ? AND subject_id = ? AND certification_type = ? AND status = 'active'
            LIMIT 1
            """,
            (subject_type, subject_id, cert_type),
        ).fetchone()))
        if row is None:
            reasons.append(f"missing certification: {subject_type}:{subject_id}:{cert_type}")

    return GateResult(allowed=not reasons, reasons=reasons)


def _external_action_task_reason(task_row: Mapping[str, object]) -> str:
    task_type = _task_value(task_row, "task_type").casefold()
    title = _task_value(task_row, "title").casefold()
    instructions = _task_value(task_row, "instructions").casefold()
    text = f"{task_type} {title} {instructions}"
    external_action_types = {
        "evidence_acquisition",
        "source_acquisition",
        "source_collection",
        "external_request",
        "human_review",
        "manual_review",
    }
    external_phrases = (
        "obtain clearer copy",
        "obtain a clearer copy",
        "obtain certified",
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
    if task_type in external_action_types or any(phrase in text for phrase in external_phrases):
        return "external/human-only action blocked: task requests outside evidence acquisition or manual intervention; record human attention or create an internal evidence-review task instead"
    return ""


def _task_value(task_row: Mapping[str, object], key: str) -> str:
    try:
        return str(task_row[key] or "")
    except (KeyError, IndexError):
        return ""


def _blocked_reasons(task_row: Mapping[str, object]) -> list[str]:
    try:
        value = json.loads(str(task_row["blocked_reasons_json"] or "[]"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return ["malformed blocked_reasons_json"]
    if not isinstance(value, list):
        return ["malformed blocked_reasons_json"]
    return [str(item) for item in cast(list[object], value) if str(item)]


def _task_repair_limit_reached(conn: sqlite3.Connection, *, task_id: str) -> bool:
    try:
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
    except sqlite3.OperationalError:
        return False
    return row is not None


def _load_json_field(task_row: Mapping[str, object], field: str, task_id: str, reasons: list[str]) -> object:
    try:
        return json.loads(str(task_row[field] or "[]"))
    except (json.JSONDecodeError, TypeError) as exc:
        reasons.append(f"malformed task gate metadata for task {task_id}: {field} must contain valid JSON: {exc}")
        return []


def _load_string_list(task_row: Mapping[str, object], field: str, task_id: str, reasons: list[str]) -> list[str]:
    value = _load_json_field(task_row, field, task_id, reasons)
    if not isinstance(value, list):
        reasons.append(f"malformed task gate metadata for task {task_id}: {field} must be a JSON array")
        return []
    items: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str) or not item:
            reasons.append(f"malformed task gate metadata for task {task_id}: {field}[{index}] must be a non-empty string")
            continue
        items.append(item)
    return items


def _load_certification_requirements(task_row: Mapping[str, object], task_id: str, reasons: list[str]) -> list[dict[str, object]]:
    value = _load_json_field(task_row, "required_certifications_json", task_id, reasons)
    if not isinstance(value, list):
        reasons.append(f"malformed task gate metadata for task {task_id}: required_certifications_json must be a JSON array")
        return []
    requirements: list[dict[str, object]] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, dict):
            reasons.append(f"malformed certification requirement at required_certifications_json[{index}]: must be a JSON object")
            continue
        requirements.append(dict(cast(dict[str, object], item)))
    return requirements
