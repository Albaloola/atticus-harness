"""Reducer decision logic and canonical artifact writing."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
import sqlite3

from typing import cast
from uuid import uuid4
from atticus.core.events import utc_now
from atticus.core.policies import LegalStage, TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.hooks import run_hooks
from atticus.providers.model_policy import default_smart_model_policy, smart_provider_policy_for_route
from atticus.scheduler.lease import complete_lease, require_active_lease
from atticus.validation.canonical_write_guard import assert_canonical_write_allowed
from atticus.validation.gates import run_validation
from atticus.workers.citation_context import allowed_citation_targets_for_task, proof_citation_targets_for_task
from atticus.workers.result_parser import ResultPacketError, parse_result
from atticus.workers.proposed_tasks import import_proposed_tasks_from_candidate


class ReductionBlocked(RuntimeError):
    """Raised when a candidate cannot be reduced safely."""


FOUNDATION_ARTIFACT_TYPES_BY_TASK_TYPE = {
    "evidence_issue_map": "evidence_registry",
    "evidence_issue_map_bundle": "evidence_registry",
    "evidence_triage": "evidence_registry",
    "production_mapping": "production_crosswalk",
    "production_mapping_bundle": "production_crosswalk",
    "evidence_organization_plan": "production_crosswalk",
    "evidence_organization_plan_bundle": "production_crosswalk",
    "chronology_event_extraction": "chronology",
    "issue_route_map": "issue_route_map",
    "authority_map": "authority_map",
    "draft_preparation": "draft",
    "citation_audit": "citation_audit",
    "hostile_opponent_review": "hostile_review",
    "privacy_review": "privacy_redaction_audit",
    "privacy_redaction_audit": "privacy_redaction_audit",
    "privacy_redaction_review": "privacy_redaction_audit",
    "privacy_redaction_verification": "privacy_redaction_audit",
    "redaction_review": "privacy_redaction_audit",
    "redaction_verification": "privacy_redaction_audit",
    "final_quality_gate": "final_quality_gate",
}

MATTER_CERTIFICATIONS_BY_TASK_TYPE = {
    "evidence_issue_map": "evidence_registry",
    "evidence_triage": "evidence_registry",
    "production_mapping": "production_mapping",
    "evidence_organization_plan": "production_mapping",
    "chronology_event_extraction": "chronology_citations",
    "issue_route_map": "issue_route_map",
    "authority_map": "authority_map",
    "draft_preparation": "draft_preparation",
    "citation_audit": "citation_audit",
    "hostile_opponent_review": "hostile_review",
    "privacy_review": "privacy_redaction_audit",
    "privacy_redaction_audit": "privacy_redaction_audit",
    "privacy_redaction_review": "privacy_redaction_audit",
    "privacy_redaction_verification": "privacy_redaction_audit",
    "redaction_review": "privacy_redaction_audit",
    "redaction_verification": "privacy_redaction_audit",
    "final_quality_gate": "final_quality_gate",
}

VALIDATED_CERTIFICATION_GATES = {"evidence_registry", "production_mapping", "chronology_citations"}
EVIDENCE_TARGET_TYPES = {"source", "artifact", "authority", "chronology_event", "claim"}
GENERIC_ARTIFACT_TYPES = {"", "markdown", "report", "draft", "reduced_result", "evidence_checklist"}
CERTIFICATION_DECISION_TASK_TYPES = {
    "authority_map",
    "draft_preparation",
    "citation_audit",
    "privacy_redaction_audit",
    "final_quality_gate",
}
DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b",
    re.IGNORECASE,
)


def choose_candidate(candidate_ids: list[str]) -> str | None:
    return candidate_ids[0] if candidate_ids else None


def reduce_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    reducer_lease_id: str,
    writer_role: str = "reducer",
    dry_run: bool = True,
) -> dict[str, object]:
    candidate = cast(sqlite3.Row | None, cast(object, conn.execute(
        "SELECT * FROM candidate_outputs WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()))
    if candidate is None:
        raise ReductionBlocked(f"unknown candidate: {candidate_id}")
    if candidate["status"] != "candidate":
        raise ReductionBlocked(f"candidate {candidate_id} has status {candidate['status']}")
    task_id = str(candidate["task_id"])
    task_row = cast(sqlite3.Row | None, cast(object, conn.execute(
        """
        SELECT task_id, matter_scope, stage, task_type, required_certifications_json,
               source_dependencies_json, artifact_dependencies_json, task_provenance_json
        FROM tasks
        WHERE task_id = ?
        """,
        (task_id,),
    ).fetchone()))
    if task_row is None:
        raise ReductionBlocked(f"candidate {candidate_id} references unknown task: {task_id}")
    task: dict[str, object] = dict(task_row)
    _ = require_active_lease(conn, lease_id=reducer_lease_id, task_id=task_id)
    assert_canonical_write_allowed(
        writer_role=writer_role,
        target_path=f"canonical://candidate/{candidate_id}",
        conn=conn,
        lease_id=reducer_lease_id,
        task_id=task_id,
    )

    payload = json.loads(str(candidate["payload_json"]))
    if not isinstance(payload, Mapping):
        raise ReductionBlocked("candidate payload must be a JSON object")
    try:
        packet = parse_result(
            {str(key): value for key, value in cast(Mapping[object, object], payload).items()},
            allowed_citation_targets=allowed_citation_targets_for_task(conn, task_id=task_id),
            proof_citation_targets=proof_citation_targets_for_task(conn, task_id=task_id),
        )
    except ResultPacketError as exc:
        raise ReductionBlocked(f"candidate failed task-context schema validation: {exc}") from exc
    proposed = packet.proposed_artifacts[0] if packet.proposed_artifacts else {}
    source_dependencies = _effective_source_dependencies(task)
    artifact_dependencies = _load_string_list(str(task.get("artifact_dependencies_json") or "[]"))
    artifact_type = _artifact_type_for_task(str(task["task_type"]), proposed)
    canonical_preview = {
        "candidate_id": candidate_id,
        "task_id": task_id,
        "matter_scope": task["matter_scope"],
        "summary": packet.summary,
        "proposed_artifact": proposed,
        "dry_run": dry_run,
    }
    if dry_run:
        return {**canonical_preview, "validations": ["reducer_packet_schema", "canonical_write_authorization", "stale_dependency"]}

    schema_validation = run_validation(
        conn,
        gate_name="reducer_packet_schema",
        target_type="candidate",
        target_id=candidate_id,
    )
    auth_validation = run_validation(
        conn,
        gate_name="canonical_write_authorization",
        target_type="candidate",
        target_id=candidate_id,
    )
    stale_validation = run_validation(
        conn,
        gate_name="stale_dependency",
        target_type="task",
        target_id=task_id,
    )
    if not schema_validation.passed or not auth_validation.passed or not stale_validation.passed:
        raise ReductionBlocked("candidate failed reducer validations")
    hook_outcomes = run_hooks(
        conn,
        event_name="PreReduce",
        matter_scope=str(task["matter_scope"]),
        payload={
            "stage": str(task["stage"]),
            "task_type": str(task["task_type"]),
            "required_certifications": _load_string_list(str(task["required_certifications_json"] or "[]")),
            "candidate_id": candidate_id,
        },
    )
    if any(not outcome.allowed for outcome in hook_outcomes):
        raise ReductionBlocked("candidate blocked by lifecycle hook")

    _ = conn.execute("SAVEPOINT reducer_accept_candidate")
    try:
        citation_source_ids = _citation_target_ids(packet.raw, "source") or source_dependencies
        citation_artifact_dependency_ids = _dedupe_ordered([*artifact_dependencies, *_citation_target_ids(packet.raw, "artifact")])
        artifact_id = repo.add_artifact(
            conn,
            matter_scope=str(task["matter_scope"]),
            path=str(proposed.get("path") or f"canonical/{task_id}/{candidate_id}.json"),
            artifact_type=artifact_type,
            stage=str(proposed.get("stage") or ""),
            trust_status=TrustStatus.VALIDATED,
            title=str(proposed.get("title") or f"Reduced result for {task_id}"),
            content=_artifact_content(packet=packet.raw, proposed=proposed, candidate_id=candidate_id),
            source_ids=citation_source_ids,
            artifact_dependency_ids=citation_artifact_dependency_ids,
            produced_by_task_id=task_id,
        )
        graph_writes = _materialize_reducer_graph(
            conn,
            task=task,
            artifact_id=artifact_id,
            packet=packet.raw,
            source_dependencies=source_dependencies,
        )
        reducer_packet_id = repo.record_reducer_packet(
            conn,
            candidate_id=candidate_id,
            reducer_lease_id=reducer_lease_id,
            decision="accepted",
            validation_result_id=schema_validation.validation_result_id,
            canonical_artifact_id=artifact_id,
            dissent=[],
        )
        _ = conn.execute(
            "UPDATE candidate_outputs SET status = 'reduced' WHERE candidate_id = ?",
            (candidate_id,),
        )
        imported_tasks = import_proposed_tasks_from_candidate(conn, candidate)
        certifications = _issue_completion_certification_if_safe(
            conn,
            task=task,
            candidate_id=candidate_id,
            artifact_id=artifact_id,
            packet=packet.raw,
        )
        complete_lease(conn, lease_id=reducer_lease_id, task_status=TaskStatus.COMPLETE)
    except Exception:
        _ = conn.execute("ROLLBACK TO SAVEPOINT reducer_accept_candidate")
        _ = conn.execute("RELEASE SAVEPOINT reducer_accept_candidate")
        raise
    _ = conn.execute("RELEASE SAVEPOINT reducer_accept_candidate")
    return {
        **canonical_preview,
        "dry_run": False,
        "artifact_id": artifact_id,
        "imported_tasks": imported_tasks,
        "reducer_packet_id": reducer_packet_id,
        "graph_writes": graph_writes,
        "certifications": certifications,
    }


def _load_string_list(text: str) -> list[str]:
    value = json.loads(text or "[]")
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value) if str(item)]


def _row_value(row: Mapping[str, object], key: str, default: object = "") -> object:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    return row.get(key, default)


def _effective_source_dependencies(task: Mapping[str, object]) -> list[str]:
    source_dependencies = _load_string_list(str(_row_value(task, "source_dependencies_json") or "[]"))
    if source_dependencies:
        return source_dependencies
    try:
        provenance = json.loads(str(_row_value(task, "task_provenance_json") or "{}"))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(provenance, Mapping):
        return []
    decomposition = provenance.get("source_bundle_decomposition")
    if not isinstance(decomposition, Mapping):
        return []
    original = decomposition.get("original_source_dependencies")
    if not isinstance(original, list):
        return []
    return _dedupe_ordered([str(item) for item in cast(list[object], original) if str(item)])


def _artifact_type_for_task(task_type: str, proposed: Mapping[str, object]) -> str:
    proposed_type = str(proposed.get("artifact_type") or "").strip()
    mapped = FOUNDATION_ARTIFACT_TYPES_BY_TASK_TYPE.get(task_type)
    if mapped and proposed_type.lower() in GENERIC_ARTIFACT_TYPES:
        return mapped
    if mapped and task_type in {
        "evidence_issue_map",
        "evidence_issue_map_bundle",
        "production_mapping",
        "production_mapping_bundle",
        "evidence_organization_plan",
        "evidence_organization_plan_bundle",
    }:
        return mapped
    return proposed_type or mapped or "reduced_result"


def _citation_target_ids(packet: Mapping[str, object], target_type: str) -> list[str]:
    return _dedupe_ordered(
        [
            str(citation.get("target_id") or "")
            for citation in _packet_items(packet, "citations")
            if str(citation.get("target_type") or "") == target_type
        ]
    )


def _dedupe_ordered(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _artifact_content(*, packet: Mapping[str, object], proposed: Mapping[str, object], candidate_id: str) -> str:
    return json.dumps(
        {
            "candidate_id": candidate_id,
            "summary": packet.get("summary") or "",
            "findings": packet.get("findings") or [],
            "citations": packet.get("citations") or [],
            "proposed_artifact": proposed,
            "proposed_content": proposed.get("content") or "",
        },
        sort_keys=True,
        indent=2,
    )


def _materialize_reducer_graph(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    artifact_id: str,
    packet: Mapping[str, object],
    source_dependencies: list[str],
) -> list[dict[str, object]]:
    task_type = str(task["task_type"])
    matter_scope = str(task["matter_scope"])
    writes: list[dict[str, object]] = []
    if task_type in {"production_mapping", "evidence_organization_plan"}:
        writes.extend(_ensure_production_mappings(conn, matter_scope=matter_scope, source_ids=source_dependencies, artifact_id=artifact_id))
    if task_type == "chronology_event_extraction":
        writes.extend(_materialize_chronology_events(conn, matter_scope=matter_scope, artifact_id=artifact_id, packet=packet))
    if task_type == "issue_route_map":
        writes.extend(_materialize_issue_route(conn, matter_scope=matter_scope, artifact_id=artifact_id, packet=packet))
    return writes


def _ensure_production_mappings(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_ids: list[str],
    artifact_id: str,
) -> list[dict[str, object]]:
    writes: list[dict[str, object]] = []
    for source_id in source_ids:
        existing = conn.execute(
            """
            SELECT mapping_id
            FROM production_mappings
            WHERE matter_scope = ? AND source_id = ? AND production_id = ?
            LIMIT 1
            """,
            (matter_scope, source_id, source_id),
        ).fetchone()
        if existing is not None:
            continue
        row = conn.execute("SELECT path FROM sources WHERE source_id = ? AND matter_scope = ?", (source_id, matter_scope)).fetchone()
        if row is None:
            continue
        mapping_id = f"prod-{uuid4().hex}"
        _ = conn.execute(
            """
            INSERT INTO production_mappings(mapping_id, matter_scope, source_id, artifact_id,
              production_id, produced_path, integrity_status, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
            """,
            (
                mapping_id,
                matter_scope,
                source_id,
                artifact_id,
                source_id,
                str(row["path"] or ""),
                json.dumps({"created_by": "reducer.production_mapping", "source_id": source_id}, sort_keys=True),
                utc_now(),
            ),
        )
        writes.append({"type": "production_mapping", "mapping_id": mapping_id, "source_id": source_id})
    return writes


def _materialize_chronology_events(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    artifact_id: str,
    packet: Mapping[str, object],
) -> list[dict[str, object]]:
    writes: list[dict[str, object]] = []
    citations_by_id = _citations_by_id(packet)
    for finding in _packet_items(packet, "findings"):
        if str(finding.get("finding_type") or "") not in {"fact", "procedure", "inference", "contradiction"}:
            continue
        citation_ids = [str(item) for item in cast(list[object], finding.get("citation_ids") or []) if str(item)]
        evidence_citations = [citations_by_id[item] for item in citation_ids if item in citations_by_id and _is_evidence_citation(citations_by_id[item])]
        if not evidence_citations:
            continue
        description = str(finding.get("text") or "").strip()
        if not description:
            continue
        event_id = f"chrono-{uuid4().hex}"
        event_date, precision = _date_from_text(description)
        now = utc_now()
        _ = conn.execute(
            """
            INSERT INTO chronology_events(chronology_event_id, matter_scope, event_date,
              event_date_precision, description, status, created_by_artifact_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?, ?)
            """,
            (event_id, matter_scope, event_date, precision, description, artifact_id, now, now),
        )
        for citation in evidence_citations:
            _add_graph_citation_span(conn, target_type="chronology_event", target_id=event_id, citation=citation)
        writes.append({"type": "chronology_event", "chronology_event_id": event_id})
    return writes


def _materialize_issue_route(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    artifact_id: str,
    packet: Mapping[str, object],
) -> list[dict[str, object]]:
    writes: list[dict[str, object]] = []
    citations_by_id = _citations_by_id(packet)
    for finding in _packet_items(packet, "findings"):
        citation_ids = [str(item) for item in cast(list[object], finding.get("citation_ids") or []) if str(item)]
        evidence_citations = [citations_by_id[item] for item in citation_ids if item in citations_by_id and _is_evidence_citation(citations_by_id[item])]
        if not evidence_citations:
            continue
        text = str(finding.get("text") or "").strip()
        if not text:
            continue
        issue_id = f"issue-{uuid4().hex}"
        now = utc_now()
        _ = conn.execute(
            """
            INSERT INTO issues(issue_id, matter_scope, title, route, status, summary, created_at, updated_at)
            VALUES (?, ?, ?, '', 'candidate', ?, ?, ?)
            """,
            (issue_id, matter_scope, text[:160], text, now, now),
        )
        claim_id = repo.add_claim(
            conn,
            matter_scope=matter_scope,
            claim_text=text,
            issue_id=issue_id,
            support_status="candidate",
            created_by_artifact_id=artifact_id,
        )
        for citation in evidence_citations:
            _add_graph_citation_span(conn, target_type="claim", target_id=claim_id, citation=citation)
        writes.append({"type": "issue_route", "issue_id": issue_id, "claim_id": claim_id})
    return writes


def _issue_completion_certification_if_safe(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    candidate_id: str,
    artifact_id: str,
    packet: Mapping[str, object],
) -> list[dict[str, object]]:
    task_type = str(task["task_type"])
    certification_type = MATTER_CERTIFICATIONS_BY_TASK_TYPE.get(task_type)
    if not certification_type:
        return []
    matter_scope = str(task["matter_scope"])
    if _has_active_matter_certification(conn, matter_scope=matter_scope, certification_type=certification_type):
        return []
    blocker = _certification_blocker(certification_type=certification_type, packet=packet)
    if blocker:
        validation_id = repo.record_validation(
            conn,
            target_type="matter",
            target_id=matter_scope,
            gate_name=certification_type,
            passed=False,
            details={"reason": blocker, "task_id": task.get("task_id"), "candidate_id": candidate_id, "artifact_id": artifact_id},
            severity="error",
        )
        _ = repo.record_human_attention(
            conn,
            target_type="task",
            target_id=str(task.get("task_id") or ""),
            severity="blocker",
            reason=f"completion certification {certification_type} withheld: {blocker}",
            matter_scope=matter_scope,
        )
        decision_task = _surface_withheld_certification_next_step(
            conn,
            task=task,
            candidate_id=candidate_id,
            artifact_id=artifact_id,
            certification_type=certification_type,
            reason=blocker,
            validation_result_id=validation_id,
            packet=packet,
        )
        return [
            {
                "certification_type": certification_type,
                "withheld": True,
                "validation_result_id": validation_id,
                "reason": blocker,
                "decision_task_id": decision_task.get("task_id", ""),
                "decision_task_created": decision_task.get("created", False),
            }
        ]
    if certification_type == "final_quality_gate":
        support_outcome = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)
        if not support_outcome.passed:
            reason = "final quality gate citation support/currentness validation failed"
            _ = repo.record_human_attention(
                conn,
                target_type="task",
                target_id=str(task.get("task_id") or ""),
                severity="blocker",
                reason=f"completion certification {certification_type} withheld: {reason}",
                matter_scope=matter_scope,
            )
            decision_task = _surface_withheld_certification_next_step(
                conn,
                task=task,
                candidate_id=candidate_id,
                artifact_id=artifact_id,
                certification_type=certification_type,
                reason=reason,
                validation_result_id=support_outcome.validation_result_id,
                packet=packet,
            )
            return [
                {
                    "certification_type": certification_type,
                    "withheld": True,
                    "validation_result_id": support_outcome.validation_result_id,
                    "reason": reason,
                    "decision_task_id": decision_task.get("task_id", ""),
                    "decision_task_created": decision_task.get("created", False),
                }
            ]
    if certification_type in VALIDATED_CERTIFICATION_GATES:
        outcome = run_validation(conn, gate_name=certification_type, target_type="matter", target_id=matter_scope)
        if not outcome.passed:
            _ = repo.record_human_attention(
                conn,
                target_type="task",
                target_id=str(task.get("task_id") or ""),
                severity="blocker",
                reason=f"completion certification {certification_type} withheld: validation failed after reduction",
                matter_scope=matter_scope,
            )
            decision_task = _surface_withheld_certification_next_step(
                conn,
                task=task,
                candidate_id=candidate_id,
                artifact_id=artifact_id,
                certification_type=certification_type,
                reason="validation failed",
                validation_result_id=outcome.validation_result_id,
                packet=packet,
            )
            return [
                {
                    "certification_type": certification_type,
                    "withheld": True,
                    "validation_result_id": outcome.validation_result_id,
                    "reason": "validation failed",
                    "decision_task_id": decision_task.get("task_id", ""),
                    "decision_task_created": decision_task.get("created", False),
                }
            ]
        validation_id = outcome.validation_result_id
    else:
        validation_id = repo.record_validation(
            conn,
            target_type="matter",
            target_id=matter_scope,
            gate_name=certification_type,
            passed=True,
            details={
                "task_id": task.get("task_id"),
                "candidate_id": candidate_id,
                "artifact_id": artifact_id,
                "basis": "reducer accepted cited candidate packet",
            },
            severity="info",
        )
    certification_id = repo.add_certification(
        conn,
        subject_type="matter",
        subject_id=matter_scope,
        certification_type=certification_type,
        validator="atticus-reducer",
        validation_result_id=validation_id,
        evidence={"task_id": str(task.get("task_id") or ""), "candidate_id": candidate_id, "artifact_id": artifact_id},
    )
    return [{"certification_id": certification_id, "certification_type": certification_type}]


def _surface_withheld_certification_next_step(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    candidate_id: str,
    artifact_id: str,
    certification_type: str,
    reason: str,
    validation_result_id: str | int,
    packet: Mapping[str, object],
) -> dict[str, object]:
    if certification_type == "final_quality_gate" and _packet_declares_operator_decision_point(packet):
        matter_scope = str(task["matter_scope"])
        task_id = str(task["task_id"])
        attention_id = repo.record_human_attention_once(
            conn,
            target_type="matter",
            target_id=matter_scope,
            severity="blocker",
            reason="final quality gate reached operator decision point: no internal Atticus repairs remain",
            matter_scope=matter_scope,
        )
        orchestrator_id = repo.upsert_matter_orchestrator(conn, matter_scope=matter_scope, status="user_intervention_required")
        _ = repo.record_orchestrator_event(
            conn,
            orchestrator_id=orchestrator_id,
            event_type="master_orchestrator.user_intervention_required",
            payload={
                "task_id": task_id,
                "candidate_id": candidate_id,
                "artifact_id": artifact_id,
                "certification_type": certification_type,
                "validation_result_id": str(validation_result_id),
                "reason": reason,
                "attention_id": attention_id or "",
                "operator_decision_point": True,
                "external_actions": "blocked",
            },
        )
        return {
            "created": False,
            "task_id": "",
            "operator_decision_required": True,
            "attention_id": attention_id or "",
        }
    return ensure_certification_decision_task(
        conn,
        task=task,
        candidate_id=candidate_id,
        artifact_id=artifact_id,
        certification_type=certification_type,
        reason=reason,
        validation_result_id=str(validation_result_id),
        packet=packet,
    )


def _packet_declares_operator_decision_point(packet: Mapping[str, object]) -> bool:
    if _packet_items(packet, "proposed_tasks"):
        return False
    text = " ".join(
        [
            str(packet.get("summary") or ""),
            *[str(item.get("text") or item.get("description") or "") for item in _packet_items(packet, "risk_flags")],
            *[str(item.get("text") or item.get("description") or "") for item in _packet_items(packet, "uncertainties")],
            *[str(item.get("content") or "") for item in _packet_items(packet, "proposed_artifacts")],
        ]
    ).lower()
    return (
        "no internal atticus repairs remain" in text
        or "no internal repair" in text
        or "operator-dependent" in text
        or "operator decision" in text
    )


def ensure_certification_decision_task(
    conn: sqlite3.Connection,
    *,
    task: Mapping[str, object],
    candidate_id: str,
    artifact_id: str,
    certification_type: str,
    reason: str,
    validation_result_id: str,
    packet: Mapping[str, object],
) -> dict[str, object]:
    """Create bounded follow-up work when a certification is withheld.

    A withheld S6-S9 certification is not a terminal success state. The harness
    should continue until it has either repaired the defect or prepared a crisp
    operator decision surface. This task is still candidate-only: it cannot
    certify the matter, perform external actions, or replace human judgment.
    """

    if certification_type not in CERTIFICATION_DECISION_TASK_TYPES:
        return {"created": False, "reason": "certification type does not require a decision task"}
    matter_scope = str(task["matter_scope"])
    parent_task_id = str(task["task_id"])
    task_id = f"{parent_task_id}--certification-decision"
    existing = conn.execute("SELECT task_id, status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if existing is not None:
        return {"created": False, "task_id": task_id, "status": str(existing["status"])}
    stage = _decision_task_stage(task)
    source_ids = _citation_target_ids(packet, "source") or _effective_source_dependencies(task)
    artifact_ids = _dedupe_ordered([artifact_id, *_citation_target_ids(packet, "artifact")])
    policy = smart_provider_policy_for_route(
        default_smart_model_policy(),
        layer="verifier",
        stage=str(stage),
        task_type="final_quality_gate" if certification_type == "final_quality_gate" else str(task["task_type"]),
        task_id=task_id,
        matter_scope=matter_scope,
        risk_level="high",
        legal_complexity="complex",
        authority_required=certification_type in {"authority_map", "final_quality_gate"},
        hostile_review_required=certification_type in {"draft_preparation", "final_quality_gate"},
        drafting_finality="final" if certification_type == "final_quality_gate" else "intermediate",
        contradiction_count=len(_packet_items(packet, "contradictions")),
        unresolved_uncertainty_count=len(_packet_items(packet, "uncertainties")),
        requested_capabilities=("final_quality_gate", "legal_reasoning", "high_risk_synthesis"),
    )
    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            title=f"Prepare operator decision packet for withheld {certification_type}",
            task_type="certification_decision_packet",
            stage=stage,
            matter_scope=matter_scope,
            source_dependencies=source_ids,
            artifact_dependencies=artifact_ids,
            provider_policy={**policy, "max_tokens": 8192},
            expected_value=max(float(str(task.get("expected_value") or 0.0)), 0.9),
            validation_gates=["citation_target_integrity", "citation_support_integrity"],
            instructions=_certification_decision_instructions(
                certification_type=certification_type,
                reason=reason,
                parent_task_id=parent_task_id,
                candidate_id=candidate_id,
                artifact_id=artifact_id,
                validation_result_id=validation_result_id,
            ),
        ),
    )
    _ = repo.record_human_attention_once(
        conn,
        target_type="task",
        target_id=task_id,
        severity="warning",
        reason=f"withheld {certification_type} requires operator decision packet",
        matter_scope=matter_scope,
    )
    orchestrator_id = repo.upsert_matter_orchestrator(conn, matter_scope=matter_scope, status="repair_required")
    _ = repo.record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.certification_decision_task_created",
        payload={
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "candidate_id": candidate_id,
            "artifact_id": artifact_id,
            "certification_type": certification_type,
            "validation_result_id": validation_result_id,
            "reason": reason,
            "external_actions": "blocked",
            "canonical_writes": "reducer_only",
        },
    )
    return {"created": True, "task_id": task_id}


def _decision_task_stage(task: Mapping[str, object]) -> LegalStage:
    try:
        return LegalStage(str(task["stage"]))
    except ValueError:
        return LegalStage.S9_FINAL_QUALITY_GATE


def _certification_decision_instructions(
    *,
    certification_type: str,
    reason: str,
    parent_task_id: str,
    candidate_id: str,
    artifact_id: str,
    validation_result_id: str,
) -> str:
    return (
        f"The reducer withheld matter certification {certification_type!r} for parent task {parent_task_id}. "
        f"Reason: {reason}. Candidate: {candidate_id}. Reduced artifact: {artifact_id}. "
        f"Validation result: {validation_result_id}. "
        "Prepare a concise operator decision packet, not a final legal conclusion. Identify: "
        "1. each unresolved defect, 2. whether Atticus can safely repair it using existing matter sources, "
        "3. the exact bounded internal follow-up task if repair is possible, 4. what genuinely requires operator "
        "judgment or new evidence, and 5. the safest next case options with caveats. "
        "If any defect is repairable using existing matter sources, include exactly one structured proposed_tasks[] "
        "entry for the highest-value bounded internal repair. If no defect is repairable, leave proposed_tasks empty "
        "and explain the operator decision needed in the proposed artifact. "
        "For review-report conclusions, use finding_type='drafting_note', 'risk', or 'contradiction'. "
        "Do not use finding_type='fact', 'law', or 'procedure' unless the finding cites primary source or verified "
        "authority proof directly. A final-quality, citation-audit, hostile-review, or decision-packet artifact can "
        "support what the review reported, but it is not primary proof of the underlying fact or legal rule. "
        "Do not propose contacting anyone, filing, serving, emailing, obtaining documents, or manual verification as "
        "runnable Atticus tasks. Those must be framed as operator decisions or human-attention items. "
        "Cite source IDs for facts and cite the final/review artifact only for what the review found."
    )


def _certification_blocker(*, certification_type: str, packet: Mapping[str, object]) -> str:
    if _evidence_citation_count(packet) == 0:
        return "reduced packet contains no evidence citations"
    if certification_type == "authority_map" and not _has_cited_finding_type(packet, {"law", "procedure"}):
        return "authority map contains no cited legal or procedural findings"
    if certification_type == "chronology_citations" and not _has_cited_finding_type(packet, {"fact", "procedure", "inference", "contradiction"}):
        return "chronology contains no cited event findings"
    if certification_type == "citation_audit" and (
        _packet_items(packet, "contradictions") or _packet_items(packet, "risk_flags") or _audit_artifact_reports_failure(packet)
    ):
        return "citation audit found defects requiring repair before certification"
    if certification_type == "privacy_redaction_audit" and (
        _packet_items(packet, "redaction_flags") or _packet_items(packet, "risk_flags") or _audit_artifact_reports_failure(packet)
    ):
        return "privacy audit found redaction defects requiring repair before certification"
    if certification_type == "final_quality_gate" and (
        _packet_items(packet, "contradictions") or _packet_items(packet, "risk_flags") or _audit_artifact_reports_failure(packet)
    ):
        return "final quality gate found unresolved defects"
    return ""


def _has_active_matter_certification(conn: sqlite3.Connection, *, matter_scope: str, certification_type: str) -> bool:
    row = conn.execute(
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
    ).fetchone()
    return row is not None


def _packet_items(packet: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = packet.get(key)
    if not isinstance(value, list):
        return []
    return [dict(cast(Mapping[str, object], item)) for item in cast(list[object], value) if isinstance(item, Mapping)]


def _citations_by_id(packet: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {str(citation["citation_id"]): citation for citation in _packet_items(packet, "citations") if citation.get("citation_id")}


def _is_evidence_citation(citation: Mapping[str, object]) -> bool:
    return str(citation.get("target_type") or "") in EVIDENCE_TARGET_TYPES


def _evidence_citation_count(packet: Mapping[str, object]) -> int:
    return sum(1 for citation in _packet_items(packet, "citations") if _is_evidence_citation(citation))


def _audit_artifact_reports_failure(packet: Mapping[str, object]) -> bool:
    for artifact in _packet_items(packet, "proposed_artifacts"):
        content = str(artifact.get("content") or "").lower()
        if "overall result: fail" in content or "overall result: failed" in content:
            return True
    return False


def _has_cited_finding_type(packet: Mapping[str, object], finding_types: set[str]) -> bool:
    citations_by_id = _citations_by_id(packet)
    for finding in _packet_items(packet, "findings"):
        if str(finding.get("finding_type") or "") not in finding_types:
            continue
        citation_ids = [str(item) for item in cast(list[object], finding.get("citation_ids") or []) if str(item)]
        if any(citation_id in citations_by_id and _is_evidence_citation(citations_by_id[citation_id]) for citation_id in citation_ids):
            return True
    return False


def _date_from_text(text: str) -> tuple[str, str]:
    match = DATE_RE.search(text)
    if match is None:
        return "", "unknown"
    value = match.group(0)
    if re.match(r"\d{4}-\d{2}-\d{2}$", value):
        return value, "day"
    return value, "text"


def _add_graph_citation_span(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    citation: Mapping[str, object],
) -> None:
    target = str(citation.get("target_type") or "")
    kwargs: dict[str, object] = {
        "target_type": target_type,
        "target_id": target_id,
        "locator": str(citation.get("locator") or ""),
    }
    if target == "source":
        kwargs["source_id"] = str(citation.get("target_id") or "")
    elif target == "artifact":
        kwargs["artifact_id"] = str(citation.get("target_id") or "")
    elif target == "authority":
        kwargs["authority_id"] = str(citation.get("target_id") or "")
    else:
        return
    quote = str(citation.get("quote") or citation.get("excerpt") or "")
    kwargs["quoted_text"] = quote
    _ = repo.add_citation_span(conn, **kwargs)  # type: ignore[arg-type]
