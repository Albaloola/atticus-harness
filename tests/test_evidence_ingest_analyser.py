"""Tests for atticus.evidence_ingest.analyser module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from atticus.evidence_ingest.analyser import (
    analyse_file,
    analyse_files_batch,
    save_analysis_results,
    load_analysis_results,
)
from atticus.tools.registry import ToolContext


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    """Create a ToolContext for testing."""
    return ToolContext(
        stage="evidence-ingest-analyse",
        workspace_path=tmp_path,
        db_path=None,
        provenance_logger=None,
        token_budget=None,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace directory for testing."""
    return tmp_path


@pytest.fixture
def sample_file_entry() -> dict[str, Any]:
    """Create a sample file entry for testing."""
    return {
        "path": "evidence/document.pdf",
        "absolute_path": "/tmp/evidence/document.pdf",
        "sha256": "abc123def456",
        "size": 1024,
    }


@pytest.fixture
def sample_ai_response() -> dict[str, Any]:
    """Create a sample AI provider response."""
    return {
        "document_type": "contract",
        "human_readable_name": "Service Agreement",
        "suggested_category": "contracts_agreements",
        "description": "Service agreement between parties",
        "quality_assessment": "clean_pdf",
        "quality_score": 3,
        "truncation": {
            "is_partial": False,
            "page_number": None,
            "total_pages_estimated": None,
            "series_id_hint": None,
        },
        "duplicate_suspicion": None,
        "is_cover_communication": False,
        "key_parties": ["Acme Corp", "Beta LLC"],
        "key_dates": ["2023-05-15"],
        "confidence": "high",
        "flags": [],
    }


class TestAnalyseFile:
    """Tests for analyse_file function."""

    @patch("atticus.evidence_ingest.analyser._call_ai_provider")
    def test_returns_correct_analysis_result(
        self,
        mock_ai_call: MagicMock,
        tool_context: ToolContext,
        tmp_path: Path,
        sample_file_entry: dict[str, Any],
        sample_ai_response: dict[str, Any],
    ) -> None:
        """Test that analyse_file returns correct analysis result dict."""
        # Create a test file
        test_file = tmp_path / "document.pdf"
        test_file.write_text("Test content")

        # Mock AI provider response
        mock_ai_call.return_value = sample_ai_response

        # Call analyse_file
        result = analyse_file(test_file, sample_file_entry, tool_context)

        # Verify AI provider was called
        mock_ai_call.assert_called_once()

        # Verify result structure
        assert "file" in result
        assert "sha256" in result
        assert "document_type" in result
        assert "human_readable_name" in result
        assert "suggested_category" in result
        assert "description" in result
        assert "confidence" in result

    @patch("atticus.evidence_ingest.analyser._call_ai_provider")
    def test_normaliser_is_applied(
        self,
        mock_ai_call: MagicMock,
        tool_context: ToolContext,
        tmp_path: Path,
        sample_file_entry: dict[str, Any],
    ) -> None:
        """Test that normaliser is applied to analysis result."""
        # Create a test file
        test_file = tmp_path / "document.pdf"
        test_file.write_text("Test content")

        # Mock AI response with non-normalised values
        mock_ai_call.return_value = {
            "document_type": "CONTRACT",  # Should be normalised to lower
            "suggested_category": "CONTRACTS_AGREEMENTS",  # Should be normalised
            "description": "  Whitespace around  ",
            "confidence": "HIGH",  # Should be normalised
        }

        result = analyse_file(test_file, sample_file_entry, tool_context)

        # Verify normalisation was applied
        assert result["document_type"] == "contract"
        assert result["suggested_category"] == "contracts_agreements"
        assert result["confidence"] == "high"
        assert result["description"] == "whitespace around"

    @patch("atticus.evidence_ingest.analyser._call_ai_provider")
    def test_token_truncation_applied(
        self,
        mock_ai_call: MagicMock,
        tool_context: ToolContext,
        tmp_path: Path,
        sample_file_entry: dict[str, Any],
    ) -> None:
        """Test that token truncation is applied for analyse stage (2000 tokens max)."""
        # Create a large test file
        test_file = tmp_path / "large_document.pdf"
        test_file.write_text("x" * 10000)

        # Mock AI provider
        mock_ai_call.return_value = {"document_type": "other"}

        # Mock Read to verify max_tokens is passed
        with patch("atticus.evidence_ingest.analyser.ReadTool") as MockRead:
            mock_instance = MagicMock()
            MockRead.return_value = mock_instance

            # Simulate successful read with truncation info
            mock_instance.invoke.return_value = MagicMock(
                success=True,
                content="truncated content",
                metadata={"truncated": True, "tokens_used": 2000},
            )

            result = analyse_file(test_file, sample_file_entry, tool_context)

            # Verify Read was called with max_tokens=2000
            call_args = mock_instance.invoke.call_args
            assert call_args[0][0]["max_tokens"] == 2000

    @patch("atticus.evidence_ingest.analyser._call_ai_provider")
    def test_handles_read_failure(
        self,
        mock_ai_call: MagicMock,
        tool_context: ToolContext,
        tmp_path: Path,
        sample_file_entry: dict[str, Any],
    ) -> None:
        """Test that analyse_file handles read failures gracefully."""
        test_file = tmp_path / "nonexistent.pdf"

        # Mock Read to return failure
        with patch("atticus.evidence_ingest.analyser.ReadTool") as MockRead:
            mock_instance = MagicMock()
            MockRead.return_value = mock_instance
            mock_instance.invoke.return_value = MagicMock(
                success=False, content="", error="File not found"
            )

            result = analyse_file(test_file, sample_file_entry, tool_context)

            # AI should not be called if read fails
            mock_ai_call.assert_not_called()

            # Default values should be returned
            assert result["document_type"] == "other"
            assert result["confidence"] == "low"


class TestAnalyseFilesBatch:
    """Tests for analyse_files_batch function."""

    @patch("atticus.evidence_ingest.analyser._call_ai_provider")
    def test_batch_processing(
        self,
        mock_ai_call: MagicMock,
        tool_context: ToolContext,
        workspace: Path,
        tmp_path: Path,
    ) -> None:
        """Test batch processing of multiple files."""
        # Create test files
        files = []
        for i in range(3):
            file_path = tmp_path / f"document_{i}.pdf"
            file_path.write_text(f"Content {i}")
            files.append({
                "path": f"document_{i}.pdf",
                "absolute_path": str(file_path),
                "sha256": f"sha256_{i}",
            })

        mock_ai_call.return_value = {"document_type": "other"}

        with patch("atticus.evidence_ingest.analyser.ReadTool") as MockRead:
            mock_instance = MagicMock()
            MockRead.return_value = mock_instance
            mock_instance.invoke.return_value = MagicMock(
                success=True, content="test", metadata={}
            )

            result = analyse_files_batch(
                files, tmp_path, workspace, tool_context
            )

            assert result["count"] == 3
            assert len(result["analyses"]) == 3

    @patch("atticus.evidence_ingest.analyser._call_ai_provider")
    def test_sha_based_skip(
        self,
        mock_ai_call: MagicMock,
        tool_context: ToolContext,
        workspace: Path,
        tmp_path: Path,
    ) -> None:
        """Test that files with same SHA are skipped."""
        # Create initial analysis results with one file already analysed
        existing_results = {
            "source_dir": str(tmp_path),
            "analyses": [
                {
                    "file": "document_0.pdf",
                    "sha256": "existing_sha",
                    "document_type": "contract",
                }
            ],
            "count": 1,
        }
        save_analysis_results(workspace, existing_results)

        # New batch includes the same file (same SHA) and a new one
        files = [
            {
                "path": "document_0.pdf",
                "absolute_path": str(tmp_path / "document_0.pdf"),
                "sha256": "existing_sha",  # Same SHA - should be skipped
            },
            {
                "path": "document_1.pdf",
                "absolute_path": str(tmp_path / "document_1.pdf"),
                "sha256": "new_sha",  # New file - should be analysed
            },
        ]

        # Create the new file
        (tmp_path / "document_1.pdf").write_text("New content")

        mock_ai_call.return_value = {"document_type": "other"}

        with patch("atticus.evidence_ingest.analyser.ReadTool") as MockRead:
            mock_instance = MagicMock()
            MockRead.return_value = mock_instance
            mock_instance.invoke.return_value = MagicMock(
                success=True, content="test", metadata={}
            )

            result = analyse_files_batch(
                files, tmp_path, workspace, tool_context
            )

            # Only the new file should be analysed (1 new + 1 existing = 2 total)
            assert result["count"] == 2
            # AI should only be called once (for the new file)
            mock_ai_call.assert_called_once()

    @patch("atticus.evidence_ingest.analyser._call_ai_provider")
    def test_incremental_save(
        self,
        mock_ai_call: MagicMock,
        tool_context: ToolContext,
        workspace: Path,
        tmp_path: Path,
    ) -> None:
        """Test that results are saved incrementally after each file."""
        files = []
        for i in range(3):
            file_path = tmp_path / f"document_{i}.pdf"
            file_path.write_text(f"Content {i}")
            files.append({
                "path": f"document_{i}.pdf",
                "absolute_path": str(file_path),
                "sha256": f"sha256_{i}",
            })

        mock_ai_call.return_value = {"document_type": "other"}

        save_calls = []

        def track_save(workspace: Path, results: dict) -> None:
            save_calls.append(results["count"])
            save_analysis_results(workspace, results)

        with patch("atticus.evidence_ingest.analyser.ReadTool") as MockRead:
            mock_instance = MagicMock()
            MockRead.return_value = mock_instance
            mock_instance.invoke.return_value = MagicMock(
                success=True, content="test", metadata={}
            )

            with patch(
                "atticus.evidence_ingest.analyser.save_analysis_results",
                side_effect=track_save,
            ):
                analyse_files_batch(files, tmp_path, workspace, tool_context)

            # Verify incremental saves happened (should be called after each file)
            assert len(save_calls) == 3


class TestSaveAndLoadAnalysisResults:
    """Tests for save_analysis_results and load_analysis_results functions."""

    def test_save_and_load_results(
        self, workspace: Path
    ) -> None:
        """Test saving and loading analysis results."""
        results = {
            "source_dir": "/evidence",
            "analyses": [
                {"file": "test.pdf", "sha256": "abc123", "document_type": "contract"}
            ],
            "count": 1,
        }

        # Save results
        save_analysis_results(workspace, results)

        # Load results
        loaded = load_analysis_results(workspace)

        assert loaded["source_dir"] == "/evidence"
        assert loaded["count"] == 1
        assert len(loaded["analyses"]) == 1
        assert loaded["analyses"][0]["file"] == "test.pdf"

    def test_load_nonexistent_results(self, workspace: Path) -> None:
        """Test loading results when file doesn't exist."""
        result = load_analysis_results(workspace)
        assert result == {}

    def test_save_overwrites_existing(self, workspace: Path) -> None:
        """Test that save overwrites existing results."""
        # Save initial results
        initial = {
            "source_dir": "/evidence",
            "analyses": [{"file": "old.pdf"}],
            "count": 1,
        }
        save_analysis_results(workspace, initial)

        # Save new results
        new = {
            "source_dir": "/evidence",
            "analyses": [{"file": "new.pdf"}],
            "count": 1,
        }
        save_analysis_results(workspace, new)

        # Load and verify only new results exist
        loaded = load_analysis_results(workspace)
        assert loaded["analyses"][0]["file"] == "new.pdf"

    def test_save_creates_directory(self, workspace: Path) -> None:
        """Test that save creates necessary directories."""
        results = {"source_dir": "/evidence", "analyses": [], "count": 0}

        # Remove 02-registers directory if it exists
        registers_dir = workspace / "02-registers"
        if registers_dir.exists():
            import shutil
            shutil.rmtree(registers_dir)

        save_analysis_results(workspace, results)

        assert registers_dir.exists()
        assert (registers_dir / "analysis_results.json").exists()
