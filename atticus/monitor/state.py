"""Monitor state — one frozen dataclass assembled from product-level APIs.

The state builder calls ``build_operator_control_panel`` and a handful of
direct SQL queries so the TUI has one consistent snapshot to render.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import sqlite3


@dataclass(frozen=True)
class MonitorState:
    """Immutable snapshot of everything the TUI needs to render one frame."""

    matter: str
    panel: dict[str, object]
    active_run: dict[str, object] | None
    recent_events: tuple[dict[str, object], ...]
    leases: tuple[dict[str, object], ...]
    continuations: tuple[dict[str, object], ...]
    reducer_reviews: tuple[dict[str, object], ...]
    human_request: dict[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            "matter": self.matter,
            "state": self.panel.get("state"),
            "headline": self.panel.get("headline"),
            "done": self.panel.get("done"),
            "safe_to_finalize": self.panel.get("safe_to_finalize"),
            "counts": self.panel.get("counts", {}),
            "next_action": self.panel.get("next_action", {}),
            "agent_packet": self.panel.get("agent_packet", {}),
            "final_gate": self.panel.get("final_gate", {}),
            "operator_request": self.panel.get("operator_request"),
            "attention_by_classification": self.panel.get("attention_by_classification", {}),
            "recommended_commands": self.panel.get("recommended_commands", {}),
            "completion_invariant": self.panel.get("completion_invariant", {}),
            "active_run": self.active_run,
            "recent_events": list(self.recent_events),
            "leases": list(self.leases),
            "continuations": list(self.continuations),
            "reducer_reviews": [dict(r) for r in self.reducer_reviews],
            "human_request": self.human_request,
        }


def build_monitor_state(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    db_path: str,
    output_dir: str = "OUT",
    live_approved: bool = False,
) -> MonitorState:
    """Build a fresh MonitorState by calling product-level APIs.

    Prefer calling the existing high-level control-panel builder over
    duplicating internal logic.
    """
    from atticus.operator_control import build_operator_control_panel
    from atticus.reducer.review_queue import list_reducer_reviews
    from atticus.db import repo

    panel = build_operator_control_panel(
        conn,
        db_path=db_path,
        matter_scope=matter_scope,
        output_dir=output_dir,
        live_approved=live_approved,
    )

    active_run = _active_run_for_matter(conn, matter_scope)
    recent_events = _recent_events_for_matter(conn, matter_scope)
    leases = _leases_for_matter(conn, matter_scope)
    continuations = _continuations_for_matter(conn, matter_scope)
    reducer_reviews = list_reducer_reviews(conn, matter_scope=matter_scope, status="open")

    requests = repo.get_human_requests_for_matter(
        conn,
        matter_scope=matter_scope,
        current_only=True,
        lane="human_request",
        status="open",
    )
    human_request = requests[0] if requests else None

    return MonitorState(
        matter=matter_scope,
        panel=panel,
        active_run=active_run,
        recent_events=recent_events,
        leases=leases,
        continuations=continuations,
        reducer_reviews=tuple(reducer_reviews),
        human_request=human_request,
    )


def run_once(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    db_path: str,
    output_dir: str = "OUT",
) -> dict[str, object]:
    """Return the full monitor state as a JSON-safe dict.

    This is the entry point used by ``--once --json`` and also internally by
    the TUI on each refresh tick.
    """
    state = build_monitor_state(
        conn,
        matter_scope=matter_scope,
        db_path=db_path,
        output_dir=output_dir,
    )
    return state.as_dict()


# ---------------------------------------------------------------------------
# Internal helpers – direct SQL for data the control panel does not expose
# ---------------------------------------------------------------------------


def _active_run_for_matter(
    conn: sqlite3.Connection, matter_scope: str
) -> dict[str, object] | None:
    row = conn.execute(
        """SELECT run_id, matter_scope, state, reason, created_at, updated_at,
                  cancelled_by, cancelled_at, cancel_reason,
                  live_provider_permission_revoked
           FROM runs
           WHERE matter_scope = ? AND state IN ('running', 'active', 'initialized')
           ORDER BY created_at DESC LIMIT 1""",
        (matter_scope,),
    ).fetchone()
    return dict(row) if row else None


def _leases_for_matter(
    conn: sqlite3.Connection, matter_scope: str
) -> tuple[dict[str, object], ...]:
    rows = conn.execute(
        """SELECT l.lease_id, l.task_id, l.worker_id, l.lease_role,
                  l.status, l.fencing_token, l.expires_at, l.created_at,
                  t.title AS task_title, t.stage
           FROM leases l
           LEFT JOIN tasks t ON t.task_id = l.task_id
           WHERE t.matter_scope = ? AND l.status = 'active'
           ORDER BY l.created_at DESC""",
        (matter_scope,),
    ).fetchall()
    return tuple(dict(r) for r in rows)


def _continuations_for_matter(
    conn: sqlite3.Connection, matter_scope: str
) -> tuple[dict[str, object], ...]:
    rows = conn.execute(
        """SELECT continuation_id, run_id, command, approval_state,
                  owner, status, wake_at, created_at, executed_at
           FROM continued_commands
           WHERE matter_scope = ? AND status IN ('scheduled', 'pending')
           ORDER BY wake_at ASC""",
        (matter_scope,),
    ).fetchall()
    return tuple(dict(r) for r in rows)


def _recent_events_for_matter(
    conn: sqlite3.Connection,
    matter_scope: str,
    *,
    limit: int = 20,
) -> tuple[dict[str, object], ...]:
    rows = conn.execute(
        """SELECT event_id, event_type, actor, matter_scope, payload_json, created_at
           FROM events
           WHERE matter_scope = ?
           ORDER BY event_id DESC LIMIT ?""",
        (matter_scope, limit),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            payload = json.loads(str(d.get("payload_json", "{}")))
            d["payload"] = payload
        except (json.JSONDecodeError, TypeError):
            d["payload"] = {}
        result.append(d)
    # Return in chronological order
    result.reverse()
    return tuple(result)
