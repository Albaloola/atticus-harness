"""Dependency and certification gates for active legal work."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from atticus.core.policies import STAGE_FOUNDATION_REQUIREMENTS

@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reasons: list[str]


def evaluate_task_gates(conn: sqlite3.Connection, task_row: sqlite3.Row) -> GateResult:
    reasons: list[str] = []
    source_deps = json.loads(task_row["source_dependencies_json"])
    artifact_deps = json.loads(task_row["artifact_dependencies_json"])
    task_deps = json.loads(task_row["task_dependencies_json"]) if "task_dependencies_json" in task_row.keys() else []
    required_certs = list(json.loads(task_row["required_certifications_json"]))
    for requirement in STAGE_FOUNDATION_REQUIREMENTS.get(task_row["stage"], []):
        scoped = dict(requirement)
        if scoped.get("subject_type") == "matter":
            scoped["subject_id"] = task_row["matter_scope"]
        required_certs.append(scoped)

    for source_id in source_deps:
        row = conn.execute(
            "SELECT stale FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            reasons.append(f"missing source dependency: {source_id}")
        elif row["stale"]:
            reasons.append(f"stale source dependency: {source_id}")

    for artifact_id in artifact_deps:
        row = conn.execute(
            "SELECT stale FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            reasons.append(f"missing artifact dependency: {artifact_id}")
        elif row["stale"]:
            reasons.append(f"stale artifact dependency: {artifact_id}")

    for dependency_task_id in task_deps:
        row = conn.execute(
            "SELECT status FROM tasks WHERE task_id = ?",
            (dependency_task_id,),
        ).fetchone()
        if row is None:
            reasons.append(f"missing task dependency: {dependency_task_id}")
        elif row["status"] != "complete":
            reasons.append(f"incomplete task dependency: {dependency_task_id}")

    for requirement in required_certs:
        subject_type = requirement.get("subject_type", "artifact")
        subject_id = requirement.get("subject_id")
        cert_type = requirement.get("certification_type")
        if not subject_id or not cert_type:
            reasons.append(f"malformed certification requirement: {requirement!r}")
            continue
        row = conn.execute(
            """
            SELECT certification_id
            FROM certifications
            WHERE subject_type = ? AND subject_id = ? AND certification_type = ? AND status = 'active'
            LIMIT 1
            """,
            (subject_type, subject_id, cert_type),
        ).fetchone()
        if row is None:
            reasons.append(f"missing certification: {subject_type}:{subject_id}:{cert_type}")

    return GateResult(allowed=not reasons, reasons=reasons)
