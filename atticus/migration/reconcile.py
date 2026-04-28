"""Foundation reconciliation before live legal work resumes."""

from __future__ import annotations

import json
import sqlite3

from typing import cast
from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.graph.certifications import certify_subject
from atticus.validation.gates import run_validation

FOUNDATION_GATES = [
    "source_inventory",
    "extraction_coverage",
    "evidence_registry",
    "production_mapping",
    "chronology_citations",
]


def reconcile_foundation(
    conn: sqlite3.Connection,
    *,
    matter_scope: str = "atticus",
    dry_run: bool = True,
    validator: str = "atticus-reconciler",
) -> dict[str, object]:
    """Validate and certify foundational matter layers in dependency order.

    Existing legacy artifacts remain candidate material. This function only
    promotes matter-level certifications when durable validation gates pass. If
    the foundation is incomplete, later-stage queued work is blocked so the live
    scheduler cannot resume stale drafting or review tasks by accident.
    """

    passed: list[str] = []
    failed: dict[str, dict[str, object]] = {}
    certifications: list[dict[str, str]] = []

    for gate in FOUNDATION_GATES:
        if _has_active_certification(conn, matter_scope=matter_scope, certification_type=gate):
            passed.append(gate)
            continue
        if dry_run:
            ok, details = _preview_gate(conn, gate_name=gate, matter_scope=matter_scope)
            if ok:
                passed.append(gate)
            else:
                failed[gate] = details
            continue
        outcome = run_validation(conn, gate_name=gate, target_type="matter", target_id=matter_scope)
        if outcome.passed:
            cert_id = certify_subject(
                conn,
                subject_type="matter",
                subject_id=matter_scope,
                certification_type=gate,
                validator=validator,
                evidence={"validation_result_id": outcome.validation_result_id, "gate": gate},
            )
            passed.append(gate)
            certifications.append({"certification_id": cert_id, "certification_type": gate})
        else:
            failed[gate] = outcome.details

    ready = not failed
    frozen_tasks: list[str] = []
    unfrozen_tasks: list[str] = []
    if not ready and not dry_run:
        frozen_tasks = _freeze_later_stage_work(conn, matter_scope=matter_scope, failed_gates=list(failed))
    elif ready and not dry_run:
        unfrozen_tasks = _unfreeze_foundation_blocked_work(conn, matter_scope=matter_scope)

    return {
        "matter_scope": matter_scope,
        "dry_run": dry_run,
        "ready_for_live_resume": ready,
        "passed": passed,
        "failed": failed,
        "certifications": certifications,
        "frozen_tasks": frozen_tasks,
        "unfrozen_tasks": unfrozen_tasks,
    }


def _has_active_certification(conn: sqlite3.Connection, *, matter_scope: str, certification_type: str) -> bool:
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(certifications)")}
    required = {"certification_id", "subject_type", "subject_id", "certification_type", "status"}
    if not required.issubset(columns):
        return False
    row = cast(object | None, conn.execute(
        """
        SELECT certification_id
        FROM certifications
        WHERE subject_type = 'matter'
          AND subject_id = ?
          AND certification_type = ?
          AND status = 'active'
        LIMIT 1
        """,
        (matter_scope, certification_type),
    ).fetchone())
    return row is not None


def _preview_gate(conn: sqlite3.Connection, *, gate_name: str, matter_scope: str) -> tuple[bool, dict[str, object]]:
    from atticus.validation import gates as validation_gates

    handlers = {
        "source_inventory": validation_gates.validate_source_inventory,
        "extraction_coverage": validation_gates.validate_extraction_coverage,
        "evidence_registry": validation_gates.validate_evidence_registry,
        "production_mapping": validation_gates.validate_production_mapping_integrity,
        "chronology_citations": validation_gates.validate_chronology_citation_completeness,
    }
    try:
        return handlers[gate_name](conn, target_type="matter", target_id=matter_scope)
    except sqlite3.OperationalError as exc:
        return False, {"reason": "legacy ledger schema is missing fields required for foundation validation", "error": str(exc), "gate": gate_name}


def _freeze_later_stage_work(conn: sqlite3.Connection, *, matter_scope: str, failed_gates: list[str]) -> list[str]:
    frozen: list[str] = []
    reason = "foundation reconciliation incomplete before live resume: " + ", ".join(failed_gates)
    for row in conn.execute(
        """
        SELECT task_id, stage
        FROM tasks
        WHERE matter_scope = ? AND status IN ('queued', 'ready') AND stage != 'S0'
        ORDER BY expected_value DESC, created_at ASC
        """,
        (matter_scope,),
    ):
        task_id = str(row["task_id"])
        repo.update_task_blocked(conn, task_id, [reason])
        frozen.append(task_id)
    if frozen:
        _ = repo.emit_event(conn, "foundation_reconciliation.froze_tasks", payload={"matter_scope": matter_scope, "task_ids": frozen, "failed_gates": failed_gates})
    return frozen


def _unfreeze_foundation_blocked_work(conn: sqlite3.Connection, *, matter_scope: str) -> list[str]:
    """Requeue tasks blocked only by an earlier failed foundation reconciliation."""

    unfrozen: list[str] = []
    prefix = "foundation reconciliation incomplete before live resume:"
    for row in conn.execute(
        """
        SELECT task_id, blocked_reasons_json
        FROM tasks
        WHERE matter_scope = ? AND status = 'blocked'
        ORDER BY expected_value DESC, created_at ASC
        """,
        (matter_scope,),
    ):
        task_id = str(row["task_id"])
        try:
            reasons = cast(list[object], json.loads(str(row["blocked_reasons_json"] or "[]")))
        except (json.JSONDecodeError, TypeError) as exc:
            _ = repo.record_human_attention(
                conn,
                target_type="task",
                target_id=task_id,
                severity="blocker",
                reason=f"foundation blocked-task reasons are malformed and could not be unfrozen: {exc}",
            )
            continue
        remaining = [str(reason) for reason in reasons if not str(reason).startswith(prefix)]
        if len(remaining) == len(reasons):
            continue
        if remaining:
            repo.update_task_blocked(conn, task_id, remaining)
        else:
            _ = conn.execute(
                "UPDATE tasks SET status = ?, blocked_reasons_json = '[]', updated_at = ? WHERE task_id = ?",
                (TaskStatus.QUEUED, utc_now(), task_id),
            )
            _ = repo.emit_event(conn, "foundation_reconciliation.unfroze_task", payload={"matter_scope": matter_scope, "task_id": task_id})
            unfrozen.append(task_id)
    if unfrozen:
        _ = repo.emit_event(conn, "foundation_reconciliation.unfroze_tasks", payload={"matter_scope": matter_scope, "task_ids": unfrozen})
    return unfrozen
