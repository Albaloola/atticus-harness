"""Executable repair continuation plane for deterministic Atticus blockers.

This module turns already-classified completion blockers and repair plans into
bounded internal transitions.  It deliberately does *not* reduce legal proof
standards, perform provider retries, or execute external/legal actions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import logging
import re
import sqlite3

logger = logging.getLogger(__name__)

from atticus.agents.repair_planner import (
    RepairPlan,
    ensure_repair_plan_for_blocker,
    ensure_repair_plans_for_matter,
    get_repair_plan,
    next_repair_plan,
    record_repair_attempt,
)
from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.db import repo
from atticus.reducer.review_queue import enqueue_open_reducer_reviews_for_matter, next_reducer_review
from atticus.status.completion import build_matter_completion_report, next_resume_action
from atticus.workflows.final_gate import create_missing_final_gate_work, final_gate_readiness


@dataclass(frozen=True)
class RepairExecutionResult:
    attempted: tuple[dict[str, object], ...]
    applied: tuple[dict[str, object], ...]
    skipped: tuple[dict[str, object], ...]
    terminal: tuple[dict[str, object], ...]
    created_task_ids: tuple[str, ...]
    unblocked_task_ids: tuple[str, ...]
    reducer_review_ids: tuple[str, ...]
    attention_ids: tuple[str, ...]
    made_progress: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def execute_repair_tick(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    max_repairs: int = 10,
    write: bool = True,
) -> RepairExecutionResult:
    """Execute one bounded repair pass for a matter.

    The pass consumes completion blockers and repair plans, queues reducer review
    for S6-S9 reducer-pending work, creates missing certification-producing
    tasks, unblocks tasks whose dependencies are now complete, and records
    terminal provider/operator lanes. Dry-run reports the same decisions without
    mutating the ledger.
    """

    attempted: list[dict[str, object]] = []
    applied: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    terminal: list[dict[str, object]] = []
    created_task_ids: list[str] = []
    unblocked_task_ids: list[str] = []
    reducer_review_ids: list[str] = []
    attention_ids: list[str] = []

    if write:
        plans = list(ensure_repair_plans_for_matter(conn, matter_scope=matter_scope))
        reviews = enqueue_open_reducer_reviews_for_matter(conn, matter_scope=matter_scope)
    else:
        plans = []
        reviews = tuple()
    for review in reviews:
        reducer_review_ids.append(review.reducer_review_id)
        applied.append({"action_type": "enqueue_reducer_review", "owner": "reducer", "candidate_id": review.candidate_id, "task_id": review.task_id, "reducer_review_id": review.reducer_review_id})

    # Dry-runs still need the current plans to describe what would happen.
    if not plans:
        plans = list(ensure_repair_plans_for_matter(conn, matter_scope=matter_scope)) if write else _derived_dry_run_plans(conn, matter_scope)

    budget = max(0, max_repairs)
    for plan in plans:
        if budget <= 0:
            break
        if plan.status not in {"proposed", "blocked", "requires_human"}:
            continue
        action = _first_action(plan)
        if action is None:
            continue
        action_type = str(action.get("action_type") or "")
        attempt = {"repair_plan_id": plan.repair_plan_id, "blocker_type": plan.blocker_type, "target_type": plan.target_type, "target_id": plan.target_id, **action}
        attempted.append(attempt)
        outcome = _execute_plan_action(conn, matter_scope=matter_scope, plan=plan, action=action, write=write)
        bucket = str(outcome.get("bucket") or "skipped")
        if bucket == "applied":
            applied.append(outcome)
            created_task_ids.extend(_string_tuple(outcome.get("created_task_ids")))
            unblocked_task_ids.extend(_string_tuple(outcome.get("unblocked_task_ids")))
            reducer_review_ids.extend(_string_tuple(outcome.get("reducer_review_ids")))
            if write:
                record_repair_attempt(conn, repair_plan_id=plan.repair_plan_id, action_type=action_type, status="succeeded", result=outcome)
        elif bucket == "terminal":
            terminal.append(outcome)
            attention_ids.extend(_string_tuple(outcome.get("attention_ids")))
            if write and plan.status != "requires_human":
                record_repair_attempt(conn, repair_plan_id=plan.repair_plan_id, action_type=action_type, status="failed", result=outcome)
        else:
            skipped.append(outcome)
        budget -= 1

    # Some blocked task dependency repairs are easiest and safest from task rows;
    # handle them even if the text-only repair plan missed the dependency id.
    if budget > 0:
        for outcome in _repair_blocked_dependencies(conn, matter_scope=matter_scope, write=write):
            attempted.append({"action_type": "repair_blocked_dependency", **outcome})
            bucket = str(outcome.get("bucket") or "skipped")
            if bucket == "applied":
                applied.append(outcome)
                unblocked_task_ids.extend(_string_tuple(outcome.get("unblocked_task_ids")))
                reducer_review_ids.extend(_string_tuple(outcome.get("reducer_review_ids")))
            elif bucket == "terminal":
                terminal.append(outcome)
                attention_ids.extend(_string_tuple(outcome.get("attention_ids")))
            else:
                skipped.append(outcome)
            budget -= 1
            if budget <= 0:
                break

    made_progress = bool(applied or created_task_ids or unblocked_task_ids or reducer_review_ids)
    result = RepairExecutionResult(
        attempted=tuple(_dedupe_dicts(attempted)),
        applied=tuple(_dedupe_dicts(applied)),
        skipped=tuple(_dedupe_dicts(skipped)),
        terminal=tuple(_dedupe_dicts(terminal)),
        created_task_ids=tuple(dict.fromkeys(created_task_ids)),
        unblocked_task_ids=tuple(dict.fromkeys(unblocked_task_ids)),
        reducer_review_ids=tuple(dict.fromkeys(reducer_review_ids)),
        attention_ids=tuple(dict.fromkeys(attention_ids)),
        made_progress=made_progress,
    )
    if write:
        _ = repo.emit_event(conn, "repair.tick", matter_scope=matter_scope, payload=result.as_dict())
    return result


def execute_repair_plan(
    conn: sqlite3.Connection,
    *,
    repair_plan_id: str,
    max_actions: int = 1,
    dry_run: bool = False,
) -> dict[str, object]:
    """Compatibility wrapper for the older ``repairs apply`` CLI."""

    plan = get_repair_plan(conn, repair_plan_id)
    if plan is None:
        raise ValueError(f"unknown repair plan: {repair_plan_id}")
    attempted: list[dict[str, object]] = []
    applied: list[dict[str, object]] = []
    blocked: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    for action in plan.actions[: max(0, max_actions)]:
        attempted.append(dict(action))
        outcome = _execute_plan_action(conn, matter_scope=plan.matter_scope, plan=plan, action=action, write=not dry_run)
        if outcome.get("bucket") == "applied":
            applied.append(outcome)
            if not dry_run:
                record_repair_attempt(conn, repair_plan_id=repair_plan_id, action_type=str(action.get("action_type") or ""), status="succeeded", result=outcome)
        elif outcome.get("bucket") == "terminal":
            blocked.append(outcome)
        else:
            failed.append(outcome)
    return {"repair_plan_id": repair_plan_id, "matter_scope": plan.matter_scope, "attempted": attempted, "applied": applied, "blocked": blocked, "failed": failed}


def execute_next_repair_plan(conn: sqlite3.Connection, *, matter_scope: str, dry_run: bool = False) -> dict[str, object] | None:
    plan = next_repair_plan(conn, matter_scope=matter_scope)
    if plan is None:
        return None
    return execute_repair_plan(conn, repair_plan_id=plan.repair_plan_id, max_actions=1, dry_run=dry_run)


def repair_tick_payload(conn: sqlite3.Connection, *, matter_scope: str, max_repairs: int = 10, write: bool = False) -> dict[str, object]:
    result = execute_repair_tick(conn, matter_scope=matter_scope, max_repairs=max_repairs, write=write)
    next_action = next_resume_action(conn, matter_scope)
    return {
        **result.as_dict(),
        "next_action": next_action,
        "can_continue_with_run_free_loop": bool(result.made_progress or str(next_action.get("owner") or "") == "scheduler" or str(next_action.get("type") or "") == "supervisor_tick"),
        "write": write,
    }


def _execute_plan_action(conn: sqlite3.Connection, *, matter_scope: str, plan: RepairPlan, action: Mapping[str, object], write: bool) -> dict[str, object]:
    action_type = str(action.get("action_type") or "")
    owner = str(action.get("owner") or plan.owner or "")
    base = {"repair_plan_id": plan.repair_plan_id, "action_type": action_type, "owner": owner, "target_type": plan.target_type, "target_id": plan.target_id}

    if owner in {"provider", "operator"} or action_type in {"provider_control_plane_attention", "operator_review_external_or_human_only_blocker"}:
        attention_id = _record_terminal_attention(conn, matter_scope=matter_scope, plan=plan, action=action, write=write)
        return {**base, "bucket": "terminal", "reason": plan.terminal_reason or plan.blocker_type, "attention_ids": ([str(attention_id)] if attention_id else [])}

    if action_type in {"create_or_run_citation_audit", "create_certification_work", "then_run_final_quality_gate"}:
        certification = str(action.get("certification_type") or _certification_from_plan(plan) or "")
        if certification == "final_quality_gate":
            readiness = final_gate_readiness(conn, matter_scope)
            if not bool(readiness.get("can_create_final_gate")):
                return {**base, "bucket": "skipped", "reason": "final_quality_gate prerequisites are not complete", "readiness_state": readiness.get("state")}
        if not write:
            return {**base, "bucket": "applied", "dry_run": True, "would_create_certification_work": certification}
        created = create_missing_final_gate_work(conn, matter_scope)
        task_id = str(created.get("task_id") or "")
        if created.get("created") is True:
            return {**base, "bucket": "applied", "created_task_ids": [task_id], "certification": created.get("certification"), "result": created}
        if task_id:
            return {**base, "bucket": "skipped", "reason": created.get("reason") or "certification task already exists", "existing_task_id": task_id, "certification": created.get("certification")}
        return {**base, "bucket": "skipped", "reason": created.get("reason") or "certification work not created", "result": created}

    if action_type in {"manual_reducer_review", "run_or_repair_dependency", "create_dependency_repair_task"}:
        dependency_task_id = str(action.get("dependency_task_id") or _dependency_from_plan(plan) or plan.target_id)
        return _repair_dependency(conn, matter_scope=matter_scope, blocked_task_id=plan.target_id if plan.target_type == "task" else "", dependency_task_id=dependency_task_id, base=base, write=write)

    if action_type == "use_deterministic_source_led_generator_or_import_packet":
        if plan.target_type == "human_attention":
            return {**base, "bucket": "skipped", "reason": "existing human_attention blocks automatic source-led generation"}
        if plan.target_type != "task":
            if not write:
                return {**base, "bucket": "applied", "dry_run": True, "would_create_repair_task": action_type}
            task_id = _ensure_internal_repair_task(conn, matter_scope=matter_scope, plan=plan, action_type=action_type)
            return {**base, "bucket": "applied", "created_task_ids": [task_id], "repair_task_id": task_id}
        if not write:
            return {**base, "bucket": "applied", "dry_run": True, "would_create_source_led_packet": True}
        try:
            from atticus.workflows.source_led_packet import create_source_led_candidate_for_task
            result = create_source_led_candidate_for_task(
                conn,
                matter_scope=matter_scope or plan.matter_scope or "unknown",
                task_id=plan.target_id,
                max_sources=12,
                source_ids=None,
                write=True,
            )
            unblocked_ids: list[str] = []
            if write and plan.blocker_type == "local_runtime_capability":
                try:
                    conn.execute(
                        "UPDATE tasks SET status='queued', blocked_reasons_json='[]' WHERE task_id=? AND status='blocked'",
                        (plan.target_id,),
                    )
                    unblocked_ids = [plan.target_id]
                except Exception as exc:
                    logger.warning("Failed to unblock task %s: %s", plan.target_id, exc)
            return {
                **base,
                "bucket": "applied",
                "created_candidate_id": result.candidate_id,
                "task_id": plan.target_id,
                "candidate_info": result.support_summary,
                "unblocked_task_ids": unblocked_ids,
            }
        except Exception as exc:
            return {**base, "bucket": "terminal", "reason": f"source-led packet generation failed: {exc}"}

    if action_type in {"decompose_or_compact_context", "repair_worker_contract_or_prompt", "orchestrator_repair_plan", "refresh_stale_dependency"}:
        if plan.target_type == "human_attention":
            return {**base, "bucket": "skipped", "reason": "existing human_attention remains operator/provider-owned; not creating recursive repair work"}
        if not write:
            return {**base, "bucket": "applied", "dry_run": True, "would_create_repair_task": action_type}
        task_id = _ensure_internal_repair_task(conn, matter_scope=matter_scope, plan=plan, action_type=action_type)
        return {**base, "bucket": "applied", "created_task_ids": [task_id], "repair_task_id": task_id}

    return {**base, "bucket": "skipped", "reason": f"unsupported or non-deterministic action type: {action_type}"}


def _repair_blocked_dependencies(conn: sqlite3.Connection, *, matter_scope: str, write: bool) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT task_id, blocked_reasons_json, task_dependencies_json
        FROM tasks
        WHERE matter_scope = ? AND status = 'blocked'
        ORDER BY updated_at ASC, task_id
        """,
        (matter_scope,),
    ).fetchall()
    outcomes: list[dict[str, object]] = []
    for row in rows:
        blocked_task_id = str(row["task_id"])
        deps = _dependency_ids_from_row(row)
        for dep in deps:
            outcomes.append(_repair_dependency(conn, matter_scope=matter_scope, blocked_task_id=blocked_task_id, dependency_task_id=dep, base={"action_type": "repair_blocked_dependency", "target_type": "task", "target_id": blocked_task_id}, write=write))
    return outcomes


def _repair_dependency(conn: sqlite3.Connection, *, matter_scope: str, blocked_task_id: str, dependency_task_id: str, base: Mapping[str, object], write: bool) -> dict[str, object]:
    dep = conn.execute("SELECT task_id, status FROM tasks WHERE task_id = ? AND matter_scope = ?", (dependency_task_id, matter_scope)).fetchone()
    if dep is None:
        attention_id = None
        if write and blocked_task_id:
            attention_id = repo.record_human_attention_once(conn, matter_scope=matter_scope, target_type="task", target_id=blocked_task_id, severity="blocker", reason=f"missing task dependency cannot be safely reconstructed: {dependency_task_id}")
        return {**base, "bucket": "terminal", "dependency_task_id": dependency_task_id, "reason": "missing_dependency", "attention_ids": ([str(attention_id)] if attention_id else [])}
    status = str(dep["status"])
    if status == str(TaskStatus.COMPLETE):
        if not blocked_task_id:
            return {**base, "bucket": "skipped", "dependency_task_id": dependency_task_id, "reason": "dependency complete; no blocked task target"}
        if write:
            repo.update_task_status(conn, blocked_task_id, str(TaskStatus.QUEUED), reason=f"dependency {dependency_task_id} complete")
            _ = conn.execute("UPDATE tasks SET blocked_reasons_json = '[]', updated_at = ? WHERE task_id = ?", (utc_now(), blocked_task_id))
        return {**base, "bucket": "applied", "dependency_task_id": dependency_task_id, "unblocked_task_ids": [blocked_task_id]}
    if status in {str(TaskStatus.QUEUED), str(TaskStatus.READY), str(TaskStatus.LEASED), str(TaskStatus.RUNNING)}:
        return {**base, "bucket": "skipped", "dependency_task_id": dependency_task_id, "reason": f"dependency is scheduler-owned: {status}"}
    if status == str(TaskStatus.REDUCER_PENDING):
        if not write:
            return {**base, "bucket": "applied", "dependency_task_id": dependency_task_id, "dry_run": True, "would_enqueue_reducer_review": True}
        before = next_reducer_review(conn, matter_scope=matter_scope)
        reviews = enqueue_open_reducer_reviews_for_matter(conn, matter_scope=matter_scope)
        ids = [review.reducer_review_id for review in reviews]
        if not ids and before is not None:
            ids = [before.reducer_review_id]
        return {**base, "bucket": "applied", "dependency_task_id": dependency_task_id, "reducer_review_ids": ids}
    if status == str(TaskStatus.FAILED):
        if write:
            plan = ensure_repair_plan_for_blocker(conn, matter_scope=matter_scope, target_type="task", target_id=dependency_task_id, reason=f"failed task: {dependency_task_id}")
            return {**base, "bucket": "applied", "dependency_task_id": dependency_task_id, "repair_plan_id": plan.repair_plan_id}
        return {**base, "bucket": "applied", "dependency_task_id": dependency_task_id, "dry_run": True, "would_create_repair_plan": True}
    return {**base, "bucket": "skipped", "dependency_task_id": dependency_task_id, "reason": f"dependency status is not auto-repairable: {status}"}


def _ensure_internal_repair_task(conn: sqlite3.Connection, *, matter_scope: str, plan: RepairPlan, action_type: str) -> str:
    task_id = _stable_task_id(matter_scope, action_type, plan.target_type, plan.target_id)
    row = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is not None:
        return task_id
    from atticus.core.policies import LegalStage
    from atticus.core.tasks import TaskSpec

    # Check if this is a citation-audit or final-gate task — these need strict citation rules
    is_citation_task = False
    original_stage = LegalStage.S0_SOURCE_INVENTORY
    if plan.target_type == "task":
        orig = conn.execute(
            "SELECT task_type, stage FROM tasks WHERE task_id = ?",
            (plan.target_id,),
        ).fetchone()
        if orig is not None:
            tt = str(orig["task_type"] or "")
            if tt in {"citation_audit", "citation_repair", "final_quality_gate", "hostile_review", "draft", "draft_preparation"}:
                is_citation_task = True
            try:
                original_stage = LegalStage(str(orig["stage"] or ""))
            except (ValueError, KeyError):
                original_stage = LegalStage.S0_SOURCE_INVENTORY

    base_instructions = (
        f"Perform bounded internal repair action {action_type} for {plan.target_type}:{plan.target_id}. "
        "Do not perform provider retries, external legal actions, or fake proof. Record a blocker if safe repair is impossible."
    )

    if is_citation_task:
        instructions = (
            f"REPAIR: {action_type} for citation/quality-gate task {plan.target_id}.\n\n"
            "Your predecessor produced quarantined output because they cited memory, validation results, "
            "derivative extraction artifacts, stale artifacts, rough drafts, or orientation-only material "
            "instead of proof-allowed sources.\n\n"
            "STRICT CITATION RULES — you MUST follow these:\n"
            "1. Cite EVERY factual, legal, procedural, contradiction, or risk finding to a proof-allowed target.\n"
            "2. PROOF-ALLOWED citation targets: sources (source_id), artifacts (artifact_id), authorities (authority_id), "
            "chronology events, and verified claims only.\n"
            "3. FORBIDDEN citation targets: memory (legal_memories), validation results, derivative extraction artifacts, "
            "stale artifacts, rough drafts, orientation-only material.\n"
            "4. If a finding cannot be cited to a proof-allowed target, mark it as 'uncertain' with the specific reason.\n"
            "5. Use the source_id as the citation target for extracted/OCR source material, not the extraction artifact_id.\n"
            "6. Do not invent evidence, authorities, quotations, dates, amounts, or legal conclusions.\n\n"
            f"{base_instructions}"
        )
    else:
        instructions = base_instructions

    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            title=f"Repair {plan.blocker_type} for {plan.target_type}:{plan.target_id}",
            task_type="internal_repair",
            stage=original_stage,
            matter_scope=matter_scope,
            status=TaskStatus.QUEUED,
            instructions=instructions,
            expected_value=0.2,
        ),
    )
    return task_id


def _record_terminal_attention(conn: sqlite3.Connection, *, matter_scope: str, plan: RepairPlan, action: Mapping[str, object], write: bool) -> int | None:
    if not write or plan.target_type == "human_attention":
        return None
    owner = str(action.get("owner") or plan.owner or "operator")
    reason = plan.terminal_reason or f"{owner}-owned terminal repair lane: {plan.blocker_type} for {plan.target_type}:{plan.target_id}"
    return repo.record_human_attention_once(conn, matter_scope=matter_scope, target_type=plan.target_type, target_id=plan.target_id, severity="blocker", reason=reason, owner="provider" if owner == "provider" else "operator")


def _derived_dry_run_plans(conn: sqlite3.Connection, matter_scope: str) -> list[RepairPlan]:
    # Dry-run CLI should be side-effect free.  Build synthetic plans from the
    # report by temporarily reusing existing planner semantics only if plans
    # already exist; otherwise report via attempted-less result.
    return []


def _first_action(plan: RepairPlan) -> Mapping[str, object] | None:
    return plan.actions[0] if plan.actions else None


def _certification_from_plan(plan: RepairPlan) -> str:
    for action in plan.actions:
        cert = str(action.get("certification_type") or "")
        if cert:
            return cert
    match = re.search(r"missing certification:\s*matter:[^:]+:([A-Za-z0-9_\-]+)", plan.terminal_reason or plan.blocker_signature)
    if match:
        return match.group(1)
    if plan.blocker_type == "missing_certification":
        # Repair planner target is matter for certifications; action metadata is
        # preferred, but legacy plans can fall back to completion report order.
        return ""
    return ""


def _dependency_from_plan(plan: RepairPlan) -> str:
    for action in plan.actions:
        dep = str(action.get("dependency_task_id") or "")
        if dep:
            return dep
    return plan.target_id if plan.target_type == "task" else ""


def _dependency_ids_from_row(row: sqlite3.Row) -> list[str]:
    deps: list[str] = []
    try:
        raw_deps = json.loads(str(row["task_dependencies_json"] or "[]"))
        if isinstance(raw_deps, list):
            deps.extend(str(item) for item in raw_deps if str(item))
    except json.JSONDecodeError:
        pass
    try:
        reasons = json.loads(str(row["blocked_reasons_json"] or "[]"))
    except json.JSONDecodeError:
        reasons = []
    if isinstance(reasons, list):
        for reason in reasons:
            text = str(reason)
            match = re.search(r"incomplete task dependency:\s*([A-Za-z0-9_.:-]+)", text)
            if match:
                deps.append(match.group(1).strip())
    return list(dict.fromkeys(deps))


def _stable_task_id(*parts: str) -> str:
    safe = "-".join(re.sub(r"[^A-Za-z0-9_.-]+", "-", part).strip("-").lower() for part in parts if part)
    return (safe or "repair-task")[:96]


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if str(item))
    if value:
        return (str(value),)
    return ()


def _dedupe_dicts(items: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    out: list[dict[str, object]] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
