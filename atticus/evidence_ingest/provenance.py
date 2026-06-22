from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
from typing import Any

from atticus.tools.registry import ToolContext
from atticus.tools.write import WriteTool


class ProvenanceLogger:
    """Logs provenance information to a JSONL file.

    Attributes:
        provenance_path: Path to the physical provenance JSONL log file.
        context: Tool context associated with this logger.
    """

    def __init__(self, workspace_path: Path, context: ToolContext) -> None:
        """Initialize the ProvenanceLogger.

        Args:
            workspace_path: Absolute path to the workspace root directory.
            context: ToolContext instance providing execution context.
        """
        self.provenance_path = workspace_path / "02-registers" / "physical_provenance.jsonl"
        self.provenance_path.parent.mkdir(parents=True, exist_ok=True)
        self.context = context

    def log(self, operation: str, **kwargs: Any) -> None:
        """Append a provenance entry to the JSONL log file.

        Args:
            operation: Identifier for the operation being recorded.
            **kwargs: Additional metadata to include in the provenance entry.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            **kwargs
        }
        try:
            with open(self.provenance_path, "a", encoding="utf-8") as f:
                json.dump(entry, f)
                f.write("\n")
        except IOError:
            pass

    def get_log(self) -> list[dict[str, Any]]:
        """Retrieve all parsed provenance entries from the log.

        Returns:
            List of provenance entry dictionaries. Returns empty list if the log
            file does not exist or is unreadable.
        """
        if not self.provenance_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with open(self.provenance_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except IOError:
            return []
        return entries
