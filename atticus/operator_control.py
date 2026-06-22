"""Human-facing control panel and agent handoff packets for Atticus.

The lower-level harness is intentionally explicit and auditable, but that makes it
hard for a non-developer operator to know what to do next.  This module provides
a small product layer over the existing diagnostics: one status payload, one next
safe command, and one clear distinction between work the harness/agent should do
automatically and questions that genuinely need the human operator.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import sqlite3

from atticus.status.completion import (
    assert_completion_has_next_action,
    build_matter_completion_report,
    next_resume_action,
    route_human_attention,
)
from atticus.status.human_attention_cleanup import plan_human_attention_cleanup
from atticus.workflows.final_gate import final_gate_readiness


AUTO_OWNERS = {"scheduler", "orchestrator", "provider_control_plane"}
HUMAN_OWNERS = {"operator"}
LEGAL_REVIEW_OWNERS = {"reducer"}


def build_operator_control_panel(
    conn: sqlite3.Connection,
    *,
    db_path: str,
    matter_scope: str,
    output_dir: str = "OUT",
    live_approved: bool = False,
) -> dict[str, object]:
    """Build a concise, human-facing matter control panel.

    This is read-only. It intentionally does not execute the next command; it
    tells a UI, OpenClaw/Hermes/Codex agent, or terminal wrapper what should
    happen next and whether the human must be interrupted.
    """

    report = build_matter_completion_report(conn, matter_scope, current_only=True)
    next_action = _materialize_command(next_resume_action(conn, matter_scope), db_path=db_path, output_dir=output_dir)
    invariant = assert_completion_has_next_action(conn, matter_scope).as_dict()
    final_gate = final_gate_readiness(conn, matter_scope=matter_scope)
    routed_attention = _routed_attention_summary(report.unresolved_human_attention, matter_scope=matter_scope)
    cleanup = plan_human_attention_cleanup(conn, matter_scope=matter_scope, provider_probe_passed=())
    ask = _operator_request(report.unresolved_human_attention, matter_scope=matter_scope)
    mode = _recommended_mode(next_action, ask=ask)

    return {
        "matter": matter_scope,
        "done": report.done,
        "safe_to_finalize": report.safe_to_finalize,
        "state": mode["state"],
        "headline": mode["headline"],
        "live_provider_gate": "removed_from_operator_control_panel",
        "counts": {
            "runnable": report.runnable_count,
            "failed": report.failed_count,
            "blocked": report.blocked_count,
            "reducer_pending": report.reducer_pending_count,
            "human_attention_current": len(report.unresolved_human_attention),
            "stale_or_auto_cleanable_attention": len(cleanup.get("actions", [])) if isinstance(cleanup, Mapping) else 0,
        },
        "next_action": next_action,
        "agent_packet": build_agent_handoff_packet(
            matter_scope=matter_scope,
            next_action=next_action,
            operator_request=ask,
            live_approved=live_approved,
        ),
        "operator_request": ask,
        "attention_by_classification": routed_attention,
        "final_gate": {
            "ready": final_gate.get("ready"),
            "complete": final_gate.get("complete"),
            "state": final_gate.get("state"),
            "open_human_attention_count": final_gate.get("open_human_attention_count"),
            "next_action": _materialize_command(final_gate.get("next_action", {}), db_path=db_path, output_dir=output_dir),
        },
        "completion_invariant": invariant,
        "recommended_commands": _recommended_commands(db_path=db_path, matter_scope=matter_scope, output_dir=output_dir, next_action=next_action),
    }


def build_agent_handoff_packet(
    *,
    matter_scope: str,
    next_action: Mapping[str, object],
    operator_request: Mapping[str, object] | None,
    live_approved: bool,
) -> dict[str, object]:
    """Return a stable packet an external agent can consume.

    Contract: if ``needs_human`` is true the agent should ask the operator the
    supplied question and later submit the answer with ``human-response``. If it
    is false, the agent should run/monitor the routed command if policy permits.
    Live provider capability is reported as operational metadata, not as a
    separate operator-interruption gate.
    """

    if operator_request:
        return {
            "schema": "atticus.agent_handoff.v1",
            "matter": matter_scope,
            "needs_human": True,
            "needs_legal_review": False,
            "may_run_without_asking_human": False,
            "question": operator_request,
            "on_answer": operator_request.get("response_command_template"),
        }

    owner = str(next_action.get("owner", ""))
    command = str(next_action.get("resume_command", ""))
    needs_legal_review = owner in LEGAL_REVIEW_OWNERS or str(next_action.get("type", "")) == "manual_reducer_review"
    requires_live = "--allow-live" in command or "ATTICUS_ENABLE_LIVE_OPENROUTER" in command
    may_run = bool(command) and not needs_legal_review and owner not in HUMAN_OWNERS
    return {
        "schema": "atticus.agent_handoff.v1",
        "matter": matter_scope,
        "needs_human": False,
        "needs_legal_review": needs_legal_review,
        "may_run_without_asking_human": may_run,
        "requires_live_provider": requires_live,
        "live_provider_gate": "not_a_human_blocker",
        "owner": owner,
        "next_command": command,
        "reason": next_action.get("reason", ""),
    }


def render_operator_control_panel(panel: Mapping[str, object]) -> str:
    """Render the control panel as terminal-friendly text."""

    lines = [
        "",
        f"Atticus Control Panel — {panel.get('matter')}",
        "=" * 72,
        f"State: {panel.get('state')}",
        f"Summary: {panel.get('headline')}",
        "Live provider gate: removed from operator control panel",
        "",
    ]
    counts = panel.get("counts", {})
    if isinstance(counts, Mapping):
        lines.append("Counts:")
        for key in ("runnable", "failed", "blocked", "reducer_pending", "human_attention_current", "stale_or_auto_cleanable_attention"):
            lines.append(f"  - {key}: {counts.get(key, 0)}")
        lines.append("")

    request = panel.get("operator_request")
    if isinstance(request, Mapping) and request:
        lines.append("Question for User:")
        lines.append(f"  {request.get('question') or request.get('title')}")
        if request.get("why_needed"):
            lines.append(f"  Why: {request.get('why_needed')}")
        acceptable = request.get("acceptable_responses")
        if isinstance(acceptable, list) and acceptable:
            lines.append("  Acceptable responses: " + ", ".join(str(item) for item in acceptable))
        if request.get("response_command_template"):
            lines.append("  Response command:")
            lines.append(f"    {request.get('response_command_template')}")
        lines.append("")

    next_action = panel.get("next_action", {})
    if isinstance(next_action, Mapping):
        lines.append("Next action:")
        lines.append(f"  Owner: {next_action.get('owner')}")
        lines.append(f"  Type: {next_action.get('type')}")
        lines.append(f"  Reason: {next_action.get('reason')}")
        if next_action.get("resume_command"):
            lines.append("  Command:")
            lines.append(f"    {next_action.get('resume_command')}")
        lines.append("")

    attention = panel.get("attention_by_classification", {})
    if isinstance(attention, Mapping) and attention:
        lines.append("Human-attention routing:")
        for key, value in sorted(attention.items()):
            lines.append(f"  - {key}: {value}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _operator_request(items: tuple[dict[str, object], ...], *, matter_scope: str) -> dict[str, object] | None:
    for item in items:
        route = route_human_attention(dict(item), matter_scope)
        if route.get("routed_owner") != "operator":
            continue
        attention_id = item.get("attention_id")
        question = str(item.get("plain_question") or item.get("reason") or "The harness needs operator input.")
        return {
            "attention_id": attention_id,
            "title": item.get("reason", "Operator input required"),
            "question": question,
            "why_needed": item.get("why_needed", ""),
            "acceptable_responses": item.get("acceptable_responses", []),
            "response_command_template": (
                f"python -m atticus.cli human-response submit --db DB --matter {matter_scope} "
                f"--attention-id {attention_id} --response-type <type> --statement <answer> --write"
            ),
        }
    return None


def _routed_attention_summary(items: tuple[dict[str, object], ...], *, matter_scope: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in items:
        route = route_human_attention(dict(item), matter_scope)
        key = f"{route.get('classification', 'unknown')}->{route.get('routed_owner', 'unknown')}"
        counter[key] += 1
    return dict(counter)


def _recommended_mode(next_action: Mapping[str, object], *, ask: Mapping[str, object] | None) -> dict[str, str]:
    if ask:
        return {"state": "needs_human_answer", "headline": "The harness has a concrete question for the operator."}
    owner = str(next_action.get("owner", ""))
    action_type = str(next_action.get("type", ""))
    command = str(next_action.get("resume_command", ""))
    if action_type == "complete":
        return {"state": "complete", "headline": "Matter completion requirements are satisfied."}
    if owner in LEGAL_REVIEW_OWNERS or action_type == "manual_reducer_review":
        return {"state": "needs_legal_review", "headline": "A high-risk legal/reducer decision must be reviewed before canonical write."}
    if owner in AUTO_OWNERS:
        return {"state": "agent_can_continue", "headline": "An agent or control panel can run the next command without asking the operator."}
    return {"state": "blocked", "headline": "The harness is blocked and needs triage."}


def _recommended_commands(*, db_path: str, matter_scope: str, output_dir: str, next_action: Mapping[str, object]) -> dict[str, str]:
    commands = {
        "status": f"python -m atticus.cli control-panel status --db {db_path} --matter {matter_scope}",
        "ask_next_human_question": f"python -m atticus.cli human-request next --db {db_path} --matter {matter_scope}",
        "cleanup_stale_attention_dry_run": f"python -m atticus.cli human-attention --db {db_path} --matter {matter_scope} --current-only --classify --cleanup --json",
        "cleanup_stale_attention_write": f"python -m atticus.cli human-attention --db {db_path} --matter {matter_scope} --current-only --classify --cleanup --write --json",
    }
    commands["continue_live"] = (
        f"ATTICUS_ENABLE_LIVE_OPENROUTER=1 python -m atticus.cli live-resume --db {db_path} --matter {matter_scope} "
        f"--probe --write --allow-live --execute-ticks 5 --output-dir {output_dir} --capacity 15"
    )
    if next_action.get("resume_command"):
        commands["next_exact"] = str(next_action["resume_command"])
    return commands


def _materialize_command(payload: object, *, db_path: str, output_dir: str) -> object:
    if not isinstance(payload, Mapping):
        return payload
    result = dict(payload)
    command = result.get("resume_command")
    if isinstance(command, str):
        result["resume_command"] = command.replace("DB", db_path).replace("OUT", output_dir)
    return result
