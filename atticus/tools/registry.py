from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STAGE_TOOL_ALLOWANCES: dict[str, list[str]] = {
    "evidence-ingest-scan": ["Glob", "Bash"],
    "evidence-ingest-analyse": ["Read"],
    "evidence-ingest-resolve": ["Read", "Grep", "NotebookEdit"],
    "evidence-ingest-execute": ["Copy", "Write", "Delete"],
    "evidence-ingest-register": ["Write", "Bash"],
    "extract-sources": ["Glob", "Read", "Bash", "Write"],
    "harvest": ["Read", "Grep", "Write", "Edit"],
    "review": ["Read", "Grep", "Edit", "Glob", "Bash", "Delete"],
    "repair": ["Read", "Grep", "Edit", "Glob", "Bash", "Delete"],
    "final-gate": ["Read", "Grep", "Write"],
    "S6": ["Read", "Grep", "Glob", "Bash", "web_search"],
    "S7": ["Read", "Grep", "Glob", "Bash", "Edit", "web_search"],
}


@dataclass
class ToolContext:
    stage: str
    workspace_path: Path
    db_path: Path | None = None
    provenance_logger: Any = None
    token_budget: int | None = None


@dataclass
class ToolResult:
    content: str | bytes | dict
    metadata: dict[str, object] = field(default_factory=dict)
    success: bool = True
    error: str | None = None


class HarnessTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    def can_handle(self, stage: str) -> bool:
        tool_name = self.name
        for allowed_stage, allowed_tools in STAGE_TOOL_ALLOWANCES.items():
            if stage == allowed_stage and tool_name in allowed_tools:
                return True
        return False

    @abstractmethod
    def invoke(self, params: dict, context: ToolContext) -> ToolResult:
        ...


_TOOL_REGISTRY: dict[str, type[HarnessTool]] = {}
_LEGAL_TOOLS: dict[str, object] = {}


def register_tool(tool_class: type[HarnessTool]) -> type[HarnessTool]:
    instance = tool_class()
    _TOOL_REGISTRY[instance.name] = tool_class
    return tool_class


def _load_legal_tools() -> None:
    if _LEGAL_TOOLS:
        return
    from atticus.tools.legal_tools import get_legal_tools

    _LEGAL_TOOLS.update(get_legal_tools())


def get_tool(name: str) -> object | None:
    _load_legal_tools()
    if name in _LEGAL_TOOLS:
        return _LEGAL_TOOLS[name]
    return _TOOL_REGISTRY.get(name)


def list_tools() -> list[object]:
    _load_legal_tools()
    return list(_TOOL_REGISTRY.values()) + list(_LEGAL_TOOLS.values())


def get_tools_for_stage(stage: str) -> list[type[HarnessTool]]:
    allowed_tool_names = STAGE_TOOL_ALLOWANCES.get(stage, [])
    return [cls for name in allowed_tool_names if (cls := _TOOL_REGISTRY.get(name)) is not None]


def invoke_tool(name: str, params: dict, context: object) -> ToolResult | dict[str, object]:
    if hasattr(context, "conn") and hasattr(context, "matter_scope"):
        return _invoke_legal_tool(name, params, context)
    return _invoke_harness_tool(name, params, context)


def _invoke_harness_tool(name: str, params: dict, context: ToolContext) -> ToolResult:
    tool_cls = get_tool(name)
    if tool_cls is None or not isinstance(tool_cls, type):
        return ToolResult(content={}, metadata={"tool": name}, success=False, error=f"Unknown tool: {name}")
    tool = tool_cls()
    if not tool.can_handle(context.stage):
        return ToolResult(
            content={},
            metadata={"tool": name, "stage": context.stage},
            success=False,
            error=f"Tool {name} not available in stage {context.stage}",
        )
    return tool.invoke(params, context)


def _invoke_legal_tool(name: str, params: dict[str, object], ctx: object) -> dict[str, object]:
    import json as _json

    from atticus.db.repo import emit_event
    from atticus.tools.base import ToolPermissionError, ToolValidationError

    tool = get_tool(name)
    if tool is None:
        raise KeyError(f"Unknown tool: {name}")

    permission_mode = getattr(ctx, "permission_mode", "default")
    requires_write = getattr(tool, "requires_write", False)
    read_only = getattr(tool, "read_only", True)
    destructive = getattr(tool, "destructive", False)

    if permission_mode == "read_only" and (requires_write or destructive or not read_only):
        emit_event(
            ctx.conn,
            "tool.invoked",
            actor=getattr(ctx, "actor", "atticus"),
            matter_scope=getattr(ctx, "matter_scope", "atticus"),
            payload={
                "tool": name,
                "status": "blocked",
                "permission_mode": permission_mode,
            },
        )
        raise ToolPermissionError(
            f"tool {name} requires write permission -- current permission_mode={permission_mode}"
        )

    try:
        result = tool.call(params, ctx)
    except (ToolValidationError, ToolPermissionError):
        raise
    except Exception as exc:
        emit_event(
            ctx.conn,
            "tool.invoked",
            actor=getattr(ctx, "actor", "atticus"),
            matter_scope=getattr(ctx, "matter_scope", "atticus"),
            payload={"tool": name, "status": "failed", "error": str(exc)},
        )
        raise ToolValidationError(str(exc)) from exc

    max_result_size = getattr(tool, "max_result_size", 50000)
    result_json = _json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(result_json) > max_result_size:
        emit_event(
            ctx.conn,
            "tool.invoked",
            actor=getattr(ctx, "actor", "atticus"),
            matter_scope=getattr(ctx, "matter_scope", "atticus"),
            payload={
                "tool": name,
                "status": "failed",
                "error": f"max_result_size={max_result_size} exceeded: {len(result_json)}",
            },
        )
        raise ToolValidationError(
            f"tool {name} result exceeds max_result_size: {len(result_json)} > {max_result_size}"
        )

    emit_event(
        ctx.conn,
        "tool.invoked",
        actor=getattr(ctx, "actor", "atticus"),
        matter_scope=getattr(ctx, "matter_scope", "atticus"),
        payload={"tool": name, "status": "success"},
    )
    return result
