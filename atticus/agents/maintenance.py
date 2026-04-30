"""Isolated control-plane maintenance orchestrator.

The maintenance orchestrator is deliberately separate from matter workers and
reducers. It diagnoses harness control-state failures, writes a report, and
emits a resume signal; it does not mutate sources, artifacts, candidates, or
canonical legal work.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from typing import cast

from atticus.core.events import utc_now
from atticus.db import repo
from atticus.db.doctor import schema_check_json, verify_schema
from atticus.scheduler.lease import expire_leases


MAINTENANCE_ISOLATION_LEVEL = "control_plane_only"


def request_maintenance(
    conn: sqlite3.Connection,
    *,
    matter_scope: str = "global",
    reason: str,
    triggered_by: str = "operator",
    payload: dict[str, object] | None = None,
    write: bool = False,
) -> dict[str, object]:
    clean_reason = reason.strip() or "maintenance requested"
    result: dict[str, object] = {
        "dry_run": not write,
        "matter_scope": matter_scope,
        "trigger_reason": clean_reason,
        "triggered_by": triggered_by,
        "isolation_level": MAINTENANCE_ISOLATION_LEVEL,
    }
    if not write:
        return result
    maintenance_run_id = repo.request_maintenance_run(
        conn,
        matter_scope=matter_scope,
        trigger_reason=clean_reason,
        triggered_by=triggered_by,
        payload=payload or {},
    )
    return {**result, "dry_run": False, "maintenance_run_id": maintenance_run_id or ""}


def maintenance_status(conn: sqlite3.Connection, *, matter_scope: str | None = None) -> dict[str, object]:
    check = verify_schema(conn)
    if not check.ok:
        return schema_check_json(conn)
    params: tuple[object, ...] = ()
    where = ""
    if matter_scope:
        where = "WHERE matter_scope = ?"
        params = (matter_scope,)
    runs = [
        _row_to_dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM maintenance_runs
            {where}
            ORDER BY updated_at DESC
            LIMIT 20
            """,
            params,
        ).fetchall()
    ]
    reports = [
        _row_to_dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM maintenance_reports
            {where}
            ORDER BY created_at DESC
            LIMIT 20
            """,
            params,
        ).fetchall()
    ]
    return {"matter_scope": matter_scope or "", "runs": runs, "reports": reports}


def maintenance_tick(
    conn: sqlite3.Connection,
    *,
    matter_scope: str = "global",
    maintenance_run_id: str | None = None,
    write: bool = False,
) -> dict[str, object]:
    check = verify_schema(conn)
    if not check.ok:
        return {"dry_run": not write, **schema_check_json(conn)}
    run = _resolve_run(conn, matter_scope=matter_scope, maintenance_run_id=maintenance_run_id, write=write)
    diagnostics = _build_diagnostics(conn, matter_scope=matter_scope)
    actions = _planned_actions(diagnostics)
    applied_actions: list[dict[str, object]] = []
    resume_signal = _resume_signal(diagnostics)
    report_summary = _summary(diagnostics, actions, resume_signal)
    if not write:
        return {
            "dry_run": True,
            "matter_scope": matter_scope,
            "maintenance_run_id": str(run.get("maintenance_run_id") or ""),
            "isolation_level": MAINTENANCE_ISOLATION_LEVEL,
            "diagnostics": diagnostics,
            "planned_actions": actions,
            "resume_signal": resume_signal,
            "summary": report_summary,
        }

    run_id = str(run["maintenance_run_id"])
    _ = conn.execute(
        "UPDATE maintenance_runs SET status = 'running', updated_at = ? WHERE maintenance_run_id = ?",
        (utc_now(), run_id),
    )
    stale_attention_closed = repo.resolve_stale_system_task_attention(conn, matter_scope=matter_scope)
    if stale_attention_closed:
        applied_actions.append(
            {
                "type": "resolve_stale_system_task_attention",
                "changed": stale_attention_closed,
                "reason": "target task is no longer blocked",
            }
        )
        diagnostics = _build_diagnostics(conn, matter_scope=matter_scope)
        resume_signal = _resume_signal(diagnostics)
        report_summary = _summary(diagnostics, actions, resume_signal)
    expired = _expire_leases_for_scope(conn, matter_scope=matter_scope)
    if expired:
        applied_actions.append({"type": "expire_stale_leases", "lease_ids": expired, "changed": len(expired)})
        diagnostics = _build_diagnostics(conn, matter_scope=matter_scope)
        resume_signal = _resume_signal(diagnostics)
        report_summary = _summary(diagnostics, actions, resume_signal)
    if resume_signal["status"] == "ready_to_resume":
        _resume_matter_orchestrators(conn, matter_scope=matter_scope)
        applied_actions.append({"type": "resume_matter_orchestrator", "status": "repair_required"})
    else:
        applied_actions.append({"type": "resume_matter_orchestrator", "status": "blocked", "reason": resume_signal["reason"]})
    report_id = repo.record_maintenance_report(
        conn,
        maintenance_run_id=run_id,
        summary=report_summary,
        diagnostics=diagnostics,
        actions=[*actions, *applied_actions],
        resume_signal=resume_signal,
    )
    return {
        "dry_run": False,
        "matter_scope": matter_scope,
        "maintenance_run_id": run_id,
        "maintenance_report_id": report_id,
        "isolation_level": MAINTENANCE_ISOLATION_LEVEL,
        "diagnostics": diagnostics,
        "applied_actions": applied_actions,
        "resume_signal": resume_signal,
        "summary": report_summary,
    }


def maintenance_report(conn: sqlite3.Connection, *, maintenance_run_id: str) -> dict[str, object]:
    check = verify_schema(conn)
    if not check.ok:
        return schema_check_json(conn)
    row = conn.execute(
        """
        SELECT *
        FROM maintenance_reports
        WHERE maintenance_run_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (maintenance_run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"maintenance report not found for run: {maintenance_run_id}")
    result = _row_to_dict(row)
    result["diagnostics"] = _json_object(str(result.pop("diagnostics_json") or "{}"))
    result["actions"] = _json_list(str(result.pop("actions_json") or "[]"))
    result["resume_signal"] = _json_object(str(result.pop("resume_signal_json") or "{}"))
    return result


def _resolve_run(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    maintenance_run_id: str | None,
    write: bool,
) -> dict[str, object]:
    if maintenance_run_id:
        row = conn.execute("SELECT * FROM maintenance_runs WHERE maintenance_run_id = ?", (maintenance_run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown maintenance run: {maintenance_run_id}")
        return _row_to_dict(row)
    row = conn.execute(
        """
        SELECT *
        FROM maintenance_runs
        WHERE matter_scope = ? AND status IN ('pending', 'running')
        ORDER BY started_at ASC
        LIMIT 1
        """,
        (matter_scope,),
    ).fetchone()
    if row is not None:
        return _row_to_dict(row)
    if not write:
        return {}
    run_id = repo.request_maintenance_run(
        conn,
        matter_scope=matter_scope,
        trigger_reason="manual maintenance tick",
        triggered_by="maintenance_orchestrator",
        payload={"source": "maintenance_tick"},
    )
    row = conn.execute("SELECT * FROM maintenance_runs WHERE maintenance_run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise RuntimeError("maintenance run request did not create a run")
    return _row_to_dict(row)


def _build_diagnostics(conn: sqlite3.Connection, *, matter_scope: str) -> dict[str, object]:
    scope_filter, params = _scope_filter(matter_scope)
    recent_errors = [
        _row_to_dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM error_logs
            {scope_filter}
            ORDER BY created_at DESC
            LIMIT 25
            """,
            params,
        ).fetchall()
    ]
    terminal_tasks = [
        _row_to_dict(row)
        for row in conn.execute(
            f"""
            SELECT t.task_id, t.matter_scope, t.status, t.blocked_reasons_json, oe.orchestrator_event_id, oe.payload_json
            FROM orchestrator_events oe
            LEFT JOIN tasks t ON t.task_id = json_extract(oe.payload_json, '$.task_id')
            WHERE oe.event_type = 'orchestrator.repair_limit_reached'
              {'AND oe.matter_scope = ?' if matter_scope != 'global' else ''}
            ORDER BY oe.created_at DESC
            LIMIT 25
            """,
            () if matter_scope == "global" else (matter_scope,),
        ).fetchall()
    ]
    open_blockers = [
        _row_to_dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM human_attention
            WHERE severity = 'blocker' AND status = 'open'
              {'' if matter_scope == 'global' else 'AND matter_scope = ?'}
            ORDER BY created_at DESC, attention_id DESC
            LIMIT 25
            """,
            () if matter_scope == "global" else (matter_scope,),
        ).fetchall()
    ]
    active_leases = [
        _row_to_dict(row)
        for row in conn.execute(
            f"""
            SELECT l.*, t.matter_scope
            FROM leases l
            JOIN tasks t ON t.task_id = l.task_id
            WHERE l.status = 'active'
              {'' if matter_scope == 'global' else 'AND t.matter_scope = ?'}
            ORDER BY l.expires_at ASC
            LIMIT 50
            """,
            () if matter_scope == "global" else (matter_scope,),
        ).fetchall()
    ]
    orchestrators = [
        _row_to_dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM matter_orchestrators
            {scope_filter}
            ORDER BY updated_at DESC
            LIMIT 25
            """,
            params,
        ).fetchall()
    ]
    return {
        "matter_scope": matter_scope,
        "recent_errors": recent_errors,
        "terminal_tasks": terminal_tasks,
        "open_blockers": open_blockers,
        "active_leases": active_leases,
        "orchestrators": orchestrators,
        "safe_write_scope": "leases, maintenance tables, events, human_attention, matter_orchestrator status only",
        "forbidden_write_scope": "sources, artifacts, candidate_outputs, reducer_packets, legal_memories",
    }


def _planned_actions(diagnostics: Mapping[str, object]) -> list[dict[str, object]]:
    active_leases = cast(list[object], diagnostics.get("active_leases") or [])
    terminal_tasks = cast(list[object], diagnostics.get("terminal_tasks") or [])
    open_blockers = cast(list[object], diagnostics.get("open_blockers") or [])
    actions: list[dict[str, object]] = [{"type": "write_maintenance_report", "required": True}]
    if active_leases:
        actions.append({"type": "expire_stale_leases", "reason": "maintenance may release expired active leases", "count": len(active_leases)})
    if terminal_tasks or open_blockers:
        actions.append({"type": "hold_for_user_intervention", "terminal_tasks": len(terminal_tasks), "open_blockers": len(open_blockers)})
    else:
        actions.append({"type": "emit_resume_signal", "reason": "no terminal blockers found"})
    return actions


def _resume_signal(diagnostics: Mapping[str, object]) -> dict[str, object]:
    terminal_count = len(cast(list[object], diagnostics.get("terminal_tasks") or []))
    blocker_count = len(cast(list[object], diagnostics.get("open_blockers") or []))
    if terminal_count or blocker_count:
        return {
            "status": "blocked_by_user_intervention",
            "reason": f"{terminal_count} terminal tasks and {blocker_count} open blocker attention items remain",
            "resume_allowed": False,
        }
    return {"status": "ready_to_resume", "reason": "maintenance found no terminal blockers", "resume_allowed": True}


def _resume_matter_orchestrators(conn: sqlite3.Connection, *, matter_scope: str) -> None:
    if matter_scope == "global":
        _ = conn.execute(
            """
            UPDATE matter_orchestrators
            SET status = 'repair_required', updated_at = ?
            WHERE status IN ('maintenance_required', 'operator_signal_pending')
            """,
            (utc_now(),),
        )
        return
    _ = conn.execute(
        """
        UPDATE matter_orchestrators
        SET status = 'repair_required', updated_at = ?
        WHERE matter_scope = ? AND status IN ('maintenance_required', 'operator_signal_pending')
        """,
        (utc_now(), matter_scope),
    )


def _expire_leases_for_scope(conn: sqlite3.Connection, *, matter_scope: str) -> list[str]:
    if matter_scope == "global":
        return expire_leases(conn)
    task_ids = [
        str(row["task_id"])
        for row in conn.execute(
            """
            SELECT l.task_id
            FROM leases l
            JOIN tasks t ON t.task_id = l.task_id
            WHERE l.status = 'active' AND t.matter_scope = ?
            """,
            (matter_scope,),
        ).fetchall()
    ]
    expired: list[str] = []
    for task_id in task_ids:
        expired.extend(expire_leases(conn, task_id=task_id))
    return expired


def _summary(diagnostics: Mapping[str, object], actions: list[dict[str, object]], resume_signal: Mapping[str, object]) -> str:
    return (
        "maintenance inspected "
        f"{len(cast(list[object], diagnostics.get('recent_errors') or []))} error logs, "
        f"{len(cast(list[object], diagnostics.get('terminal_tasks') or []))} terminal tasks, "
        f"{len(cast(list[object], diagnostics.get('open_blockers') or []))} open blockers; "
        f"planned {len(actions)} actions; resume status: {resume_signal.get('status')}"
    )


def _scope_filter(matter_scope: str) -> tuple[str, tuple[object, ...]]:
    if matter_scope == "global":
        return "", ()
    return "WHERE matter_scope = ?", (matter_scope,)


def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {str(key): row[key] for key in row.keys()}


def _json_object(text: str) -> dict[str, object]:
    loaded = json.loads(text)
    return dict(cast(Mapping[str, object], loaded)) if isinstance(loaded, Mapping) else {}


def _json_list(text: str) -> list[dict[str, object]]:
    loaded = json.loads(text)
    return [dict(cast(Mapping[str, object], item)) for item in cast(list[object], loaded) if isinstance(item, Mapping)] if isinstance(loaded, list) else []
