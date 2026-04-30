"""Deterministic repair plans for blocked Atticus work."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import sqlite3
from uuid import uuid4

from atticus.core.events import utc_now


PROVIDER_BLOCKER_TERMS = (
    "openrouter",
    "provider",
    "api key",
    "api_key",
    "unauthorized",
    "forbidden",
    "insufficient credits",
    "http 401",
    "http 402",
    "http 403",
)


@dataclass(frozen=True)
class RepairPlan:
    repair_plan_id: str
    matter_scope: str
    target_type: str
    target_id: str
    blocker_signature: str
    blocker_type: str
    severity: str
    status: str
    owner: str
    retry_after: str
    terminal_reason: str
    actions: tuple[dict[str, object], ...]
    max_attempts: int
    attempts_so_far: int
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def ensure_repair_plan_for_blocker(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    target_type: str,
    target_id: str,
    reason: str,
    max_attempts: int = 3,
) -> RepairPlan:
    """Create or return a deduped plan for a blocker reason."""

    clean_reason = " ".join(reason.strip().split()) or "unknown blocker"
    blocker_type = classify_blocker(clean_reason)
    signature = blocker_signature(blocker_type=blocker_type, reason=clean_reason)
    actions = actions_for_blocker(conn, matter_scope=matter_scope, target_type=target_type, target_id=target_id, reason=clean_reason, blocker_type=blocker_type)
    actions = _with_typed_action_fields(actions, target_type=target_type, target_id=target_id, blocker_type=blocker_type)
    status = "requires_human" if blocker_type in {"provider_control_plane", "external_or_human_action"} else "proposed"
    owner = _owner_for_actions(actions, fallback="operator" if status == "requires_human" else "orchestrator")
    terminal_reason = clean_reason if status == "requires_human" and blocker_type == "external_or_human_action" else ""
    severity = "blocker" if status == "requires_human" or blocker_type in {"missing_certification", "incomplete_dependency"} else "warning"
    existing = _plan_by_signature(
        conn,
        matter_scope=matter_scope,
        target_type=target_type,
        target_id=target_id,
        blocker_signature=signature,
    )
    if existing is not None:
        if existing.actions != actions or (status == "requires_human" and existing.status != "requires_human"):
            next_status = "requires_human" if status == "requires_human" else existing.status
            _ = conn.execute(
                """
                UPDATE repair_plans
                SET actions_json = ?, severity = ?, status = ?, owner = ?, terminal_reason = ?, updated_at = ?
                WHERE repair_plan_id = ?
                """,
                (_json(list(actions)), severity, next_status, owner, terminal_reason, utc_now(), existing.repair_plan_id),
            )
            updated = get_repair_plan(conn, existing.repair_plan_id)
            if updated is None:
                raise RuntimeError("repair plan vanished during action refresh")
            return updated
        return existing

    now = utc_now()
    repair_plan_id = repair_plan_id_for(
        matter_scope=matter_scope,
        target_type=target_type,
        target_id=target_id,
        blocker_signature=signature,
    )
    _ = conn.execute(
        """
        INSERT INTO repair_plans(repair_plan_id, matter_scope, target_type, target_id,
          blocker_signature, blocker_type, severity, status, actions_json,
          owner, retry_after, terminal_reason, attempts_so_far, max_attempts, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?, ?, ?)
        """,
        (
            repair_plan_id,
            matter_scope,
            target_type,
            target_id,
            signature,
            blocker_type,
            severity,
            status,
            _json(list(actions)),
            owner,
            terminal_reason,
            max_attempts,
            now,
            now,
        ),
    )
    _emit_repair_event(
        conn,
        matter_scope=matter_scope,
        event_type="repair.plan_created",
        payload={
            "repair_plan_id": repair_plan_id,
            "target_type": target_type,
            "target_id": target_id,
            "blocker_signature": signature,
            "blocker_type": blocker_type,
            "status": status,
            "actions": list(actions),
        },
    )
    if status == "requires_human" and target_type != "human_attention":
        _record_attention_once(
            conn,
            matter_scope=matter_scope,
            target_type=target_type,
            target_id=target_id,
            severity="blocker",
            reason=f"repair requires human: {clean_reason}",
        )
    created = _plan_by_signature(
        conn,
        matter_scope=matter_scope,
        target_type=target_type,
        target_id=target_id,
        blocker_signature=signature,
    )
    if created is None:
        raise RuntimeError("repair plan insert did not produce a readable row")
    return created


def record_repair_attempt(
    conn: sqlite3.Connection,
    *,
    repair_plan_id: str,
    action_type: str,
    status: str,
    result: Mapping[str, object] | None = None,
) -> RepairPlan:
    row = conn.execute("SELECT * FROM repair_plans WHERE repair_plan_id = ?", (repair_plan_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown repair plan: {repair_plan_id}")
    matter_scope = str(row["matter_scope"])
    attempt_id = f"repair-attempt-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO repair_attempts(repair_attempt_id, repair_plan_id, matter_scope, action_type, status, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (attempt_id, repair_plan_id, matter_scope, action_type, status, _json(dict(result or {})), utc_now()),
    )
    attempts = int(row["attempts_so_far"]) + 1
    max_attempts = int(row["max_attempts"])
    next_status = "applied" if status == "succeeded" else "requires_human" if attempts >= max_attempts else "proposed"
    _ = conn.execute(
        """
        UPDATE repair_plans
        SET attempts_so_far = ?, status = ?, updated_at = ?
        WHERE repair_plan_id = ?
        """,
        (attempts, next_status, utc_now(), repair_plan_id),
    )
    if next_status == "requires_human":
        _ = conn.execute(
            "UPDATE repair_plans SET owner = ?, terminal_reason = CASE WHEN terminal_reason = '' THEN ? ELSE terminal_reason END WHERE repair_plan_id = ?",
            ("operator", f"attempt budget exhausted after {attempts} attempts", repair_plan_id),
        )
        _record_attention_once(
            conn,
            matter_scope=matter_scope,
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            severity="blocker",
            reason=f"repair plan attempt limit reached: {repair_plan_id}",
        )
    _emit_repair_event(
        conn,
        matter_scope=matter_scope,
        event_type="repair.attempt_recorded",
        payload={
            "repair_plan_id": repair_plan_id,
            "repair_attempt_id": attempt_id,
            "action_type": action_type,
            "status": status,
            "attempts_so_far": attempts,
            "max_attempts": max_attempts,
            "plan_status": next_status,
        },
    )
    updated = get_repair_plan(conn, repair_plan_id)
    if updated is None:
        raise RuntimeError("repair plan vanished after attempt")
    return updated


def list_repair_plans(conn: sqlite3.Connection, *, matter_scope: str, status: str | None = None) -> tuple[RepairPlan, ...]:
    sql = "SELECT * FROM repair_plans WHERE matter_scope = ?"
    params: list[object] = [matter_scope]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY CASE severity WHEN 'blocker' THEN 0 ELSE 1 END, updated_at DESC"
    return tuple(_row_to_plan(row) for row in conn.execute(sql, tuple(params)).fetchall())


def get_repair_plan(conn: sqlite3.Connection, repair_plan_id: str) -> RepairPlan | None:
    row = conn.execute("SELECT * FROM repair_plans WHERE repair_plan_id = ?", (repair_plan_id,)).fetchone()
    return _row_to_plan(row) if row is not None else None


def next_repair_plan(conn: sqlite3.Connection, *, matter_scope: str) -> RepairPlan | None:
    row = conn.execute(
        """
        SELECT *
        FROM repair_plans
        WHERE matter_scope = ? AND status IN ('proposed', 'blocked', 'requires_human')
        ORDER BY
          CASE status WHEN 'requires_human' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,
          CASE severity WHEN 'blocker' THEN 0 ELSE 1 END,
          updated_at DESC
        LIMIT 1
        """,
        (matter_scope,),
    ).fetchone()
    return _row_to_plan(row) if row is not None else None


def ensure_repair_plans_for_matter(conn: sqlite3.Connection, *, matter_scope: str) -> tuple[RepairPlan, ...]:
    """Ensure every currently visible completion blocker has a repair plan."""

    from atticus.status.completion import build_matter_completion_report

    report = build_matter_completion_report(conn, matter_scope)
    plans: list[RepairPlan] = []
    for requirement in report.requirements:
        if requirement.status == "satisfied":
            continue
        target_type = requirement.requirement_type
        target_id = requirement.name
        reason = requirement.blocking_reason or requirement.repair_action or requirement.status
        if requirement.requirement_type == "certification":
            target_type = "matter"
            target_id = matter_scope
            reason = f"missing certification: matter:{matter_scope}:{requirement.name}"
        elif requirement.requirement_type == "task":
            target_type = "task"
            target_id = str(requirement.evidence.get("task_id") or requirement.name)
            if requirement.status == "pending" and requirement.repair_action == "manual_reducer_review":
                reason = f"incomplete task dependency: {target_id}"
            elif requirement.status == "failed":
                reason = f"failed task: {target_id}"
        elif requirement.requirement_type == "artifact":
            target_type = "artifact"
            target_id = requirement.name
            reason = f"stale artifact dependency: {target_id}"
        elif requirement.requirement_type == "human_review":
            target_type = "human_attention"
            target_id = str(requirement.evidence.get("attention_id") or requirement.name)
        plans.append(
            ensure_repair_plan_for_blocker(
                conn,
                matter_scope=matter_scope,
                target_type=target_type,
                target_id=target_id,
                reason=reason,
            )
        )
    return tuple(plans)


def classify_blocker(reason: str) -> str:
    lowered = reason.lower()
    if "missing certification:" in lowered:
        return "missing_certification"
    if "incomplete task dependency:" in lowered:
        return "incomplete_dependency"
    if "local_stub capability block" in lowered or "local/no-live runtime cannot produce reducer-grade" in lowered:
        return "local_runtime_capability"
    if any(term in lowered for term in PROVIDER_BLOCKER_TERMS):
        return "provider_control_plane"
    if "worker output quarantined" in lowered or "quarantined" in lowered:
        return "worker_contract"
    if "token" in lowered or "context" in lowered or "exceeds" in lowered:
        return "context_budget"
    if "cost limit" in lowered or "budget" in lowered:
        return "cost_or_budget"
    if "external/human-only action" in lowered or "external legal action" in lowered:
        return "external_or_human_action"
    if "stale source" in lowered or "stale artifact" in lowered:
        return "stale_dependency"
    return "generic_blocker"


def blocker_signature(*, blocker_type: str, reason: str) -> str:
    normalized = " ".join(reason.casefold().split())
    return hashlib.sha256(f"{blocker_type}:{normalized}".encode("utf-8")).hexdigest()


def repair_plan_id_for(*, matter_scope: str, target_type: str, target_id: str, blocker_signature: str) -> str:
    raw = f"{matter_scope}:{target_type}:{target_id}:{blocker_signature}"
    return f"repair-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def actions_for_blocker(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    target_type: str,
    target_id: str,
    reason: str,
    blocker_type: str,
) -> tuple[dict[str, object], ...]:
    if blocker_type == "missing_certification":
        certification = reason.rsplit(":", 1)[-1].strip()
        if certification == "citation_audit":
            return (
                {
                    "action_type": "create_or_run_citation_audit",
                    "owner": "orchestrator",
                    "repairability": "auto",
                    "target_type": target_type,
                    "target_id": target_id,
                    "certification_type": certification,
                    "blocker_kind": blocker_type,
                    "resume_command": f"python -m atticus.cli repair-tick --db DB --matter {matter_scope} --write --json",
                },
                {
                    "action_type": "then_run_final_quality_gate",
                    "owner": "orchestrator",
                    "repairability": "auto",
                    "target_type": target_type,
                    "target_id": target_id,
                    "certification_type": "final_quality_gate",
                    "blocker_kind": blocker_type,
                    "resume_command": f"python -m atticus.cli final-gate readiness --db DB --matter {matter_scope} --json",
                },
            )
        return (
            {
                "action_type": "create_certification_work",
                "certification_type": certification,
                "owner": "orchestrator",
                "repairability": "auto" if certification != "final_quality_gate" else "scheduler",
                "target_type": target_type,
                "target_id": target_id,
                "blocker_kind": blocker_type,
                "resume_command": f"python -m atticus.cli repair-tick --db DB --matter {matter_scope} --write --json",
            },
        )
    if blocker_type == "incomplete_dependency":
        dependency_id = reason.rsplit(":", 1)[-1].strip()
        dependency = conn.execute("SELECT task_id, status, stage, task_type FROM tasks WHERE task_id = ?", (dependency_id,)).fetchone()
        if dependency is not None and str(dependency["status"]) == "reducer_pending":
            return (
                {
                    "action_type": "manual_reducer_review",
                    "owner": "reducer",
                    "repairability": "reducer",
                    "target_type": target_type,
                    "target_id": target_id,
                    "dependency_task_id": dependency_id,
                    "blocker_kind": blocker_type,
                    "resume_command": f"python -m atticus.cli inspect --db DB --type task --id {dependency_id}",
                },
            )
        if dependency is not None and str(dependency["status"]) in {"queued", "ready", "blocked"}:
            return (
                {
                    "action_type": "run_or_repair_dependency",
                    "owner": "scheduler",
                    "repairability": "scheduler",
                    "target_type": target_type,
                    "target_id": target_id,
                    "dependency_task_id": dependency_id,
                    "blocker_kind": blocker_type,
                    "dependency_status": str(dependency["status"]),
                    "resume_command": f"python -m atticus.cli run-free-loop --db DB --matter {matter_scope} --capacity 15 --max-ticks 1",
                },
            )
        return (
            {
                "action_type": "create_dependency_repair_task",
                "owner": "orchestrator",
                "dependency_task_id": dependency_id,
                "resume_command": f"python -m atticus.cli orchestrator tick --db DB --matter {matter_scope} --write",
            },
        )
    if blocker_type == "provider_control_plane":
        return (
            {
                "action_type": "provider_control_plane_attention",
                "owner": "provider",
                "retry_worker": False,
                "resume_command": f"python -m atticus.cli provider-probe --db DB --matter {matter_scope}",
            },
        )
    if blocker_type == "worker_contract":
        return (
            {
                "action_type": "repair_worker_contract_or_prompt",
                "owner": "orchestrator",
                "resume_command": f"python -m atticus.cli inspect --db DB --type {target_type} --id {target_id}",
            },
        )
    if blocker_type == "local_runtime_capability":
        return (
            {
                "action_type": "use_deterministic_source_led_generator_or_import_packet",
                "owner": "orchestrator",
                "retry_worker": False,
                "resume_command": f"python -m atticus.cli import-candidates --help",
            },
            {
                "action_type": "use_provider_backed_worker_if_authorized",
                "owner": "provider",
                "retry_worker": False,
                "resume_command": f"python -m atticus.cli provider-health --db DB --matter {matter_scope} --group-by-policy --json",
            },
        )
    if blocker_type == "context_budget":
        return (
            {
                "action_type": "decompose_or_compact_context",
                "owner": "orchestrator",
                "resume_command": f"python -m atticus.cli schedule --db DB --matter {matter_scope} --dry-run",
            },
        )
    if blocker_type == "cost_or_budget":
        return (
            {
                "action_type": "normalize_cost_limit_or_request_budget",
                "owner": "operator",
                "resume_command": f"python -m atticus.cli budget --db DB --scope-type matter --scope-id {matter_scope} check",
            },
        )
    if blocker_type == "external_or_human_action":
        return (
            {
                "action_type": "operator_review_external_or_human_only_blocker",
                "owner": "operator",
                "retry_worker": False,
                "resume_command": f"python -m atticus.cli human-attention --db DB --matter {matter_scope}",
            },
        )
    if blocker_type == "stale_dependency":
        return (
            {
                "action_type": "refresh_stale_dependency",
                "owner": "orchestrator",
                "resume_command": f"python -m atticus.cli extract-sources --db DB --matter {matter_scope} --write",
            },
        )
    return (
        {
            "action_type": "orchestrator_repair_plan",
            "owner": "orchestrator",
            "resume_command": f"python -m atticus.cli orchestrator tick --db DB --matter {matter_scope} --write",
        },
    )


def _plan_by_signature(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    target_type: str,
    target_id: str,
    blocker_signature: str,
) -> RepairPlan | None:
    row = conn.execute(
        """
        SELECT *
        FROM repair_plans
        WHERE matter_scope = ? AND target_type = ? AND target_id = ? AND blocker_signature = ?
        LIMIT 1
        """,
        (matter_scope, target_type, target_id, blocker_signature),
    ).fetchone()
    return _row_to_plan(row) if row is not None else None


def _row_to_plan(row: sqlite3.Row) -> RepairPlan:
    actions = json.loads(str(row["actions_json"] or "[]"))
    if not isinstance(actions, list):
        actions = []
    normalized_actions = tuple(dict(item) for item in actions if isinstance(item, Mapping))
    return RepairPlan(
        repair_plan_id=str(row["repair_plan_id"]),
        matter_scope=str(row["matter_scope"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        blocker_signature=str(row["blocker_signature"]),
        blocker_type=str(row["blocker_type"]),
        severity=str(row["severity"]),
        status=str(row["status"]),
        owner=str(row["owner"]) if "owner" in row.keys() else "orchestrator",
        retry_after=str(row["retry_after"] or "") if "retry_after" in row.keys() else "",
        terminal_reason=str(row["terminal_reason"] or "") if "terminal_reason" in row.keys() else "",
        actions=normalized_actions,
        max_attempts=int(row["max_attempts"]),
        attempts_so_far=int(row["attempts_so_far"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _owner_for_actions(actions: tuple[dict[str, object], ...], *, fallback: str) -> str:
    for action in actions:
        owner = str(action.get("owner") or "").strip()
        if owner:
            return owner
    return fallback


def _emit_repair_event(conn: sqlite3.Connection, *, matter_scope: str, event_type: str, payload: Mapping[str, object]) -> None:
    from atticus.db import repo

    _ = repo.emit_event(conn, event_type, matter_scope=matter_scope, payload=dict(payload))


def _record_attention_once(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    target_type: str,
    target_id: str,
    severity: str,
    reason: str,
) -> None:
    from atticus.db import repo

    _ = repo.record_human_attention_once(
        conn,
        matter_scope=matter_scope,
        target_type=target_type,
        target_id=target_id,
        severity=severity,
        reason=reason,
    )


def _with_typed_action_fields(
    actions: tuple[dict[str, object], ...],
    *,
    target_type: str,
    target_id: str,
    blocker_type: str,
) -> tuple[dict[str, object], ...]:
    owner_repairability = {
        "orchestrator": "auto",
        "scheduler": "scheduler",
        "reducer": "reducer",
        "provider": "provider",
        "operator": "operator",
    }
    enriched: list[dict[str, object]] = []
    for action in actions:
        item = dict(action)
        owner = str(item.get("owner") or "")
        item.setdefault("repairability", owner_repairability.get(owner, "terminal"))
        item.setdefault("target_type", target_type)
        item.setdefault("target_id", target_id)
        item.setdefault("blocker_kind", blocker_type)
        if blocker_type == "provider_control_plane":
            item["repairability"] = "provider"
        if blocker_type == "external_or_human_action":
            item["repairability"] = "terminal"
        enriched.append(item)
    return tuple(enriched)
