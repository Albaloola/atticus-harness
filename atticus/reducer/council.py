"""Council aggregation primitives."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import sqlite3
from typing import Any
from uuid import uuid4

from atticus.core.events import utc_now


@dataclass(frozen=True)
class CouncilDecision:
    decision: str
    selected_candidate_id: str | None
    rationale: str
    votes: list[dict[str, Any]]


def collect_votes(votes: list[dict]) -> dict:
    decision = reduce_votes(votes)
    return {"votes": votes, "count": len(votes), "decision": decision.decision, "selected_candidate_id": decision.selected_candidate_id}


def reduce_votes(votes: list[dict[str, Any]]) -> CouncilDecision:
    if not votes:
        return CouncilDecision("blocked", None, "no votes supplied", [])
    explicit_rejects = [vote for vote in votes if vote.get("vote") == "reject"]
    if explicit_rejects:
        return CouncilDecision("blocked", None, "one or more council roles rejected the packet", votes)
    candidates = [vote.get("candidate_id") for vote in votes if vote.get("candidate_id")]
    selected = Counter(candidates).most_common(1)[0][0] if candidates else None
    return CouncilDecision("accept" if selected else "needs_human_attention", selected, "majority candidate selected", votes)


def create_council_run(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    council_type: str,
    reducer_logic: str,
    task_id: str | None = None,
) -> str:
    council_run_id = f"council-{uuid4().hex}"
    now = utc_now()
    conn.execute(
        """
        INSERT INTO council_runs(council_run_id, matter_scope, task_id, council_type, status,
          reducer_logic, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (council_run_id, matter_scope, task_id, council_type, reducer_logic, now, now),
    )
    return council_run_id
