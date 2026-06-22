from __future__ import annotations

from pathlib import Path

import pytest

from atticus.tools.glob import GlobTool as Glob
from atticus.tools.registry import ToolContext, ToolResult


@pytest.fixture
def glob_tool() -> Glob:
    return Glob()


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(stage="test", workspace_path=tmp_path)


def test_can_handle_returns_true_for_all_stages(glob_tool: Glob):
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
        assert glob_tool.can_handle(stage) is True


def test_invoke_with_txt_pattern_finds_correct_files(glob_tool: Glob, tool_context: ToolContext, tmp_path: Path):
    (tmp_path / "a.txt").write_text("content a")
    (tmp_path / "b.txt").write_text("content b")
    (tmp_path / "c.py").write_text("content c")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "d.txt").write_text("content d")

    result = glob_tool.invoke({"pattern": "*.txt"}, tool_context)

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert isinstance(result.content, list)
    assert sorted(result.content) == sorted([str(tmp_path / "a.txt"), str(tmp_path / "b.txt")])
    assert result.metadata["match_count"] == 2
    assert result.metadata["pattern"] == "*.txt"


def test_invoke_with_recursive_pattern_finds_files_in_subdirectories(glob_tool: Glob, tool_context: ToolContext, tmp_path: Path):
    (tmp_path / "a.py").write_text("content a")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "b.py").write_text("content b")
    subdir2 = tmp_path / "subdir" / "nested"
    subdir2.mkdir()
    (subdir2 / "c.py").write_text("content c")

    result = glob_tool.invoke({"pattern": "**/*.py"}, tool_context)

    assert result.success is True
    assert isinstance(result.content, list)
    assert str(tmp_path / "a.py") in result.content
    assert str(subdir / "b.py") in result.content
    assert str(subdir2 / "c.py") in result.content
    assert result.metadata["match_count"] == 3


def test_invoke_with_non_existent_directory_returns_empty_list(glob_tool: Glob, tmp_path: Path):
    non_existent = tmp_path / "does_not_exist"
    context = ToolContext(stage="test", workspace_path=non_existent)

    result = glob_tool.invoke({"pattern": "*.txt"}, context)

    assert result.success is False
    assert result.content == []
    assert result.metadata["match_count"] == 0
    assert "not found" in result.error.lower()


def test_invoke_results_sorted_by_modification_time_newest_first(glob_tool: Glob, tool_context: ToolContext, tmp_path: Path):
    import time

    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_c = tmp_path / "c.txt"

    file_a.write_text("content a")
    time.sleep(0.01)
    file_b.write_text("content b")
    time.sleep(0.01)
    file_c.write_text("content c")

    result = glob_tool.invoke({"pattern": "*.txt"}, tool_context)

    assert result.success is True
    assert isinstance(result.content, list)
    assert len(result.content) == 3

    paths = [Path(p) for p in result.content]
    mtimes = [p.stat().st_mtime for p in paths]

    assert mtimes == sorted(mtimes, reverse=True)
    assert result.content[0] == str(file_c)
    assert result.content[1] == str(file_b)
    assert result.content[2] == str(file_a)


def test_invoke_with_explicit_path_parameter(glob_tool: Glob, tool_context: ToolContext, tmp_path: Path):
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "a.txt").write_text("content a")
    (subdir / "b.txt").write_text("content b")
    (tmp_path / "root.txt").write_text("root content")

    result = glob_tool.invoke({"pattern": "*.txt", "path": str(subdir)}, tool_context)

    assert result.success is True
    assert isinstance(result.content, list)
    assert len(result.content) == 2
    assert all(p.startswith(str(subdir)) for p in result.content)


def test_invoke_with_missing_pattern_returns_error(glob_tool: Glob, tool_context: ToolContext):
    result = glob_tool.invoke({}, tool_context)

    assert result.success is False
    assert result.content == []
    assert "pattern is required" in result.error


def test_invoke_with_invalid_pattern_type_returns_error(glob_tool: Glob, tool_context: ToolContext):
    result = glob_tool.invoke({"pattern": 123}, tool_context)

    assert result.success is False
    assert result.content == []
    assert "pattern is required" in result.error
