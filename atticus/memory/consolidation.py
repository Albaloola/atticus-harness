"""Dry-run-first case memory consolidation."""

from __future__ import annotations

from collections.abc import Mapping
import re
import sqlite3
from typing import cast

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def consolidate_case_memory(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    dry_run: bool = True,
) -> dict[str, object]:
    memories = repo.list_legal_memories(conn, matter_scope=matter_scope, status=None)
    active_memories = [memory for memory in memories if memory.get("status") == "active"]
    candidate_memories = [memory for memory in memories if memory.get("status") == "candidate"]
    stale_memories = [memory for memory in memories if memory.get("stale")]
    contradictions = [memory for memory in memories if memory.get("type") == "contradiction" and memory.get("status") != "rejected"]
    duplicate_groups = _duplicate_groups(memories)
    validation_failures = _recent_validation_failures(conn)
    open_attention = _open_attention(conn)
    reduced_candidates = _reduced_candidate_count(conn, matter_scope=matter_scope)

    proposed_tasks = []
    if stale_memories or contradictions or duplicate_groups or candidate_memories:
        proposed_tasks.append(_review_task(matter_scope=matter_scope, stale_memories=stale_memories, contradictions=contradictions, duplicate_groups=duplicate_groups, candidate_memories=candidate_memories))

    existing = _existing_task_ids(conn, [str(task["task_id"]) for task in proposed_tasks])
    created: list[str] = []
    if not dry_run:
        repo.ensure_matter(conn, matter_scope)
        for task in proposed_tasks:
            task_id = str(task["task_id"])
            if task_id in existing:
                continue
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=task_id,
                    title=str(task["title"]),
                    task_type=str(task["task_type"]),
                    instructions=str(task["instructions"]),
                    matter_scope=matter_scope,
                    stage=LegalStage.S7_HOSTILE_REVIEW,
                    status=TaskStatus.QUEUED,
                    source_dependencies=[],
                    artifact_dependencies=[],
                    validation_gates=["memory_source_refs", "contradiction_detection", "stale_memory_review"],
                ),
            )
            created.append(task_id)
        _ = repo.emit_event(
            conn,
            "legal_memory.consolidation_planned",
            matter_scope=matter_scope,
            payload={"created_task_ids": created, "existing_task_ids": sorted(existing)},
        )

    return {
        "dry_run": dry_run,
        "matter_scope": matter_scope,
        "orient": {
            "memory_count": len(memories),
            "active_memory_count": len(active_memories),
            "candidate_memory_count": len(candidate_memories),
            "stale_memory_count": len(stale_memories),
            "open_contradiction_count": len(contradictions),
        },
        "gather_signal": {
            "recent_validation_failures": validation_failures,
            "open_human_attention": open_attention,
            "reduced_candidate_count": reduced_candidates,
        },
        "consolidate": {
            "duplicate_memory_groups": duplicate_groups,
            "stale_memory_ids": [str(memory["memory_id"]) for memory in stale_memories],
            "candidate_memory_ids": [str(memory["memory_id"]) for memory in candidate_memories],
            "contradiction_memory_ids": [str(memory["memory_id"]) for memory in contradictions],
        },
        "proposed_tasks": proposed_tasks,
        "existing_task_ids": sorted(existing),
        "created_task_ids": created,
        "canonical_memory_mutated": False,
    }


def _review_task(
    *,
    matter_scope: str,
    stale_memories: list[dict[str, object]],
    contradictions: list[dict[str, object]],
    duplicate_groups: list[dict[str, object]],
    candidate_memories: list[dict[str, object]],
) -> dict[str, object]:
    task_id = _safe_component(f"{matter_scope}-memory-consolidation-review")
    stale_ids = [str(memory["memory_id"]) for memory in stale_memories]
    contradiction_ids = [str(memory["memory_id"]) for memory in contradictions]
    candidate_ids = [str(memory["memory_id"]) for memory in candidate_memories]
    return {
        "task_id": task_id,
        "matter_scope": matter_scope,
        "task_type": "memory_consolidation_review",
        "stage": str(LegalStage.S7_HOSTILE_REVIEW),
        "title": "Review candidate, stale, duplicate, and contradictory legal memory",
        "instructions": (
            f"Memory consolidation review for matter_scope: {matter_scope}. "
            f"Candidate memory ids: {', '.join(candidate_ids) if candidate_ids else 'none'}. "
            f"Stale memory ids: {', '.join(stale_ids) if stale_ids else 'none'}. "
            f"Contradiction memory ids: {', '.join(contradiction_ids) if contradiction_ids else 'none'}. "
            f"Duplicate groups: {duplicate_groups if duplicate_groups else 'none'}. "
            "Produce worker_result_packet.v2 candidate, not canonical output. "
            "Do not silently activate, delete, merge, certify, or overwrite memory. "
            "Recommend explicit accept/reject/mark-stale follow-up actions with citations. "
            "Memory is an operational aid, not proof; current evidence and validation records control. "
            "Do not send, file, serve, upload, email, contact, message, or perform external legal actions."
        ),
    }


def _duplicate_groups(memories: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for memory in memories:
        if memory.get("status") == "rejected":
            continue
        key = (str(memory.get("type") or ""), str(memory.get("name") or "").strip().lower())
        groups.setdefault(key, []).append(str(memory["memory_id"]))
    return [
        {"type": key[0], "normalized_name": key[1], "memory_ids": ids}
        for key, ids in sorted(groups.items())
        if len(ids) > 1 and key[1]
    ]


def _recent_validation_failures(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT validation_result_id, target_type, target_id, gate_name, severity
        FROM validation_results
        WHERE passed = 0
        ORDER BY validation_result_id DESC
        LIMIT 20
        """
    ).fetchall()
    return [dict(cast(Mapping[str, object], row)) for row in rows]


def _open_attention(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT attention_id, target_type, target_id, severity, reason
        FROM human_attention
        WHERE status = 'open'
        ORDER BY attention_id DESC
        LIMIT 20
        """
    ).fetchall()
    return [dict(cast(Mapping[str, object], row)) for row in rows]


def _reduced_candidate_count(conn: sqlite3.Connection, *, matter_scope: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM candidate_outputs co
        JOIN tasks t ON t.task_id = co.task_id
        WHERE t.matter_scope = ? AND co.status = 'reduced'
        """,
        (matter_scope,),
    ).fetchone()
    return int(str(row["n"] if row is not None else 0))


def _existing_task_ids(conn: sqlite3.Connection, task_ids: list[str]) -> set[str]:
    if not task_ids:
        return set()
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE task_id IN (%s)" % ",".join("?" for _ in task_ids),
        tuple(task_ids),
    ).fetchall()
    return {str(row["task_id"]) for row in rows}


def _safe_component(value: str) -> str:
    return _SAFE_ID_RE.sub("-", value.strip()).strip(".-") or "memory-consolidation-review"
