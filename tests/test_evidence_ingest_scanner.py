from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from atticus.evidence_ingest.scanner import scan_source_directory
from atticus.tools.registry import ToolContext


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ToolContext(stage="evidence-ingest-scan", workspace_path=workspace)


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    src.mkdir()
    return src


def test_scan_directory_returns_correct_structure(source_dir: Path, tool_context: ToolContext):
    (source_dir / "test.txt").write_text("hello world")
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert isinstance(result, dict)
    assert "source_dir" in result
    assert "files" in result
    assert "count" in result
    assert result["source_dir"] == str(source_dir)
    assert isinstance(result["files"], list)
    assert result["count"] == 1


def test_scan_directory_returns_file_records_with_required_fields(source_dir: Path, tool_context: ToolContext):
    (source_dir / "document.txt").write_text("content")
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert len(result["files"]) == 1
    file_record = result["files"][0]

    assert "path" in file_record
    assert "absolute_path" in file_record
    assert "sha256" in file_record
    assert "size_bytes" in file_record
    assert "extension" in file_record


def test_scan_directory_computes_correct_sha256(source_dir: Path, tool_context: ToolContext):
    content = "hello world"
    test_file = source_dir / "test.txt"
    test_file.write_text(content)
    workspace = tool_context.workspace_path

    expected_sha256 = hashlib.sha256(content.encode()).hexdigest()

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert len(result["files"]) == 1
    assert result["files"][0]["sha256"] == expected_sha256


def test_scan_directory_detects_txt_format(source_dir: Path, tool_context: ToolContext):
    (source_dir / "document.txt").write_text("text content")
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert len(result["files"]) == 1
    assert result["files"][0]["extension"] == ".txt"


def test_scan_directory_detects_pdf_format(source_dir: Path, tool_context: ToolContext):
    pdf_content = "%PDF-1.4 fake pdf content"
    (source_dir / "document.pdf").write_bytes(pdf_content.encode())
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert len(result["files"]) == 1
    assert result["files"][0]["extension"] == ".pdf"


def test_scan_directory_detects_jpg_format(source_dir: Path, tool_context: ToolContext):
    jpg_content = b"\xff\xd8\xff\xe0fake jpg content"
    (source_dir / "image.jpg").write_bytes(jpg_content)
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert len(result["files"]) == 1
    assert result["files"][0]["extension"] == ".jpg"


def test_scan_directory_with_multiple_files(source_dir: Path, tool_context: ToolContext):
    (source_dir / "file1.txt").write_text("content1")
    (source_dir / "file2.pdf").write_bytes(b"pdf content")
    (source_dir / "subdir").mkdir()
    (source_dir / "subdir" / "file3.jpg").write_bytes(b"jpg content")
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert result["count"] == 3
    assert len(result["files"]) == 3
    extensions = {f["extension"] for f in result["files"]}
    assert extensions == {".txt", ".pdf", ".jpg"}


def test_scan_directory_absolute_path_is_correct(source_dir: Path, tool_context: ToolContext):
    test_file = source_dir / "test.txt"
    test_file.write_text("content")
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert len(result["files"]) == 1
    assert result["files"][0]["absolute_path"] == str(test_file.resolve())


def test_scan_directory_size_bytes_is_correct(source_dir: Path, tool_context: ToolContext):
    content = "hello"
    test_file = source_dir / "test.txt"
    test_file.write_text(content)
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert len(result["files"]) == 1
    assert result["files"][0]["size_bytes"] == len(content.encode())


def test_scan_directory_saves_raw_inventory_json(source_dir: Path, tool_context: ToolContext):
    (source_dir / "test.txt").write_text("content")
    workspace = tool_context.workspace_path

    scan_source_directory(source_dir, workspace, tool_context)

    inventory_path = workspace / "02-registers" / "raw_inventory.json"
    assert inventory_path.exists()

    with open(inventory_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)

    assert "source_dir" in saved_data
    assert "files" in saved_data
    assert "count" in saved_data


def test_scan_directory_json_roundtrip(source_dir: Path, tool_context: ToolContext):
    (source_dir / "doc1.txt").write_text("content1")
    (source_dir / "doc2.pdf").write_bytes(b"pdf content")
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    inventory_path = workspace / "02-registers" / "raw_inventory.json"
    with open(inventory_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)

    assert saved_data["source_dir"] == result["source_dir"]
    assert saved_data["count"] == result["count"]
    assert len(saved_data["files"]) == len(result["files"])

    for original, saved in zip(result["files"], saved_data["files"]):
        assert original["path"] == saved["path"]
        assert original["sha256"] == saved["sha256"]
        assert original["size_bytes"] == saved["size_bytes"]
        assert original["extension"] == saved["extension"]


def test_scan_directory_empty_source_dir(source_dir: Path, tool_context: ToolContext):
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert result["count"] == 0
    assert len(result["files"]) == 0


def test_scan_directory_relative_path_is_correct(source_dir: Path, tool_context: ToolContext):
    subdir = source_dir / "subdir"
    subdir.mkdir()
    test_file = subdir / "test.txt"
    test_file.write_text("content")
    workspace = tool_context.workspace_path

    result = scan_source_directory(source_dir, workspace, tool_context)

    assert len(result["files"]) == 1
    assert result["files"][0]["path"] == "subdir/test.txt"
