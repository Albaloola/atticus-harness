from pathlib import Path
from typing import Any, Dict, List

import pytest

from atticus.tools.delete import DeleteTool
from atticus.tools.registry import ToolContext, ToolResult


@pytest.fixture
def delete_tool() -> DeleteTool:
    return DeleteTool()


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        stage="repair",
        workspace_path=tmp_path,
        provenance_logger=None,
    )


class TestDeleteToolCanHandle:
    def test_can_handle_repair_stage(self, delete_tool: DeleteTool):
        assert delete_tool.can_handle("repair") is True

    def test_can_handle_cleanup_stage(self, delete_tool: DeleteTool):
        assert delete_tool.can_handle("cleanup") is True

    def test_can_handle_execute_stage(self, delete_tool: DeleteTool):
        assert delete_tool.can_handle("execute") is True

    def test_cannot_handle_other_stages(self, delete_tool: DeleteTool):
        assert delete_tool.can_handle("review") is False
        assert delete_tool.can_handle("harvest") is False
        assert delete_tool.can_handle("unknown") is False


class TestDeleteToolInvokeFile:
    def test_delete_file_success(self, delete_tool: DeleteTool, tool_context: ToolContext, tmp_path: Path):
        test_file = tmp_path / "test_file.txt"
        test_file.write_text("content")

        params: Dict[str, Any] = {"path": str(test_file)}
        result: ToolResult = delete_tool.invoke(params, tool_context)

        assert result.success is True
        assert result.error is None
        assert result.content == {"path": str(test_file.resolve()), "type": "file"}
        assert not test_file.exists()

    def test_delete_file_with_reason(self, delete_tool: DeleteTool, tool_context: ToolContext, tmp_path: Path):
        test_file = tmp_path / "test_file.txt"
        test_file.write_text("content")

        params: Dict[str, Any] = {"path": str(test_file), "reason": "cleanup test"}
        result: ToolResult = delete_tool.invoke(params, tool_context)

        assert result.success is True
        assert result.metadata.get("reason") == "cleanup test"

    def test_delete_nonexistent_file(self, delete_tool: DeleteTool, tool_context: ToolContext, tmp_path: Path):
        nonexistent = tmp_path / "does_not_exist.txt"

        params: Dict[str, Any] = {"path": str(nonexistent)}
        result: ToolResult = delete_tool.invoke(params, tool_context)

        assert result.success is False
        assert result.error is not None
        assert "Path not found" in result.error

    def test_delete_file_missing_path_param(self, delete_tool: DeleteTool, tool_context: ToolContext):
        params: Dict[str, Any] = {}
        result: ToolResult = delete_tool.invoke(params, tool_context)

        assert result.success is False
        assert result.error == "Missing required parameter: path"


class TestDeleteToolInvokeDirectory:
    def test_delete_empty_directory_success(self, delete_tool: DeleteTool, tool_context: ToolContext, tmp_path: Path):
        test_dir = tmp_path / "empty_dir"
        test_dir.mkdir()

        params: Dict[str, Any] = {"path": str(test_dir)}
        result: ToolResult = delete_tool.invoke(params, tool_context)

        assert result.success is True
        assert result.error is None
        assert result.content == {"path": str(test_dir.resolve()), "type": "directory"}
        assert not test_dir.exists()

    def test_delete_non_empty_directory_fails(self, delete_tool: DeleteTool, tool_context: ToolContext, tmp_path: Path):
        test_dir = tmp_path / "non_empty_dir"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        params: Dict[str, Any] = {"path": str(test_dir)}
        result: ToolResult = delete_tool.invoke(params, tool_context)

        assert result.success is False
        assert result.error is not None
        assert "Failed to delete" in result.error
        assert test_dir.exists()

    def test_delete_directory_with_reason(self, delete_tool: DeleteTool, tool_context: ToolContext, tmp_path: Path):
        test_dir = tmp_path / "empty_dir"
        test_dir.mkdir()

        params: Dict[str, Any] = {"path": str(test_dir), "reason": "remove temp dir"}
        result: ToolResult = delete_tool.invoke(params, tool_context)

        assert result.success is True
        assert result.metadata.get("reason") == "remove temp dir"


class TestDeleteToolProvenanceLogging:
    def test_provenance_logger_called_on_file_delete(self, delete_tool: DeleteTool, tmp_path: Path):
        logs: List[Dict[str, str]] = []

        def provenance_logger(entry: Dict[str, str]) -> None:
            logs.append(entry)

        context = ToolContext(
            stage="repair",
            workspace_path=tmp_path,
            provenance_logger=provenance_logger,
        )

        test_file = tmp_path / "test_file.txt"
        test_file.write_text("content")

        params: Dict[str, Any] = {"path": str(test_file), "reason": "test reason"}
        delete_tool.invoke(params, context)

        assert len(logs) == 1
        assert logs[0]["operation"] == "delete"
        assert logs[0]["path"] == str(test_file.resolve())
        assert logs[0]["reason"] == "test reason"
        assert "timestamp" in logs[0]

    def test_provenance_logger_called_on_directory_delete(self, delete_tool: DeleteTool, tmp_path: Path):
        logs: List[Dict[str, str]] = []

        def provenance_logger(entry: Dict[str, str]) -> None:
            logs.append(entry)

        context = ToolContext(
            stage="cleanup",
            workspace_path=tmp_path,
            provenance_logger=provenance_logger,
        )

        test_dir = tmp_path / "empty_dir"
        test_dir.mkdir()

        params: Dict[str, Any] = {"path": str(test_dir)}
        delete_tool.invoke(params, context)

        assert len(logs) == 1
        assert logs[0]["operation"] == "delete"
        assert logs[0]["path"] == str(test_dir.resolve())

    def test_no_provenance_logging_when_logger_none(self, delete_tool: DeleteTool, tool_context: ToolContext, tmp_path: Path):
        test_file = tmp_path / "test_file.txt"
        test_file.write_text("content")

        params: Dict[str, Any] = {"path": str(test_file)}
        result: ToolResult = delete_tool.invoke(params, tool_context)

        assert result.success is True
