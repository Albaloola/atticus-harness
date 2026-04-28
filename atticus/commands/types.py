"""Types for Atticus command metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommandType = Literal["local", "workflow", "prompt"]


@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    command_type: CommandType = "local"
    aliases: tuple[str, ...] = ()
    source: str = "builtin"
    read_only_safe: bool = False
    requires_write: bool = False
    requires_live: bool = False
    supports_dry_run: bool = False
    hidden: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "description": self.description,
            "command_type": self.command_type,
            "source": self.source,
            "read_only_safe": self.read_only_safe,
            "requires_write": self.requires_write,
            "requires_live": self.requires_live,
            "supports_dry_run": self.supports_dry_run,
            "hidden": self.hidden,
        }
