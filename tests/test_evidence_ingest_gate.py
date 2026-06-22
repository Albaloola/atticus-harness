from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from atticus.evidence_ingest.gate import (
    HIGH_CONFIDENCE,
    MEDIUM_CONFIDENCE,
    _classify_confidence,
    accept_plan,
    run_quality_gate,
)


def _make_scan_result(source_id: str, confidence: float) -> dict:
    return {"source_id": source_id, "confidence": confidence}


def _make_resolution_plan(sources: list[dict] | None = None, plan_path: str = "") -> dict:
    return {"sources": sources or [], "plan_path": plan_path}


class TestRunQualityGate:
    """Tests for run_quality_gate function."""

    @patch("atticus.evidence_ingest.gate.run_all_validations")
    def test_all_clear_when_validation_passes_and_all_confidence_high(self, mock_validate):
        mock_validate.return_value = {"status": "ALL_CLEAR", "errors": []}

        scan_results = [
            _make_scan_result("src-1", 0.95),
            _make_scan_result("src-2", 1.0),
        ]
        resolution_plan = _make_resolution_plan()

        result = run_quality_gate(scan_results, resolution_plan)

        assert result["status"] == "ALL_CLEAR"
        assert result["quarantined"] is False
        assert len(result["confidence"]["auto_approved"]) == 2
        assert len(result["confidence"]["needs_glance"]) == 0
        assert len(result["confidence"]["needs_human_review"]) == 0

    @patch("atticus.evidence_ingest.gate.run_all_validations")
    def test_partial_when_some_confidence_medium(self, mock_validate):
        mock_validate.return_value = {"status": "ALL_CLEAR", "errors": []}

        scan_results = [
            _make_scan_result("src-1", 0.95),
            _make_scan_result("src-2", 0.8),
        ]
        resolution_plan = _make_resolution_plan()

        result = run_quality_gate(scan_results, resolution_plan)

        assert result["status"] == "PARTIAL"
        assert len(result["confidence"]["auto_approved"]) == 1
        assert len(result["confidence"]["needs_glance"]) == 1
        assert "src-2" in result["confidence"]["needs_glance"]

    @patch("atticus.evidence_ingest.gate.run_all_validations")
    def test_blocked_when_some_confidence_low(self, mock_validate):
        mock_validate.return_value = {"status": "ALL_CLEAR", "errors": []}

        scan_results = [
            _make_scan_result("src-1", 0.95),
            _make_scan_result("src-2", 0.5),
        ]
        resolution_plan = _make_resolution_plan()

        result = run_quality_gate(scan_results, resolution_plan)

        assert result["status"] == "PARTIAL"
        assert len(result["confidence"]["needs_human_review"]) == 1
        assert "src-2" in result["confidence"]["needs_human_review"]

    @patch("atticus.evidence_ingest.gate._call_ai_correction")
    @patch("atticus.evidence_ingest.gate.run_all_validations")
    def test_retry_then_quarantine_when_validation_fails(self, mock_validate, mock_ai):
        mock_validate.side_effect = [
            {"status": "EXCEPTIONS", "errors": ["some error"], "error_codes": ["test_error"]},
            {"status": "EXCEPTIONS", "errors": ["some error"], "error_codes": ["test_error"]},
        ]
        mock_ai.return_value = None

        scan_results = [_make_scan_result("src-1", 0.95)]
        resolution_plan = _make_resolution_plan()

        result = run_quality_gate(scan_results, resolution_plan)

        assert result["status"] == "BLOCKED"
        assert result["quarantined"] is True
        assert mock_validate.call_count == 2

    @patch("atticus.evidence_ingest.gate._call_ai_correction")
    @patch("atticus.evidence_ingest.gate.run_all_validations")
    def test_retry_succeeds_on_second_attempt(self, mock_validate, mock_ai):
        mock_validate.side_effect = [
            {"status": "EXCEPTIONS", "errors": ["some error"], "error_codes": ["test_error"]},
            {"status": "ALL_CLEAR", "errors": []},
        ]
        mock_ai.return_value = {"sources": [], "plan_path": ""}

        scan_results = [_make_scan_result("src-1", 0.95)]
        resolution_plan = _make_resolution_plan()

        result = run_quality_gate(scan_results, resolution_plan)

        assert result["status"] == "ALL_CLEAR"
        assert result["quarantined"] is False
        assert mock_validate.call_count == 2

    @patch("atticus.evidence_ingest.gate.run_all_validations")
    def test_plan_path_preserved_in_result(self, mock_validate):
        mock_validate.return_value = {"status": "ALL_CLEAR", "errors": []}

        scan_results = [_make_scan_result("src-1", 0.95)]
        resolution_plan = _make_resolution_plan(plan_path="/path/to/plan.json")

        result = run_quality_gate(scan_results, resolution_plan)

        assert result["plan_path"] == "/path/to/plan.json"


class TestAcceptPlan:
    """Tests for accept_plan function."""

    def test_accept_plan_adds_accepted_at_timestamp(self):
        import time

        resolution_plan = _make_resolution_plan()

        result = accept_plan(resolution_plan, accepted_by="human")

        assert "metadata" in result
        assert "accepted_at" in result["metadata"]
        assert isinstance(result["metadata"]["accepted_at"], float)
        assert result["metadata"]["accepted_at"] <= time.time()
        assert result["metadata"]["accepted_by"] == "human"

    def test_accept_plan_preserves_existing_metadata(self):
        resolution_plan = _make_resolution_plan()
        resolution_plan["metadata"] = {"existing": "data"}

        result = accept_plan(resolution_plan, accepted_by="ai")

        assert result["metadata"]["existing"] == "data"
        assert "accepted_at" in result["metadata"]
        assert result["metadata"]["accepted_by"] == "ai"


class TestClassifyConfidence:
    """Tests for _classify_confidence function."""

    def test_auto_approved_for_high_confidence(self):
        scan_results = [
            _make_scan_result("src-1", 0.95),
            _make_scan_result("src-2", 1.0),
            _make_scan_result("src-3", HIGH_CONFIDENCE),
        ]
        resolution_plan = _make_resolution_plan()

        result = _classify_confidence(scan_results, resolution_plan)

        assert len(result["auto_approved"]) == 3
        assert "src-1" in result["auto_approved"]
        assert "src-2" in result["auto_approved"]
        assert "src-3" in result["auto_approved"]
        assert len(result["needs_glance"]) == 0
        assert len(result["needs_human_review"]) == 0

    def test_needs_glance_for_medium_confidence(self):
        scan_results = [
            _make_scan_result("src-1", 0.8),
            _make_scan_result("src-2", MEDIUM_CONFIDENCE),
            _make_scan_result("src-3", 0.89),
        ]
        resolution_plan = _make_resolution_plan()

        result = _classify_confidence(scan_results, resolution_plan)

        assert len(result["needs_glance"]) == 3
        assert "src-1" in result["needs_glance"]
        assert "src-2" in result["needs_glance"]
        assert "src-3" in result["needs_glance"]
        assert len(result["auto_approved"]) == 0
        assert len(result["needs_human_review"]) == 0

    def test_needs_human_review_for_low_confidence(self):
        scan_results = [
            _make_scan_result("src-1", 0.5),
            _make_scan_result("src-2", 0.0),
            _make_scan_result("src-3", MEDIUM_CONFIDENCE - 0.01),
        ]
        resolution_plan = _make_resolution_plan()

        result = _classify_confidence(scan_results, resolution_plan)

        assert len(result["needs_human_review"]) == 3
        assert "src-1" in result["needs_human_review"]
        assert "src-2" in result["needs_human_review"]
        assert "src-3" in result["needs_human_review"]
        assert len(result["auto_approved"]) == 0
        assert len(result["needs_glance"]) == 0

    def test_mixed_confidence_levels(self):
        scan_results = [
            _make_scan_result("src-high", 0.95),
            _make_scan_result("src-medium", 0.8),
            _make_scan_result("src-low", 0.5),
        ]
        resolution_plan = _make_resolution_plan()

        result = _classify_confidence(scan_results, resolution_plan)

        assert result["auto_approved"] == ["src-high"]
        assert result["needs_glance"] == ["src-medium"]
        assert result["needs_human_review"] == ["src-low"]

    def test_string_confidence_conversion(self):
        scan_results = [
            {"source_id": "src-high", "confidence": "high"},
            {"source_id": "src-medium", "confidence": "medium"},
            {"source_id": "src-low", "confidence": "low"},
        ]
        resolution_plan = _make_resolution_plan()

        result = _classify_confidence(scan_results, resolution_plan)

        assert result["auto_approved"] == ["src-high"]
        assert result["needs_glance"] == ["src-medium"]
        assert result["needs_human_review"] == ["src-low"]

    def test_empty_scan_results(self):
        result = _classify_confidence([], _make_resolution_plan())

        assert result["auto_approved"] == []
        assert result["needs_glance"] == []
        assert result["needs_human_review"] == []
