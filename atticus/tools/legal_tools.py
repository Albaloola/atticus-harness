"""Legal-domain tools for Atticus harness: source inspection, draft editing,
candidate management, legal memory search, citation validation, and context packs."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json

from atticus.core.policies import TrustStatus
from atticus.db import repo
from atticus.tools.base import (
    BaseTool,
    ToolContext,
    ToolMetadata,
    ToolPermissionError,
    ToolValidationError,
    require_string,
)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── ListMatterSources ───────────────────────────────────────────────────────


class ListMatterSourcesTool(BaseTool):
    metadata = ToolMetadata(
        name="ListMatterSources",
        description="List all sources scoped to the current matter, with their source-material derivatives.",
        input_schema={"type": "object"},
        output_schema={"type": "object", "properties": {"sources": {"type": "array"}}},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        sources = ctx.conn.execute(
            "SELECT * FROM sources WHERE matter_scope = ? ORDER BY source_id",
            (ctx.matter_scope,),
        ).fetchall()
        source_ids = [str(row["source_id"]) for row in sources]
        derivatives = repo.source_material_derivatives(
            ctx.conn,
            matter_scope=ctx.matter_scope,
            source_ids=source_ids,
        )
        result: list[dict[str, object]] = []
        for row in sources:
            sid = str(row["source_id"])
            sourcelist = derivatives.get(sid, [])
            result.append(
                {
                    "source_id": sid,
                    "path": row["path"],
                    "source_type": row["source_type"],
                    "sha256": row["sha256"],
                    "trust_status": row["trust_status"],
                    "stale": bool(row["stale"]),
                    "source_material_derivatives": sourcelist,
                    "source_material_available": len(sourcelist) > 0,
                    "ocr_available": any(
                        d.get("ocr") is not None for d in sourcelist
                    ),
                }
            )
        return {"sources": result}


# ── InspectRecord ───────────────────────────────────────────────────────────


class InspectRecordTool(BaseTool):
    metadata = ToolMetadata(
        name="InspectRecord",
        description="Inspect a source or artifact record with full derivative and metadata details.",
        input_schema={
            "type": "object",
            "properties": {
                "record_type": {"type": "string", "enum": ["source", "artifact"]},
                "record_id": {"type": "string"},
            },
            "required": ["record_type", "record_id"],
        },
        output_schema={"type": "object", "properties": {"record": {"type": "object"}}},
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        record_type = require_string(input_data, "record_type")
        record_id = require_string(input_data, "record_id")
        if record_type not in ("source", "artifact"):
            raise ToolValidationError(f"unsupported record_type: {record_type}")

        row = ctx.conn.execute(
            f"SELECT * FROM {record_type}s WHERE {record_type}_id = ? AND matter_scope = ?",
            (record_id, ctx.matter_scope),
        ).fetchone()
        if row is None:
            raise ToolValidationError(f"{record_type} not found: {record_id}")

        record: dict[str, object] = {}
        for key in row.keys():
            val = row[key]
            if isinstance(val, str) and key.endswith("_json"):
                try:
                    record[key[:-5]] = json.loads(val)
                except json.JSONDecodeError:
                    record[key] = val
            else:
                record[key] = val
        record["stale"] = bool(row["stale"]) if "stale" in row.keys() else False

        if record_type == "source":
            derivatives = repo.source_material_derivatives(
                ctx.conn,
                matter_scope=ctx.matter_scope,
                source_ids=[record_id],
            ).get(record_id, [])
            record["source_material_derivatives"] = derivatives

        return {"record": record}


# ── ReadDraftArtifact ───────────────────────────────────────────────────────


class ReadDraftArtifactTool(BaseTool):
    metadata = ToolMetadata(
        name="ReadDraftArtifact",
        description="Read a draft artifact's content, returning the text and a content hash for edit guard.",
        input_schema={
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "content_hash": {"type": "string"},
            },
        },
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        artifact_id = require_string(input_data, "artifact_id")
        row = ctx.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND matter_scope = ?",
            (artifact_id, ctx.matter_scope),
        ).fetchone()
        if row is None:
            raise ToolValidationError(f"artifact not found: {artifact_id}")
        if row["trust_status"] == TrustStatus.VALIDATED:
            raise ToolPermissionError(
                f"artifact {artifact_id} is validated — read-only draft edits not permitted"
            )
        content = str(row["content"] or "")
        content_hash = _hash_text(content)
        ctx.read_state.setdefault("draft_artifact_reads", {})[artifact_id] = {
            "content_hash": content_hash,
            "content_length": len(content),
        }
        return {"content": content, "content_hash": content_hash}


# ── EditDraftArtifact ───────────────────────────────────────────────────────


class EditDraftArtifactTool(BaseTool):
    metadata = ToolMetadata(
        name="EditDraftArtifact",
        description="Edit a draft artifact's content with version tracking and edit guards.",
        input_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "read_hash": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["artifact_id", "old", "new", "read_hash"],
        },
        output_schema={
            "type": "object",
            "properties": {"replacements": {"type": "integer"}},
        },
        destructive=True,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        artifact_id = require_string(input_data, "artifact_id")
        old_text = require_string(input_data, "old")
        new_text = require_string(input_data, "new")
        read_hash = require_string(input_data, "read_hash")
        replace_all = bool(input_data.get("replace_all", False))

        row = ctx.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND matter_scope = ?",
            (artifact_id, ctx.matter_scope),
        ).fetchone()
        if row is None:
            raise ToolValidationError(f"artifact not found: {artifact_id}")

        content = str(row["content"] or "")
        current_hash = _hash_text(content)

        prior_read = ctx.read_state.get("draft_artifact_reads", {}).get(artifact_id)
        if prior_read is None:
            raise ToolValidationError(
                f"artifact {artifact_id} must be read before editing "
                f"-- call ReadDraftArtifact first"
            )

        if current_hash != read_hash:
            raise ToolValidationError(
                f"artifact {artifact_id} content changed since read -- "
                f"re-read the artifact before editing"
            )

        count = content.count(old_text)
        if count == 0:
            raise ToolValidationError(
                f"text '{old_text}' not found in artifact {artifact_id}"
            )
        if count > 1 and not replace_all:
            raise ToolValidationError(
                f"text '{old_text}' appears {count} times — "
                f"must appear exactly once, or use replace_all=True"
            )

        new_content = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
        replacements = count if replace_all else 1
        new_hash = _hash_text(new_content)

        version_row = ctx.conn.execute(
            "SELECT MAX(version_number) as ver FROM artifact_versions WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        ver = version_row["ver"] if version_row else None
        next_version = 1 if ver is None else int(ver) + 1

        _ = ctx.conn.execute(
            "UPDATE artifacts SET content = ?, sha256 = ?, updated_at = ? WHERE artifact_id = ?",
            (new_content, new_hash, repo.utc_now(), artifact_id),
        )

        repo.add_artifact_version(
            ctx.conn,
            artifact_id=artifact_id,
            version_number=next_version,
            sha256=new_hash,
            content=new_content,
            status=str(row["trust_status"]),
        )

        repo.emit_event(
            ctx.conn,
            "artifact.draft_edited",
            actor=ctx.actor,
            matter_scope=ctx.matter_scope,
            payload={
                "artifact_id": artifact_id,
                "version": next_version,
                "replacements": replacements,
            },
        )

        ctx.read_state.setdefault("draft_artifact_reads", {})[artifact_id] = {
            "content_hash": new_hash,
            "content_length": len(new_content),
        }

        return {"replacements": replacements}


# ── WriteDraftArtifact ──────────────────────────────────────────────────────


class WriteDraftArtifactTool(BaseTool):
    metadata = ToolMetadata(
        name="WriteDraftArtifact",
        description="Write a new draft artifact or overwrite an existing one.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "artifact_type": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "artifact_type", "title", "content"],
        },
        output_schema={
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
        },
        destructive=True,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        path = require_string(input_data, "path")
        artifact_type = require_string(input_data, "artifact_type")
        title = require_string(input_data, "title")
        content = require_string(input_data, "content")
        artifact_id = repo.add_artifact(
            ctx.conn,
            matter_scope=ctx.matter_scope,
            path=path,
            artifact_type=artifact_type,
            trust_status=TrustStatus.CANDIDATE,
            title=title,
            content=content,
        )
        return {"artifact_id": artifact_id}


# ── ReduceCandidate ─────────────────────────────────────────────────────────


class ReduceCandidateTool(BaseTool):
    metadata = ToolMetadata(
        name="ReduceCandidate",
        description="Reduce a candidate output to canonical, accepting it into the matter record.",
        input_schema={
            "type": "object",
            "properties": {
                "record_type": {"type": "string"},
                "record_id": {"type": "string"},
                "reducer_notes": {"type": "string"},
            },
            "required": ["record_type", "record_id"],
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
        read_only=False,
        destructive=True,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        record_type = require_string(input_data, "record_type")
        record_id = require_string(input_data, "record_id")
        notes = str(input_data.get("reducer_notes", ""))
        if record_type == "artifact":
            _ = ctx.conn.execute(
                "UPDATE artifacts SET trust_status = ? WHERE artifact_id = ? AND matter_scope = ?",
                (TrustStatus.CERTIFIED, record_id, ctx.matter_scope),
            )
        _ = repo.emit_event(
            ctx.conn,
            "candidate.reduced",
            actor=ctx.actor,
            matter_scope=ctx.matter_scope,
            payload={
                "record_type": record_type,
                "record_id": record_id,
                "reducer_notes": notes,
            },
        )
        return {"status": "reduced"}


# ── RejectCandidate ─────────────────────────────────────────────────────────


class RejectCandidateTool(BaseTool):
    metadata = ToolMetadata(
        name="RejectCandidate",
        description="Reject a candidate output, marking it as unsuitable.",
        input_schema={
            "type": "object",
            "properties": {
                "record_type": {"type": "string"},
                "record_id": {"type": "string"},
                "rejection_reason": {"type": "string"},
            },
            "required": ["record_type", "record_id"],
        },
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
        destructive=True,
        requires_write=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        record_type = require_string(input_data, "record_type")
        record_id = require_string(input_data, "record_id")
        reason = str(input_data.get("rejection_reason", ""))
        if record_type == "artifact":
            _ = ctx.conn.execute(
                "UPDATE artifacts SET trust_status = ? WHERE artifact_id = ? AND matter_scope = ?",
                (TrustStatus.REJECTED, record_id, ctx.matter_scope),
            )
        _ = repo.emit_event(
            ctx.conn,
            "candidate.rejected",
            actor=ctx.actor,
            matter_scope=ctx.matter_scope,
            payload={
                "record_type": record_type,
                "record_id": record_id,
                "rejection_reason": reason,
            },
        )
        return {"status": "rejected"}


# ── SearchLegalMemory ───────────────────────────────────────────────────────


class SearchLegalMemoryTool(BaseTool):
    metadata = ToolMetadata(
        name="SearchLegalMemory",
        description="Search legal memory entries scoped to the current matter.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "memory_type": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {"results": {"type": "array"}},
        },
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        query = str(input_data.get("query", ""))
        mem_type = str(input_data.get("memory_type", ""))
        limit = int(input_data.get("limit", 20))

        where = ["matter_scope = ?"]
        params: list[object] = [ctx.matter_scope]
        if query:
            where.append("(name LIKE ? OR description LIKE ? OR content LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        if mem_type:
            where.append("type = ?")
            params.append(mem_type)

        rows = ctx.conn.execute(
            f"SELECT * FROM legal_memories WHERE {' AND '.join(where)} ORDER BY type, name LIMIT ?",
            (*params, limit),
        ).fetchall()

        results: list[dict[str, object]] = []
        for row in rows:
            rec: dict[str, object] = dict(row)
            if "source_refs_json" in rec:
                try:
                    rec["source_refs"] = json.loads(str(rec.pop("source_refs_json")))
                except (json.JSONDecodeError, KeyError):
                    pass
            results.append(rec)

        return {"results": results}


# ── ValidateCitation ────────────────────────────────────────────────────────


class ValidateCitationTool(BaseTool):
    metadata = ToolMetadata(
        name="ValidateCitation",
        description="Validate a legal citation against registered sources and authorities.",
        input_schema={
            "type": "object",
            "properties": {
                "citation_text": {"type": "string"},
                "citation_type": {"type": "string"},
            },
            "required": ["citation_text"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "valid": {"type": "boolean"},
                "validation_notes": {"type": "string"},
            },
        },
        read_only=True,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        citation_text = require_string(input_data, "citation_text")
        citation_type = str(input_data.get("citation_type", "source"))
        valid = False
        notes = ""
        if citation_type == "source":
            row = ctx.conn.execute(
                "SELECT 1 FROM sources WHERE source_id = ? AND matter_scope = ?",
                (citation_text, ctx.matter_scope),
            ).fetchone()
            valid = row is not None
            notes = "source found" if valid else "source not found in matter scope"
        elif citation_type == "artifact":
            row = ctx.conn.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id = ? AND matter_scope = ?",
                (citation_text, ctx.matter_scope),
            ).fetchone()
            valid = row is not None
            notes = "artifact found" if valid else "artifact not found in matter scope"
        else:
            notes = f"unknown citation_type: {citation_type}"
        return {"valid": valid, "validation_notes": notes}


# ── BuildContextPack ────────────────────────────────────────────────────────


class BuildContextPackTool(BaseTool):
    metadata = ToolMetadata(
        name="BuildContextPack",
        description="Build a context pack bundle of relevant sources, artifacts, and memories for a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "source_ids": {"type": "array"},
                "artifact_ids": {"type": "array"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {"context_pack_id": {"type": "string"}},
        },
        read_only=False,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        task_id = str(input_data.get("task_id", ""))
        source_ids = input_data.get("source_ids", [])
        artifact_ids = input_data.get("artifact_ids", [])

        sources: list[dict[str, object]] = []
        if isinstance(source_ids, list):
            for sid in source_ids:
                row = ctx.conn.execute(
                    "SELECT * FROM sources WHERE source_id = ? AND matter_scope = ?",
                    (str(sid), ctx.matter_scope),
                ).fetchone()
                if row:
                    sources.append(dict(row))

        artifacts: list[dict[str, object]] = []
        if isinstance(artifact_ids, list):
            for aid in artifact_ids:
                row = ctx.conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ? AND matter_scope = ?",
                    (str(aid), ctx.matter_scope),
                ).fetchone()
                if row:
                    artifacts.append(dict(row))

        return {
            "context_pack_id": f"cp-{task_id or 'adhoc'}",
            "sources": sources,
            "artifacts": artifacts,
        }


# ── OversizedTool ───────────────────────────────────────────────────────────


class OversizedToolTool(BaseTool):
    metadata = ToolMetadata(
        name="OversizedTool",
        description="Return too much output for testing result-size enforcement.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        read_only=True,
        max_result_size=32,
    )

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        del input_data, ctx
        return {"blob": "x" * 100}


# ── Registry ────────────────────────────────────────────────────────────────


_ALL_TOOLS: dict[str, BaseTool] = {}


def _register_tools() -> None:
    """Register all legal tools. Call at module import time."""
    classes: list[type[BaseTool]] = [
        ListMatterSourcesTool,
        InspectRecordTool,
        ReadDraftArtifactTool,
        EditDraftArtifactTool,
        WriteDraftArtifactTool,
        ReduceCandidateTool,
        RejectCandidateTool,
        SearchLegalMemoryTool,
        ValidateCitationTool,
        BuildContextPackTool,
        OversizedToolTool,
    ]
    for cls in classes:
        inst = cls()
        _ALL_TOOLS[inst.name] = inst


def get_legal_tools() -> dict[str, BaseTool]:
    """Return the dict of all registered legal tools (name -> instance)."""
    if not _ALL_TOOLS:
        _register_tools()
    return _ALL_TOOLS


def get_legal_tool(name: str) -> BaseTool | None:
    """Get a single legal tool by name."""
    tools = get_legal_tools()
    return tools.get(name)


def list_legal_tool_instances() -> list[BaseTool]:
    """Return all legal tool instances."""
    return list(get_legal_tools().values())
