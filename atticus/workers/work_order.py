"""Build bounded worker work orders without launching workers."""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3

from typing import cast
from atticus.context.packs import build_context_pack
from atticus.context.sections import UNTRUSTED_EVIDENCE_BOUNDARY, context_provider_policy, context_task_instructions
from atticus.skills.registry import skills_for_task
from atticus.workers.contracts import WorkOrder


WORK_ORDER_INSTRUCTIONS = (
    "Produce one structured worker_result_packet.v2 candidate, not canonical output. "
    "Treat Atticus as the durable source of truth: workers propose, reducers decide. "
    "Use only this matter's provided sources, artifacts, authorities, memory index, and task contract. "
    f"{UNTRUSTED_EVIDENCE_BOUNDARY} "
    "Separate fact, law, procedure, inference, contradiction, risk, drafting note, and uncertainty. "
    "Return compact JSON only; for broad evidence-map or source-review tasks, capture only the strongest supported "
    "findings first and propose bounded follow-up tasks for expansion, missing detail, or low-confidence OCR instead "
    "of exhausting the output budget. For broad tasks, return at most 4 findings, 6 citations, 3 uncertainties, "
    "3 risk_flags, 3 redaction_flags, and 1 proposed_task. Keep summary under 600 characters, citation quotes under "
    "180 characters, finding text under 280 characters, and proposed_artifacts[0].content under 1200 characters. "
    "Cite every factual, legal, procedural, contradiction, and risk finding to an allowed context target; "
    "when using source_materials or extracted/OCR text, cite the source_id as target_type='source' rather than "
    "the generated extraction artifact unless that artifact is explicitly allowed in citation_targets. "
    "If support is missing, set reasoning_status to uncertain or needs_research and propose a follow-up task. "
    "Use finding_type='procedure' only for source-supported legal, court, university, or administrative procedure; "
    "use finding_type='drafting_note' with reasoning_status='uncertain' for harness limitations, task feasibility, "
    "tool availability, OCR capability gaps, or recommended operational next steps. "
    "Do not propose tasks requiring unconfigured external tools or services such as cloud OCR, email, filing, upload, "
    "or contact workflows; request human/tool setup instead. "
    "Do not invent citations, authorities, documents, dates, quotes, admissions, deadlines, remedies, or procedural posture. "
    "Do not include quoted_text_hash unless the work order provides the exact SHA-256 hex digest; never guess, summarize, "
    "or placeholder a hash. "
    "Flag stale evidence, weak support, contradictions, privacy/redaction concerns, and missing certifications. "
    "The selected provider/model and fallback policy are fixed by Atticus policy; do not request another model, "
    "enable fallback, or route through held/free/reserved providers. Cache telemetry may explain cost, never truth. "
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
    task_instructions = context_task_instructions(task)
    instructions = WORK_ORDER_INSTRUCTIONS
    if task_instructions:
        instructions = f"{WORK_ORDER_INSTRUCTIONS}\n\nTask-specific coordinator contract:\n{task_instructions}"
    raw_provider_policy = _load_json_object(task, "provider_policy_json")
    provider_policy = context_provider_policy(raw_provider_policy)
    model_decision = raw_provider_policy.get("model_decision")
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
        provider_policy=provider_policy,
        model_decision=cast(dict[str, object], model_decision) if isinstance(model_decision, Mapping) else {},
        model_decision_reason=str(provider_policy.get("model_decision_reason") or ""),
        context_pack=context_pack.as_dict(),
        skills=[
            skill.as_work_order_context()
            for skill in skills_for_task(
                task_type=str(task["task_type"]),
                stage=str(task["stage"]),
                title=str(task["title"]),
            )
        ],
    )


def _optional_task_text(task: Mapping[str, object], field: str) -> str:
    if field not in task.keys():
        return ""
    return str(task[field] or "").strip()


def _load_string_list(task: Mapping[str, object], field: str) -> list[str]:
    value = json.loads(str(task[field] or "[]"))
    if not isinstance(value, list):
        raise ValueError(f"{field} for task {task['task_id']} must be a JSON array")
    items: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str):
            raise ValueError(f"{field}[{index}] for task {task['task_id']} must be a string")
        items.append(item)
    return items


def _load_mapping_list(task: Mapping[str, object], field: str) -> list[dict[str, object]]:
    value = json.loads(str(task[field] or "[]"))
    if not isinstance(value, list):
        raise ValueError(f"{field} for task {task['task_id']} must be a JSON array")
    items: list[dict[str, object]] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field}[{index}] for task {task['task_id']} must be a JSON object")
        items.append({str(key): value for key, value in cast(Mapping[object, object], item).items()})
    return items


def _load_json_object(task: Mapping[str, object], field: str) -> dict[str, object]:
    value = json.loads(str(task[field] or "{}"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} for task {task['task_id']} must be a JSON object")
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}
