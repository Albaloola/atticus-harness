"""Matter-scoped orchestrator state and repair proposals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import sqlite3
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.agents.decomposition import decomposition_repair_action
from atticus.db import repo
from atticus.providers.model_policy import default_smart_model_policy, load_model_routing_policy, smart_provider_policy_for_route
from atticus.scheduler.capacity import MAX_PARALLEL_AGENT_CAPACITY, agent_capacity
from atticus.scheduler.gates import evaluate_task_gates
from atticus.scheduler.lease import LeaseError, acquire_lease, expire_leases
from atticus.scheduler.supervisor_invariants import evaluate_no_silent_idle


OPERATOR_SIGNAL_TYPES = {"suggestion", "directive", "redirect", "attention"}
OPERATOR_SIGNAL_ALIASES = {"suggest": "suggestion", "direct": "directive", "note": "attention"}
OPERATOR_SIGNAL_PRIORITIES = {"low", "normal", "high", "blocker"}


def ensure_matter_orchestrator(conn: sqlite3.Connection, matter_scope: str) -> str:
    current = repo.get_matter_orchestrator(conn, matter_scope=matter_scope)
    if current is not None:
        return str(current["orchestrator_id"])
    return repo.upsert_matter_orchestrator(conn, matter_scope=matter_scope, status="idle")


def orchestrator_tick(conn: sqlite3.Connection, matter_scope: str, capacity: int, *, dry_run: bool = True) -> dict[str, object]:
    capacity_requested = max(0, capacity)
    capacity_effective = agent_capacity(capacity_requested)
    current = repo.get_matter_orchestrator(conn, matter_scope=matter_scope)
    orchestrator_id = str(current["orchestrator_id"]) if current is not None else ""
    if not dry_run and not orchestrator_id:
        orchestrator_id = ensure_matter_orchestrator(conn, matter_scope)
    if not dry_run:
        _ = expire_leases(conn)
    candidates = _runnable_matter_tasks(conn, matter_scope=matter_scope, capacity=capacity_effective)
    blocked_repairs: list[dict[str, object]] = []
    terminal_blocks: list[dict[str, object]] = []
    for task in _blocked_matter_tasks(conn, matter_scope=matter_scope, capacity=capacity_effective):
        task_id = str(task["task_id"])
        terminal_state = _terminal_repair_state(conn, task_id=task_id)
        if terminal_state is not None:
            terminal_blocks.append(
                {
                    "task_id": task_id,
                    "status": "user_intervention_required",
                    "reason": str(terminal_state.get("reason") or "orchestrator repair limit reached"),
                    "repair_attempt_limit": terminal_state.get("repair_attempt_limit"),
                    "signal_count": terminal_state.get("signal_count"),
                    "attention_id": terminal_state.get("attention_id") or "",
                }
            )
            continue
        blocked_reasons = _json_list(str(task["blocked_reasons_json"] or "[]"))
        blocked_repairs.append(
            {
                "task_id": task_id,
                "blocked_reasons": blocked_reasons,
                "proposed_actions": _repair_actions_for_blocked_task(
                    conn,
                    matter_scope=matter_scope,
                    task_id=task_id,
                    reasons=blocked_reasons,
                ),
            }
        )
    operator_signals = _pending_operator_signals(conn, matter_scope=matter_scope, capacity=max(1, capacity_effective))
    leased: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    routed_operator_signals: list[dict[str, object]] = []
    if not dry_run:
        conn.execute("SAVEPOINT orchestrator_tick")
        leased_lease_ids: list[str] = []
        try:
            for task in candidates:
                task_id = str(task["task_id"])
                try:
                    lease_id = acquire_lease(
                        conn,
                        task_id=task_id,
                        worker_id=f"orchestrator-{orchestrator_id}",
                        seconds=900,
                        dry_run=False,
                    )
                    leased_lease_ids.append(lease_id)
                except LeaseError as exc:
                    skipped.append({"task_id": task_id, "reason": str(exc)})
                    continue
                leased.append({"task_id": task_id, "lease_id": lease_id})
            _ = repo.record_orchestrator_event(
                conn,
                orchestrator_id=orchestrator_id,
                event_type="orchestrator.tick",
                payload={
                    "dry_run": False,
                    "capacity_requested": capacity_requested,
                    "capacity_effective": capacity_effective,
                    "capacity_limit": MAX_PARALLEL_AGENT_CAPACITY,
                    "leased": leased,
                    "skipped": skipped,
                    "blocked_repairs": blocked_repairs,
                    "terminal_blocks": terminal_blocks,
                    "operator_signals": operator_signals,
                },
            )
            for repair in blocked_repairs:
                _ = repo.record_orchestrator_repair_proposed(
                    conn,
                    orchestrator_id=orchestrator_id,
                    task_id=str(repair["task_id"]),
                    payload=repair,
                )
            for signal in operator_signals:
                route_payload = {
                    "operator_signal_event_id": str(signal["operator_signal_event_id"]),
                    "signal_type": str(signal["signal_type"]),
                    "priority": str(signal["priority"]),
                    "target_task_id": str(signal.get("target_task_id") or ""),
                    "message": str(signal["message"]),
                    "route": "master_orchestrator_to_matter_orchestrator",
                    "delivery_status": "routed_for_next_planning_tick",
                }
                routed_event_id = repo.record_orchestrator_event(
                    conn,
                    orchestrator_id=orchestrator_id,
                    event_type="orchestrator.operator_signal_routed",
                    payload=route_payload,
                )
                _ = repo.emit_event(
                    conn,
                    "master_orchestrator.operator_signal_routed",
                    matter_scope=matter_scope,
                    payload={**route_payload, "routed_event_id": routed_event_id},
                )
                routed_operator_signals.append({**route_payload, "routed_event_id": routed_event_id})
            conn.execute("RELEASE SAVEPOINT orchestrator_tick")
        except BaseException:
            for lid in leased_lease_ids:
                try:
                    conn.execute("UPDATE leases SET status='failed' WHERE lease_id = ? AND status = 'active'", (lid,))
                except Exception:
                    pass
            conn.execute("ROLLBACK TO SAVEPOINT orchestrator_tick")
            conn.execute("RELEASE SAVEPOINT orchestrator_tick")
            raise
    result: dict[str, object] = {
        "dry_run": dry_run,
        "matter_scope": matter_scope,
        "orchestrator_id": orchestrator_id,
        "would_create_orchestrator": dry_run and not orchestrator_id,
        "capacity": capacity_effective,
        "capacity_requested": capacity_requested,
        "capacity_effective": capacity_effective,
        "capacity_limit": MAX_PARALLEL_AGENT_CAPACITY,
        "runnable_task_ids": [str(task["task_id"]) for task in candidates],
        "blocked_repairs": blocked_repairs,
        "terminal_blocks": terminal_blocks,
        "operator_signals": operator_signals,
        "routed_operator_signals": routed_operator_signals,
        "leased": leased,
        "skipped": skipped,
        "external_actions": "blocked",
    }
    if capacity_effective > 0:
        result["no_silent_idle"] = evaluate_no_silent_idle(conn, matter_scope, result, write=not dry_run)
    else:
        result["no_silent_idle"] = {"ok": True, "matter_scope": matter_scope, "reason": "capacity_zero"}
    return result


def record_operator_signal(
    conn: sqlite3.Connection,
    matter_scope: str,
    signal_type: str,
    message: str,
    *,
    target_task_id: str | None = None,
    priority: str = "normal",
    requested_by: str = "operator",
    write: bool = True,
) -> dict[str, object]:
    raw_type = signal_type.strip().lower()
    normalized_type = OPERATOR_SIGNAL_ALIASES.get(raw_type, raw_type)
    normalized_priority = priority.strip().lower()
    clean_message = message.strip()
    if normalized_type not in OPERATOR_SIGNAL_TYPES:
        raise ValueError(f"unsupported operator signal type: {signal_type}")
    if normalized_priority not in OPERATOR_SIGNAL_PRIORITIES:
        raise ValueError(f"unsupported operator signal priority: {priority}")
    if not clean_message:
        raise ValueError("operator signal requires a message")
    target_task_id = target_task_id.strip() if target_task_id else None
    if target_task_id:
        task = conn.execute("SELECT matter_scope FROM tasks WHERE task_id = ?", (target_task_id,)).fetchone()
        if task is None:
            raise ValueError(f"unknown task: {target_task_id}")
        task_matter_scope = str(task["matter_scope"])
        if task_matter_scope != matter_scope:
            raise ValueError(f"task {target_task_id} belongs to matter {task_matter_scope}, not {matter_scope}")
    current = repo.get_matter_orchestrator(conn, matter_scope=matter_scope)
    payload = {
        "signal_type": normalized_type,
        "message": clean_message,
        "target_task_id": target_task_id or "",
        "priority": normalized_priority,
        "requested_by": requested_by.strip() or "operator",
        "route": "operator_to_master_orchestrator_to_matter_orchestrator",
        "status": "pending_orchestrator_review",
        "external_actions": "blocked",
    }
    result: dict[str, object] = {
        "dry_run": not write,
        "matter_scope": matter_scope,
        "would_create_orchestrator": current is None,
        **payload,
    }
    if not write:
        return result
    orchestrator_id = ensure_matter_orchestrator(conn, matter_scope)
    severity = "blocker" if normalized_priority == "blocker" else "warning" if normalized_priority == "high" else "info"
    attention_id = repo.record_human_attention_once(
        conn,
        target_type="task" if target_task_id else "matter",
        target_id=target_task_id or matter_scope,
        severity=severity,
        reason=f"operator {normalized_type}: {clean_message}",
        matter_scope=matter_scope,
    )
    _ = conn.execute(
        "UPDATE matter_orchestrators SET status = 'operator_signal_pending', updated_at = ? WHERE orchestrator_id = ?",
        (utc_now(), orchestrator_id),
    )
    event_payload = {**payload, "attention_id": attention_id or ""}
    event_id = repo.record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.operator_signal",
        payload=event_payload,
    )
    _ = repo.emit_event(
        conn,
        "master_orchestrator.operator_signal_received",
        matter_scope=matter_scope,
        payload={"orchestrator_event_id": event_id, **event_payload},
    )
    return {**result, "dry_run": False, "orchestrator_id": orchestrator_id, "orchestrator_event_id": event_id, "attention_id": attention_id}


def report_worker_failure_to_orchestrator(
    conn: sqlite3.Connection,
    task_id: str,
    failure_reason: str,
    *,
    matter_scope: str | None = None,
) -> str:
    task = conn.execute("SELECT matter_scope FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    task_matter_scope = str(task["matter_scope"])
    if matter_scope is not None and task_matter_scope != matter_scope:
        raise ValueError(f"task {task_id} belongs to matter {task_matter_scope}, not {matter_scope}")
    return repo.record_orchestrator_worker_failure(
        conn,
        task_id=task_id,
        failure_reason=failure_reason,
        matter_scope=task_matter_scope,
        source="orchestrator.report_worker_failure",
    )


def orchestrator_plan_repair(conn: sqlite3.Connection, matter_scope: str, failure_event_id: str) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT oe.*, mo.failure_count
        FROM orchestrator_events oe
        JOIN matter_orchestrators mo ON mo.orchestrator_id = oe.orchestrator_id
        WHERE oe.orchestrator_event_id = ? AND oe.matter_scope = ?
        """,
        (failure_event_id, matter_scope),
    ).fetchone()
    if row is None:
        raise ValueError(f"failure event not found in matter {matter_scope}: {failure_event_id}")
    payload = _json_object(str(row["payload_json"] or "{}"))
    reasons = _payload_reasons(payload)
    reason = " ".join(reasons).lower()
    task_id = str(payload.get("task_id") or "")
    actions: list[dict[str, object]] = []
    if task_id:
        actions.extend(_repair_actions_for_blocked_task(conn, matter_scope=matter_scope, task_id=task_id, reasons=reasons))
    if any(term in reason for term in ("citation", "unsupported", "fabricated")):
        actions.append({"type": "verifier_task", "task_type": "citation_audit", "reason": "failure mentions citation/support"})
    if any(term in reason for term in ("context", "token", "missing source", "stale")):
        actions.append({"type": "context_rebuild", "reason": "failure suggests context/source mismatch"})
    if int(row["failure_count"]) >= 2 or any(term in reason for term in ("contradiction", "complex", "uncertain")):
        actions.append({"type": "model_upgrade", "decision_tier": "pro_orchestrator", "reason": "repeated or complex failure requires Pro review"})
    if not actions:
        actions.append({"type": "human_intervention", "reason": "failure reason does not map to a safe automatic repair"})
    return {
        "matter_scope": matter_scope,
        "failure_event_id": failure_event_id,
        "proposed_actions": actions,
        "retry_limit": 1,
        "external_actions": "blocked",
        "canonical_writes": "reducer_only",
    }


def orchestrator_select_model(conn: sqlite3.Connection, matter_scope: str, task_id: str) -> dict[str, object]:
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ? AND matter_scope = ?", (task_id, matter_scope)).fetchone())
    if task is None:
        raise ValueError(f"task not found in matter {matter_scope}: {task_id}")
    provider_policy = _provider_policy(task)
    policy_raw = provider_policy.get("model_routing")
    policy = load_model_routing_policy(cast(Mapping[str, object], policy_raw)) if isinstance(policy_raw, Mapping) else default_smart_model_policy()
    decision_policy = smart_provider_policy_for_route(
        policy,
        layer=_layer_for_task(task),
        stage=str(task["stage"]),
        task_type=str(task["task_type"]),
        task_id=task_id,
        matter_scope=matter_scope,
        expected_value=float(str(task["expected_value"] or 0.0)),
    )
    orchestrator_id = ensure_matter_orchestrator(conn, matter_scope)
    _ = conn.execute(
        "UPDATE matter_orchestrators SET model_decision_json = ?, updated_at = ? WHERE orchestrator_id = ?",
        (json.dumps(decision_policy.get("model_decision") or {}, sort_keys=True), utc_now(), orchestrator_id),
    )
    _ = repo.record_orchestrator_event(
        conn,
        orchestrator_id=orchestrator_id,
        event_type="orchestrator.model_selected",
        payload={"task_id": task_id, "provider_policy": decision_policy},
    )
    return decision_policy


def _runnable_matter_tasks(conn: sqlite3.Connection, *, matter_scope: str, capacity: int) -> list[Mapping[str, object]]:
    if capacity <= 0:
        return []
    rows = cast(list[Mapping[str, object]], conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE matter_scope = ? AND status IN (?, ?, ?)
        ORDER BY expected_value DESC, created_at ASC
        """,
        (matter_scope, str(TaskStatus.QUEUED), str(TaskStatus.READY), str(TaskStatus.BLOCKED)),
    ).fetchall())
    runnable: list[Mapping[str, object]] = []
    for task in rows:
        if conn.execute("SELECT 1 FROM leases WHERE task_id = ? AND status = 'active'", (task["task_id"],)).fetchone() is not None:
            continue
        gate_result = evaluate_task_gates(conn, task)
        if gate_result.allowed:
            runnable.append(task)
        if len(runnable) >= capacity:
            break
    return runnable


def _blocked_matter_tasks(conn: sqlite3.Connection, *, matter_scope: str, capacity: int) -> list[Mapping[str, object]]:
    if capacity <= 0:
        return []
    return [
        cast(Mapping[str, object], row)
        for row in conn.execute(
            """
            SELECT task_id, task_type, stage, blocked_reasons_json
            FROM tasks
            WHERE matter_scope = ? AND status = ?
            ORDER BY expected_value DESC, updated_at ASC, created_at ASC
            LIMIT ?
            """,
            (matter_scope, str(TaskStatus.BLOCKED), capacity),
        ).fetchall()
    ]


def _pending_operator_signals(conn: sqlite3.Connection, *, matter_scope: str, capacity: int) -> list[dict[str, object]]:
    if capacity <= 0:
        return []
    rows = conn.execute(
        """
        SELECT oe.orchestrator_event_id, oe.payload_json, oe.created_at
        FROM orchestrator_events oe
        WHERE oe.matter_scope = ?
          AND oe.event_type = 'orchestrator.operator_signal'
          AND NOT EXISTS (
            SELECT 1
            FROM orchestrator_events routed
            WHERE routed.matter_scope = oe.matter_scope
              AND routed.event_type = 'orchestrator.operator_signal_routed'
              AND json_extract(routed.payload_json, '$.operator_signal_event_id') = oe.orchestrator_event_id
          )
        ORDER BY oe.created_at ASC
        LIMIT ?
        """,
        (matter_scope, capacity),
    ).fetchall()
    signals: list[dict[str, object]] = []
    for row in rows:
        payload = _json_object_or_empty(str(row["payload_json"] or "{}"))
        signals.append(
            {
                "operator_signal_event_id": str(row["orchestrator_event_id"]),
                "created_at": str(row["created_at"]),
                "signal_type": str(payload.get("signal_type") or "attention"),
                "message": str(payload.get("message") or ""),
                "target_task_id": str(payload.get("target_task_id") or ""),
                "priority": str(payload.get("priority") or "normal"),
                "requested_by": str(payload.get("requested_by") or "operator"),
                "status": "pending_orchestrator_review",
            }
        )
    return signals


def _terminal_repair_state(conn: sqlite3.Connection, *, task_id: str) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT payload_json
        FROM orchestrator_events
        WHERE event_type = 'orchestrator.repair_limit_reached'
          AND json_extract(payload_json, '$.task_id') = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return _json_object_or_empty(str(row["payload_json"] or "{}"))


def _repair_actions_for_blocked_task(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    task_id: str,
    reasons: list[str],
) -> list[dict[str, object]]:
    reason_text = " ".join(reasons).lower()
    actions: list[dict[str, object]] = []
    missing_certs = _missing_certifications(reasons)
    if missing_certs:
        actions.append(
            {
                "type": "foundation_gap_task",
                "task_type": "certification_repair",
                "missing_certifications": missing_certs,
                "reason": "gate is waiting on explicit foundation certifications",
            }
        )
    incomplete_deps = _incomplete_task_dependencies(reasons)
    if incomplete_deps:
        actions.append(
            {
                "type": "dependency_repair",
                "task_type": "dependency_unblock",
                "dependency_task_ids": incomplete_deps,
                "reason": "dependent task is not complete",
            }
        )
    decomposition_action = decomposition_repair_action(conn, task_id=task_id, reasons=reasons)
    if decomposition_action is not None:
        actions.append(decomposition_action)
    if any(term in reason_text for term in ("source dependency", "artifact dependency", "stale")):
        actions.append({"type": "context_rebuild", "reason": "source, artifact, or stale dependency gate failed"})
    if any(term in reason_text for term in ("malformed", "budget blocked", "cost limit")):
        actions.append({"type": "human_intervention", "reason": "operator policy or malformed metadata blocks safe auto-repair"})
    if _draft_hostile_review_deadlock(conn, matter_scope=matter_scope, task_id=task_id, reasons=reasons):
        actions.append(
            {
                "type": "profile_dependency_repair",
                "task_type": "adaptive_plan_repair",
                "reason": "draft task is waiting on hostile-review certification while hostile review waits on the draft",
            }
        )
    if not actions:
        actions.append({"type": "human_intervention", "reason": "blocked gate does not map to a safe automatic repair"})
    return actions


def _missing_certifications(reasons: list[str]) -> list[str]:
    prefix = "missing certification: "
    return _ordered_unique(reason[len(prefix):] for reason in reasons if reason.startswith(prefix))


def _incomplete_task_dependencies(reasons: list[str]) -> list[str]:
    prefix = "incomplete task dependency: "
    return _ordered_unique(reason[len(prefix):] for reason in reasons if reason.startswith(prefix))


def _ordered_unique(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _draft_hostile_review_deadlock(conn: sqlite3.Connection, *, matter_scope: str, task_id: str, reasons: list[str]) -> bool:
    if not any(reason.endswith(":hostile_review") for reason in reasons):
        return False
    task = conn.execute("SELECT task_type FROM tasks WHERE task_id = ? AND matter_scope = ?", (task_id, matter_scope)).fetchone()
    if task is None or str(task["task_type"]) != "draft_preparation":
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM tasks
        WHERE matter_scope = ?
          AND task_type IN ('hostile_opponent_review', 'hostile_review')
          AND status = ?
          AND blocked_reasons_json LIKE ?
        LIMIT 1
        """,
        (matter_scope, str(TaskStatus.BLOCKED), f"%incomplete task dependency: {task_id}%"),
    ).fetchone()
    return row is not None


def _provider_policy(task: Mapping[str, object]) -> dict[str, object]:
    try:
        loaded = json.loads(str(task["provider_policy_json"] or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(cast(Mapping[str, object], loaded)) if isinstance(loaded, Mapping) else {}


def _json_object(text: str) -> dict[str, object]:
    loaded = json.loads(text)
    return dict(cast(Mapping[str, object], loaded)) if isinstance(loaded, Mapping) else {}


def _json_object_or_empty(text: str) -> dict[str, object]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return dict(cast(Mapping[str, object], loaded)) if isinstance(loaded, Mapping) else {}


def _json_list(text: str) -> list[str]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in cast(list[object], loaded)]


def _payload_reasons(payload: Mapping[str, object]) -> list[str]:
    if isinstance(payload.get("reasons"), list):
        return [str(item) for item in cast(list[object], payload["reasons"])]
    reason = payload.get("failure_reason") or payload.get("reason") or ""
    return [str(reason)] if str(reason) else []


def _layer_for_task(task: Mapping[str, object]) -> str:
    task_type = str(task["task_type"])
    if "hostile" in task_type:
        return "hostile_review"
    if "final_quality" in task_type:
        return "verifier"
    if "reducer" in task_type:
        return "reducer"
    return "worker"
