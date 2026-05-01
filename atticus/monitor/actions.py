"""Action handlers for the interactive monitor.

Each action is a function that takes the current ``MonitorState`` (and a DB
connection) and returns a command dict describing *what would happen*.

The TUI shows the command to the user for confirmation before executing it.
High-risk actions (stop run, accept reducer, write human response) always
require an explicit confirmation step.
"""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import cast

from atticus.monitor.state import MonitorState, build_monitor_state
from atticus.reducer.review_queue import ReducerReviewItem


def action_resume(
    conn: sqlite3.Connection,
    *,
    state: MonitorState,
    db_path: str,
    output_dir: str,
) -> dict[str, object]:
    """Return the command to resume safe harness work.

    Returns a command dict or None if no safe resume action exists.
    High-risk reducer/legal review is NOT auto-run — the TUI shows the
    review screen instead.
    """
    agent_packet = state.panel.get("agent_packet", {})
    if not isinstance(agent_packet, Mapping):
        return _blocked("agent packet unavailable")

    needs_legal = bool(agent_packet.get("needs_legal_review", False))
    if needs_legal:
        return _legal_review_required(state)

    if not agent_packet.get("may_run_without_asking_human", False):
        if agent_packet.get("needs_human", False):
            return _human_question_pending(state)
        return _blocked("agent packet reports cannot continue without human input")

    next_action = state.panel.get("next_action", {})
    if isinstance(next_action, Mapping):
        command = next_action.get("resume_command", "")
        reason = next_action.get("reason", "")
        owner = next_action.get("owner", "")
        action_type = next_action.get("type", "")
        if command:
            return {
                "can_run": True,
                "owner": owner,
                "type": action_type,
                "reason": reason,
                "command": command,
                "confirmation_required": False,
                "summary": f"Run {action_type} ({owner}): {reason[:80]}",
            }

    return _blocked("no runnable action in current state")


def action_stop(
    conn: sqlite3.Connection,
    *,
    state: MonitorState,
    db_path: str,
    matter_scope: str,
) -> dict[str, object]:
    """Return the stop command for the current active run.

    Confirmation is always required.  If there is no active run the TUI
    shows a message instead.
    """
    active_run = state.active_run
    if active_run is None:
        return {
            "can_run": False,
            "confirmation_required": False,
            "summary": "No active run to stop.",
        }

    run_id = str(active_run.get("run_id", ""))
    return {
        "can_run": True,
        "confirmation_required": True,
        "summary": f"Stop run {run_id[:12]}",
        "command": f"python -m atticus.cli run stop --db {db_path} --run-id {run_id} --write",
        "stop_run_id": run_id,
        "dry_run_prompt": (
            f"This will cancel run {run_id[:12]}, revoke live provider"
            f" approval, cancel continuations, and release leases."
        ),
    }


def action_answer_human(
    conn: sqlite3.Connection,
    *,
    state: MonitorState,
    db_path: str,
    matter_scope: str,
    response_type: str = "",
    statement: str = "",
) -> dict[str, object]:
    """Return a human-response submit command for the current request."""
    agent_packet = state.panel.get("agent_packet", {})
    if not isinstance(agent_packet, Mapping):
        return _no_human_request()

    if not agent_packet.get("needs_human", False):
        return _no_human_request()

    operator_request = state.panel.get("operator_request")
    if not isinstance(operator_request, Mapping):
        return _no_human_request()

    attention_id = operator_request.get("attention_id", 0)
    question = str(operator_request.get("question", "No question text"))
    response_template = str(operator_request.get("response_command_template", ""))

    return {
        "can_run": True,
        "confirmation_required": True,
        "attention_id": attention_id,
        "question": question,
        "summary": f"Answer human request #{attention_id}: {question[:80]}",
        "response_types": operator_request.get("acceptable_responses", []),
        "response_command_template": response_template,
    }


def action_show_final_gate(state: MonitorState) -> dict[str, object]:
    """Return the final-gate detail block for display."""
    final_gate = state.panel.get("final_gate", {})
    if not isinstance(final_gate, Mapping):
        return {"summary": "Final gate information unavailable."}
    return {
        "state": final_gate.get("state"),
        "ready": final_gate.get("ready"),
        "complete": final_gate.get("complete"),
        "open_human_attention_count": final_gate.get("open_human_attention_count"),
        "missing_certifications": final_gate.get("missing_certifications", []),
        "blocked_reasons": final_gate.get("blocked_reasons", []),
        "next_action": final_gate.get("next_action", {}),
        "summary": (
            f"Final gate: {final_gate.get('state', 'unknown')} — "
            f"ready={final_gate.get('ready')}, complete={final_gate.get('complete')}"
        ),
    }


def action_show_reducer_reviews(
    state: MonitorState,
) -> dict[str, object]:
    """Return reducer-review queue details."""
    reviews = state.reducer_reviews
    if not reviews:
        return {"summary": "No open reducer reviews.", "reviews": []}

    return {
        "summary": f"{len(reviews)} open reducer review(s)",
        "reviews": [r if isinstance(r, dict) else _review_as_dict(r) for r in reviews],
    }


def action_show_leases(state: MonitorState) -> dict[str, object]:
    """Return active lease details."""
    leases = state.leases
    if not leases:
        return {"summary": "No active leases.", "leases": []}
    return {
        "summary": f"{len(leases)} active lease(s)",
        "leases": [dict(l) if isinstance(l, Mapping) else l for l in leases],
    }


def action_show_continuations(state: MonitorState) -> dict[str, object]:
    """Return pending continuation details."""
    continuations = state.continuations
    if not continuations:
        return {"summary": "No pending continuations.", "continuations": []}
    return {
        "summary": f"{len(continuations)} pending continuation(s)",
        "continuations": [
            dict(c) if isinstance(c, Mapping) else c for c in continuations
        ],
    }


def action_show_command(state: MonitorState) -> dict[str, object]:
    """Return the exact next-action command for display/copying."""
    next_action = state.panel.get("next_action", {})
    if not isinstance(next_action, Mapping):
        return {"summary": "No next action available."}
    command = next_action.get("resume_command", "")
    if not command:
        return {"summary": "No exact command in current next action."}
    return {
        "command": command,
        "owner": next_action.get("owner"),
        "type": next_action.get("type"),
        "reason": next_action.get("reason"),
        "summary": command,
    }


def action_refresh(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    db_path: str,
    output_dir: str,
) -> MonitorState:
    """Refresh the monitor state — called on 'R' press and auto-refresh."""
    return build_monitor_state(
        conn,
        matter_scope=matter_scope,
        db_path=db_path,
        output_dir=output_dir,
    )


def action_execute_stop(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    reason: str = "operator requested stop",
    revoke_live: bool = False,
) -> dict[str, object]:
    """Actually execute the run stop when the user confirms."""
    from atticus.db import repo

    cancel_result = repo.cancel_run(
        conn,
        run_id=run_id,
        cancelled_by="operator",
        cancel_reason=reason,
        revoke_live=revoke_live,
    )
    cancelled_continuations = repo.cancel_continuations_for_run(conn, run_id=run_id)

    released_leases: list[str] = []
    run_prefix = run_id[:8]
    lease_rows = conn.execute(
        "SELECT lease_id FROM leases WHERE lease_id LIKE ? AND status='active'",
        (f"%{run_prefix}%",),
    ).fetchall()
    for row in lease_rows:
        conn.execute(
            "UPDATE leases SET status='cancelled' WHERE lease_id=?",
            (str(row["lease_id"]),),
        )
        released_leases.append(str(row["lease_id"]))

    return {
        "run_id": run_id,
        "cancelled": cancel_result.get("cancelled", False),
        "cancelled_continuations": len(cancelled_continuations),
        "released_leases": len(released_leases),
        "summary": f"Run {run_id[:12]} cancelled.",
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _review_as_dict(item: ReducerReviewItem | dict[str, object]) -> dict[str, object]:
    if isinstance(item, dict):
        return item
    return item.as_dict()


def _blocked(reason: str) -> dict[str, object]:
    return {
        "can_run": False,
        "confirmation_required": False,
        "summary": reason,
    }


def _no_human_request() -> dict[str, object]:
    return {
        "can_run": False,
        "confirmation_required": False,
        "summary": "No human request pending.",
    }


def _legal_review_required(state: MonitorState) -> dict[str, object]:
    return {
        "can_run": False,
        "confirmation_required": False,
        "requires_legal_review": True,
        "summary": (
            "High-risk reducer/legal review is pending. "
            "Open the reducer review screen to review before continuing."
        ),
    }


def _human_question_pending(state: MonitorState) -> dict[str, object]:
    operator_request = state.panel.get("operator_request", {})
    if isinstance(operator_request, Mapping):
        question = str(operator_request.get("question", ""))
    else:
        question = ""
    return {
        "can_run": False,
        "confirmation_required": False,
        "requires_human_answer": True,
        "summary": f"Human question pending: {question[:120]}" if question else "Human question pending.",
    }
