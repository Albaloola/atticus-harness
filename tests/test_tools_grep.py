from pathlib import Path

import pytest

from atticus.tools.grep import GrepTool as Grep
from atticus.tools.registry import ToolContext, ToolResult


@pytest.fixture
def grep_tool():
    return Grep()


@pytest.fixture
def tool_context(tmp_path):
    return ToolContext(
        stage="harvest",
        workspace_path=tmp_path,
    )


def create_test_files(tmp_path, files_dict):
    for rel_path, content in files_dict.items():
        file_path = tmp_path / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)


class TestGrepCanHandle:
    def test_can_handle_allowed_stages(self, grep_tool):
        allowed_stages = ["resolve", "harvest", "review", "repair", "evidence-ingest-resolve"]
        for stage in allowed_stages:
            assert grep_tool.can_handle(stage) is True

    def test_can_handle_disallowed_stages(self, grep_tool):
        disallowed_stages = ["evidence-ingest-scan", "evidence-ingest-analyse", "evidence-ingest-execute", "extract-sources", "final-gate"]
        for stage in disallowed_stages:
            assert grep_tool.can_handle(stage) is False


class TestGrepInvoke:
    def test_invoke_finds_matches(self, grep_tool, tool_context, tmp_path):
        create_test_files(tmp_path, {
            "file1.txt": "hello world\nfoo bar\nbaz qux",
            "file2.txt": "test hello\nother line",
        })
        result = grep_tool.invoke({"pattern": "hello"}, tool_context)
        assert result.success is True
        assert isinstance(result.content, list)
        assert len(result.content) == 2
        assert all("hello" in m["line"] for m in result.content)

    def test_invoke_with_include_filter(self, grep_tool, tool_context, tmp_path):
        create_test_files(tmp_path, {
            "file1.txt": "hello world",
            "file2.py": "hello world",
            "subdir/file3.txt": "hello again",
        })
        result = grep_tool.invoke({"pattern": "hello", "include": "*.txt"}, tool_context)
        assert result.success is True
        paths = [m["path"] for m in result.content]
        assert all(p.endswith(".txt") for p in paths)
        assert len(result.content) == 2

    def test_invoke_with_max_results(self, grep_tool, tool_context, tmp_path):
        create_test_files(tmp_path, {
            "file1.txt": "match here",
            "file2.txt": "match here too",
            "file3.txt": "another match",
            "file4.txt": "match again",
        })
        result = grep_tool.invoke({"pattern": "match", "max_results": 2}, tool_context)
        assert result.success is True
        assert len(result.content) == 2

    def test_invoke_no_matches(self, grep_tool, tool_context, tmp_path):
        create_test_files(tmp_path, {
            "file1.txt": "hello world",
            "file2.txt": "foo bar",
        })
        result = grep_tool.invoke({"pattern": "nonexistentpattern"}, tool_context)
        assert result.success is True
        assert result.content == []

    def test_invoke_missing_pattern(self, grep_tool, tool_context):
        result = grep_tool.invoke({"pattern": ""}, tool_context)
        assert result.success is False
        assert result.error == "pattern is required"

    def test_invoke_results_sorted_by_mtime(self, grep_tool, tool_context, tmp_path):
        import os
        import time
        old_file = tmp_path / "old.txt"
        old_file.write_text("match in old")
        old_mtime = old_file.stat().st_mtime
        time.sleep(1.5)
        new_file = tmp_path / "new.txt"
        new_file.write_text("match in new")
        new_mtime = new_file.stat().st_mtime
        
        assert new_mtime > old_mtime, "new file should have later mtime"
        
        result = grep_tool.invoke({"pattern": "match"}, tool_context)
        assert result.success is True
        assert len(result.content) == 2
        paths = [Path(m["path"]) for m in result.content]
        assert paths[0].stat().st_mtime >= paths[1].stat().st_mtime

    def test_invoke_result_structure(self, grep_tool, tool_context, tmp_path):
        create_test_files(tmp_path, {"test.txt": "line with pattern"})
        result = grep_tool.invoke({"pattern": "pattern"}, tool_context)
        assert result.success is True
        match = result.content[0]
        assert "path" in match
        assert "line_number" in match
        assert "line" in match
        assert "match" in match
        assert match["line_number"] == 1
        assert match["line"] == "line with pattern"
        assert match["match"] == "pattern"

    def test_invoke_with_custom_path(self, grep_tool, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        create_test_files(subdir, {"target.txt": "find me"})
        context = ToolContext(stage="harvest", workspace_path=tmp_path)
        result = grep_tool.invoke({"pattern": "find", "path": str(subdir)}, context)
        assert result.success is True
        assert len(result.content) == 1
