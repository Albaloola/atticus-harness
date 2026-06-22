"""Base types for Atticus legal tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import sqlite3
from typing import ClassVar, Protocol, TypedDict


class ToolValidationError(ValueError):
    """Raised when tool input or state is invalid."""


class ToolPermissionError(PermissionError):
    """Raised when a tool is not permitted in the current context."""


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    matter_scope: str
    actor: str
    task_id: str | None = None
    lease_id: str | None = None
    permission_mode: str = "default"
    output_dir: Path | None = None
    read_state: dict[str, dict[str, object]] = field(default_factory=dict)


class LegalTool(Protocol):
    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    read_only: bool
    destructive: bool
    concurrency_safe: bool
    requires_write: bool
    requires_live: bool
    hidden: bool
    max_result_size: int

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]: ...


@dataclass(frozen=True)
class ToolMetadata:
    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    read_only: bool = True
    destructive: bool = False
    concurrency_safe: bool = True
    requires_write: bool = False
    requires_live: bool = False
    hidden: bool = False
    max_result_size: int = 50_000
    max_result_size_chars: int = 100_000
    search_hint: str | None = None
    should_defer: bool = False
    always_load: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "concurrency_safe": self.concurrency_safe,
            "requires_write": self.requires_write,
            "requires_live": self.requires_live,
            "hidden": self.hidden,
            "max_result_size": self.max_result_size,
            "max_result_size_chars": self.max_result_size_chars,
            "search_hint": self.search_hint,
            "should_defer": self.should_defer,
            "always_load": self.always_load,
        }


class BaseTool:
    metadata: ClassVar[ToolMetadata]

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def input_schema(self) -> dict[str, object]:
        return self.metadata.input_schema

    @property
    def output_schema(self) -> dict[str, object]:
        return self.metadata.output_schema

    @property
    def read_only(self) -> bool:
        return self.metadata.read_only

    @property
    def destructive(self) -> bool:
        return self.metadata.destructive

    @property
    def concurrency_safe(self) -> bool:
        return self.metadata.concurrency_safe

    @property
    def requires_write(self) -> bool:
        return self.metadata.requires_write

    @property
    def requires_live(self) -> bool:
        return self.metadata.requires_live

    @property
    def hidden(self) -> bool:
        return self.metadata.hidden

    @property
    def max_result_size(self) -> int:
        return self.metadata.max_result_size

    def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
        raise NotImplementedError


def require_string(input_data: Mapping[str, object], key: str) -> str:
    value = input_data.get(key)
    if not isinstance(value, str) or not value:
        raise ToolValidationError(f"{key} is required")
    return value


# ---------------------------------------------------------------------------
# Permission infrastructure (ported from Claude Code's Tool.ts buildTool pattern)
# ---------------------------------------------------------------------------


class ToolPermissionMode(Enum):
    """Permission mode for tool execution context.

    Ported from Claude Code: 'default', 'acceptEdits', 'plan', 'bypass'.
    """

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypass"


@dataclass(frozen=True)
class ToolPermissionContext:
    """Immutable permission context governing tool execution.

    Defines allow/deny/ask rules plus working directory scope and bypass
    availability for a tool invocation.
    """

    mode: ToolPermissionMode = ToolPermissionMode.DEFAULT
    additional_working_directories: tuple[str, ...] = ()
    always_allow_rules: tuple[str, ...] = ()
    always_deny_rules: tuple[str, ...] = ()
    always_ask_rules: tuple[str, ...] = ()
    is_bypass_available: bool = False


class ValidationResult(TypedDict, total=False):
    """Discriminated union for tool input validation results.

    On success:  {"result": True}
    On failure:  {"result": False, "message": "...", "error_code": "..."}
    """

    result: bool
    message: str
    error_code: str


@dataclass
class ToolDefaults:
    """Fail-closed defaults for tool definitions.

    Used by build_tool() to fill gaps in partial tool definitions.
    """

    is_enabled: bool = True
    is_concurrency_safe: bool = False
    is_read_only: bool = False
    is_destructive: bool = False


def _default_check_permissions(
    input_data: dict[str, object],
) -> dict[str, object]:
    """Default permission check: allow all input without modification."""
    return {"behavior": "allow", "updated_input": input_data}


def _default_to_auto_classifier_input(
    tool_name: str, input_data: dict[str, object],
) -> dict[str, object]:
    """Default auto-classifier: pass through tool name and input."""
    return {"tool_name": tool_name, "input": input_data}


def tool_matches_name(tool: object, name: str) -> bool:
    """Check whether *tool* matches *name* by primary name or aliases.

    Returns True if ``tool.name == name`` or ``name in (tool.aliases or [])``.
    """
    if hasattr(tool, "name") and getattr(tool, "name") == name:
        return True
    aliases = getattr(tool, "aliases", None)
    if aliases is not None and name in aliases:
        return True
    return False


def build_tool(raw: dict[str, object] | object) -> dict[str, object]:
    """Build a tool definition with safe (fail-closed) defaults.

    Port of Claude Code's ``buildTool()`` pattern.  Accepts a partial dict
    or any object with public attributes and fills in safe defaults for every
    field that is missing.

    Filled defaults
    ---------------
    * ``is_enabled``         → True (from ``ToolDefaults``)
    * ``is_concurrency_safe`` → False  (fail-closed)
    * ``is_read_only``        → False  (fail-closed)
    * ``is_destructive``      → False  (fail-closed)
    * ``check_permissions``   → allow-all lambda
    * ``to_auto_classifier_input`` → pass-through lambda
    * ``user_facing_name``    → falls back to ``name`` or ``"unnamed_tool"``
    * ``aliases``             → []
    * ``search_hint``         → None
    * ``should_defer``        → False
    * ``always_load``         → False
    * ``max_result_size_chars`` → 100 000
    """
    if isinstance(raw, dict):
        tool_dict: dict[str, object] = dict(raw)
    else:
        tool_dict = {
            k: v for k, v in vars(raw).items() if not k.startswith("_")
        }

    defaults = ToolDefaults()

    tool_dict.setdefault("is_enabled", defaults.is_enabled)
    tool_dict.setdefault("is_concurrency_safe", defaults.is_concurrency_safe)
    tool_dict.setdefault("is_read_only", defaults.is_read_only)
    tool_dict.setdefault("is_destructive", defaults.is_destructive)
    tool_dict.setdefault("check_permissions", _default_check_permissions)
    tool_dict.setdefault(
        "to_auto_classifier_input", _default_to_auto_classifier_input,
    )
    tool_dict.setdefault(
        "user_facing_name", tool_dict.get("name", "unnamed_tool"),
    )
    tool_dict.setdefault("aliases", [])
    tool_dict.setdefault("search_hint", None)
    tool_dict.setdefault("should_defer", False)
    tool_dict.setdefault("always_load", False)
    tool_dict.setdefault("max_result_size_chars", 100_000)

    return tool_dict
