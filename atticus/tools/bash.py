from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

from atticus.tools.registry import HarnessTool, ToolResult, ToolContext, STAGE_TOOL_ALLOWANCES, register_tool


@register_tool
class BashTool(HarnessTool):
    """Run a shell command with timeout and safety restrictions."""

    @property
    def name(self) -> str:
        return "Bash"

    @property
    def description(self) -> str:
        return "Run a shell command with timeout and safety restrictions."

    def can_handle(self, stage: str) -> bool:
        """Check if tool is available in the given stage.

        Args:
            stage: The harness stage to check.

        Returns:
            True if tool is available in the given stage.
        """
        tool_name = self.name
        for allowed_stage, allowed_tools in STAGE_TOOL_ALLOWANCES.items():
            if stage == allowed_stage and tool_name in allowed_tools:
                return True
        return False

    def invoke(self, params: dict, context: ToolContext) -> ToolResult:
        """Execute the Bash tool with given parameters and context.

        Args:
            params: Tool parameters including:
                - command (required): Shell command to run.
                - timeout (optional): Timeout in seconds (default 60).
                - cwd (optional): Working directory (defaults to context.workspace_path).
                - sandboxed (optional): If True, restrict to workspace directory.
            context: Tool execution context with workspace_path attribute.

        Returns:
            ToolResult containing the output and metadata.
        """
        command = params.get("command")
        if not command or not isinstance(command, str):
            return ToolResult(
                content={"stdout": "", "stderr": "", "returncode": -1},
                metadata={"error": "command is required"},
                success=False,
                error="command is required",
            )

        timeout = int(params.get("timeout", 60))
        cwd = params.get("cwd", str(context.workspace_path))
        sandboxed = params.get("sandboxed", True)

        if sandboxed:
            workspace_path = Path(context.workspace_path).resolve()
            cwd_path = Path(cwd).resolve()

            if not str(cwd_path).startswith(str(workspace_path)):
                return ToolResult(
                    content={"stdout": "", "stderr": "", "returncode": -1},
                    metadata={"command": command, "cwd": cwd},
                    success=False,
                    error=f"Sandboxed mode: cwd {cwd} is outside workspace {workspace_path}",
                )

            if ".." in command:
                return ToolResult(
                    content={"stdout": "", "stderr": "", "returncode": -1},
                    metadata={"command": command, "cwd": cwd},
                    success=False,
                    error="Sandboxed mode: command cannot contain '..'",
                )

        try:
            result = subprocess.run(
                shlex.split(command),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            return ToolResult(
                content={
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                },
                metadata={
                    "command": command,
                    "cwd": cwd,
                    "timeout": timeout,
                },
                success=result.returncode == 0,
                error=result.stderr if result.returncode != 0 else None,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                content={"stdout": "", "stderr": f"Command timed out after {timeout} seconds", "returncode": -1},
                metadata={"command": command, "cwd": cwd, "timeout": timeout},
                success=False,
                error=f"Command timed out after {timeout} seconds",
            )
        except Exception as e:
            return ToolResult(
                content={"stdout": "", "stderr": str(e), "returncode": -1},
                metadata={"command": command, "cwd": cwd},
                success=False,
                error=str(e),
            )
