from __future__ import annotations

from pathlib import Path
from typing import Any

from atticus.tools.registry import HarnessTool, ToolResult, ToolContext, register_tool


@register_tool
class EditTool(HarnessTool):
    """Make a targeted text replacement in a file (find/replace with context)."""

    @property
    def name(self) -> str:
        return "Edit"

    @property
    def description(self) -> str:
        return "Make a targeted text replacement in a file (find/replace with context)."

    def can_handle(self, stage: str) -> bool:
        """Check if tool is available in the given stage.

        Args:
            stage: The harness stage to check.

        Returns:
            True if stage is resolve, harvest, review, or repair.
        """
        return stage in (
            "resolve",
            "evidence-ingest-resolve",
            "harvest",
            "review",
            "repair",
        )

    def invoke(self, params: dict, context: ToolContext) -> ToolResult:
        """Execute the edit tool with given parameters and context.

        Args:
            params: Tool parameters including path, old_text, new_text,
                expected_replacements.
            context: Tool execution context.

        Returns:
            ToolResult containing the output and metadata.
        """
        path_str = params.get("path")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(
                content="",
                success=False,
                error="path is required and must be a non-empty string",
            )

        old_text = params.get("old_text")
        if not isinstance(old_text, str):
            return ToolResult(
                content="",
                success=False,
                error="old_text is required and must be a string",
            )

        new_text = params.get("new_text")
        if not isinstance(new_text, str):
            return ToolResult(
                content="",
                success=False,
                error="new_text is required and must be a string",
            )

        expected_replacements = params.get("expected_replacements", 1)
        if not isinstance(expected_replacements, int) or expected_replacements < 0:
            expected_replacements = 1

        path = Path(path_str).resolve()

        if not path.is_file():
            return ToolResult(
                content="",
                success=False,
                error=f"File not found: {path}",
            )

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(
                content="",
                success=False,
                error=f"Failed to read file: {e}",
            )

        count = content.count(old_text)

        if count != expected_replacements:
            return ToolResult(
                content="",
                metadata={"expected": expected_replacements, "found": count},
                success=False,
                error=f"Expected {expected_replacements} replacement(s), found {count}",
            )

        new_content = content.replace(old_text, new_text)

        try:
            path.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return ToolResult(
                content="",
                success=False,
                error=f"Failed to write file: {e}",
            )

        if context.provenance_logger is not None:
            try:
                log_method = getattr(context.provenance_logger, "log", None)
                if callable(log_method):
                    log_method(
                        tool="Edit",
                        action="edit",
                        path=str(path),
                        old_text_length=len(old_text),
                        new_text_length=len(new_text),
                        replacements=count,
                    )
            except Exception:
                pass

        return ToolResult(
            content={"path": str(path), "replacements": count},
            metadata={
                "old_text_length": len(old_text),
                "new_text_length": len(new_text),
            },
            success=True,
            error=None,
        )
