from __future__ import annotations

from pathlib import Path
from typing import Any

from atticus.tools.registry import HarnessTool, ToolContext, ToolResult, register_tool


@register_tool
class GlobTool(HarnessTool):
    """List files matching a glob pattern in a directory."""

    @property
    def name(self) -> str:
        return "Glob"

    @property
    def description(self) -> str:
        return "List files matching a glob pattern in a directory."

    def can_handle(self, stage: str) -> bool:
        """Check if tool is available in the given stage.

        Glob tool is available in all stages.

        Args:
            stage: The harness stage to check.

        Returns:
            True always - Glob is available everywhere.
        """
        return True

    def invoke(self, params: dict, context: ToolContext) -> ToolResult:
        """Execute the Glob tool with given parameters and context.

        Args:
            params: Tool parameters including:
                - pattern (required): Glob pattern (e.g., "**/*.pdf").
                - path (optional): Directory to search (defaults to context.workspace_path).
            context: Tool execution context with workspace_path attribute.

        Returns:
            ToolResult containing the list of matching file paths and metadata.
        """
        pattern = params.get("pattern")
        if not pattern or not isinstance(pattern, str):
            return ToolResult(
                content=[],
                metadata={"pattern": pattern, "search_path": None, "match_count": 0},
                success=False,
                error="pattern is required",
            )

        path_param = params.get("path")
        if path_param and isinstance(path_param, str):
            search_path = Path(path_param).resolve()
        else:
            search_path = context.workspace_path.resolve()

        if not search_path.exists():
            return ToolResult(
                content=[],
                metadata={"pattern": pattern, "search_path": str(search_path), "match_count": 0},
                success=False,
                error=f"Search path not found: {search_path}",
            )

        if not search_path.is_dir():
            return ToolResult(
                content=[],
                metadata={"pattern": pattern, "search_path": str(search_path), "match_count": 0},
                success=False,
                error=f"Not a directory: {search_path}",
            )

        try:
            matched_files = []

            for item in search_path.glob(pattern):
                if item.is_file():
                    matched_files.append(item)

            matched_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

            result_paths = [str(p) for p in matched_files]

            return ToolResult(
                content=result_paths,
                metadata={
                    "pattern": pattern,
                    "search_path": str(search_path),
                    "match_count": len(result_paths),
                },
                success=True,
                error=None,
            )

        except Exception as e:
            return ToolResult(
                content=[],
                metadata={"pattern": pattern, "search_path": str(search_path), "match_count": 0},
                success=False,
                error=f"Glob search failed: {str(e)}",
            )
