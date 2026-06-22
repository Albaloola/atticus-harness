from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from atticus.tools.registry import HarnessTool, ToolResult, register_tool


@register_tool
class DeleteTool(HarnessTool):
    """Tool for deleting files or empty directories with provenance logging."""

    name: str = "Delete"
    description: str = "Delete a file or empty directory, logging to provenance when used on source evidence."

    def can_handle(self, stage: str) -> bool:
        """Check if the tool can handle the given stage.

        Args:
            stage: The execution stage to check.

        Returns:
            True if stage is repair, cleanup, or execute; False otherwise.
        """
        return stage in {"repair", "cleanup", "execute"}

    def invoke(self, params: Dict[str, Any], context: Any) -> ToolResult:
        """Delete a file or empty directory.

        Args:
            params: Parameters for the deletion. Must contain "path", optional "reason".
            context: Execution context, may contain provenance_logger.

        Returns:
            ToolResult indicating success or failure of the deletion.
        """
        # Extract required parameters
        path_str: Optional[str] = params.get("path")
        if not path_str:
            return ToolResult(
                success=False,
                error="Missing required parameter: path",
                content=None,
                metadata={"params": params}
            )

        # Extract optional reason, default to "unspecified"
        reason: str = params.get("reason", "unspecified")

        # Resolve path to absolute
        abs_path = Path(path_str).resolve()

        # Check if path exists
        if not abs_path.exists():
            return ToolResult(
                success=False,
                error=f"Path not found: {abs_path}",
                content=None,
                metadata={"path": str(abs_path), "reason": reason}
            )

        # Prepare metadata
        metadata: Dict[str, str] = {
            "path": str(abs_path),
            "reason": reason
        }

        # Determine path type and perform deletion
        try:
            if abs_path.is_file():
                abs_path.unlink()
                content: Dict[str, str] = {
                    "path": str(abs_path),
                    "type": "file"
                }
            elif abs_path.is_dir():
                # rmdir only deletes empty directories
                abs_path.rmdir()
                content = {
                    "path": str(abs_path),
                    "type": "directory"
                }
            else:
                return ToolResult(
                    success=False,
                    error=f"Path is neither a file nor a directory: {abs_path}",
                    content=None,
                    metadata=metadata
                )
        except OSError as e:
            return ToolResult(
                success=False,
                error=f"Failed to delete {abs_path}: {str(e)}",
                content=None,
                metadata=metadata
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Unexpected error deleting {abs_path}: {str(e)}",
                content=None,
                metadata=metadata
            )

        # Log to provenance if available
        provenance_logger = getattr(context, 'provenance_logger', None)
        if provenance_logger is not None:
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            log_entry: Dict[str, str] = {
                "timestamp": timestamp,
                "operation": "delete",
                "path": str(abs_path),
                "reason": reason or "unspecified"
            }
            if callable(provenance_logger):
                provenance_logger(log_entry)

        # Return success result
        return ToolResult(
            success=True,
            error=None,
            content=content,
            metadata=metadata
        )
