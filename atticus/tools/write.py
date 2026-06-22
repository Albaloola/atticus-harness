from __future__ import annotations

from pathlib import Path
from typing import Any

from atticus.tools.registry import HarnessTool, ToolContext, ToolResult, register_tool


@register_tool
class WriteTool(HarnessTool):
    """Write content to a file, creating directories as needed.

    Attributes:
        name: Tool name "Write".
        description: Tool description.
    """

    @property
    def name(self) -> str:
        """Tool name.

        Returns:
            The string "Write".
        """
        return "Write"

    @property
    def description(self) -> str:
        """Tool description.

        Returns:
            Description of the Write tool.
        """
        return "Write content to a file, creating directories as needed."

    def can_handle(self, stage: str) -> bool:
        """Check if tool is available in the given stage.

        Args:
            stage: The harness stage to check.

        Returns:
            True for all stages except scan stage.
        """
        return "scan" not in stage.lower()

    def invoke(self, params: dict, context: ToolContext) -> ToolResult:
        """Write content to a file.

        Args:
            params: Tool parameters including:
                - path (str, required): File path to write.
                - content (str | bytes, required): Content to write.
                - mode (str, optional): Write mode, "w" for text or "wb" for bytes.
            context: Tool execution context.

        Returns:
            ToolResult with write operation result.
        """
        try:
            path_param = params.get("path")
            content = params.get("content")
            mode = params.get("mode", "w")

            if path_param is None:
                return ToolResult(
                    content={"path": None, "bytes_written": 0},
                    metadata={"path": None, "mode": mode},
                    success=False,
                    error="Missing required parameter: path",
                )

            if content is None:
                return ToolResult(
                    content={"path": str(path_param), "bytes_written": 0},
                    metadata={"path": str(path_param), "mode": mode},
                    success=False,
                    error="Missing required parameter: content",
                )

            path = Path(path_param).resolve()

            path.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(content, bytes) or mode == "wb":
                if isinstance(content, str):
                    content_bytes = content.encode("utf-8")
                else:
                    content_bytes = content
                with open(path, "wb") as f:
                    f.write(content_bytes)
                bytes_written = len(content_bytes)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                bytes_written = len(content.encode("utf-8")) if isinstance(content, str) else len(content)

            if context.provenance_logger is not None:
                context.provenance_logger.log(
                    "write",
                    {"path": str(path), "bytes_written": bytes_written},
                )

            return ToolResult(
                content={"path": str(path), "bytes_written": bytes_written},
                metadata={"path": str(path), "mode": mode},
                success=True,
                error=None,
            )

        except PermissionError as e:
            return ToolResult(
                content={"path": str(path_param) if "path_param" in locals() else None, "bytes_written": 0},
                metadata={"path": str(path_param) if "path_param" in locals() else None, "mode": params.get("mode", "w")},
                success=False,
                error=f"Permission error: {e}",
            )
        except OSError as e:
            return ToolResult(
                content={"path": str(path_param) if "path_param" in locals() else None, "bytes_written": 0},
                metadata={"path": str(path_param) if "path_param" in locals() else None, "mode": params.get("mode", "w")},
                success=False,
                error=f"OS error: {e}",
            )
        except Exception as e:
            return ToolResult(
                content={"path": str(path_param) if "path_param" in locals() else None, "bytes_written": 0},
                metadata={"path": str(path_param) if "path_param" in locals() else None, "mode": params.get("mode", "w")},
                success=False,
                error=str(e),
            )
