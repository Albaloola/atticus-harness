"""Operator runbook export for a matter's current control-plane state."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import cast

from atticus.agents.repair_executor import execute_repair_tick
from atticus.agents.repair_planner import list_repair_plans, next_repair_plan
from atticus.reducer.review_queue import list_reducer_reviews
from atticus.status.completion import build_matter_completion_report, next_resume_action
from atticus.workflows.final_gate import final_gate_readiness, plan_final_gate_repairs


def build_runbook(conn: sqlite3.Connection, *, matter_scope: str, db_path: str = "DB") -> dict[str, object]:
    completion = _materialize_resume_commands(build_matter_completion_report(conn, matter_scope).as_dict(), db_path)
    next_action = _materialize_resume_commands(next_resume_action(conn, matter_scope), db_path)
    repairs = [plan.as_dict() for plan in list_repair_plans(conn, matter_scope=matter_scope)]
    next_repair = next_repair_plan(conn, matter_scope=matter_scope)
    reducer_reviews = [review.as_dict() for review in list_reducer_reviews(conn, matter_scope=matter_scope)]
    provider_failures = _provider_failure_groups(conn, matter_scope=matter_scope)
    provider_taxonomy = _provider_taxonomy(conn, matter_scope=matter_scope)
    error_logs = _recent_error_logs(conn, matter_scope=matter_scope)
    human_attention = _human_attention(conn, matter_scope=matter_scope)
    task_counts = _task_status_counts(conn, matter_scope=matter_scope)
    certifications = _certifications(conn, matter_scope=matter_scope)
    stale_dependencies = _stale_dependencies(conn, matter_scope=matter_scope)
    final_gate = _materialize_resume_commands(final_gate_readiness(conn, matter_scope), db_path)
    final_gate_repairs = _materialize_resume_commands({"repairs": plan_final_gate_repairs(conn, matter_scope)}, db_path)
    repair_executions = _recent_repair_executions(conn, matter_scope=matter_scope)
    repair_executor = execute_repair_tick(conn, matter_scope=matter_scope, max_repairs=10, write=False).as_dict()
    terminal_lanes = [item for item in repair_executor.get("terminal", []) if isinstance(item, Mapping)]
    return {
        "matter_scope": matter_scope,
        "completion": completion,
        "next_action": next_action,
        "exact_next_action": next_action,
        "certifications": certifications,
        "task_status_counts": task_counts,
        "blocked_tasks": _tasks_by_status(conn, matter_scope=matter_scope, statuses=("blocked",)),
        "failed_tasks": _tasks_by_status(conn, matter_scope=matter_scope, statuses=("failed",)),
        "reducer_pending_tasks": completion.get("reducer_pending", []) if isinstance(completion, Mapping) else [],
        "reducer_review_queue": reducer_reviews,
        "reducer_review_commands": _reducer_review_commands(reducer_reviews, db_path),
        "repair_plans": repairs,
        "next_repair_plan": next_repair.as_dict() if next_repair is not None else None,
        "blocker_ownership": _blocker_ownership(completion),
        "provider_failure_groups": provider_failures,
        "provider_taxonomy": provider_taxonomy,
        "error_logs": error_logs,
        "human_attention_summary": human_attention["summary"],
        "open_human_attention": human_attention["open"],
        "stale_dependencies": stale_dependencies,
        "stale_warnings": _stale_warnings(completion, stale_dependencies),
        "final_gate": final_gate,
        "final_gate_repairs": final_gate_repairs["repairs"] if isinstance(final_gate_repairs, Mapping) else [],
        "repair_executions": repair_executions,
        "repair_executor": repair_executor,
        "next_auto_repairable_action": (repair_executor.get("attempted") or [None])[0] if isinstance(repair_executor.get("attempted"), list) and repair_executor.get("attempted") else None,
        "next_reducer_owned_action": (reducer_reviews[0] if reducer_reviews else None),
        "terminal_provider_operator_blockers": terminal_lanes,
        "repair_tick_write_can_make_progress": bool(repair_executor.get("made_progress") or repair_executor.get("attempted")),
        "run_free_loop_can_continue_safely": str(cast(Mapping[str, object], next_action).get("owner") or "") not in {"provider", "operator"},
        "exact_resume_command": str(cast(Mapping[str, object], next_action).get("resume_command") or ""),
    }


def render_runbook_markdown(runbook: Mapping[str, object]) -> str:
    matter_scope = str(runbook["matter_scope"])
    completion = cast(Mapping[str, object], runbook["completion"])
    next_action = cast(Mapping[str, object], runbook["next_action"])
    lines = [
        f"# Atticus Matter Runbook: {matter_scope}",
        "",
        "## Completion",
        f"- Done: {completion.get('done')}",
        f"- Safe to finalize: {completion.get('safe_to_finalize')}",
        f"- Blocked: {completion.get('blocked')}",
        f"- Missing certifications: {_join(completion.get('missing_certifications'))}",
        f"- Runnable tasks: {completion.get('runnable_count')}",
        f"- Reducer pending: {completion.get('reducer_pending_count')}",
        f"- Failed tasks: {completion.get('failed_count')}",
        f"- Blocked tasks: {completion.get('blocked_count')}",
        "",
        "## Next Action",
        f"- Exact next action: {next_action.get('type') or 'none'}",
        f"- Type: {next_action.get('type')}",
        f"- Owner: {next_action.get('owner')}",
        f"- Reason: {next_action.get('reason')}",
        f"- Resume command: `{next_action.get('resume_command') or ''}`",
        "",
        "## Blocker Ownership",
        *_table(["requirement_id", "status", "owner", "repair_action", "blocking_reason", "resume_command"], cast(list[Mapping[str, object]], runbook["blocker_ownership"])),
        "",
        "## Certifications",
        *_table(["certification_type", "status", "created_at"], cast(list[Mapping[str, object]], runbook["certifications"])),
        "",
        "## Task Counts",
        *_key_value_table(cast(Mapping[str, object], runbook["task_status_counts"])),
        "",
        "## Reducer Review Queue",
        *_table(["candidate_id", "task_id", "stage", "task_type", "priority", "status", "reason", "recommended_action"], cast(list[Mapping[str, object]], runbook["reducer_review_queue"])),
        "",
        "## Reducer Review Commands",
        *_table(["candidate_id", "show_command", "accept_template", "reject_template"], cast(list[Mapping[str, object]], runbook["reducer_review_commands"])),
        "",
        "## Repair Plans",
        *_table(["repair_plan_id", "target_type", "target_id", "blocker_type", "status", "severity"], cast(list[Mapping[str, object]], runbook["repair_plans"])),
        "",
        "## Provider Taxonomy",
        *_table(["route_class", "requested_provider", "requested_model", "actual_provider", "actual_model", "fallback_allowed", "fallback_policy_result", "runs", "input_tokens", "output_tokens", "cache_hit_tokens", "latest_at"], cast(list[Mapping[str, object]], runbook["provider_taxonomy"])),
        "",
        "## Provider Failure Groups",
        *_table(["error_type", "severity", "terminal", "occurrences", "latest_message"], cast(list[Mapping[str, object]], runbook["provider_failure_groups"])),
        "",
        "## Human Attention Summary",
        *_table(["severity", "owner", "signature", "reason", "count"], cast(list[Mapping[str, object]], runbook["human_attention_summary"])),
        "",
        "## Open Human Attention",
        *_table(["attention_id", "target_type", "target_id", "severity", "owner", "signature", "reason"], cast(list[Mapping[str, object]], runbook["open_human_attention"])),
        "",
        "## Stale Warnings",
        *_table(["target_type", "target_id", "warning"], cast(list[Mapping[str, object]], runbook["stale_warnings"])),
        "",
        "## Stale Dependencies",
        *_table(["target_type", "target_id", "reason"], cast(list[Mapping[str, object]], runbook["stale_dependencies"])),
        "",
        "## Final Gate",
        f"- Ready: {cast(Mapping[str, object], runbook['final_gate']).get('ready')}",
        f"- Next action: {cast(Mapping[str, object], cast(Mapping[str, object], runbook['final_gate']).get('next_action') or {}).get('type', '')}",
        "",
        "## Repair Executor",
        f"- repair-tick --write can make progress: {runbook.get('repair_tick_write_can_make_progress')}",
        f"- run-free-loop can continue safely: {runbook.get('run_free_loop_can_continue_safely')}",
        f"- Next auto-repairable action: {cast(Mapping[str, object] | None, runbook.get('next_auto_repairable_action'))}",
        f"- Next reducer-owned action: {cast(Mapping[str, object] | None, runbook.get('next_reducer_owned_action'))}",
        *_table(["repair_plan_id", "action_type", "owner", "reason"], cast(list[Mapping[str, object]], runbook.get("terminal_provider_operator_blockers") or [])),
        "",
        "## Repair Executions",
        *_table(["repair_plan_id", "action_type", "mode", "status", "created_at"], cast(list[Mapping[str, object]], runbook["repair_executions"])),
        "",
        "## Exact Resume Command",
        f"```bash\n{runbook.get('exact_resume_command') or 'true'}\n```",
        "",
    ]
    return "\n".join(lines)


def export_runbook(conn: sqlite3.Connection, *, matter_scope: str, out_path: str | Path, db_path: str = "DB") -> dict[str, object]:
    runbook = build_runbook(conn, matter_scope=matter_scope, db_path=db_path)
    rendered = render_runbook_markdown(runbook)
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    return {"matter_scope": matter_scope, "out": str(destination), "runbook": runbook}


def _task_status_counts(conn: sqlite3.Connection, *, matter_scope: str) -> dict[str, int]:
    return {
        str(row["status"]): int(row["n"])
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM tasks
            WHERE matter_scope = ?
            GROUP BY status
            ORDER BY status
            """,
            (matter_scope,),
        )
    }


def _tasks_by_status(conn: sqlite3.Connection, *, matter_scope: str, statuses: tuple[str, ...]) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT task_id, title, task_type, stage, status, blocked_reasons_json, updated_at
        FROM tasks
        WHERE matter_scope = ? AND status IN ({placeholders})
        ORDER BY updated_at DESC, task_id
        LIMIT 50
        """.format(placeholders=", ".join("?" for _ in statuses)),
        (matter_scope, *statuses),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _certifications(conn: sqlite3.Connection, *, matter_scope: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT certification_type, status, validator, validation_result_id, created_at
        FROM certifications
        WHERE subject_type = 'matter' AND subject_id = ?
        ORDER BY certification_type, created_at DESC
        """,
        (matter_scope,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _provider_failure_groups(conn: sqlite3.Connection, *, matter_scope: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT error_type, severity, terminal,
               SUM(occurrence_count) AS occurrences,
               MAX(created_at) AS latest_at
        FROM error_logs
        WHERE matter_scope = ? AND (
          error_type LIKE 'provider_%'
          OR error_type LIKE '%provider%'
          OR message LIKE '%OpenRouter%'
        )
        GROUP BY error_type, severity, terminal
        ORDER BY latest_at DESC
        LIMIT 25
        """,
        (matter_scope,),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        latest = conn.execute(
            """
            SELECT message
            FROM error_logs
            WHERE matter_scope = ? AND error_type = ? AND severity = ? AND terminal = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (matter_scope, row["error_type"], row["severity"], row["terminal"]),
        ).fetchone()
        result.append(
            {
                "error_type": row["error_type"],
                "severity": row["severity"],
                "terminal": bool(row["terminal"]),
                "occurrences": int(row["occurrences"] or 0),
                "latest_at": row["latest_at"],
                "latest_message": latest["message"] if latest is not None else "",
            }
        )
    return result


def _provider_taxonomy(conn: sqlite3.Connection, *, matter_scope: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT pr.requested_provider, pr.requested_model, pr.actual_provider, pr.actual_model,
               pr.fallback_allowed, pr.fallback_policy_result,
               COUNT(*) AS runs,
               SUM(pr.input_tokens) AS input_tokens,
               SUM(pr.output_tokens) AS output_tokens,
               SUM(pr.cache_hit_tokens) AS cache_hit_tokens,
               MAX(pr.created_at) AS latest_at
        FROM provider_runs pr
        JOIN tasks t ON t.task_id = pr.task_id
        WHERE t.matter_scope = ?
        GROUP BY pr.requested_provider, pr.requested_model, pr.actual_provider, pr.actual_model,
                 pr.fallback_allowed, pr.fallback_policy_result
        ORDER BY latest_at DESC
        LIMIT 50
        """,
        (matter_scope,),
    ).fetchall()
    return [
        {
            "route_class": _provider_route_class(
                requested_provider=str(row["requested_provider"]),
                requested_model=str(row["requested_model"]),
                actual_provider=str(row["actual_provider"]),
                actual_model=str(row["actual_model"]),
                fallback_allowed=bool(row["fallback_allowed"]),
                fallback_policy_result=str(row["fallback_policy_result"]),
            ),
            "requested_provider": row["requested_provider"],
            "requested_model": row["requested_model"],
            "actual_provider": row["actual_provider"],
            "actual_model": row["actual_model"],
            "fallback_allowed": bool(row["fallback_allowed"]),
            "fallback_policy_result": row["fallback_policy_result"],
            "runs": int(row["runs"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cache_hit_tokens": int(row["cache_hit_tokens"] or 0),
            "latest_at": row["latest_at"],
        }
        for row in rows
    ]


def _provider_route_class(
    *,
    requested_provider: str,
    requested_model: str,
    actual_provider: str,
    actual_model: str,
    fallback_allowed: bool,
    fallback_policy_result: str,
) -> str:
    if actual_provider in {"", "missing"} or actual_model in {"", "missing"}:
        return "provider_failure"
    if requested_provider == actual_provider and requested_model == actual_model:
        return "exact_route"
    if requested_provider == "openrouter" and _openrouter_versioned_model_match(requested_model, actual_model):
        return "openrouter_endpoint_provenance"
    if fallback_allowed:
        return "explicit_fallback_route"
    if fallback_policy_result and fallback_policy_result not in {"not_needed", "exact_match"}:
        return str(fallback_policy_result)
    return "provider_model_drift"


def _openrouter_versioned_model_match(requested_model: str, actual_model: str) -> bool:
    if not requested_model or not actual_model:
        return False
    return actual_model == requested_model or actual_model.startswith(f"{requested_model}-")


def _recent_error_logs(conn: sqlite3.Connection, *, matter_scope: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT error_log_id, target_type, target_id, error_type, message, severity,
               escalation_level, occurrence_count, consecutive_count, terminal, created_at
        FROM error_logs
        WHERE matter_scope = ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (matter_scope,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _human_attention(conn: sqlite3.Connection, *, matter_scope: str) -> dict[str, object]:
    rows = [
        _row_to_dict(row)
        for row in conn.execute(
            """
            SELECT attention_id, target_type, target_id, severity, reason, status,
                   owner, signature, superseded_by, created_at
            FROM human_attention
            WHERE matter_scope = ?
            ORDER BY CASE severity WHEN 'blocker' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     attention_id DESC
            LIMIT 100
            """,
            (matter_scope,),
        ).fetchall()
    ]
    grouped = Counter(
        (str(row["severity"]), str(row["owner"]), str(row["signature"]), str(row["reason"]))
        for row in rows
        if str(row["status"]) == "open"
    )
    summary = [
        {"severity": severity, "owner": owner, "signature": signature, "reason": reason, "count": count}
        for (severity, owner, signature, reason), count in grouped.most_common(25)
    ]
    return {"open": [row for row in rows if str(row["status"]) == "open"], "summary": summary}


def _stale_dependencies(conn: sqlite3.Connection, *, matter_scope: str) -> list[dict[str, object]]:
    stale: list[dict[str, object]] = []
    stale.extend(
        {
            "target_type": "source",
            "target_id": row["source_id"],
            "reason": "source stale",
        }
        for row in conn.execute("SELECT source_id FROM sources WHERE matter_scope = ? AND stale = 1 ORDER BY source_id LIMIT 50", (matter_scope,))
    )
    stale.extend(
        {
            "target_type": "artifact",
            "target_id": row["artifact_id"],
            "reason": "artifact stale",
        }
        for row in conn.execute("SELECT artifact_id FROM artifacts WHERE matter_scope = ? AND stale = 1 ORDER BY artifact_id LIMIT 50", (matter_scope,))
    )
    return stale


def _table(columns: list[str], rows: list[Mapping[str, object]]) -> list[str]:
    if not rows:
        return ["_None._"]
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(_cell(row.get(column, "")) for column in columns) + " |"
        for row in rows
    ]
    return [header, divider, *body]


def _key_value_table(values: Mapping[str, object]) -> list[str]:
    if not values:
        return ["_None._"]
    return _table(["key", "value"], [{"key": key, "value": value} for key, value in values.items()])


def _join(value: object) -> str:
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value) or "none"
    return str(value or "none")


def _blocker_ownership(completion: Mapping[str, object]) -> list[dict[str, object]]:
    requirements = completion.get("requirements")
    if not isinstance(requirements, list):
        return []
    return [
        {
            "requirement_id": item.get("requirement_id", ""),
            "status": item.get("status", ""),
            "owner": item.get("owner", ""),
            "repair_action": item.get("repair_action", ""),
            "blocking_reason": item.get("blocking_reason", ""),
            "resume_command": item.get("resume_command", ""),
        }
        for item in requirements
        if isinstance(item, Mapping) and item.get("status") != "satisfied"
    ]


def _reducer_review_commands(reviews: list[Mapping[str, object]], db_path: str) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = []
    for review in reviews:
        candidate_id = str(review.get("candidate_id", ""))
        if not candidate_id:
            continue
        commands.append(
            {
                "candidate_id": candidate_id,
                "show_command": f"python -m atticus.cli reducer-review show --db {db_path} --candidate-id {candidate_id} --json",
                "accept_template": f"python -m atticus.cli reducer-review accept --db {db_path} --candidate-id {candidate_id} --lease-id LEASE_ID --write --json",
                "reject_template": f"python -m atticus.cli reducer-review reject --db {db_path} --candidate-id {candidate_id} --reason \"REASON\" --write --json",
            }
        )
    return commands


def _recent_repair_executions(conn: sqlite3.Connection, *, matter_scope: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT payload_json, created_at
        FROM events
        WHERE matter_scope = ? AND event_type = 'repair.plan_executed'
        ORDER BY created_at DESC
        LIMIT 25
        """,
        (matter_scope,),
    ).fetchall()
    items: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"] or "{}")) if row["payload_json"] else {}
        outcome = payload.get("outcome") if isinstance(payload.get("outcome"), Mapping) else {}
        items.append(
            {
                "repair_plan_id": str(payload.get("repair_plan_id") or ""),
                "action_type": str(payload.get("action_type") or ""),
                "mode": str(outcome.get("mode") or ""),
                "status": "executed",
                "outcome": str(outcome),
                "created_at": str(row["created_at"]),
            }
        )
    return items


def _stale_warnings(completion: Mapping[str, object], stale_dependencies: list[Mapping[str, object]]) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    stale_artifacts = completion.get("stale_artifacts")
    if isinstance(stale_artifacts, list | tuple):
        warnings.extend(
            {
                "target_type": "artifact",
                "target_id": str(artifact_id),
                "warning": "stale artifact blocks finalization until rebuilt, replaced, or deliberately superseded",
            }
            for artifact_id in stale_artifacts
        )
    warnings.extend(
        {
            "target_type": str(item.get("target_type", "")),
            "target_id": str(item.get("target_id", "")),
            "warning": f"{item.get('reason', 'stale dependency')} blocks proof reuse until repaired or superseded",
        }
        for item in stale_dependencies
    )
    return warnings


def _cell(value: object) -> str:
    text = json.dumps(value, sort_keys=True) if isinstance(value, dict | list | tuple) else str(value)
    return text.replace("\n", " ").replace("|", "\\|")[:220]


def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    result = {str(key): row[key] for key in row.keys()}
    if "terminal" in result:
        result["terminal"] = bool(result["terminal"])
    return result


def _materialize_resume_commands(value: object, db_path: str) -> object:
    if isinstance(value, str):
        return value.replace("--db DB", f"--db {db_path}")
    if isinstance(value, Mapping):
        return {str(key): _materialize_resume_commands(item, db_path) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_materialize_resume_commands(item, db_path) for item in value]
    return value
