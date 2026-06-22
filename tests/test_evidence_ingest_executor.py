from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atticus.evidence_ingest.executor import (
    check_plan_accepted,
    execute_plan,
    format_filename,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "02-registers").mkdir()
    (ws / "01-evidence").mkdir()
    return ws


@pytest.fixture
def tool_context(tmp_path: Path):
    from atticus.tools.registry import ToolContext
    return ToolContext(
        stage="execute",
        workspace_path=tmp_path,
        provenance_logger=None,
    )


@pytest.fixture
def sample_plan():
    return {
        "sources": [
            {
                "source_id": "src_001",
                "original_path": "/tmp/file1.pdf",
                "stored_path": "documents/file1.pdf",
                "category": "documents",
            }
        ],
    }


class TestCheckPlanAccepted:
    def test_returns_false_if_no_accepted_at(self):
        plan = {"sources": []}
        assert check_plan_accepted(plan) is False

    def test_returns_true_if_accepted_at_present(self):
        plan = {"accepted_at": "2026-05-01T12:00:00", "sources": []}
        assert check_plan_accepted(plan) is True


class TestExecutePlan:
    def test_refuses_without_accepted_plan(self, workspace):
        plan = {"sources": []}
        with pytest.raises(RuntimeError, match="not been accepted"):
            execute_plan(workspace, plan)

    def test_copies_files_to_category_subdirectories(self, workspace, tool_context, sample_plan):
        plan = {**sample_plan, "accepted_at": "2026-05-01T12:00:00"}

        with patch("atticus.tools.copy.CopyTool") as MockCopy:
            mock_copy = MagicMock()
            mock_result = MagicMock()
            mock_result.success = True
            mock_copy.invoke.return_value = mock_result
            MockCopy.return_value = mock_copy

            result = execute_plan(workspace, plan, context=tool_context)

            assert result["operation_count"] == 1
            assert result["operations"][0]["action"] == "copy"

    def test_moves_duplicates_to_duplicates_dir(self, workspace, tool_context):
        plan = {
            "accepted_at": "2026-05-01T12:00:00",
            "sources": [
                {
                    "source_id": "src_001",
                    "original_path": "/tmp/file1.pdf",
                    "stored_path": "documents/file1.pdf",
                    "duplicate_of": "src_000",
                }
            ],
        }

        with patch("atticus.tools.copy.CopyTool") as MockCopy:
            mock_copy = MagicMock()
            mock_result = MagicMock()
            mock_result.success = True
            mock_copy.invoke.return_value = mock_result
            MockCopy.return_value = mock_copy

            result = execute_plan(workspace, plan, context=tool_context)

            assert result["operation_count"] == 1
            assert result["operations"][0]["action"] == "moved_duplicate"
            assert result["operations"][0]["duplicate_of"] == "src_000"
            assert "__duplicates__" in result["operations"][0]["to"]

    def test_groups_truncation_series_into_subdirectories(self, workspace, tool_context):
        plan = {
            "accepted_at": "2026-05-01T12:00:00",
            "sources": [
                {
                    "source_id": "src_001",
                    "original_path": "/tmp/doc_p1.pdf",
                    "stored_path": "documents/truncation_series_001/doc_p1.pdf",
                    "category": "documents",
                },
                {
                    "source_id": "src_002",
                    "original_path": "/tmp/doc_p2.pdf",
                    "stored_path": "documents/truncation_series_001/doc_p2.pdf",
                    "category": "documents",
                },
            ],
        }

        with patch("atticus.tools.copy.CopyTool") as MockCopy:
            mock_copy = MagicMock()
            mock_result = MagicMock()
            mock_result.success = True
            mock_copy.invoke.return_value = mock_result
            MockCopy.return_value = mock_copy

            result = execute_plan(workspace, plan, context=tool_context)

            assert result["operation_count"] == 2
            dest_paths = [op["to"] for op in result["operations"]]
            assert all("truncation_series_001" in p for p in dest_paths)

    def test_provenance_log_written(self, workspace, tool_context):
        plan = {
            "accepted_at": "2026-05-01T12:00:00",
            "sources": [],
        }

        with patch("atticus.evidence_ingest.provenance.ProvenanceLogger") as MockProvenance:
            mock_logger = MagicMock()
            MockProvenance.return_value = mock_logger

            execute_plan(workspace, plan, context=tool_context)

            mock_logger.log.assert_called_once_with("execute", dry_run=False, operation_count=0, error_count=0)


class TestFormatFilename:
    def test_with_page_info(self):
        result = format_filename("document.pdf", page_info={"page": 1, "total": 5})
        assert "page_1_of_5" in result or "p1" in result

    def test_without_page_info(self):
        result = format_filename("document.pdf")
        assert result == "document.pdf" or "document" in result
