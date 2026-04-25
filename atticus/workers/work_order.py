"""Build bounded worker work orders without launching workers."""

from __future__ import annotations

import json
import sqlite3

from atticus.context.packs import build_context_pack
from atticus.workers.contracts import WorkOrder


def build_work_order(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str | None = None,
    persist_context: bool = True,
) -> WorkOrder:
    task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task is None:
        raise KeyError(f"unknown task: {task_id}")
    context_pack = build_context_pack(conn, task_id=task_id, persist=persist_context)
    return WorkOrder(
        task_id=task["task_id"],
        title=task["title"],
        stage=task["stage"],
        task_type=task["task_type"],
        matter_scope=task["matter_scope"],
        lease_id=lease_id,
        context_pack_id=context_pack.context_pack_id,
        instructions=(
            "Produce a structured worker result packet only. Do not write canonical memory. "
            "Do not send, file, upload, email, contact, or otherwise perform external legal actions."
        ),
        source_dependencies=json.loads(task["source_dependencies_json"]),
        artifact_dependencies=json.loads(task["artifact_dependencies_json"]),
        required_certifications=json.loads(task["required_certifications_json"]),
        validation_gates=json.loads(task["validation_gates_json"]),
        provider_policy=json.loads(task["provider_policy_json"]),
    )
