from __future__ import annotations

import time
from pathlib import Path

import pytest

from atticus.tools.bash import BashTool as Bash
from atticus.tools.registry import ToolContext, ToolResult


@pytest.fixture
def bash_tool():
    return Bash()


@pytest.fixture
def tool_context(tmp_path):
    return ToolContext(
        stage="review",
        workspace_path=tmp_path,
    )


class TestBashToolCanHandle:
    def test_can_handle_allowed_stages(self, bash_tool):
        allowed_stages = [
            "evidence-ingest-scan",
            "evidence-ingest-register",
            "extract-sources",
            "review",
            "repair",
        ]
        for stage in allowed_stages:
            assert bash_tool.can_handle(stage) is True

    def test_can_handle_disallowed_stages(self, bash_tool):
        disallowed_stages = [
            "evidence-ingest-analyse",
            "evidence-ingest-resolve",
            "evidence-ingest-execute",
            "harvest",
            "final-gate",
        ]
        for stage in disallowed_stages:
            assert bash_tool.can_handle(stage) is False


class TestBashToolInvoke:
    def test_invoke_simple_command(self, bash_tool, tool_context):
        result = bash_tool.invoke({"command": "echo hello"}, tool_context)

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert "hello" in result.content["stdout"]

    def test_invoke_captures_stdout(self, bash_tool, tool_context):
        result = bash_tool.invoke({"command": "echo 'test output'"}, tool_context)

        assert result.success is True
        assert result.content["stdout"].strip() == "test output"

    def test_invoke_captures_stderr(self, bash_tool, tool_context):
        result = bash_tool.invoke(
            {"command": "python -c \"import sys; sys.stderr.write('error msg')\""},
            tool_context,
        )

        assert result.success is True
        assert "error msg" in result.content["stderr"]

    def test_invoke_captures_returncode(self, bash_tool, tool_context):
        result = bash_tool.invoke({"command": "python -c \"exit(42)\""}, tool_context)

        assert result.success is False
        assert result.content["returncode"] == 42

    def test_invoke_non_zero_return_code(self, bash_tool, tool_context):
        result = bash_tool.invoke({"command": "false"}, tool_context)

        assert result.success is False
        assert result.content["returncode"] != 0
        assert result.error is not None

    def test_invoke_timeout(self, bash_tool, tool_context):
        start = time.time()
        result = bash_tool.invoke(
            {"command": "sleep 5", "timeout": 1},
            tool_context,
        )
        elapsed = time.time() - start

        assert result.success is False
        assert "timed out" in result.error.lower()
        assert elapsed < 3

    def test_invoke_sandboxed_rejects_outside_workspace(self, bash_tool, tool_context):
        result = bash_tool.invoke(
            {"command": "echo test", "cwd": "/tmp", "sandboxed": True},
            tool_context,
        )

        assert result.success is False
        assert "outside workspace" in result.error.lower()

    def test_invoke_sandboxed_allows_inside_workspace(self, bash_tool, tool_context):
        result = bash_tool.invoke(
            {"command": "echo 'inside workspace'", "sandboxed": True},
            tool_context,
        )

        assert result.success is True
        assert "inside workspace" in result.content["stdout"]

    def test_invoke_sandboxed_rejects_double_dot_in_command(self, bash_tool, tool_context):
        result = bash_tool.invoke(
            {"command": "cd .. && echo test", "sandboxed": True},
            tool_context,
        )

        assert result.success is False
        assert ".." in result.error

    def test_invoke_unsandboxed_allows_outside_workspace(self, bash_tool, tool_context):
        result = bash_tool.invoke(
            {"command": "echo 'outside'", "cwd": "/tmp", "sandboxed": False},
            tool_context,
        )

        assert result.success is True
        assert "outside" in result.content["stdout"]

    def test_invoke_missing_command_returns_error(self, bash_tool, tool_context):
        result = bash_tool.invoke({"command": ""}, tool_context)

        assert result.success is False
        assert result.error == "command is required"

    def test_invoke_metadata_contains_command_and_cwd(self, bash_tool, tool_context):
        result = bash_tool.invoke(
            {"command": "echo test", "cwd": str(tool_context.workspace_path)},
            tool_context,
        )

        assert "command" in result.metadata
        assert "cwd" in result.metadata
        assert result.metadata["command"] == "echo test"
