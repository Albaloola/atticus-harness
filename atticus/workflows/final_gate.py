"""Deterministic final-gate readiness and repair planning."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
import sqlite3
from typing import cast

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.status.completion import FINAL_LEGAL_DRAFT_CERTIFICATIONS, build_matter_completion_report, route_human_attention


CERTIFICATION_TASK_POLICY: dict[str, tuple[str, LegalStage, str]] = {
    "authority_map": ("authority_map", LegalStage.S6_AUTHORITY_LAW_MAP, "Map verified legal authorities to the live issue route."),
    "draft_preparation": ("draft_preparation", LegalStage.S8_DRAFT_PREPARATION, "Prepare or repair the evidence-grounded legal draft."),
    "hostile_review": ("hostile_opponent_review", LegalStage.S7_HOSTILE_REVIEW, "Run hostile review against the current draft and evidence map."),
    "privacy_redaction_audit": ("privacy_redaction_audit", LegalStage.S9_FINAL_QUALITY_GATE, "Audit the current draft for privacy and redaction defects."),
    "citation_audit": ("citation_audit", LegalStage.S7_HOSTILE_REVIEW, "Audit every material citation before final quality review."),
    "final_quality_gate": ("final_quality_gate", LegalStage.S9_FINAL_QUALITY_GATE, "Run the final deterministic quality gate over the current matter record."),
}


def final_gate_readiness(conn: sqlite3.Connection, matter_scope: str) -> dict[str, object]:
    report = build_matter_completion_report(conn, matter_scope)
    active = _active_certifications(conn, matter_scope)
    missing = [cert for cert in FINAL_LEGAL_DRAFT_CERTIFICATIONS if cert not in active]
    open_reviews = list(report.reducer_review_queue)
    blocked_reasons: list[dict[str, object]] = []
    for cert in missing:
        blocked_reasons.append(
            {
                "type": "missing_certification",
                "certification": cert,
                "owner": "orchestrator",
                "signature": f"missing_certification:matter:{matter_scope}:{cert}",
                "repair": _repair_for_missing_certification(cert),
                "resume_command": f"python -m atticus.cli final-gate create-missing --db DB --matter {matter_scope} --write --json",
            }
        )
    for review in open_reviews:
        blocked_reasons.append(
            {
                "type": "reducer_review_required",
                "candidate_id": review["candidate_id"],
                "task_id": review["task_id"],
                "owner": "reducer",
                "signature": f"reducer_review:{review['candidate_id']}",
                "repair": "review and accept/reject reducer candidate",
                "resume_command": f"python -m atticus.cli reducer-review show --db DB --candidate-id {review['candidate_id']} --json",
            }
        )
    for artifact_id in report.stale_artifacts:
        blocked_reasons.append({
            "type": "stale_artifact",
            "artifact_id": artifact_id,
            "owner": "orchestrator",
            "signature": f"stale_artifact:{artifact_id}",
            "repair": "rebuild or replace stale artifact",
            "resume_command": f"python -m atticus.cli inspect --db DB --type artifact --id {artifact_id}",
        })
    for attention in report.unresolved_human_attention:
        route = route_human_attention(dict(attention), matter_scope)
        blocked_reasons.append(
            {
                "type": "open_human_attention",
                "attention_id": attention["attention_id"],
                "reason": attention["reason"],
                "owner": route["routed_owner"],
                "signature": attention.get("signature") or f"human_attention:{attention['attention_id']}",
                "repair": route["routed_action"],
                "resume_command": route["routed_command"],
                "routed_owner": route["routed_owner"],
                "routed_action": route["routed_action"],
                "routed_command": route["routed_command"],
                "routed_lane": attention.get("routed_lane", "human_request"),
                "classification": route["classification"],
            }
        )
    failed_without_plan = _failed_final_tasks_without_repair_plan(conn, matter_scope)
    for task in failed_without_plan:
        blocked_reasons.append(
            {
                "type": "failed_final_task_without_repair_plan",
                "task_id": task["task_id"],
                "task_type": task["task_type"],
                "owner": "orchestrator",
                "signature": f"failed_final_task:{task['task_id']}",
                "repair": "create or apply repair plan for failed final workflow task",
                "resume_command": f"python -m atticus.cli orchestrator worker-failed --db DB --matter {matter_scope} --task-id {task['task_id']} --reason \"inspect failed final workflow task\" --write",
            }
        )
    only_final_missing = missing == ["final_quality_gate"]
    operator_blocked = any(
        str(a.get("routed_lane", "human_request")) == "human_request"
        and route_human_attention(dict(a), matter_scope)["routed_owner"] == "operator"
        and route_human_attention(dict(a), matter_scope)["routed_owner"] != "provider_control_plane"
        for a in report.unresolved_human_attention
    )
    state_payload = compute_final_gate_state(
        matter_scope=matter_scope,
        active_certifications=active,
        missing=missing,
        open_reviews=open_reviews,
        blocked_reasons=blocked_reasons,
        can_create_final_gate=only_final_missing and not open_reviews and not report.stale_artifacts and not operator_blocked,
    )
    return {
        "matter_scope": matter_scope,
        "state": state_payload["state"],
        "owner": state_payload["owner"],
        "ready": not blocked_reasons,
        "complete": "final_quality_gate" in active and not blocked_reasons,
        "can_create_final_gate": only_final_missing and not open_reviews and not report.stale_artifacts and not operator_blocked,
        "missing_certifications": missing,
        "active_certifications": [cert for cert in FINAL_LEGAL_DRAFT_CERTIFICATIONS if cert in active],
        "reducer_review_queue": open_reviews,
        "stale_artifacts": list(report.stale_artifacts),
        "open_human_attention_count": len(report.unresolved_human_attention),
        "failed_final_tasks_without_repair_plan": failed_without_plan,
        "blocked_reasons": blocked_reasons,
        "next_action": state_payload["next_action"],
    }


def compute_final_gate_state(
    *,
    matter_scope: str,
    active_certifications: set[str],
    missing: list[str],
    open_reviews: list[dict[str, object]],
    blocked_reasons: list[dict[str, object]],
    can_create_final_gate: bool,
) -> dict[str, object]:
    next_action = _next_final_gate_action(matter_scope, missing, open_reviews, blocked_reasons)
    if "final_quality_gate" in active_certifications and not blocked_reasons:
        state = "certified"
        owner = "none"
    elif any(
        str(reason.get("type") or "") == "open_human_attention"
        and str(reason.get("routed_lane", "human_request")) == "human_request"
        and str(reason.get("routed_owner") or "") != "provider_control_plane"
        for reason in blocked_reasons
    ):
        ha_reason = next(
            r for r in blocked_reasons
            if str(r.get("type") or "") == "open_human_attention"
            and str(r.get("routed_lane", "human_request")) == "human_request"
            and str(r.get("routed_owner") or "") != "provider_control_plane"
        )
        state = "human_blocked"
        owner = str(ha_reason.get("routed_owner") or "operator")
    elif open_reviews:
        state = "waiting_reducer_review"
        owner = "reducer"
    elif any(str(reason.get("type") or "") in {"stale_artifact", "failed_final_task_without_repair_plan"} for reason in blocked_reasons):
        state = "repairing"
        owner = "orchestrator"
    elif not active_certifications and missing:
        state = "not_started"
        owner = "orchestrator"
    elif missing and missing[0] != "final_quality_gate":
        state = "creating_missing"
        owner = "orchestrator"
    elif can_create_final_gate:
        state = "ready_for_final_task"
        owner = "orchestrator"
    elif missing == ["final_quality_gate"]:
        state = "final_task_pending"
        owner = "scheduler"
    else:
        state = "repairing"
        owner = str(next_action.get("owner") or "orchestrator")
    return {"state": state, "owner": owner, "next_action": next_action}


def record_final_gate_state(conn: sqlite3.Connection, matter_scope: str) -> dict[str, object]:
    readiness = final_gate_readiness(conn, matter_scope)
    blockers = {"blocked_reasons": readiness.get("blocked_reasons") or []}
    next_action = readiness.get("next_action") if isinstance(readiness.get("next_action"), Mapping) else {}
    _ = conn.execute(
        """
        INSERT INTO final_gate_states(matter_scope, state, blocker_json, next_action_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(matter_scope) DO UPDATE SET
          state = excluded.state,
          blocker_json = excluded.blocker_json,
          next_action_json = excluded.next_action_json,
          updated_at = excluded.updated_at
        """,
        (
            matter_scope,
            str(readiness["state"]),
            json.dumps(blockers, sort_keys=True, separators=(",", ":")),
            json.dumps(next_action, sort_keys=True, separators=(",", ":")),
            repo.utc_now(),
        ),
    )
    conn.commit()
    return readiness


def plan_final_gate_repairs(conn: sqlite3.Connection, matter_scope: str) -> list[dict[str, object]]:
    readiness = final_gate_readiness(conn, matter_scope)
    repairs: list[dict[str, object]] = []
    # Only reducer reviews prevent gate creation; operator-owned attention is valid to proceed in parallel
    reviews = cast(list[object], readiness.get("reducer_review_queue", []))
    for reason in cast(list[Mapping[str, object]], readiness["blocked_reasons"]):
        if reason["type"] == "missing_certification":
            cert = str(reason["certification"])
            if cert == "final_quality_gate" and reviews:
                repairs.append(
                    {
                        "type": "final_gate_prerequisites_blocked",
                        "certification": cert,
                        "reason": "final quality gate cannot be created while reducer review, stale dependencies, or human attention remain open",
                        "write_command": f"python -m atticus.cli final-gate readiness --db DB --matter {matter_scope} --json",
                    }
                )
                continue
            repairs.append(
                {
                    "type": "create_missing_certification_work",
                    "certification": cert,
                    "task_type": CERTIFICATION_TASK_POLICY.get(cert, (cert, LegalStage.S9_FINAL_QUALITY_GATE, ""))[0],
                    "stage": str(CERTIFICATION_TASK_POLICY.get(cert, (cert, LegalStage.S9_FINAL_QUALITY_GATE, ""))[1]),
                    "write_command": str(reason.get("resume_command") or f"python -m atticus.cli final-gate create-missing --db DB --matter {matter_scope} --write --json"),
                }
            )
        elif reason["type"] == "reducer_review_required":
            repairs.append(
                {
                    "type": "manual_reducer_review",
                    "candidate_id": reason["candidate_id"],
                    "task_id": reason["task_id"],
                    "write_command": str(reason.get("resume_command") or f"python -m atticus.cli reducer-review show --db DB --candidate-id {reason['candidate_id']} --json"),
                }
            )
        else:
            repairs.append(dict(reason))
    order = {"manual_reducer_review": 0, "create_missing_certification_work": 1, "final_gate_prerequisites_blocked": 2}
    return sorted(repairs, key=lambda item: (order.get(str(item.get("type") or ""), 9), str(item.get("task_id") or item.get("certification") or "")))


def create_missing_final_gate_work(conn: sqlite3.Connection, matter_scope: str) -> dict[str, object]:
    readiness = final_gate_readiness(conn, matter_scope)
    missing = cast(list[str], readiness["missing_certifications"])
    if not missing:
        return {"created": False, "reason": "final gate requirements are already satisfied", "readiness": readiness}
    reviews = cast(list[object], readiness["reducer_review_queue"])
    if reviews:
        return {"created": False, "reason": "reducer review must be resolved before creating more final-gate work", "readiness": readiness}
    certification = missing[0]
    if certification == "final_quality_gate" and len(missing) > 1:
        return {"created": False, "reason": "final quality gate cannot be created before prerequisite certifications", "readiness": readiness}
    existing = _existing_open_certification_task(conn, matter_scope=matter_scope, certification=certification)
    if existing:
        return {"created": False, "reason": "certification-producing task already exists", "task_id": existing, "certification": certification, "readiness": readiness}
    task_type, stage, purpose = CERTIFICATION_TASK_POLICY.get(
        certification,
        (certification, LegalStage.S9_FINAL_QUALITY_GATE, f"Create missing certification {certification}."),
    )
    task_id = _task_id(matter_scope=matter_scope, certification=certification)
    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            title=f"Create missing {certification} certification",
            task_type=task_type,
            stage=stage,
            matter_scope=matter_scope,
            status=TaskStatus.QUEUED,
            source_dependencies=_source_ids(conn, matter_scope),
            artifact_dependencies=_latest_final_artifacts(conn, matter_scope),
            required_certifications=_prior_certification_requirements(matter_scope, certification),
            validation_gates=["citation_target_integrity", "citation_support_integrity"] if certification in {"citation_audit", "final_quality_gate"} else [],
            instructions=(
                f"{purpose} Work only from existing matter evidence, artifacts, and authorities. "
                "If a human legal decision or external action is required, record it as a blocker instead of pretending finality."
            ),
            expected_value=0.95,
        ),
    )
    _ = record_final_gate_state(conn, matter_scope)
    return {
        "created": True,
        "task_id": task_id,
        "certification": certification,
        "task_type": task_type,
        "stage": str(stage),
        "next_command": f"python -m atticus.cli schedule --db DB --matter {matter_scope} --capacity 15 --json",
    }


def _active_certifications(conn: sqlite3.Connection, matter_scope: str) -> set[str]:
    return {
        str(row["certification_type"])
        for row in conn.execute(
            """
            SELECT certification_type
            FROM certifications
            WHERE subject_type = 'matter' AND subject_id = ? AND status = 'active'
            """,
            (matter_scope,),
        ).fetchall()
    }


def _failed_final_tasks_without_repair_plan(conn: sqlite3.Connection, matter_scope: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT task_id, task_type, stage
        FROM tasks
        WHERE matter_scope = ?
          AND status = 'failed'
          AND task_type IN ('citation_audit', 'citation_repair', 'draft_preparation',
                            'final_quality_gate', 'hostile_opponent_review',
                            'privacy_redaction_audit', 'redaction_fix', 'redaction_review')
        ORDER BY updated_at DESC, task_id
        """,
        (matter_scope,),
    ).fetchall()
    failed: list[dict[str, object]] = []
    for row in rows:
        plan = conn.execute(
            """
            SELECT repair_plan_id
            FROM repair_plans
            WHERE matter_scope = ? AND target_type = 'task' AND target_id = ? AND status IN ('proposed', 'applied', 'blocked', 'requires_human')
            LIMIT 1
            """,
            (matter_scope, row["task_id"]),
        ).fetchone()
        if plan is None:
            failed.append({"task_id": row["task_id"], "task_type": row["task_type"], "stage": row["stage"]})
    return failed


def _next_final_gate_action(
    matter_scope: str,
    missing: list[str],
    open_reviews: list[dict[str, object]],
    blocked_reasons: list[dict[str, object]],
) -> dict[str, object]:
    if not blocked_reasons:
        return {"type": "complete", "owner": "none", "resume_command": ""}
    if open_reviews:
        review = open_reviews[0]
        return {
            "type": "manual_reducer_review",
            "owner": "reducer",
            "candidate_id": review["candidate_id"],
            "task_id": review["task_id"],
            "signature": f"reducer_review:{review['candidate_id']}",
            "resume_command": f"python -m atticus.cli reducer-review show --db DB --candidate-id {review['candidate_id']} --json",
        }
    if missing:
        return {
            "type": "create_missing_certification_work",
            "owner": "orchestrator",
            "certification": missing[0],
            "signature": f"missing_certification:matter:{matter_scope}:{missing[0]}",
            "resume_command": f"python -m atticus.cli final-gate create-missing --db DB --matter {matter_scope} --write --json",
        }
    first = blocked_reasons[0]
    owner = str(first.get("routed_owner") or first.get("owner") or "orchestrator")
    return {
        "type": first["type"],
        "owner": owner,
        "signature": first.get("signature") or f"final_gate_blocker:{first['type']}",
        "resume_command": first.get("resume_command") or f"python -m atticus.cli matter-health --db DB --matter {matter_scope} --why-not-done --json",
    }


def _repair_for_missing_certification(certification: str) -> str:
    if certification == "citation_audit":
        return "create or run citation audit after resolving reducer-pending citation repairs"
    if certification == "final_quality_gate":
        return "create final quality gate only after every prerequisite certification is active"
    return f"create or run {certification} certification work"


def _existing_open_certification_task(conn: sqlite3.Connection, *, matter_scope: str, certification: str) -> str:
    task_type = CERTIFICATION_TASK_POLICY.get(certification, (certification, LegalStage.S9_FINAL_QUALITY_GATE, ""))[0]
    row = conn.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE matter_scope = ?
          AND task_type = ?
          AND status IN ('queued', 'ready', 'leased', 'running', 'reducer_pending', 'blocked')
        ORDER BY created_at DESC, task_id
        LIMIT 1
        """,
        (matter_scope, task_type),
    ).fetchone()
    return str(row["task_id"]) if row is not None else ""


def _task_id(*, matter_scope: str, certification: str) -> str:
    safe_matter = re.sub(r"[^A-Za-z0-9_.-]+", "-", matter_scope).strip("-").lower() or "matter"
    return f"{safe_matter}-{certification}-auto"


def _source_ids(conn: sqlite3.Connection, matter_scope: str) -> list[str]:
    return [
        str(row["source_id"])
        for row in conn.execute(
            "SELECT source_id FROM sources WHERE matter_scope = ? AND stale = 0 ORDER BY source_id",
            (matter_scope,),
        ).fetchall()
    ]


def _latest_final_artifacts(conn: sqlite3.Connection, matter_scope: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT artifact_id
        FROM artifacts
        WHERE matter_scope = ?
          AND stale = 0
          AND artifact_type IN ('draft', 'draft_complaint', 'complaint_draft', 'redacted_draft',
                                'citation_audit', 'hostile_review', 'privacy_redaction_audit')
        ORDER BY created_at DESC, artifact_id
        LIMIT 8
        """,
        (matter_scope,),
    ).fetchall()
    return [str(row["artifact_id"]) for row in rows]


def _prior_certification_requirements(matter_scope: str, certification: str) -> list[dict[str, object]]:
    requirements: list[dict[str, object]] = []
    for cert in FINAL_LEGAL_DRAFT_CERTIFICATIONS:
        if cert == certification:
            break
        requirements.append({"subject_type": "matter", "subject_id": matter_scope, "certification_type": cert})
    return requirements
