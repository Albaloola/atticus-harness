import pytest
from pathlib import Path
from atticus.tools.edit import EditTool
from atticus.tools.registry import ToolContext, ToolResult


@pytest.mark.parametrize("stage", ["resolve", "harvest", "review", "repair"])
def test_can_handle_supported_stages(stage):
    tool = EditTool()
    assert tool.can_handle(stage) is True


def test_can_handle_unsupported_stage():
    tool = EditTool()
    assert tool.can_handle("extract-sources") is False


def test_invoke_valid_find_replace(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world\nfoo bar")
    
    tool = EditTool()
    context = ToolContext(stage="review", workspace_path=tmp_path)
    params = {
        "path": str(test_file),
        "old_text": "hello",
        "new_text": "hi",
        "expected_replacements": 1
    }
    
    result = tool.invoke(params, context)
    
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert test_file.read_text() == "hi world\nfoo bar"


def test_invoke_expected_replacements_mismatch(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world\nfoo bar")
    
    tool = EditTool()
    context = ToolContext(stage="review", workspace_path=tmp_path)
    params = {
        "path": str(test_file),
        "old_text": "hello",
        "new_text": "hi",
        "expected_replacements": 2
    }
    
    result = tool.invoke(params, context)
    
    assert result.success is False
    assert "Expected 2 replacement(s), found 1" in result.error
    assert test_file.read_text() == "hello world\nfoo bar"


def test_invoke_non_existent_old_text(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")
    
    tool = EditTool()
    context = ToolContext(stage="review", workspace_path=tmp_path)
    params = {
        "path": str(test_file),
        "old_text": "nonexistent",
        "new_text": "new"
    }
    
    result = tool.invoke(params, context)
    
    assert result.success is False
    assert "Expected 1 replacement(s), found 0" in result.error
    assert test_file.read_text() == "hello world"


def test_invoke_replaces_correct_text(tmp_path):
    test_file = tmp_path / "test.txt"
    original = "line1\nline2\nline3"
    test_file.write_text(original)
    
    tool = EditTool()
    context = ToolContext(stage="review", workspace_path=tmp_path)
    params = {
        "path": str(test_file),
        "old_text": "line2",
        "new_text": "modified"
    }
    
    result = tool.invoke(params, context)
    
    assert result.success is True
    assert result.content == {"path": str(test_file), "replacements": 1}
    assert test_file.read_text() == "line1\nmodified\nline3"


def test_invoke_missing_path():
    tool = EditTool()
    context = ToolContext(stage="review", workspace_path=Path("/tmp"))
    params = {"old_text": "a", "new_text": "b"}
    
    result = tool.invoke(params, context)
    
    assert result.success is False
    assert "path is required" in result.error


def test_invoke_file_not_found(tmp_path):
    tool = EditTool()
    context = ToolContext(stage="review", workspace_path=tmp_path)
    params = {
        "path": str(tmp_path / "nonexistent.txt"),
        "old_text": "a",
        "new_text": "b"
    }
    
    result = tool.invoke(params, context)
    
    assert result.success is False
    assert "File not found" in result.error
