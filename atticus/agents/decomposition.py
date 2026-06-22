"""Safe decomposition for oversized matter-scoped worker tasks."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import sqlite3
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.context.token_budget import (
    SourceTokenEstimate,
    bundle_token_total,
    source_token_estimates,
    token_balanced_source_bundles,
)
from atticus.db import repo
from atticus.providers.model_policy import resolve_provider_policy_from_parent


BROAD_TASK_SOURCE_THRESHOLD = 25
BROAD_TASK_MAX_SOURCES_PER_BUNDLE = 6
BROAD_TASK_TARGET_SOURCE_TOKENS = 6_000
BROAD_TASK_MAX_OUTPUT_TOKENS = 4096
DECOMPOSABLE_BROAD_TASK_TYPES = {
    "evidence_issue_map",
    "evidence_organization_plan",
    "production_mapping",
}
DECOMPOSITION_FAILURE_MARKERS = (
    "unterminated string",
    "response did not contain a json message",
    "exhausted max_tokens",
    "finish_reason",
    "output token limit",
    "max_output_tokens",
    "provider call timed out",
    "prompt_too_long",
    "context pack exceeds token budget",
)
DECOMPOSITION_PROVENANCE_KEY = "source_bundle_decomposition"
SYNTHESIS_COMPACT_RETRY_KEY = "synthesis_compact_retry"


def broad_task_decomposition_candidate(task: Mapping[str, object], *, reason: str = "") -> bool:
    """Return true when a task should be split before another provider attempt."""

    if str(_mapping_value(task, "task_type") or "") not in DECOMPOSABLE_BROAD_TASK_TYPES:
        return False
    source_ids = _json_list(str(_mapping_value(task, "source_dependencies_json") or "[]"))
    if len(source_ids) <= BROAD_TASK_SOURCE_THRESHOLD:
        return False
    provenance = _json_object(str(_mapping_value(task, "task_provenance_json") or "{}"))
    decomposition = provenance.get(DECOMPOSITION_PROVENANCE_KEY)
    if isinstance(decomposition, Mapping) and decomposition.get("role") in {"parent", "child"}:
        return False
    if not reason:
        return True
    reason_text = reason.lower()
    return any(marker in reason_text for marker in DECOMPOSITION_FAILURE_MARKERS) or reason_text == "pre_dispatch_token_budget"


def decompose_broad_task_if_needed(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    reason: str,
    chunk_size: int = BROAD_TASK_MAX_SOURCES_PER_BUNDLE,
    write: bool = True,
) -> dict[str, object]:
    """Split one broad task into bounded source-bundle children.

    The parent becomes a synthesis task that depends on the child tasks. Children
    stay candidate-only and cite raw sources; the reducer is still the only
    canonical writer.
    """

    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    existing = _existing_decomposition(task)
    if existing:
        return {
            "applied": False,
            "reason": "already_decomposed",
            "task_id": task_id,
            "child_task_ids": existing.get("child_task_ids") or [],
        }
    if not broad_task_decomposition_candidate(task, reason=reason):
        return {
            "applied": False,
            "reason": "not_decomposable",
            "task_id": task_id,
            "source_count": len(_json_list(str(task["source_dependencies_json"] or "[]"))),
        }
    source_ids = _json_list(str(task["source_dependencies_json"] or "[]"))
    estimates = source_token_estimates(
        conn,
        matter_scope=str(task["matter_scope"]),
        source_ids=source_ids,
    )
    chunks = token_balanced_source_bundles(
        source_ids,
        estimates,
        target_tokens=BROAD_TASK_TARGET_SOURCE_TOKENS,
        max_sources_per_bundle=max(1, chunk_size),
    )
    child_task_ids = [_child_task_id(task_id, index, chunk) for index, chunk in enumerate(chunks, start=1)]
    result = {
        "applied": write,
        "task_id": task_id,
        "matter_scope": str(task["matter_scope"]),
        "task_type": str(task["task_type"]),
        "source_count": len(source_ids),
        "target_source_tokens": BROAD_TASK_TARGET_SOURCE_TOKENS,
        "max_sources_per_bundle": chunk_size,
        "child_task_ids": child_task_ids,
        "source_token_estimates": [estimate.as_dict() for estimate in estimates],
        "bundle_token_estimates": [
            {"bundle_index": index, "source_count": len(chunk), "estimated_source_tokens": bundle_token_total(chunk, estimates)}
            for index, chunk in enumerate(chunks, start=1)
        ],
        "reason": reason,
    }
    if not write:
        return result

    now = utc_now()
    child_task_type = _child_task_type(str(task["task_type"]))
    for index, chunk in enumerate(chunks, start=1):
        child_id = child_task_ids[index - 1]
        estimated_source_tokens = bundle_token_total(chunk, estimates)
        provider_policy = _child_provider_policy(
            _json_object(str(task["provider_policy_json"] or "{}")),
            child_task={
                "task_id": child_id,
                "task_type": child_task_type,
                "stage": str(task["stage"]),
                "matter_scope": str(task["matter_scope"]),
                "expected_value": _child_expected_value(task, len(chunks)),
                "validation_gates": _json_list(str(task["validation_gates_json"] or "[]")),
                "source_count": len(chunk),
                "extracted_char_count": estimated_source_tokens * 4,
            },
            chunk_count=len(chunks),
        )
        if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (child_id,)).fetchone() is None:
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=child_id,
                    title=f"Source bundle {index}/{len(chunks)}: {task['title']}",
                    task_type=child_task_type,
                    instructions=_child_instructions(
                        parent_task=task,
                        index=index,
                        total=len(chunks),
                        source_count=len(chunk),
                        estimated_source_tokens=estimated_source_tokens,
                        source_estimates=[estimate for estimate in estimates if estimate.source_id in set(chunk)],
                    ),
                    matter_scope=str(task["matter_scope"]),
                    stage=cast(object, task["stage"]),  # type: ignore[arg-type]
                    status=TaskStatus.QUEUED,
                    source_dependencies=chunk,
                    artifact_dependencies=[],
                    task_dependencies=[],
                    matter_dependencies=_json_list(str(task["matter_dependencies_json"] or "[]")),
                    required_certifications=_json_mapping_list(str(task["required_certifications_json"] or "[]")),
                    validation_gates=_json_list(str(task["validation_gates_json"] or "[]")),
                    staleness_rules=_json_object(str(task["staleness_rules_json"] or "{}")),
                    provider_policy=provider_policy,
                    cost_limit_usd=_optional_float(task["cost_limit_usd"]),
                    expected_value=_child_expected_value(task, len(chunks)),
                ),
            )
        _ = conn.execute(
            """
            UPDATE tasks
            SET parent_task_id = ?, task_provenance_json = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                task_id,
                _json(
                    {
                        DECOMPOSITION_PROVENANCE_KEY: {
                            "role": "child",
                            "parent_task_id": task_id,
                            "bundle_index": index,
                            "bundle_count": len(chunks),
                            "source_count": len(chunk),
                            "estimated_source_tokens": estimated_source_tokens,
                            "source_fingerprint": _fingerprint_strings(chunk),
                            "reason": reason,
                        }
                    }
                ),
                now,
                child_id,
            ),
        )

    parent_provenance = _json_object(str(task["task_provenance_json"] or "{}"))
    parent_provenance[DECOMPOSITION_PROVENANCE_KEY] = {
        "role": "parent",
        "reason": reason,
        "source_count": len(source_ids),
        "target_source_tokens": BROAD_TASK_TARGET_SOURCE_TOKENS,
        "max_sources_per_bundle": chunk_size,
        "source_fingerprint": _fingerprint_strings(source_ids),
        "original_source_dependencies": source_ids,
        "child_task_ids": child_task_ids,
        "source_token_estimates": [estimate.as_dict() for estimate in estimates],
        "bundle_token_estimates": result["bundle_token_estimates"],
        "created_at": now,
    }
    parent_policy = _parent_provider_policy(_json_object(str(task["provider_policy_json"] or "{}")))
    _ = conn.execute(
        """
        UPDATE tasks
        SET source_dependencies_json = '[]',
            task_dependencies_json = ?,
            instructions = ?,
            provider_policy_json = ?,
            task_provenance_json = ?,
            updated_at = ?
        WHERE task_id = ?
        """,
        (
            _json(child_task_ids),
            _parent_synthesis_instructions(task, child_count=len(child_task_ids), reason=reason),
            _json(parent_policy),
            _json(parent_provenance),
            now,
            task_id,
        ),
    )
    repo.update_task_blocked(conn, task_id, [f"incomplete task dependency: {child_id}" for child_id in child_task_ids])
    _ = repo.emit_event(
        conn,
        "task.decomposed",
        matter_scope=str(task["matter_scope"]),
        payload=result,
    )
    return result


def decomposition_repair_action(conn: sqlite3.Connection, *, task_id: str, reasons: list[str]) -> dict[str, object] | None:
    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        return None
    reason = " ".join(reasons)
    if not broad_task_decomposition_candidate(task, reason=reason):
        return None
    source_count = len(_json_list(str(task["source_dependencies_json"] or "[]")))
    return {
        "type": "source_bundle_decomposition",
        "task_type": _child_task_type(str(task["task_type"])),
        "source_count": source_count,
        "target_source_tokens": BROAD_TASK_TARGET_SOURCE_TOKENS,
        "max_sources_per_bundle": BROAD_TASK_MAX_SOURCES_PER_BUNDLE,
        "estimated_child_tasks": (source_count + BROAD_TASK_MAX_SOURCES_PER_BUNDLE - 1) // BROAD_TASK_MAX_SOURCES_PER_BUNDLE,
        "reason": "provider/context output budget requires bounded source bundles before retry",
    }


def compact_decomposed_parent_if_needed(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    reason: str,
    write: bool = True,
) -> dict[str, object]:
    """Tighten a decomposed parent synthesis after overlong JSON failures."""

    task = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    provenance = _json_object(str(_mapping_value(task, "task_provenance_json") or "{}"))
    decomposition = provenance.get(DECOMPOSITION_PROVENANCE_KEY)
    if not isinstance(decomposition, Mapping) or decomposition.get("role") != "parent":
        return {"applied": False, "reason": "not_decomposed_parent", "task_id": task_id}
    if not _compact_parent_retry_candidate(reason):
        return {"applied": False, "reason": "not_compact_retry_failure", "task_id": task_id}
    if isinstance(provenance.get(SYNTHESIS_COMPACT_RETRY_KEY), Mapping):
        return {"applied": False, "reason": "already_compacted", "task_id": task_id}
    result = {
        "applied": write,
        "task_id": task_id,
        "matter_scope": str(task["matter_scope"]),
        "reason": reason,
        "max_tokens": 2048,
    }
    if not write:
        return result

    now = utc_now()
    provider_policy = _parent_provider_policy(_json_object(str(task["provider_policy_json"] or "{}")), max_tokens=2048)
    provenance[SYNTHESIS_COMPACT_RETRY_KEY] = {
        "reason": reason,
        "max_tokens": 2048,
        "created_at": now,
    }
    _ = conn.execute(
        """
        UPDATE tasks
        SET status = ?,
            blocked_reasons_json = '[]',
            instructions = ?,
            provider_policy_json = ?,
            task_provenance_json = ?,
            updated_at = ?
        WHERE task_id = ?
        """,
        (
            TaskStatus.QUEUED,
            _compact_parent_synthesis_instructions(task, reason=reason),
            _json(provider_policy),
            _json(provenance),
            now,
            task_id,
        ),
    )
    _ = repo.emit_event(
        conn,
        "task.synthesis_compacted",
        matter_scope=str(task["matter_scope"]),
        payload=result,
    )
    return result


def _existing_decomposition(task: Mapping[str, object]) -> Mapping[str, object] | None:
    provenance = _json_object(str(_mapping_value(task, "task_provenance_json") or "{}"))
    decomposition = provenance.get(DECOMPOSITION_PROVENANCE_KEY)
    if isinstance(decomposition, Mapping):
        return cast(Mapping[str, object], decomposition)
    return None


def _mapping_value(mapping: Mapping[str, object], key: str, default: object = "") -> object:
    if hasattr(mapping, "keys") and key in mapping.keys():
        return mapping[key]
    return default


def _child_task_type(task_type: str) -> str:
    return {
        "evidence_issue_map": "evidence_issue_map_bundle",
        "production_mapping": "production_mapping_bundle",
        "evidence_organization_plan": "evidence_organization_plan_bundle",
    }.get(task_type, f"{task_type}_bundle")


def _child_instructions(
    *,
    parent_task: Mapping[str, object],
    index: int,
    total: int,
    source_count: int,
    estimated_source_tokens: int,
    source_estimates: list[SourceTokenEstimate],
) -> str:
    ledger_rows = [
        {
            "source_id": estimate.source_id,
            "estimated_tokens": estimate.estimated_tokens,
            "available_chars": estimate.available_chars,
            "basis": estimate.estimation_basis,
        }
        for estimate in source_estimates
    ]
    return (
        f"Source-bundle task {index}/{total} for parent {parent_task['task_id']}. "
        f"Analyze only this bundle's {source_count} source_dependencies_json entries "
        f"(estimated local source text tokens: {estimated_source_tokens}). "
        f"Document ledger for this bundle: {_json(ledger_rows)}. "
        "Work document-by-document. For each source, record only the strongest directly supported facts, "
        "dates, amounts, contradictions, missing OCR/extraction concerns, and redaction concerns. "
        "Produce a compact worker_result_packet.v2 for this bundle: at most 3 findings, 5 citations, "
        "2 uncertainties, 2 risk_flags, 2 redaction_flags, and 1 proposed_task. Keep the summary under "
        "500 characters, citation quotes under 160 characters, finding text under 260 characters, and "
        "proposed_artifacts[0].content under 1000 characters. "
        "Every material finding must cite source IDs from this bundle. Prefer one concise finding per source unless "
        "the source contains multiple independently important facts. "
        "Set proposed_artifacts[0].artifact_type to evidence_registry for evidence work or production_crosswalk for production work. "
        "The proposed artifact content must be a concise source-review ledger keyed by source_id with terse bullets; "
        "it is not a full matter analysis. "
        "Do not claim full-matter completion; this is one bounded child packet for later synthesis. "
        f"Parent instructions excerpt: {str(parent_task['instructions'] or '')[:1200]}"
    )


def _parent_synthesis_instructions(task: Mapping[str, object], *, child_count: int, reason: str) -> str:
    return (
        f"{str(task['instructions'] or '')}\n\n"
        "Synthesis retry after source-bundle decomposition. Do not reprocess raw source files in this parent task. "
        f"Wait for all {child_count} task_dependencies_json children to complete, then synthesize only from the validated "
        "evidence_registry/production_crosswalk artifacts produced by those dependencies. Cite those artifact targets for "
        "bundle-level synthesis and preserve their embedded source citations in the proposed artifact content. "
        "Keep the parent packet compact: at most 10 findings, 16 citations, 6 uncertainties, 6 risk_flags, "
        "6 redaction_flags, and 4 proposed_tasks. "
        f"Decomposition reason: {reason}"
    )


def _child_provider_policy(
    policy: Mapping[str, object],
    *,
    child_task: Mapping[str, object],
    chunk_count: int,
) -> dict[str, object]:
    if isinstance(policy.get("model_routing"), Mapping):
        child = resolve_provider_policy_from_parent(
            policy,
            proposed_task=cast(Mapping[object, object], child_task),
            layer="worker",
        )
    else:
        child = dict(policy)
    child["max_tokens"] = min(_positive_int(child.get("max_tokens"), BROAD_TASK_MAX_OUTPUT_TOKENS), BROAD_TASK_MAX_OUTPUT_TOKENS)
    estimated = _optional_float(child.get("estimated_cost_usd"))
    if estimated is not None and chunk_count > 0 and "model_routing" not in policy:
        child["estimated_cost_usd"] = max(0.0, estimated / chunk_count)
    return child


def _parent_provider_policy(policy: Mapping[str, object], *, max_tokens: int = BROAD_TASK_MAX_OUTPUT_TOKENS) -> dict[str, object]:
    parent = dict(policy)
    parent["max_tokens"] = min(_positive_int(parent.get("max_tokens"), max_tokens), max_tokens)
    return parent


def _compact_parent_retry_candidate(reason: str) -> bool:
    reason_text = reason.lower()
    return any(marker in reason_text for marker in DECOMPOSITION_FAILURE_MARKERS)


def _compact_parent_synthesis_instructions(task: Mapping[str, object], *, reason: str) -> str:
    return (
        f"{str(task['instructions'] or '')[:1800]}\n\n"
        "Synthesis retry after overlong provider JSON. Return an intentionally tiny worker_result_packet.v2. "
        "Do not enumerate every source or every child artifact. Do not narrate the whole matter. "
        "Use the completed child production_crosswalk/evidence_registry artifacts as the durable detail layer. "
        "The parent packet should only certify the synthesis bridge: at most 2 findings, 4 citations, "
        "1 uncertainty, 1 risk_flag, 0 redaction_flags, 0 proposed_tasks, and exactly 1 proposed_artifact. "
        "Keep summary under 240 characters, finding text under 180 characters, citation quotes under 120 characters, "
        "and proposed_artifacts[0].content under 600 characters. "
        "For production mapping, proposed_artifacts[0].artifact_type must be production_crosswalk and content should be "
        "one compact JSON object or terse text with child_artifact_count, source_count, and synthesis_status. "
        "Cite only representative validated child artifacts or source IDs that are present in citation_targets. "
        "Do not include quoted_text_hash unless an exact SHA-256 is provided. "
        f"Previous failure: {reason}"
    )


def _child_expected_value(task: Mapping[str, object], chunk_count: int) -> float:
    raw = _optional_float(task["expected_value"])
    if raw is None:
        return 0.0
    return raw / max(1, chunk_count)


def _optional_float(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return None


def _positive_int(raw: object, default: int) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _child_task_id(parent_task_id: str, index: int, source_ids: list[str]) -> str:
    return f"{parent_task_id}-bundle-{index:02d}-{_fingerprint_strings(source_ids)[:8]}"


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _fingerprint_strings(items: list[str]) -> str:
    return hashlib.sha256(_json(items).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value) if str(item)]


def _json_mapping_list(raw: str) -> list[dict[str, object]]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [dict(cast(Mapping[str, object], item)) for item in cast(list[object], value) if isinstance(item, Mapping)]


def _json_object(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}
