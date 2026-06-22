from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from atticus.tools.registry import ToolContext
from atticus.tools.write import WriteTool


@pytest.fixture
def write_tool() -> WriteTool:
    return WriteTool()


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        stage="execute",
        workspace_path=tmp_path,
        provenance_logger=None,
    )


def test_can_handle_returns_false_for_scan_stage(write_tool: WriteTool):
    assert write_tool.can_handle("scan") is False
    assert write_tool.can_handle("evidence-ingest-scan") is False


def test_can_handle_returns_true_for_non_scan_stages(write_tool: WriteTool):
    assert write_tool.can_handle("execute") is True
    assert write_tool.can_handle("evidence-ingest-execute") is True
    assert write_tool.can_handle("harvest") is True


def test_invoke_with_text_content(write_tool: WriteTool, tool_context: ToolContext, tmp_path: Path):
    test_file = tmp_path / "test_output.txt"
    content = "Hello, World!"

    result = write_tool.invoke(
        params={"path": str(test_file), "content": content},
        context=tool_context,
    )

    assert result.success is True
    assert result.error is None
    assert result.content["path"] == str(test_file)
    assert result.content["bytes_written"] == len(content.encode("utf-8"))
    assert test_file.exists()
    assert test_file.read_text() == content


def test_invoke_with_binary_content(write_tool: WriteTool, tool_context: ToolContext, tmp_path: Path):
    test_file = tmp_path / "test_output.bin"
    content = b"\x00\x01\x02\x03"

    result = write_tool.invoke(
        params={"path": str(test_file), "content": content, "mode": "wb"},
        context=tool_context,
    )

    assert result.success is True
    assert result.error is None
    assert result.content["path"] == str(test_file)
    assert result.content["bytes_written"] == len(content)
    assert test_file.exists()
    assert test_file.read_bytes() == content


def test_invoke_creates_parent_directories(write_tool: WriteTool, tool_context: ToolContext, tmp_path: Path):
    test_file = tmp_path / "subdir" / "nested" / "test.txt"
    content = "Nested file content"

    result = write_tool.invoke(
        params={"path": str(test_file), "content": content},
        context=tool_context,
    )

    assert result.success is True
    assert test_file.exists()
    assert test_file.read_text() == content


def test_invoke_with_nonexistent_parent_dirs(write_tool: WriteTool, tool_context: ToolContext, tmp_path: Path):
    test_file = tmp_path / "a" / "b" / "c" / "test.txt"
    content = "Deeply nested"

    result = write_tool.invoke(
        params={"path": str(test_file), "content": content},
        context=tool_context,
    )

    assert result.success is True
    assert test_file.exists()
    assert test_file.read_text() == content
    assert test_file.parent.exists()


def test_invoke_provenance_logging(write_tool: WriteTool, tool_context: ToolContext, tmp_path: Path):
    mock_logger = MagicMock()
    tool_context.provenance_logger = mock_logger

    test_file = tmp_path / "provenance_test.txt"
    content = "Provenance test"

    result = write_tool.invoke(
        params={"path": str(test_file), "content": content},
        context=tool_context,
    )

    assert result.success is True
    mock_logger.log.assert_called_once_with(
        "write",
        {"path": str(test_file), "bytes_written": len(content.encode("utf-8"))},
    )


def test_invoke_missing_path_parameter(write_tool: WriteTool, tool_context: ToolContext):
    result = write_tool.invoke(
        params={"content": "some content"},
        context=tool_context,
    )

    assert result.success is False
    assert result.error == "Missing required parameter: path"
    assert result.content["bytes_written"] == 0


def test_invoke_missing_content_parameter(write_tool: WriteTool, tool_context: ToolContext, tmp_path: Path):
    test_file = tmp_path / "missing_content.txt"

    result = write_tool.invoke(
        params={"path": str(test_file)},
        context=tool_context,
    )

    assert result.success is False
    assert result.error == "Missing required parameter: content"
    assert result.content["bytes_written"] == 0
