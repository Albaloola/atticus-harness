from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from atticus.tools.copy import CopyTool, compute_sha256
from atticus.tools.registry import ToolContext, ToolResult


@pytest.fixture
def copy_tool() -> CopyTool:
    return CopyTool()


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        stage="execute",
        workspace_path=tmp_path,
        provenance_logger=None,
    )


def test_can_handle_execute_stage(copy_tool: CopyTool):
    assert copy_tool.can_handle("execute") is True
    assert copy_tool.can_handle("evidence-ingest-execute") is True


def test_can_handle_repair_stage(copy_tool: CopyTool):
    assert copy_tool.can_handle("repair") is True


def test_can_handle_register_stage(copy_tool: CopyTool):
    assert copy_tool.can_handle("register") is True
    assert copy_tool.can_handle("evidence-ingest-register") is True


def test_can_handle_other_stages(copy_tool: CopyTool):
    assert copy_tool.can_handle("analyse") is False
    assert copy_tool.can_handle("review") is False
    assert copy_tool.can_handle("harvest") is False


def test_invoke_copies_file_correctly(copy_tool: CopyTool, tool_context: ToolContext, tmp_path: Path):
    src_file = tmp_path / "source.txt"
    src_file.write_text("Hello, Atticus!")
    dst_file = tmp_path / "dest.txt"

    result = copy_tool.invoke({"src": str(src_file), "dst": str(dst_file)}, tool_context)

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert dst_file.exists()
    assert dst_file.read_text() == "Hello, Atticus!"


def test_invoke_preserves_sha256(copy_tool: CopyTool, tool_context: ToolContext, tmp_path: Path):
    src_file = tmp_path / "source.txt"
    src_file.write_text("SHA-256 test content")
    dst_file = tmp_path / "dest.txt"

    src_sha = compute_sha256(src_file)
    assert src_sha == hashlib.sha256(src_file.read_bytes()).hexdigest()

    result = copy_tool.invoke({"src": str(src_file), "dst": str(dst_file)}, tool_context)

    assert result.success is True
    assert result.content["sha256"] == src_sha
    assert result.metadata["sha256"] == src_sha

    dst_sha = compute_sha256(dst_file)
    assert dst_sha == src_sha


def test_invoke_creates_parent_dirs(copy_tool: CopyTool, tool_context: ToolContext, tmp_path: Path):
    src_file = tmp_path / "source.txt"
    src_file.write_text("Parent dirs test")
    dst_file = tmp_path / "subdir1" / "subdir2" / "dest.txt"

    result = copy_tool.invoke({"src": str(src_file), "dst": str(dst_file)}, tool_context)

    assert result.success is True
    assert dst_file.exists()
    assert dst_file.parent.exists()


def test_invoke_nonexistent_src_returns_failure(copy_tool: CopyTool, tool_context: ToolContext, tmp_path: Path):
    nonexistent_src = tmp_path / "nonexistent.txt"
    dst_file = tmp_path / "dest.txt"

    result = copy_tool.invoke({"src": str(nonexistent_src), "dst": str(dst_file)}, tool_context)

    assert result.success is False
    assert result.error is not None
    assert "does not exist" in result.error.lower() or "nonexistent" in result.error.lower()
    assert not dst_file.exists()


def test_invoke_missing_src_param(copy_tool: CopyTool, tool_context: ToolContext, tmp_path: Path):
    dst_file = tmp_path / "dest.txt"

    result = copy_tool.invoke({"dst": str(dst_file)}, tool_context)

    assert result.success is False
    assert "src parameter is required" in result.error


def test_invoke_missing_dst_param(copy_tool: CopyTool, tool_context: ToolContext, tmp_path: Path):
    src_file = tmp_path / "source.txt"
    src_file.write_text("test")

    result = copy_tool.invoke({"src": str(src_file)}, tool_context)

    assert result.success is False
    assert "dst parameter is required" in result.error


def test_invoke_with_provenance_logger(copy_tool: CopyTool, tmp_path: Path):
    logged_entries = []

    def mock_logger(entry):
        logged_entries.append(entry)

    context = ToolContext(
        stage="execute",
        workspace_path=tmp_path,
        provenance_logger=mock_logger,
    )

    src_file = tmp_path / "source.txt"
    src_file.write_text("Provenance test")
    dst_file = tmp_path / "dest.txt"

    result = copy_tool.invoke(
        {"src": str(src_file), "dst": str(dst_file), "reason": "test copy"},
        context,
    )

    assert result.success is True
    assert len(logged_entries) == 1
    entry = logged_entries[0]
    assert entry["operation"] == "copy"
    assert entry["from"] == str(src_file.resolve())
    assert entry["to"] == str(dst_file.resolve())
    assert "sha256" in entry
    assert entry["reason"] == "test copy"


def test_invoke_copies_binary_file(copy_tool: CopyTool, tool_context: ToolContext, tmp_path: Path):
    src_file = tmp_path / "binary.bin"
    binary_data = bytes(range(256))
    src_file.write_bytes(binary_data)
    dst_file = tmp_path / "dest.bin"

    result = copy_tool.invoke({"src": str(src_file), "dst": str(dst_file)}, tool_context)

    assert result.success is True
    assert dst_file.read_bytes() == binary_data


def test_compute_sha256_returns_valid_hash(tmp_path: Path):
    test_file = tmp_path / "hash_test.txt"
    test_file.write_text("test content")

    sha256 = compute_sha256(test_file)
    assert len(sha256) == 64
    assert all(c in "0123456789abcdef" for c in sha256)


def test_invoke_preserve_sha_false(copy_tool: CopyTool, tool_context: ToolContext, tmp_path: Path):
    src_file = tmp_path / "source.txt"
    src_file.write_text("No SHA preservation")
    dst_file = tmp_path / "dest.txt"

    result = copy_tool.invoke(
        {"src": str(src_file), "dst": str(dst_file), "preserve_sha": False},
        tool_context,
    )

    assert result.success is True
    assert result.content["sha256"] is None
