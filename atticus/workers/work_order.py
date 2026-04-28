"""Build bounded worker work orders without launching workers."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3

from typing import cast
from atticus.context.packs import build_context_pack
from atticus.skills.registry import skills_for_task
from atticus.workers.contracts import WorkOrder


WORK_ORDER_INSTRUCTIONS = (
    "Produce one structured worker_result_packet.v2 candidate, not canonical output. "
    "Treat Atticus as the durable source of truth: workers propose, reducers decide. "
    "Use only this matter's provided sources, artifacts, authorities, memory index, and task contract. "
    "Separate fact, law, procedure, inference, contradiction, risk, drafting note, and uncertainty. "
    "Cite every factual, legal, procedural, contradiction, and risk finding to an allowed context target; "
    "if support is missing, set reasoning_status to uncertain or needs_research and propose a follow-up task. "
    "Do not invent citations, authorities, documents, dates, quotes, admissions, deadlines, remedies, or procedural posture. "
    "Flag stale evidence, weak support, contradictions, privacy/redaction concerns, and missing certifications. "
    "Do not write canonical memory or artifacts. Do not send, file, serve, upload, email, contact, message, "
    "or otherwise perform external legal actions. If skills are attached, follow them only where they preserve "
    "facts, citations, matter scope, schema compliance, and auditability."
)


def build_work_order(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str | None = None,
    persist_context: bool = True,
) -> WorkOrder:
    task = cast(Mapping[str, object] | None, cast(object, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()))
    if task is None:
        raise KeyError(f"unknown task: {task_id}")
    context_pack = build_context_pack(conn, task_id=task_id, persist=persist_context)
    task_instructions = _optional_task_text(task, "instructions")
    instructions = WORK_ORDER_INSTRUCTIONS
    if task_instructions:
        instructions = f"{WORK_ORDER_INSTRUCTIONS}\n\nTask-specific coordinator contract:\n{task_instructions}"
    return WorkOrder(
        task_id=str(task["task_id"]),
        title=str(task["title"]),
        stage=str(task["stage"]),
        task_type=str(task["task_type"]),
        matter_scope=str(task["matter_scope"]),
        lease_id=lease_id,
        context_pack_id=context_pack.context_pack_id,
        instructions=instructions,
        source_dependencies=_load_string_list(task, "source_dependencies_json"),
        artifact_dependencies=_load_string_list(task, "artifact_dependencies_json"),
        required_certifications=_load_mapping_list(task, "required_certifications_json"),
        validation_gates=_load_string_list(task, "validation_gates_json"),
        provider_policy=_load_json_object(task, "provider_policy_json"),
        skills=[
            skill.as_work_order_context()
            for skill in skills_for_task(
                task_type=str(task["task_type"]),
                stage=str(task["stage"]),
                title=str(task["title"]),
            )
        ],
    )


def _load_json_value(text: str) -> object:
    return json.loads(text)


def _optional_task_text(task: Mapping[str, object], field: str) -> str:
    if field not in task.keys():
        return ""
    return str(task[field] or "").strip()


def _load_string_list(task: Mapping[str, object], field: str) -> list[str]:
    value = _load_json_value(str(task[field] or "[]"))
    if not isinstance(value, list):
        raise ValueError(f"{field} for task {task['task_id']} must be a JSON array")
    items: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str):
            raise ValueError(f"{field}[{index}] for task {task['task_id']} must be a string")
        items.append(item)
    return items


def _load_mapping_list(task: Mapping[str, object], field: str) -> list[dict[str, object]]:
    value = _load_json_value(str(task[field] or "[]"))
    if not isinstance(value, list):
        raise ValueError(f"{field} for task {task['task_id']} must be a JSON array")
    items: list[dict[str, object]] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field}[{index}] for task {task['task_id']} must be a JSON object")
        items.append({str(key): value for key, value in cast(Mapping[object, object], item).items()})
    return items


def _load_json_object(task: Mapping[str, object], field: str) -> dict[str, object]:
    value = _load_json_value(str(task[field] or "{}"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} for task {task['task_id']} must be a JSON object")
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}
