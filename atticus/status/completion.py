"""Matter completion and next-action reporting.

This module is intentionally read-only. It answers the control-plane question
that simple task counts cannot answer: is the matter actually done, and if not,
what owns the next safe transition?
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import sqlite3
from uuid import uuid4

from atticus.core.events import utc_now
from atticus.scheduler.gates import blocked_task_auto_requeue_allowed, evaluate_task_gates


FINAL_LEGAL_DRAFT_CERTIFICATIONS: tuple[str, ...] = (
    "source_inventory",
    "extraction_coverage",
    "evidence_registry",
    "production_mapping",
    "chronology_citations",
    "issue_route_map",
    "authority_map",
    "draft_preparation",
    "hostile_review",
    "privacy_redaction_audit",
    "citation_audit",
    "final_quality_gate",
)

FINAL_WORK_TASK_TYPES = {
    "authority_map",
    "authority_audit",
    "citation_audit",
    "citation_repair",
    "draft",
    "draft_preparation",
    "final_quality_gate",
    "hostile_opponent_review",
    "hostile_review",
    "privacy_redaction_audit",
    "redaction_fix",
    "redaction_review",
}


@dataclass(frozen=True)
class MatterCompletionRequirement:
    requirement_id: str
    requirement_type: str
    stage: str
    name: str
    status: str
    owner: str
    blocking_reason: str
    repair_action: str
    resume_command: str
    evidence: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MatterCompletionReport:
    matter_scope: str
    done: bool
    safe_to_finalize: bool
    blocked: bool
    runnable_count: int
    reducer_pending_count: int
    failed_count: int
    blocked_count: int
    reducer_pending: tuple[dict[str, object], ...]
    reducer_review_queue: tuple[dict[str, object], ...]
    missing_certifications: tuple[str, ...]
    stale_artifacts: tuple[str, ...]
    unresolved_human_attention: tuple[dict[str, object], ...]
    requirements: tuple[MatterCompletionRequirement, ...]

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["requirements"] = [requirement.as_dict() for requirement in self.requirements]
        return data


@dataclass(frozen=True)
class CompletionInvariantResult:
    incomplete: bool
    next_action_type: str
    owner: str
    repair_plan_id: str
    reducer_review_id: str
    runnable_task_id: str
    terminal_human_attention_id: str
    ok: bool
    reason: str
    next_action: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MatterCompletionSnapshot:
    snapshot_id: str
    matter_scope: str
    done: bool
    safe_to_finalize: bool
    blocked: bool
    primary_next_action: dict[str, object]
    counts: dict[str, int]
    report: dict[str, object]
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def build_matter_completion_report(conn: sqlite3.Connection, matter_scope: str) -> MatterCompletionReport:
    required_certifications = required_certifications_for_goal(conn, matter_scope)
    active_certifications = _active_certifications(conn, matter_scope)
    missing_certifications = tuple(cert for cert in required_certifications if cert not in active_certifications)
    runnable = _tasks_by_status(conn, matter_scope, ("queued", "ready"))
    reducer_pending = _tasks_by_status(conn, matter_scope, ("reducer_pending",))
    reducer_review_queue = tuple(_open_reducer_review_queue(conn, matter_scope))
    failed = _tasks_by_status(conn, matter_scope, ("failed",))
    blocked_tasks = _tasks_by_status(conn, matter_scope, ("blocked",))
    stale_artifacts = tuple(_stale_artifact_ids(conn, matter_scope))
    attention = tuple(_open_human_attention(conn, matter_scope))

    requirements: list[MatterCompletionRequirement] = []
    for certification in required_certifications:
        if certification in active_certifications:
            requirements.append(
                MatterCompletionRequirement(
                    requirement_id=f"certification:{certification}",
                    requirement_type="certification",
                    stage=_stage_for_certification(certification),
                    name=certification,
                    status="satisfied",
                    owner="reducer",
                    blocking_reason="",
                    repair_action="none",
                    resume_command="",
                    evidence={"certification": active_certifications[certification]},
                )
            )
            continue
        requirements.append(_missing_certification_requirement(matter_scope, certification, reducer_pending))

    for task in reducer_pending:
        requirements.append(_task_requirement(matter_scope, task, status="pending", owner="reducer", repair_action="manual_reducer_review"))
    for task in failed:
        requirements.append(_task_requirement(matter_scope, task, status="failed", owner="orchestrator", repair_action="inspect_failure_and_plan_repair"))
    for task in blocked_tasks:
        requirements.append(_task_requirement(matter_scope, task, status="blocked", owner=_owner_for_blocked_task(task), repair_action=_repair_action_for_blocked_task(task)))
    for artifact_id in stale_artifacts:
        requirements.append(
            MatterCompletionRequirement(
                requirement_id=f"artifact:{artifact_id}",
                requirement_type="artifact",
                stage="",
                name=artifact_id,
                status="stale",
                owner="orchestrator",
                blocking_reason="artifact is stale",
                repair_action="rebuild_or_replace_stale_artifact",
                resume_command=f"python -m atticus.cli inspect --db DB --type artifact --id {artifact_id}",
                evidence={"artifact_id": artifact_id},
            )
        )
    for item in attention:
        requirements.append(
            MatterCompletionRequirement(
                requirement_id=f"human_attention:{item['attention_id']}",
                requirement_type="human_review",
                stage="",
                name=str(item["reason"]),
                status="blocked",
                owner="operator",
                blocking_reason=str(item["reason"]),
                repair_action="operator_resolve_human_attention",
                resume_command=f"python -m atticus.cli human-attention --db DB --matter {matter_scope}",
                evidence=dict(item),
            )
        )

    done = not missing_certifications and not runnable and not reducer_pending and not failed and not blocked_tasks and not stale_artifacts and not attention
    safe_to_finalize = done and "final_quality_gate" in active_certifications
    blocked = not done and (bool(missing_certifications) or bool(reducer_pending) or bool(failed) or bool(blocked_tasks) or bool(stale_artifacts) or bool(attention))
    return MatterCompletionReport(
        matter_scope=matter_scope,
        done=done,
        safe_to_finalize=safe_to_finalize,
        blocked=blocked,
        runnable_count=len(runnable),
        reducer_pending_count=len(reducer_pending),
        failed_count=len(failed),
        blocked_count=len(blocked_tasks),
        reducer_pending=tuple(reducer_pending),
        reducer_review_queue=reducer_review_queue,
        missing_certifications=missing_certifications,
        stale_artifacts=stale_artifacts,
        unresolved_human_attention=attention,
        requirements=tuple(requirements),
    )


def required_certifications_for_goal(conn: sqlite3.Connection, matter_scope: str, goal: str | None = None) -> list[str]:
    """Return the currently required certifications for a matter.

    The first implementation is deliberately conservative for final drafting
    work. It can later be replaced by matter-profile/workflow-goal policy
    without changing callers.
    """

    if _looks_like_final_work(conn, matter_scope, goal):
        return list(FINAL_LEGAL_DRAFT_CERTIFICATIONS)
    observed = _observed_matter_certifications(conn, matter_scope)
    if observed:
        ordered = [cert for cert in FINAL_LEGAL_DRAFT_CERTIFICATIONS if cert in observed]
        return ordered or sorted(observed)
    return ["source_inventory", "extraction_coverage"]


def explain_why_not_done(conn: sqlite3.Connection, matter_scope: str) -> dict[str, object]:
    report = build_matter_completion_report(conn, matter_scope)
    next_action = next_resume_action(conn, matter_scope)
    return {
        **report.as_dict(),
        "why_not_done": [requirement.as_dict() for requirement in report.requirements if requirement.status != "satisfied"],
        "next_action": next_action,
        "completion_invariant": assert_completion_has_next_action(conn, matter_scope).as_dict(),
    }


def build_completion_snapshot(conn: sqlite3.Connection, matter_scope: str) -> MatterCompletionSnapshot:
    report = build_matter_completion_report(conn, matter_scope)
    next_action = next_resume_action(conn, matter_scope)
    return MatterCompletionSnapshot(
        snapshot_id=f"completion-snapshot-{uuid4().hex}",
        matter_scope=matter_scope,
        done=report.done,
        safe_to_finalize=report.safe_to_finalize,
        blocked=report.blocked,
        primary_next_action=next_action,
        counts={
            "runnable": report.runnable_count,
            "reducer_pending": report.reducer_pending_count,
            "failed": report.failed_count,
            "blocked": report.blocked_count,
            "open_reducer_reviews": len(report.reducer_review_queue),
            "open_human_attention": len(report.unresolved_human_attention),
            "missing_certifications": len(report.missing_certifications),
            "stale_artifacts": len(report.stale_artifacts),
        },
        report=report.as_dict(),
        created_at=utc_now(),
    )


def record_completion_snapshot(conn: sqlite3.Connection, matter_scope: str) -> MatterCompletionSnapshot:
    snapshot = build_completion_snapshot(conn, matter_scope)
    _ = conn.execute(
        """
        INSERT INTO matter_completion_snapshots(
          snapshot_id, matter_scope, done, safe_to_finalize, blocked,
          primary_next_action_json, counts_json, report_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.snapshot_id,
            snapshot.matter_scope,
            int(snapshot.done),
            int(snapshot.safe_to_finalize),
            int(snapshot.blocked),
            _json(snapshot.primary_next_action),
            _json(snapshot.counts),
            _json(snapshot.report),
            snapshot.created_at,
        ),
    )
    return snapshot


def assert_completion_has_next_action(conn: sqlite3.Connection, matter_scope: str) -> CompletionInvariantResult:
    """Classify the mandatory next action for a matter.

    This is deliberately stricter than a narrative report. If a matter is
    incomplete, a blocker explanation only satisfies the invariant when it maps
    to one primary action owned by a reducer, scheduler, orchestrator, provider,
    or operator/human-review lane.
    """

    report = build_matter_completion_report(conn, matter_scope)
    next_action = next_resume_action(conn, matter_scope)
    action_type = str(next_action.get("type") or "")
    owner = str(next_action.get("owner") or "")
    if report.done:
        return CompletionInvariantResult(
            incomplete=False,
            next_action_type=action_type or "complete",
            owner=owner or "none",
            repair_plan_id="",
            reducer_review_id="",
            runnable_task_id="",
            terminal_human_attention_id="",
            ok=True,
            reason="matter_complete",
            next_action=next_action,
        )

    reducer_review_id = _reducer_review_id_for_next_action(conn, matter_scope, next_action)
    runnable_task_id = _runnable_task_id_for_next_action(conn, matter_scope, next_action)
    repair_plan_id = _repair_plan_id_for_next_action(conn, matter_scope, next_action)
    attention_id = str(next_action.get("attention_id") or "")
    has_primary = bool(action_type and owner)
    classified = action_type in {
        "manual_reducer_review",
        "missing_certification",
        "blocked_task",
        "failed_task",
        "human_attention",
        "supervisor_tick",
    }
    has_owner_proof = bool(reducer_review_id or runnable_task_id or repair_plan_id or attention_id or action_type in {"missing_certification", "supervisor_tick"})
    ok = has_primary and classified and has_owner_proof
    reason = "primary_next_action_classified" if ok else "incomplete_matter_without_actionable_primary_next_action"
    return CompletionInvariantResult(
        incomplete=True,
        next_action_type=action_type,
        owner=owner,
        repair_plan_id=repair_plan_id,
        reducer_review_id=reducer_review_id,
        runnable_task_id=runnable_task_id,
        terminal_human_attention_id=attention_id,
        ok=ok,
        reason=reason,
        next_action=next_action,
    )


def next_resume_action(conn: sqlite3.Connection, matter_scope: str) -> dict[str, object]:
    report = build_matter_completion_report(conn, matter_scope)
    if report.done:
        return {
            "type": "complete",
            "owner": "none",
            "reason": "matter completion requirements are satisfied",
            "resume_command": "",
        }

    review_queue = _open_reducer_review_queue(conn, matter_scope)
    if review_queue:
        item = review_queue[0]
        return {
            "type": "manual_reducer_review",
            "owner": "reducer",
            "task_id": item["task_id"],
            "candidate_id": item["candidate_id"],
            "stage": item["stage"],
            "task_type": item["task_type"],
            "reason": item["reason"],
            "after": _after_reducer_review(report),
            "resume_command": f"python -m atticus.cli reducer-review show --db DB --candidate-id {item['candidate_id']} --json",
        }

    reducer_pending = _tasks_by_status(conn, matter_scope, ("reducer_pending",))
    if reducer_pending:
        task = _choose_reducer_pending_task(reducer_pending)
        candidate_id = _latest_candidate_for_task(conn, str(task["task_id"]))
        command = f"python -m atticus.cli inspect --db DB --type candidate --id {candidate_id}" if candidate_id else f"python -m atticus.cli inspect --db DB --type task --id {task['task_id']}"
        return {
            "type": "manual_reducer_review",
            "owner": "reducer",
            "task_id": task["task_id"],
            "candidate_id": candidate_id,
            "stage": task["stage"],
            "task_type": task["task_type"],
            "reason": "high-risk or gated candidate is reducer_pending",
            "after": _after_reducer_review(report),
            "resume_command": command,
        }

    runnable_or_requeueable = _runnable_or_requeueable_tasks(conn, matter_scope)
    if runnable_or_requeueable:
        task = _choose_runnable_task(runnable_or_requeueable)
        requeue_note = " or safely requeueable blocked" if str(task["status"]) == "blocked" else ""
        return {
            "type": "supervisor_tick",
            "owner": "scheduler",
            "task_id": task["task_id"],
            "stage": task["stage"],
            "task_type": task["task_type"],
            "reason": f"runnable{requeue_note} tasks remain",
            "after": "rerun matter-health after the scheduler tick",
            "resume_command": f"python -m atticus.cli run-free-loop --db DB --matter {matter_scope} --output-dir OUT --capacity 15 --max-ticks 1",
        }

    if report.missing_certifications:
        certification = report.missing_certifications[0]
        return {
            "type": "missing_certification",
            "owner": "orchestrator",
            "certification": certification,
            "reason": f"required certification is missing: {certification}",
            "after": _after_missing_certification(certification),
            "resume_command": f"python -m atticus.cli coordinator plan --db DB --matter {matter_scope} --goal \"create missing {certification} work\"",
        }

    if report.blocked_count:
        blocked = _tasks_by_status(conn, matter_scope, ("blocked",))
        task = blocked[0]
        return {
            "type": "blocked_task",
            "owner": _owner_for_blocked_task(task),
            "task_id": task["task_id"],
            "reason": _blocked_reason_text(task),
            "resume_command": f"python -m atticus.cli orchestrator repair --db DB --matter {matter_scope} --failure-event-id EVENT_ID",
        }

    if report.failed_count:
        failed = _tasks_by_status(conn, matter_scope, ("failed",))
        task = failed[0]
        return {
            "type": "failed_task",
            "owner": "orchestrator",
            "task_id": task["task_id"],
            "reason": "task failed and needs repair planning",
            "resume_command": f"python -m atticus.cli orchestrator worker-failed --db DB --matter {matter_scope} --task-id {task['task_id']} --reason \"inspect failed task\" --write",
        }

    if report.unresolved_human_attention:
        item = report.unresolved_human_attention[0]
        return {
            "type": "human_attention",
            "owner": "operator",
            "attention_id": item["attention_id"],
            "reason": item["reason"],
            "resume_command": f"python -m atticus.cli human-attention --db DB --matter {matter_scope}",
        }

    return {
        "type": "unknown_incomplete",
        "owner": "maintenance",
        "reason": "matter is incomplete but no direct next action was classified",
        "resume_command": f"python -m atticus.cli maintenance trigger --db DB --matter {matter_scope} --reason \"matter incomplete with no classified next action\" --write",
    }


def _looks_like_final_work(conn: sqlite3.Connection, matter_scope: str, goal: str | None) -> bool:
    if goal and any(term in goal.lower() for term in ("draft", "complaint", "filing", "final", "citation audit", "quality gate", "support pack")):
        return True
    row = conn.execute(
        """
        SELECT 1
        FROM tasks
        WHERE matter_scope = ?
          AND (stage IN ('S8', 'S9') OR task_type IN ({placeholders}))
        LIMIT 1
        """.format(placeholders=", ".join("?" for _ in FINAL_WORK_TASK_TYPES)),
        (matter_scope, *sorted(FINAL_WORK_TASK_TYPES)),
    ).fetchone()
    return row is not None


def _active_certifications(conn: sqlite3.Connection, matter_scope: str) -> dict[str, dict[str, object]]:
    rows = conn.execute(
        """
        SELECT certification_id, certification_type, subject_type, subject_id, validator, created_at
        FROM certifications
        WHERE subject_type = 'matter' AND subject_id = ? AND status = 'active'
        ORDER BY created_at DESC
        """,
        (matter_scope,),
    ).fetchall()
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        cert_type = str(row["certification_type"])
        result.setdefault(cert_type, _row_to_dict(row))
    return result


def _observed_matter_certifications(conn: sqlite3.Connection, matter_scope: str) -> set[str]:
    return {
        str(row["certification_type"])
        for row in conn.execute(
            """
            SELECT DISTINCT certification_type
            FROM certifications
            WHERE subject_type = 'matter' AND subject_id = ? AND status = 'active'
            """,
            (matter_scope,),
        ).fetchall()
    }


def _tasks_by_status(conn: sqlite3.Connection, matter_scope: str, statuses: tuple[str, ...]) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT task_id, matter_scope, title, task_type, stage, status, blocked_reasons_json, updated_at, created_at, expected_value,
               source_dependencies_json, artifact_dependencies_json, task_dependencies_json, matter_dependencies_json,
               required_certifications_json, validation_gates_json, provider_policy_json, cost_limit_usd
        FROM tasks
        WHERE matter_scope = ? AND status IN ({placeholders})
        ORDER BY
          CASE stage WHEN 'S9' THEN 0 WHEN 'S8' THEN 1 WHEN 'S7' THEN 2 WHEN 'S6' THEN 3 ELSE 4 END,
          expected_value DESC,
          updated_at DESC,
          created_at ASC
        """.format(placeholders=", ".join("?" for _ in statuses)),
        (matter_scope, *statuses),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _stale_artifact_ids(conn: sqlite3.Connection, matter_scope: str) -> list[str]:
    rows = conn.execute(
        "SELECT artifact_id FROM artifacts WHERE matter_scope = ? AND stale = 1 ORDER BY updated_at DESC LIMIT 50",
        (matter_scope,),
    ).fetchall()
    return [str(row["artifact_id"]) for row in rows]


def _open_human_attention(conn: sqlite3.Connection, matter_scope: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT attention_id, matter_scope, target_type, target_id, severity, reason, status,
               owner, signature, superseded_by, created_at
        FROM human_attention
        WHERE matter_scope = ? AND status = 'open'
        ORDER BY CASE severity WHEN 'blocker' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, attention_id DESC
        LIMIT 50
        """,
        (matter_scope,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _open_reducer_review_queue(conn: sqlite3.Connection, matter_scope: str) -> list[dict[str, object]]:
    try:
        rows = conn.execute(
            """
            SELECT reducer_review_id, matter_scope, candidate_id, task_id, stage, task_type,
                   priority, status, reason, recommended_action, created_at, updated_at
            FROM reducer_review_queue
            WHERE matter_scope = ? AND status = 'open'
            ORDER BY priority ASC, updated_at ASC
            LIMIT 25
            """,
            (matter_scope,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_row_to_dict(row) for row in rows]


def _missing_certification_requirement(
    matter_scope: str,
    certification: str,
    reducer_pending: list[dict[str, object]],
) -> MatterCompletionRequirement:
    repair_action = "create_certification_work"
    owner = "orchestrator"
    resume_command = f"python -m atticus.cli coordinator plan --db DB --matter {matter_scope} --goal \"create missing {certification} work\""
    if certification == "citation_audit":
        repair_action = "run_citation_audit_or_reduce_pending_citation_repair"
        if any(str(task["task_type"]) in {"citation_repair", "citation_audit"} for task in reducer_pending):
            owner = "reducer"
            resume_command = f"python -m atticus.cli matter-health --db DB --matter {matter_scope} --why-not-done"
    elif certification == "final_quality_gate":
        repair_action = "complete_prior_final_gate_requirements_then_run_final_quality_gate"
    return MatterCompletionRequirement(
        requirement_id=f"certification:{certification}",
        requirement_type="certification",
        stage=_stage_for_certification(certification),
        name=certification,
        status="blocked" if certification in {"citation_audit", "final_quality_gate"} else "pending",
        owner=owner,
        blocking_reason=f"missing certification: matter:{matter_scope}:{certification}",
        repair_action=repair_action,
        resume_command=resume_command,
        evidence={"certification_type": certification, "subject_type": "matter", "subject_id": matter_scope},
    )


def _task_requirement(
    matter_scope: str,
    task: Mapping[str, object],
    *,
    status: str,
    owner: str,
    repair_action: str,
) -> MatterCompletionRequirement:
    task_id = str(task["task_id"])
    return MatterCompletionRequirement(
        requirement_id=f"task:{task_id}",
        requirement_type="task",
        stage=str(task["stage"]),
        name=str(task["title"] or task_id),
        status=status,
        owner=owner,
        blocking_reason=_blocked_reason_text(task) if status == "blocked" else status,
        repair_action=repair_action,
        resume_command=_resume_command_for_task(matter_scope, task, status=status),
        evidence=dict(task),
    )


def _resume_command_for_task(matter_scope: str, task: Mapping[str, object], *, status: str) -> str:
    task_id = str(task["task_id"])
    if status == "pending":
        return f"python -m atticus.cli inspect --db DB --type task --id {task_id}"
    if status == "blocked":
        return f"python -m atticus.cli orchestrator repair --db DB --matter {matter_scope} --failure-event-id EVENT_ID"
    if status == "failed":
        return f"python -m atticus.cli orchestrator worker-failed --db DB --matter {matter_scope} --task-id {task_id} --reason \"inspect failed task\" --write"
    return f"python -m atticus.cli inspect --db DB --type task --id {task_id}"


def _choose_reducer_pending_task(tasks: list[dict[str, object]]) -> dict[str, object]:
    def priority(task: Mapping[str, object]) -> tuple[int, str]:
        task_type = str(task["task_type"])
        if task_type in {"citation_repair", "citation_audit"}:
            return (0, str(task["created_at"]))
        if str(task["stage"]) in {"S9", "S8", "S7", "S6"}:
            return (1, str(task["created_at"]))
        return (2, str(task["created_at"]))

    return sorted(tasks, key=priority)[0]


def _choose_runnable_task(tasks: list[dict[str, object]]) -> dict[str, object]:
    def priority(task: Mapping[str, object]) -> tuple[int, str]:
        task_type = str(task["task_type"])
        title = str(task.get("title") or "").lower()
        task_id = str(task["task_id"]).lower()
        urgent_terms = ("urgent", "hardship", "notice-to-quit", "notice to quit", "ntq", "pause")
        if any(term in title or term in task_id for term in urgent_terms):
            return (0, str(task["created_at"]))
        if task_type in {"citation_repair", "citation_audit"}:
            return (1, str(task["created_at"]))
        if str(task["stage"]) in {"S9", "S8", "S7", "S6"}:
            return (2, str(task["created_at"]))
        return (3, str(task["created_at"]))

    return sorted(tasks, key=priority)[0]


def _runnable_or_requeueable_tasks(conn: sqlite3.Connection, matter_scope: str) -> list[dict[str, object]]:
    tasks = _tasks_by_status(conn, matter_scope, ("queued", "ready"))
    blocked = _tasks_by_status(conn, matter_scope, ("blocked",))
    for task in blocked:
        if not blocked_task_auto_requeue_allowed(task):
            continue
        gate = evaluate_task_gates(conn, task)
        if gate.allowed:
            tasks.append(task)
    return tasks


def _latest_candidate_for_task(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        """
        SELECT candidate_id
        FROM candidate_outputs
        WHERE task_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return str(row["candidate_id"]) if row is not None else ""


def _owner_for_blocked_task(task: Mapping[str, object]) -> str:
    reason = _blocked_reason_text(task).lower()
    if "provider" in reason or "openrouter" in reason or "api_key" in reason:
        return "provider"
    if "human" in reason or "operator" in reason or "user intervention" in reason:
        return "operator"
    if "reducer" in reason or "candidate" in reason:
        return "reducer"
    return "orchestrator"


def _repair_action_for_blocked_task(task: Mapping[str, object]) -> str:
    reason = _blocked_reason_text(task).lower()
    if "missing certification" in reason:
        return "create_or_run_certification_producing_task"
    if "incomplete task dependency" in reason:
        return "complete_dependency_or_reduce_pending_candidate"
    if "provider" in reason or "openrouter" in reason:
        return "provider_control_plane_attention"
    if "token" in reason or "context" in reason:
        return "decompose_or_compact_context"
    return "orchestrator_repair_plan"


def _blocked_reason_text(task: Mapping[str, object]) -> str:
    raw = task.get("blocked_reasons_json", "[]")
    try:
        value = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return str(raw)
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return str(value)


def _stage_for_certification(certification: str) -> str:
    return {
        "source_inventory": "S0",
        "extraction_coverage": "S1",
        "evidence_registry": "S2",
        "production_mapping": "S3",
        "chronology_citations": "S4",
        "issue_route_map": "S5",
        "authority_map": "S6",
        "hostile_review": "S7",
        "citation_audit": "S7",
        "draft_preparation": "S8",
        "privacy_redaction_audit": "S9",
        "final_quality_gate": "S9",
    }.get(certification, "")


def _after_reducer_review(report: MatterCompletionReport) -> str:
    missing = set(report.missing_certifications)
    if "citation_audit" in missing:
        return "run citation audit, then final quality gate"
    if "final_quality_gate" in missing:
        return "run final quality gate"
    return "rerun matter-health"


def _after_missing_certification(certification: str) -> str:
    if certification == "citation_audit":
        return "run citation audit, then final quality gate"
    if certification == "final_quality_gate":
        return "run final quality gate after citation/privacy/hostile review gates"
    return "rerun matter-health after certification-producing work completes"


def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _reducer_review_id_for_next_action(conn: sqlite3.Connection, matter_scope: str, next_action: Mapping[str, object]) -> str:
    candidate_id = str(next_action.get("candidate_id") or "")
    if candidate_id:
        row = conn.execute(
            """
            SELECT reducer_review_id
            FROM reducer_review_queue
            WHERE matter_scope = ? AND candidate_id = ? AND status = 'open'
            LIMIT 1
            """,
            (matter_scope, candidate_id),
        ).fetchone()
        if row is not None:
            return str(row["reducer_review_id"])
    if str(next_action.get("type") or "") == "manual_reducer_review":
        row = conn.execute(
            """
            SELECT reducer_review_id
            FROM reducer_review_queue
            WHERE matter_scope = ? AND status = 'open'
            ORDER BY priority ASC, updated_at ASC
            LIMIT 1
            """,
            (matter_scope,),
        ).fetchone()
        return str(row["reducer_review_id"]) if row is not None else ""
    return ""


def _runnable_task_id_for_next_action(conn: sqlite3.Connection, matter_scope: str, next_action: Mapping[str, object]) -> str:
    if str(next_action.get("type") or "") != "supervisor_tick":
        return ""
    row = conn.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE matter_scope = ? AND status IN ('queued', 'ready')
        ORDER BY expected_value DESC, created_at ASC
        LIMIT 1
        """,
        (matter_scope,),
    ).fetchone()
    return str(row["task_id"]) if row is not None else ""


def _repair_plan_id_for_next_action(conn: sqlite3.Connection, matter_scope: str, next_action: Mapping[str, object]) -> str:
    action_type = str(next_action.get("type") or "")
    target_type = ""
    target_id = ""
    if action_type in {"blocked_task", "failed_task"}:
        target_type = "task"
        target_id = str(next_action.get("task_id") or "")
    elif action_type == "missing_certification":
        target_type = "matter"
        target_id = matter_scope
    if not target_type or not target_id:
        return ""
    row = conn.execute(
        """
        SELECT repair_plan_id
        FROM repair_plans
        WHERE matter_scope = ?
          AND target_type = ?
          AND target_id = ?
          AND status IN ('proposed', 'blocked', 'requires_human', 'applied')
        ORDER BY CASE status WHEN 'requires_human' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END, updated_at DESC
        LIMIT 1
        """,
        (matter_scope, target_type, target_id),
    ).fetchone()
    return str(row["repair_plan_id"]) if row is not None else ""
