"""Registry and built-in tools for Atticus legal operations."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import sqlite3
from typing import cast

from atticus.core.events import utc_now
from atticus.core.policies import TrustStatus
from atticus.db import repo
from atticus.reducer.reducer import reduce_candidate
from atticus.retrieval.search import search_memory
from atticus.status.inspect import TABLES_BY_TYPE, summarize_row
from atticus.tools.base import BaseTool, ToolContext, ToolMetadata, ToolPermissionError, ToolValidationError, require_string
from atticus.workers.outputs import record_worker_result, reject_candidate_output


def list_tools() -> list[BaseTool]:
    return [
        SearchLegalMemoryTool(),
        InspectRecordTool(),
        BuildContextPackTool(),
        ValidateCitationTool(),
        ListMatterArtifactsTool(),
        ListMatterSourcesTool(),
        ExplainValidationGateTool(),
        ReadDraftArtifactTool(),
        RecordCandidateTool(),
        ReduceCandidateTool(),
        RejectCandidateTool(),
        WriteDraftArtifactTool(),
        EditDraftArtifactTool(),
        MarkMemoryStaleTool(),
        CreateProposedTaskTool(),
    ]


def get_tool(name: str) -> BaseTool:
    for tool in list_tools():
        if tool.name == name:
            return tool
    raise KeyError(f"unknown Atticus legal tool: {name}")


def invoke_tool(name: str, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
    tool = get_tool(name)
    try:
        _require_tool_permission(tool, ctx)
        result = tool.call(input_data, ctx)
        _enforce_tool_result_size(tool, result)
    except Exception as exc:
        _emit_tool_invocation(
            ctx,
            tool,
            input_data,
            {},
            status="blocked" if isinstance(exc, ToolPermissionError) else "failed",
            error=str(exc) or exc.__class__.__name__,
        )
        raise
    _emit_tool_invocation(ctx, tool, input_data, result, status="succeeded")
    return result


class SearchLegalMemoryTool(BaseTool):
    metadata = ToolMetadata(
        name="SearchLegalMemory",
        description="Search matter-scoped legal memory without launching workers.",
        input_schema={"type": "object", "required": ["question"]},
        output_schema={"type": "object"},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        question = require_string(input_data, "question")
        rows = search_memory(ctx.conn, question, matter_scope=ctx.matter_scope, authorized_matter_scope=ctx.matter_scope)
        return {"results": rows}


class InspectRecordTool(BaseTool):
    metadata = ToolMetadata(
        name="InspectRecord",
        description="Inspect one matter ledger record by type and id.",
        input_schema={"type": "object", "required": ["record_type", "record_id"]},
        output_schema={"type": "object"},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        record_type = require_string(input_data, "record_type")
        record_id = require_string(input_data, "record_id")
        table_info = TABLES_BY_TYPE.get(record_type)
        if table_info is None:
            raise ToolValidationError(f"unsupported inspect type: {record_type}")
        table, pk = table_info
        row = ctx.conn.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (record_id,)).fetchone()
        if row is None:
            raise ToolValidationError(f"{record_type} not found: {record_id}")
        summary = summarize_row(dict(row))
        if "matter_scope" in summary and summary["matter_scope"] != ctx.matter_scope:
            raise ToolPermissionError(f"{record_type} is outside matter scope {ctx.matter_scope}")
        return {"record": summary}


class BuildContextPackTool(BaseTool):
    metadata = ToolMetadata(
        name="BuildContextPack",
        description="Build a read-only preview of a task context pack.",
        input_schema={"type": "object", "required": ["task_id"]},
        output_schema={"type": "object"},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        task_id = require_string(input_data, "task_id")
        _require_task_matter(ctx, task_id)
        from atticus.context.packs import build_context_pack

        return {"context_pack": build_context_pack(ctx.conn, task_id=task_id, persist=False).as_dict()}


class ValidateCitationTool(BaseTool):
    metadata = ToolMetadata(
        name="ValidateCitation",
        description="Validate that a citation target exists in the current matter.",
        input_schema={"type": "object", "required": ["target_type", "target_id"]},
        output_schema={"type": "object"},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        target_type = require_string(input_data, "target_type")
        target_id = require_string(input_data, "target_id")
        return {"exists": _target_exists(ctx, target_type=target_type, target_id=target_id)}


class ListMatterArtifactsTool(BaseTool):
    metadata = ToolMetadata(
        name="ListMatterArtifacts",
        description="List artifacts in the current matter.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        del input_data
        rows = [
            dict(row)
            for row in ctx.conn.execute(
                "SELECT artifact_id, path, artifact_type, trust_status, stale, title FROM artifacts WHERE matter_scope = ? ORDER BY artifact_id",
                (ctx.matter_scope,),
            )
        ]
        return {"artifacts": rows}


class ListMatterSourcesTool(BaseTool):
    metadata = ToolMetadata(
        name="ListMatterSources",
        description="List sources in the current matter.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        del input_data
        rows = [
            dict(row)
            for row in ctx.conn.execute(
                "SELECT source_id, path, source_type, sha256, trust_status, stale FROM sources WHERE matter_scope = ? ORDER BY source_id",
                (ctx.matter_scope,),
            )
        ]
        return {"sources": rows}


class ExplainValidationGateTool(BaseTool):
    metadata = ToolMetadata(
        name="ExplainValidationGate",
        description="Explain a built-in validation gate by running it against a target.",
        input_schema={"type": "object", "required": ["gate_name", "target_type", "target_id"]},
        output_schema={"type": "object"},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        del ctx
        gate_name = require_string(input_data, "gate_name")
        target_type = require_string(input_data, "target_type")
        target_id = require_string(input_data, "target_id")
        descriptions = {
            "source_inventory": "Matter must have registered, non-stale sources with valid hashes.",
            "stale_dependency": "Task source/artifact dependencies must not be marked stale.",
            "reducer_packet_schema": "Candidate must parse as the current strict worker result packet.",
            "canonical_write_authorization": "Reducer-only canonical write guard must authorize the reducer lease.",
        }
        return {
            "gate_name": gate_name,
            "target_type": target_type,
            "target_id": target_id,
            "description": descriptions.get(gate_name, "Built-in validation gate; run atticus validate to execute it."),
            "diagnostic_only": True,
        }


class ReadDraftArtifactTool(BaseTool):
    metadata = ToolMetadata(
        name="ReadDraftArtifact",
        description="Read an editable draft artifact and record a read hash.",
        input_schema={"type": "object", "required": ["artifact_id"]},
        output_schema={"type": "object"},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        artifact_id = require_string(input_data, "artifact_id")
        row = _artifact_for_edit(ctx, artifact_id=artifact_id, require_editable=True)
        content = str(row["content"] or "")
        content_hash = _hash_text(content)
        ctx.read_state[artifact_id] = {"content_hash": content_hash, "read_at": utc_now()}
        return {"artifact_id": artifact_id, "content": content, "content_hash": content_hash}


class EditDraftArtifactTool(BaseTool):
    metadata = ToolMetadata(
        name="EditDraftArtifact",
        description="Edit an editable draft artifact using read-before-write hash and exact replacement.",
        input_schema={"type": "object", "required": ["artifact_id", "old", "new", "read_hash"]},
        output_schema={"type": "object"},
        read_only=False,
        destructive=True,
        concurrency_safe=False,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        artifact_id = require_string(input_data, "artifact_id")
        old = require_string(input_data, "old")
        new = str(input_data.get("new") or "")
        read_hash = require_string(input_data, "read_hash")
        replace_all = bool(input_data.get("replace_all") or False)
        read_state = ctx.read_state.get(artifact_id)
        if read_state is None or read_state.get("content_hash") != read_hash:
            raise ToolValidationError("draft artifact must be read before editing with the same read_hash")
        row = _artifact_for_edit(ctx, artifact_id=artifact_id, require_editable=True)
        content = str(row["content"] or "")
        current_hash = _hash_text(content)
        if current_hash != read_hash:
            raise ToolValidationError("draft artifact changed since read; read again before editing")
        matches = content.count(old)
        if replace_all:
            if matches == 0:
                raise ToolValidationError("old text did not match")
            updated = content.replace(old, new)
            replacements = matches
        else:
            if matches != 1:
                raise ToolValidationError(f"old text must match exactly once, matched {matches}")
            updated = content.replace(old, new, 1)
            replacements = 1
        version = _next_artifact_version(ctx, artifact_id)
        content_hash = _hash_text(updated)
        _ = ctx.conn.execute(
            "UPDATE artifacts SET content = ?, sha256 = ?, updated_at = ? WHERE artifact_id = ?",
            (updated, content_hash, utc_now(), artifact_id),
        )
        version_id = repo.add_artifact_version(
            ctx.conn,
            artifact_id=artifact_id,
            version_number=version,
            sha256=content_hash,
            content=updated,
            status=str(row["trust_status"]),
            created_by_task_id=ctx.task_id,
            created_by_role=ctx.actor,
        )
        ctx.read_state[artifact_id] = {"content_hash": content_hash, "read_at": utc_now()}
        _ = repo.emit_event(
            ctx.conn,
            "artifact.draft_edited",
            actor=ctx.actor,
            matter_scope=ctx.matter_scope,
            payload={"artifact_id": artifact_id, "artifact_version_id": version_id, "replacements": replacements},
        )
        return {
            "artifact_id": artifact_id,
            "artifact_version_id": version_id,
            "content_hash": content_hash,
            "replacements": replacements,
            "patch_summary": {"old_length": len(old), "new_length": len(new), "replace_all": replace_all},
        }


class RecordCandidateTool(BaseTool):
    metadata = ToolMetadata(
        name="RecordCandidate",
        description="Record a worker candidate packet through the lease-protected candidate path.",
        input_schema={"type": "object", "required": ["task_id", "lease_id", "worker_id", "payload"]},
        output_schema={"type": "object"},
        read_only=False,
        destructive=False,
        concurrency_safe=False,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        payload = input_data.get("payload")
        if not isinstance(payload, Mapping):
            raise ToolValidationError("payload must be a JSON object")
        candidate_id = record_worker_result(
            ctx.conn,
            task_id=require_string(input_data, "task_id"),
            lease_id=require_string(input_data, "lease_id"),
            worker_id=require_string(input_data, "worker_id"),
            payload={str(key): value for key, value in cast(Mapping[object, object], payload).items()},
        )
        return {"candidate_id": candidate_id}


class ReduceCandidateTool(BaseTool):
    metadata = ToolMetadata(
        name="ReduceCandidate",
        description="Reduce a candidate through the reducer-only canonical path.",
        input_schema={"type": "object", "required": ["candidate_id", "lease_id"]},
        output_schema={"type": "object"},
        read_only=False,
        destructive=True,
        concurrency_safe=False,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        return reduce_candidate(
            ctx.conn,
            candidate_id=require_string(input_data, "candidate_id"),
            reducer_lease_id=require_string(input_data, "lease_id"),
            dry_run=False,
        )


class RejectCandidateTool(BaseTool):
    metadata = ToolMetadata(
        name="RejectCandidate",
        description="Quarantine a valid but unsuitable candidate.",
        input_schema={"type": "object", "required": ["candidate_id", "reason"]},
        output_schema={"type": "object"},
        read_only=False,
        destructive=True,
        concurrency_safe=False,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        return reject_candidate_output(
            ctx.conn,
            candidate_id=require_string(input_data, "candidate_id"),
            reason=require_string(input_data, "reason"),
            dry_run=False,
        )


class WriteDraftArtifactTool(BaseTool):
    metadata = ToolMetadata(
        name="WriteDraftArtifact",
        description="Create a candidate draft artifact; never creates validated canonical output.",
        input_schema={"type": "object", "required": ["path", "artifact_type", "title", "content"]},
        output_schema={"type": "object"},
        read_only=False,
        destructive=False,
        concurrency_safe=False,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        artifact_id = repo.add_artifact(
            ctx.conn,
            matter_scope=ctx.matter_scope,
            path=require_string(input_data, "path"),
            artifact_type=require_string(input_data, "artifact_type"),
            trust_status=TrustStatus.CANDIDATE,
            title=str(input_data.get("title") or ""),
            content=str(input_data.get("content") or ""),
            produced_by_task_id=ctx.task_id,
        )
        return {"artifact_id": artifact_id}


class MarkMemoryStaleTool(BaseTool):
    metadata = ToolMetadata(
        name="MarkMemoryStale",
        description="Mark a matter-scoped legal memory as stale.",
        input_schema={"type": "object", "required": ["memory_id", "reason"]},
        output_schema={"type": "object"},
        read_only=False,
        destructive=False,
        concurrency_safe=False,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        memory_id = require_string(input_data, "memory_id")
        repo.mark_legal_memory_stale(ctx.conn, memory_id=memory_id, matter_scope=ctx.matter_scope, reason=require_string(input_data, "reason"))
        return {"memory_id": memory_id, "stale": True}


class CreateProposedTaskTool(BaseTool):
    metadata = ToolMetadata(
        name="CreateProposedTask",
        description="Create a queued proposed task in the current matter.",
        input_schema={"type": "object", "required": ["task_id", "title", "task_type"]},
        output_schema={"type": "object"},
        read_only=False,
        destructive=False,
        concurrency_safe=False,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        from atticus.core.tasks import TaskSpec

        task_id = require_string(input_data, "task_id")
        repo.add_task(
            ctx.conn,
            TaskSpec(
                task_id=task_id,
                title=require_string(input_data, "title"),
                task_type=require_string(input_data, "task_type"),
                matter_scope=ctx.matter_scope,
            ),
        )
        return {"task_id": task_id}


def _require_task_matter(ctx: ToolContext, task_id: str) -> None:
    row = ctx.conn.execute("SELECT matter_scope FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise ToolValidationError(f"task not found: {task_id}")
    if row["matter_scope"] != ctx.matter_scope:
        raise ToolPermissionError(f"task {task_id} is outside matter scope {ctx.matter_scope}")


def _target_exists(ctx: ToolContext, *, target_type: str, target_id: str) -> bool:
    table_column = {
        "source": ("sources", "source_id"),
        "artifact": ("artifacts", "artifact_id"),
        "authority": ("legal_authorities", "authority_id"),
        "chronology_event": ("chronology_events", "chronology_event_id"),
        "claim": ("claims", "claim_id"),
        "memory": ("legal_memories", "memory_id"),
    }.get(target_type)
    if table_column is None:
        return False
    table, column = table_column
    exists = ctx.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    if exists is None:
        return False
    return ctx.conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ? AND matter_scope = ? LIMIT 1", (target_id, ctx.matter_scope)).fetchone() is not None


def _artifact_for_edit(ctx: ToolContext, *, artifact_id: str, require_editable: bool) -> Mapping[str, object]:
    row = ctx.conn.execute("SELECT * FROM artifacts WHERE artifact_id = ? AND matter_scope = ?", (artifact_id, ctx.matter_scope)).fetchone()
    if row is None:
        raise ToolValidationError(f"artifact not found in matter {ctx.matter_scope}: {artifact_id}")
    if require_editable and row["trust_status"] in {TrustStatus.VALIDATED, TrustStatus.CERTIFIED, TrustStatus.REJECTED}:
        raise ToolPermissionError(f"validated/certified artifacts cannot be edited by draft tools: {artifact_id}")
    return cast(Mapping[str, object], row)


def _next_artifact_version(ctx: ToolContext, artifact_id: str) -> int:
    row = ctx.conn.execute("SELECT COALESCE(MAX(version_number), 0) AS n FROM artifact_versions WHERE artifact_id = ?", (artifact_id,)).fetchone()
    return int(str(row["n"] if row else 0)) + 1


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_tool_permission(tool: BaseTool, ctx: ToolContext) -> None:
    mode = ctx.permission_mode.strip().lower()
    write_blocked = mode in {"read_only", "readonly", "dry_run", "no_write"}
    if write_blocked and (tool.requires_write or tool.destructive or not tool.read_only):
        raise ToolPermissionError(f"{tool.name} requires write permission; current permission_mode is {ctx.permission_mode}")
    if tool.requires_live and mode not in {"live", "allow_live"}:
        raise ToolPermissionError(f"{tool.name} requires explicit live permission")


def _enforce_tool_result_size(tool: BaseTool, result: Mapping[str, object]) -> None:
    encoded = json.dumps(result, sort_keys=True, default=str, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size > tool.max_result_size:
        raise ToolValidationError(f"{tool.name} result exceeds max_result_size: {size} > {tool.max_result_size}")


def _emit_tool_invocation(
    ctx: ToolContext,
    tool: BaseTool,
    input_data: Mapping[str, object],
    result: Mapping[str, object],
    *,
    status: str,
    error: str = "",
) -> None:
    payload: dict[str, object] = {
        "tool": tool.name,
        "read_only": tool.read_only,
        "destructive": tool.destructive,
        "requires_write": tool.requires_write,
        "requires_live": tool.requires_live,
        "permission_mode": ctx.permission_mode,
        "status": status,
        "task_id": ctx.task_id,
        "lease_id": ctx.lease_id,
        "activity": _activity_summary(tool.name, input_data, result),
    }
    if error:
        payload["error"] = error[:1000]
    try:
        _ = repo.emit_event(
            ctx.conn,
            "tool.invoked",
            actor=ctx.actor,
            matter_scope=ctx.matter_scope,
            payload=payload,
        )
    except sqlite3.OperationalError as exc:
        if "readonly" in str(exc).lower() or "read-only" in str(exc).lower():
            return
        raise


def _activity_summary(name: str, input_data: Mapping[str, object], result: Mapping[str, object]) -> str:
    target = input_data.get("artifact_id") or input_data.get("candidate_id") or input_data.get("task_id") or input_data.get("memory_id") or ""
    return f"{name} {target}".strip()[:240] or json.dumps({"tool": name, "result_keys": sorted(result)}, sort_keys=True)[:240]
