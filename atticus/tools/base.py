"""Base types for Atticus legal tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from typing import ClassVar, Protocol


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
