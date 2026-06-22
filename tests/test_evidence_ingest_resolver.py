from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from atticus.evidence_ingest.resolver import (
    resolve_analysis_results,
    save_resolution_plan,
    load_resolution_plan,
)
from atticus.tools.registry import ToolContext


@pytest.fixture
def workspace_with_analysis(tmp_path: Path) -> Path:
    """Create a workspace with sample analysis_results.json."""
    workspace = tmp_path / "matter"
    registers_dir = workspace / "02-registers"
    registers_dir.mkdir(parents=True)

    analysis_data = {
        "analyses": [
            {
                "file": "contract.pdf",
                "sha256": "abc123",
                "document_type": "contract",
                "human_readable_name": "Service Agreement",
                "suggested_category": "contracts_agreements",
                "description": "Service agreement between parties",
            },
            {
                "file": "email.msg",
                "sha256": "def456",
                "document_type": "email",
                "human_readable_name": "Email Exchange",
                "suggested_category": "correspondence",
                "description": "Email discussion about settlement",
            },
        ]
    }

    with open(registers_dir / "analysis_results.json", "w", encoding="utf-8") as f:
        json.dump(analysis_data, f, indent=2)

    return workspace


@pytest.fixture
def tool_context(workspace_with_analysis: Path) -> ToolContext:
    """Create a ToolContext for testing."""
    return ToolContext(
        stage="evidence-ingest-resolve",
        workspace_path=workspace_with_analysis,
    )


SAMPLE_RESOLUTION_PLAN = {
    "duplicate_groups": [
        {
            "source_ids": ["src_001", "src_002"],
            "keep": "src_001",
            "reason": "exact_duplicate",
        }
    ],
    "truncation_groups": [
        {
            "series_id": "invoice_series_jan2023",
            "source_ids": ["src_003", "src_004"],
            "recommended_order": ["src_003", "src_004"],
            "is_complete": True,
        }
    ],
    "recategorisations": [
        {
            "source_id": "src_005",
            "new_category": "financial_records",
        }
    ],
    "renames": [
        {
            "source_id": "src_003",
            "new_name": "January 2023 Invoices - Part 1",
        }
    ],
    "needs_human_review": [
        {
            "source_id": "src_006",
            "reason": "Low confidence analysis",
        }
    ],
}


class TestResolveAnalysisResults:
    """Tests for resolve_analysis_results()."""

    @patch("atticus.evidence_ingest.normaliser.normalise_analysis_result")
    def test_resolve_analysis_returns_correct_plan(
        self,
        mock_normalise: MagicMock,
        workspace_with_analysis: Path,
        tool_context: ToolContext,
    ) -> None:
        """Test that resolve_analysis_results returns a correct resolution plan."""
        mock_normalise.return_value = (
            {
                "file": "contract.pdf",
                "sha256": "abc123",
                "document_type": "contract",
                "human_readable_name": "Service Agreement",
                "suggested_category": "contracts_agreements",
                "description": "Service agreement between parties",
            },
            [],
        )

        result = resolve_analysis_results(workspace_with_analysis, tool_context)

        assert "sources" in result
        assert "duplicate_groups" in result
        assert "truncation_groups" in result
        assert "recategorisations" in result
        assert "renames" in result
        assert "needs_human_review" in result
        assert "normalisation_warnings" in result

    def test_resolve_analysis_calls_normaliser(
        self,
        workspace_with_analysis: Path,
        tool_context: ToolContext,
    ) -> None:
        """Test that normaliser is applied to analysis results."""
        with patch("atticus.evidence_ingest.normaliser.normalise_analysis_result") as mock_normalise:
            mock_normalise.return_value = (
                {
                    "file": "contract.pdf",
                    "sha256": "abc123",
                    "document_type": "contract",
                    "human_readable_name": "Service Agreement",
                    "suggested_category": "contracts_agreements",
                    "description": "Service agreement",
                },
                [],
            )

            resolve_analysis_results(workspace_with_analysis, tool_context)

            assert mock_normalise.call_count == 2

    def test_resolve_analysis_plan_has_required_fields(
        self,
        workspace_with_analysis: Path,
        tool_context: ToolContext,
    ) -> None:
        """Test that resolution plan has all required fields."""
        result = resolve_analysis_results(workspace_with_analysis, tool_context)

        required_fields = [
            "sources",
            "duplicate_groups",
            "truncation_groups",
            "recategorisations",
            "renames",
            "needs_human_review",
            "normalisation_warnings",
        ]

        for field in required_fields:
            assert field in result, f"Missing required field: {field}"

    def test_resolve_analysis_creates_sources(
        self,
        workspace_with_analysis: Path,
        tool_context: ToolContext,
    ) -> None:
        """Test that sources are correctly created from analyses."""
        result = resolve_analysis_results(workspace_with_analysis, tool_context)

        assert len(result["sources"]) == 2
        assert result["sources"][0]["source_id"] == "NAP-SRC-0000"
        assert result["sources"][0]["original_path"] == "contract.pdf"
        assert result["sources"][1]["source_id"] == "NAP-SRC-0001"
        assert result["sources"][1]["original_path"] == "email.msg"

    def test_resolve_analysis_raises_when_analysis_missing(
        self,
        tmp_path: Path,
        tool_context: ToolContext,
    ) -> None:
        """Test that FileNotFoundError is raised when analysis results missing."""
        workspace = tmp_path / "empty_matter"
        (workspace / "02-registers").mkdir(parents=True)

        with pytest.raises(FileNotFoundError, match="Analysis results not found"):
            resolve_analysis_results(workspace, tool_context)

    @patch("atticus.evidence_ingest.normaliser.normalise_analysis_result")
    def test_resolve_analysis_collects_normalisation_warnings(
        self,
        mock_normalise: MagicMock,
        workspace_with_analysis: Path,
        tool_context: ToolContext,
    ) -> None:
        """Test that normalisation warnings are collected."""
        mock_normalise.side_effect = [
            (
                {"file": "contract.pdf", "document_type": "contract"},
                ["invalid_category: unknown"],
            ),
            (
                {"file": "email.msg", "document_type": "email"},
                ["invalid_document_type: letter"],
            ),
        ]

        result = resolve_analysis_results(workspace_with_analysis, tool_context)

        assert len(result["normalisation_warnings"]) == 2


class TestSaveAndLoadResolutionPlan:
    """Tests for save_resolution_plan() and load_resolution_plan()."""

    def test_save_and_load_resolution_plan(
        self,
        tmp_path: Path,
    ) -> None:
        """Test saving and loading a resolution plan."""
        workspace = tmp_path / "matter"
        (workspace / "02-registers").mkdir(parents=True)

        save_resolution_plan(workspace, SAMPLE_RESOLUTION_PLAN)

        loaded = load_resolution_plan(workspace)

        assert loaded["duplicate_groups"] == SAMPLE_RESOLUTION_PLAN["duplicate_groups"]
        assert loaded["truncation_groups"] == SAMPLE_RESOLUTION_PLAN["truncation_groups"]
        assert loaded["recategorisations"] == SAMPLE_RESOLUTION_PLAN["recategorisations"]

    def test_save_resolution_plan_creates_file(
        self,
        tmp_path: Path,
    ) -> None:
        """Test that save_resolution_plan creates the file."""
        workspace = tmp_path / "matter"
        (workspace / "02-registers").mkdir(parents=True)

        output_path = save_resolution_plan(workspace, SAMPLE_RESOLUTION_PLAN)

        assert output_path.exists()
        assert output_path.name == "resolution_plan.json"

    def test_load_resolution_plan_raises_when_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """Test that load_resolution_plan raises FileNotFoundError when missing."""
        workspace = tmp_path / "empty_matter"
        (workspace / "02-registers").mkdir(parents=True)

        with pytest.raises(FileNotFoundError, match="Resolution plan not found"):
            load_resolution_plan(workspace)

    def test_save_resolution_plan_creates_parent_dirs(
        self,
        tmp_path: Path,
    ) -> None:
        """Test that save_resolution_plan creates parent directories."""
        workspace = tmp_path / "new_matter"

        save_resolution_plan(workspace, SAMPLE_RESOLUTION_PLAN)

        assert (workspace / "02-registers" / "resolution_plan.json").exists()

    def test_round_trip_preserves_all_fields(
        self,
        tmp_path: Path,
    ) -> None:
        """Test that save/load round trip preserves all fields."""
        workspace = tmp_path / "matter"
        (workspace / "02-registers").mkdir(parents=True)

        save_resolution_plan(workspace, SAMPLE_RESOLUTION_PLAN)
        loaded = load_resolution_plan(workspace)

        assert loaded == SAMPLE_RESOLUTION_PLAN


class TestMockedAIProvider:
    """Tests with mocked AI provider calls."""

    @patch("atticus.evidence_ingest.normaliser.normalise_analysis_result")
    def test_mocked_ai_provider_returns_sample_plan(
        self,
        mock_normalise: MagicMock,
        workspace_with_analysis: Path,
        tool_context: ToolContext,
    ) -> None:
        """Test that AI provider can be mocked to return a sample resolution plan."""
        mock_normalise.return_value = (
            {"file": "test.pdf", "document_type": "contract"},
            [],
        )

        with patch("atticus.evidence_ingest.resolver.resolve_analysis_results") as mock_resolve:
            mock_resolve.return_value = SAMPLE_RESOLUTION_PLAN

            result = mock_resolve(workspace_with_analysis, tool_context)

            assert result == SAMPLE_RESOLUTION_PLAN
            assert len(result["duplicate_groups"]) == 1
            assert result["duplicate_groups"][0]["reason"] == "exact_duplicate"

    def test_resolution_plan_structure_matches_prompt_schema(
        self,
        workspace_with_analysis: Path,
        tool_context: ToolContext,
    ) -> None:
        """Test that resolution plan matches the schema in RESOLVE_SYSTEM_PROMPT."""
        result = resolve_analysis_results(workspace_with_analysis, tool_context)

        assert isinstance(result["duplicate_groups"], list)
        assert isinstance(result["truncation_groups"], list)
        assert isinstance(result["recategorisations"], list)
        assert isinstance(result["renames"], list)
        assert isinstance(result["needs_human_review"], list)

        if result["duplicate_groups"]:
            group = result["duplicate_groups"][0]
            assert "source_ids" in group
            assert "keep" in group
            assert "reason" in group

        if result["truncation_groups"]:
            group = result["truncation_groups"][0]
            assert "series_id" in group
            assert "source_ids" in group
            assert "recommended_order" in group
            assert "is_complete" in group
