from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atticus.tools.read import ReadTool
from atticus.tools.registry import ToolContext, ToolResult
from atticus.tools.token_budget import truncate_text_to_tokens


@pytest.fixture
def read_tool() -> ReadTool:
    return ReadTool()


@pytest.fixture
def tool_context() -> ToolContext:
    return ToolContext(
        stage="test",
        workspace_path=Path("/tmp"),
        db_path=None,
        provenance_logger=None,
        token_budget=None,
    )


class TestReadToolCanHandle:
    def test_can_handle_all_stages(self, read_tool: ReadTool):
        stages = [
            "evidence-ingest-scan",
            "evidence-ingest-analyse",
            "evidence-ingest-resolve",
            "evidence-ingest-execute",
            "evidence-ingest-register",
            "extract-sources",
            "harvest",
            "review",
            "repair",
            "final-gate",
            "unknown-stage",
        ]
        for stage in stages:
            assert read_tool.can_handle(stage) is True


class TestReadToolInvokeTextFile:
    def test_read_text_file(self, read_tool: ReadTool, tool_context: ToolContext, tmp_path: Path):
        test_file = tmp_path / "test.txt"
        test_content = "Hello, World!\nThis is a test file."
        test_file.write_text(test_content)

        result = read_tool.invoke({"path": str(test_file)}, tool_context)

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.content == test_content
        assert result.error is None
        assert result.metadata["path"] == str(test_file)
        assert result.metadata["size"] == len(test_content)

    def test_read_text_file_with_unicode(self, read_tool: ReadTool, tool_context: ToolContext, tmp_path: Path):
        test_file = tmp_path / "test_unicode.txt"
        test_content = "Café ñoño 中文 🎉"
        test_file.write_text(test_content, encoding="utf-8")

        result = read_tool.invoke({"path": str(test_file)}, tool_context)

        assert result.success is True
        assert result.content == test_content

    def test_read_markdown_file(self, read_tool: ReadTool, tool_context: ToolContext, tmp_path: Path):
        test_file = tmp_path / "test.md"
        test_content = "# Header\n\nSome **bold** text."
        test_file.write_text(test_content)

        result = read_tool.invoke({"path": str(test_file)}, tool_context)

        assert result.success is True
        assert result.content == test_content


class TestReadToolInvokeMaxTokens:
    def test_read_with_max_tokens_truncation(self, read_tool: ReadTool, tmp_path: Path):
        context = ToolContext(
            stage="evidence-ingest-analyse",
            workspace_path=tmp_path,
            db_path=None,
            provenance_logger=None,
            token_budget=None,
        )

        test_file = tmp_path / "large_file.txt"
        content = "a" * 10000
        test_file.write_text(content)

        result = read_tool.invoke({"path": str(test_file), "max_tokens": 100}, context)

        assert result.success is True
        assert result.metadata["tokens_used"] <= 100
        assert result.metadata["truncated"] is True

    def test_read_with_default_max_tokens_2000(self, read_tool: ReadTool, tmp_path: Path):
        context = ToolContext(
            stage="evidence-ingest-analyse",
            workspace_path=tmp_path,
            db_path=None,
            provenance_logger=None,
            token_budget=None,
        )

        test_file = tmp_path / "large_file.txt"
        content = "b" * 10000
        test_file.write_text(content)

        result = read_tool.invoke({"path": str(test_file)}, context)

        assert result.success is True
        assert result.metadata["tokens_used"] <= 2000
        assert result.metadata["truncated"] is True


class TestReadToolInvokeNonExistentFile:
    def test_read_non_existent_file(self, read_tool: ReadTool, tool_context: ToolContext, tmp_path: Path):
        non_existent = tmp_path / "does_not_exist.txt"

        result = read_tool.invoke({"path": str(non_existent)}, tool_context)

        assert result.success is False
        assert result.content == ""
        assert "File not found" in result.error
        assert result.metadata["path"] == str(non_existent)

    def test_read_directory_instead_of_file(self, read_tool: ReadTool, tool_context: ToolContext, tmp_path: Path):
        result = read_tool.invoke({"path": str(tmp_path)}, tool_context)

        assert result.success is False
        assert "Not a file" in result.error


class TestReadToolInvokeImageFile:
    def test_read_png_image_file(self, read_tool: ReadTool, tool_context: ToolContext, tmp_path: Path):
        image_file = tmp_path / "test.png"
        png_bytes = (
            b'\x89PNG\r\n\x1a\n'
            b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
            b'\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N'
            b'\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        image_file.write_bytes(png_bytes)

        result = read_tool.invoke({"path": str(image_file)}, tool_context)

        assert result.success is True
        assert result.metadata["encoding"] == "base64"
        assert result.metadata["mime_type"] == "image/png"
        assert len(result.content) > 0

    def test_read_jpeg_image_file(self, read_tool: ReadTool, tool_context: ToolContext, tmp_path: Path):
        image_file = tmp_path / "test.jpg"
        jpeg_bytes = (
            b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x60\x00\x60\x00\x00'
            b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x04\x11\x05\x12!1\x06\x12AQ\x07aq\x13"2\x81\x08\x14B\x91\xa1\xb1\xc1\t#3C\xd1\x15R\xd2\xe1\xf0$4r\x82%\'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\x92\xbf\xff\xd9'
        )
        image_file.write_bytes(jpeg_bytes)

        result = read_tool.invoke({"path": str(image_file)}, tool_context)

        assert result.success is True
        assert result.metadata["encoding"] == "base64"
        assert result.metadata["mime_type"] == "image/jpeg"


class TestReadToolTokenBudgetEnforcement:
    def test_token_budget_truncation_to_2000_tokens(self, read_tool: ReadTool, tmp_path: Path):
        context = ToolContext(
            stage="evidence-ingest-analyse",
            workspace_path=tmp_path,
            db_path=None,
            provenance_logger=None,
            token_budget=None,
        )

        test_file = tmp_path / "huge_file.txt"
        content = "x" * 20000
        test_file.write_text(content)

        result = read_tool.invoke({"path": str(test_file)}, context)

        assert result.success is True
        assert result.metadata["tokens_used"] <= 2000
        assert result.metadata["truncated"] is True

    def test_truncate_text_to_tokens_function(self):
        text = "a" * 10000
        truncated, tokens_used = truncate_text_to_tokens(text, max_tokens=100)

        assert tokens_used <= 100

    def test_truncate_text_to_tokens_no_truncation_needed(self):
        text = "short text"
        truncated, tokens_used = truncate_text_to_tokens(text, max_tokens=2000)

        assert truncated == text
        assert tokens_used <= 2000


class TestReadToolMissingPath:
    def test_invoke_without_path(self, read_tool: ReadTool, tool_context: ToolContext):
        result = read_tool.invoke({}, tool_context)

        assert result.success is False
        assert result.error == "path is required"

    def test_invoke_with_none_path(self, read_tool: ReadTool, tool_context: ToolContext):
        result = read_tool.invoke({"path": None}, tool_context)

        assert result.success is False
        assert result.error == "path is required"

    def test_invoke_with_non_string_path(self, read_tool: ReadTool, tool_context: ToolContext):
        result = read_tool.invoke({"path": 123}, tool_context)

        assert result.success is False
        assert result.error == "path is required"
