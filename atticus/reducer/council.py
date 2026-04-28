"""Council aggregation primitives."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import sqlite3

from uuid import uuid4

from atticus.core.events import utc_now


@dataclass(frozen=True)
class CouncilDecision:
    decision: str
    selected_candidate_id: str | None
    rationale: str
    votes: list[dict[str, object]]


def collect_votes(votes: list[dict[str, object]]) -> dict[str, object]:
    decision = reduce_votes(votes)
    return {"votes": votes, "count": len(votes), "decision": decision.decision, "selected_candidate_id": decision.selected_candidate_id}


def reduce_votes(votes: list[dict[str, object]]) -> CouncilDecision:
    if not votes:
        return CouncilDecision("blocked", None, "no votes supplied", [])
    explicit_rejects = [vote for vote in votes if vote.get("vote") == "reject"]
    if explicit_rejects:
        return CouncilDecision("blocked", None, "one or more council roles rejected the packet", votes)

    candidates = [str(vote.get("candidate_id")) for vote in votes if vote.get("candidate_id")]
    if not candidates:
        return CouncilDecision("needs_human_attention", None, "no candidate received an affirmative vote", votes)

    counts = Counter(candidates)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    top_candidate, top_count = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == top_count:
        tied = [candidate for candidate, count in ranked if count == top_count]
        return CouncilDecision("needs_human_attention", None, f"tie between candidates: {', '.join(tied)}", votes)

    required_majority = (len(votes) // 2) + 1
    if top_count < required_majority:
        return CouncilDecision(
            "needs_human_attention",
            None,
            f"no majority: {top_candidate} received {top_count}/{len(votes)} votes, required {required_majority}",
            votes,
        )
    return CouncilDecision("accept", top_candidate, f"unique majority selected {top_candidate} with {top_count}/{len(votes)} votes", votes)


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
    _ = conn.execute(
        """
        INSERT INTO council_runs(council_run_id, matter_scope, task_id, council_type, status,
          reducer_logic, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (council_run_id, matter_scope, task_id, council_type, reducer_logic, now, now),
    )
    return council_run_id
