"""Manual reducer review queue for high-risk legal candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import sqlite3
from uuid import uuid4

from atticus.agents.repair_planner import ensure_repair_plan_for_blocker
from atticus.core.events import utc_now
from atticus.db import repo
from atticus.reducer.reducer import reduce_candidate


@dataclass(frozen=True)
class ReducerReviewItem:
    reducer_review_id: str
    matter_scope: str
    candidate_id: str
    task_id: str
    stage: str
    task_type: str
    priority: int
    status: str
    reason: str
    recommended_action: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def enqueue_reducer_review(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    reason: str,
    priority: int = 50,
    recommended_action: str = "manual_reducer_review",
) -> ReducerReviewItem:
    row = conn.execute(
        """
        SELECT co.candidate_id, co.task_id, t.matter_scope, t.stage, t.task_type
        FROM candidate_outputs co
        JOIN tasks t ON t.task_id = co.task_id
        WHERE co.candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown candidate: {candidate_id}")
    now = utc_now()
    review_id = f"reducer-review-{uuid4().hex}"
    _ = conn.execute(
        """
        INSERT INTO reducer_review_queue(reducer_review_id, matter_scope, candidate_id, task_id,
          stage, task_type, priority, status, reason, recommended_action, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
        ON CONFLICT(candidate_id) DO UPDATE SET
          priority = MIN(reducer_review_queue.priority, excluded.priority),
          status = CASE WHEN reducer_review_queue.status IN ('accepted', 'rejected') THEN reducer_review_queue.status ELSE 'open' END,
          reason = excluded.reason,
          recommended_action = excluded.recommended_action,
          updated_at = excluded.updated_at
        """,
        (
            review_id,
            str(row["matter_scope"]),
            candidate_id,
            str(row["task_id"]),
            str(row["stage"]),
            str(row["task_type"]),
            priority,
            reason,
            recommended_action,
            now,
            now,
        ),
    )
    _ = repo.emit_event(
        conn,
        "reducer.review_queued",
        matter_scope=str(row["matter_scope"]),
        payload={
            "candidate_id": candidate_id,
            "task_id": str(row["task_id"]),
            "stage": str(row["stage"]),
            "task_type": str(row["task_type"]),
            "reason": reason,
            "recommended_action": recommended_action,
        },
    )
    item = get_reducer_review_by_candidate(conn, candidate_id)
    if item is None:
        raise RuntimeError("reducer review insert did not produce a readable row")
    return item


def list_reducer_reviews(conn: sqlite3.Connection, *, matter_scope: str, status: str = "open") -> tuple[ReducerReviewItem, ...]:
    rows = conn.execute(
        """
        SELECT *
        FROM reducer_review_queue
        WHERE matter_scope = ? AND (? = '' OR status = ?)
        ORDER BY priority ASC, updated_at ASC
        """,
        (matter_scope, status, status),
    ).fetchall()
    return tuple(_row_to_item(row) for row in rows)


def enqueue_open_reducer_reviews_for_matter(conn: sqlite3.Connection, *, matter_scope: str) -> tuple[ReducerReviewItem, ...]:
    rows = conn.execute(
        """
        SELECT co.candidate_id, t.stage, t.task_type
        FROM candidate_outputs co
        JOIN tasks t ON t.task_id = co.task_id
        WHERE t.matter_scope = ? AND t.status = 'reducer_pending' AND co.status = 'candidate'
        ORDER BY co.created_at ASC
        """,
        (matter_scope,),
    ).fetchall()
    items: list[ReducerReviewItem] = []
    for row in rows:
        stage = str(row["stage"])
        if stage not in {"S6", "S7", "S8", "S9"}:
            continue
        items.append(
            enqueue_reducer_review(
                conn,
                candidate_id=str(row["candidate_id"]),
                reason=f"high-risk legal stage {stage} requires manual reducer review",
                priority=_priority_for(stage=stage, task_type=str(row["task_type"])),
            )
        )
    return tuple(items)


def get_reducer_review_by_candidate(conn: sqlite3.Connection, candidate_id: str) -> ReducerReviewItem | None:
    row = conn.execute("SELECT * FROM reducer_review_queue WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return _row_to_item(row) if row is not None else None


def accept_reducer_review(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    reducer_lease_id: str,
    write: bool,
) -> dict[str, object]:
    item = get_reducer_review_by_candidate(conn, candidate_id)
    if item is None:
        raise ValueError(f"candidate is not queued for reducer review: {candidate_id}")
    if not write:
        return {"dry_run": True, "review": item.as_dict(), "would_reduce_candidate": candidate_id}
    reduction = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease_id, dry_run=False)
    _ = conn.execute(
        "UPDATE reducer_review_queue SET status = 'accepted', updated_at = ? WHERE candidate_id = ?",
        (utc_now(), candidate_id),
    )
    _ = repo.emit_event(
        conn,
        "reducer.review_accepted",
        matter_scope=item.matter_scope,
        payload={"candidate_id": candidate_id, "task_id": item.task_id, "reduction": reduction},
    )
    return {"dry_run": False, "review": get_reducer_review_by_candidate(conn, candidate_id).as_dict(), "reduction": reduction}


def reject_reducer_review(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    reason: str,
    write: bool,
) -> dict[str, object]:
    item = get_reducer_review_by_candidate(conn, candidate_id)
    if item is None:
        raise ValueError(f"candidate is not queued for reducer review: {candidate_id}")
    if not reason.strip():
        raise ValueError("reducer review rejection requires a reason")
    if not write:
        return {"dry_run": True, "review": item.as_dict(), "would_reject_candidate": candidate_id, "reason": reason}
    _ = conn.execute("UPDATE candidate_outputs SET status = 'quarantined', quarantined_reason = ? WHERE candidate_id = ?", (reason, candidate_id))
    _ = conn.execute(
        "UPDATE reducer_review_queue SET status = 'rejected', reason = ?, updated_at = ? WHERE candidate_id = ?",
        (reason, utc_now(), candidate_id),
    )
    plan = ensure_repair_plan_for_blocker(
        conn,
        matter_scope=item.matter_scope,
        target_type="candidate",
        target_id=candidate_id,
        reason=f"reducer review rejected: {reason}",
    )
    _ = repo.emit_event(
        conn,
        "reducer.review_rejected",
        matter_scope=item.matter_scope,
        payload={"candidate_id": candidate_id, "task_id": item.task_id, "reason": reason, "repair_plan_id": plan.repair_plan_id},
    )
    return {"dry_run": False, "review": get_reducer_review_by_candidate(conn, candidate_id).as_dict(), "repair_plan": plan.as_dict()}


def review_item_summary(conn: sqlite3.Connection, *, candidate_id: str) -> dict[str, object]:
    item = get_reducer_review_by_candidate(conn, candidate_id)
    if item is None:
        raise ValueError(f"candidate is not queued for reducer review: {candidate_id}")
    candidate = conn.execute("SELECT status, output_type, payload_json, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone()
    payload: Mapping[str, object] = {}
    if candidate is not None:
        try:
            raw = json.loads(str(candidate["payload_json"] or "{}"))
            if isinstance(raw, Mapping):
                payload = raw
        except json.JSONDecodeError:
            payload = {}
    citations = payload.get("citations")
    findings = payload.get("findings")
    risk_flags = payload.get("risk_flags")
    redaction_flags = payload.get("redaction_flags")
    return {
        **item.as_dict(),
        "candidate_status": str(candidate["status"]) if candidate is not None else "",
        "citations_count": len(citations) if isinstance(citations, list) else 0,
        "findings_count": len(findings) if isinstance(findings, list) else 0,
        "risk_flags_count": len(risk_flags) if isinstance(risk_flags, list) else 0,
        "redaction_flags_count": len(redaction_flags) if isinstance(redaction_flags, list) else 0,
        "could_unblock": _could_unblock(item.task_type),
    }


def _could_unblock(task_type: str) -> str:
    return {
        "citation_repair": "citation_audit or final_quality_gate",
        "citation_audit": "citation_audit",
        "final_quality_gate": "final_quality_gate",
        "privacy_redaction_audit": "privacy_redaction_audit",
        "hostile_opponent_review": "hostile_review",
        "authority_map": "authority_map",
    }.get(task_type, "")


def _priority_for(*, stage: str, task_type: str) -> int:
    if task_type in {"citation_repair", "citation_audit", "final_quality_gate"}:
        return 10
    if stage in {"S9", "S8"}:
        return 20
    if stage in {"S7", "S6"}:
        return 30
    return 50


def _row_to_item(row: sqlite3.Row) -> ReducerReviewItem:
    return ReducerReviewItem(
        reducer_review_id=str(row["reducer_review_id"]),
        matter_scope=str(row["matter_scope"]),
        candidate_id=str(row["candidate_id"]),
        task_id=str(row["task_id"]),
        stage=str(row["stage"]),
        task_type=str(row["task_type"]),
        priority=int(row["priority"]),
        status=str(row["status"]),
        reason=str(row["reason"]),
        recommended_action=str(row["recommended_action"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
