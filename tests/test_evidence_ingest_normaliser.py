"""Tests for atticus.evidence_ingest.normaliser."""

import pytest

from atticus.evidence_ingest.normaliser import (
    normalise_string,
    normalise_document_type,
    normalise_category,
    normalise_analysis_result,
    normalise_description,
)
from atticus.evidence_ingest.prompts import DOCUMENT_TYPES, CATEGORIES


class TestNormaliseString:
    """Tests for normalise_string function."""

    def test_lowercase(self):
        """Test that string is converted to lowercase."""
        result = normalise_string("HELLO WORLD")
        assert result == "hello world"

    def test_strip_whitespace(self):
        """Test that leading and trailing whitespace is stripped."""
        result = normalise_string("  hello world  ")
        assert result == "hello world"

    def test_synonym_mapping_lease(self):
        """Test synonym mapping: lease -> agreement."""
        result = normalise_string("lease")
        assert result == "agreement"

    def test_synonym_mapping_tenancy_agreement(self):
        """Test synonym mapping: tenancy agreement -> agreement."""
        result = normalise_string("tenancy agreement")
        assert result == "agreement"

    def test_no_synonym_match(self):
        """Test that non-synonym strings are returned as normalised."""
        result = normalise_string("contract")
        assert result == "contract"


class TestNormaliseDocumentType:
    """Tests for normalise_document_type function."""

    def test_valid_type_returns_same(self):
        """Test that valid document type returns the same value."""
        doc_type = "contract"
        result, warnings = normalise_document_type(doc_type)
        assert result == "contract"
        assert warnings == []

    def test_synonym_returns_canonical(self):
        """Test that synonym returns canonical type."""
        result, warnings = normalise_document_type("lease")
        assert result == "agreement"
        assert warnings == []

    def test_invalid_type_returns_other_with_warning(self):
        """Test that invalid type returns 'other' and warning."""
        result, warnings = normalise_document_type("invalid_type")
        assert result == "other"
        assert len(warnings) == 1
        assert "invalid_document_type" in warnings[0]

    def test_whitespace_handling(self):
        """Test that whitespace is stripped before validation."""
        result, warnings = normalise_document_type("  contract  ")
        assert result == "contract"
        assert warnings == []


class TestNormaliseCategory:
    """Tests for normalise_category function."""

    def test_valid_category_returns_same(self):
        """Test that valid category returns the same value."""
        category = "communications"
        result, warnings = normalise_category(category)
        assert result == "communications"
        assert warnings == []

    def test_invalid_returns_other_with_warning(self):
        """Test that invalid category returns 'other' and warning."""
        result, warnings = normalise_category("invalid_category")
        assert result == "other"
        assert len(warnings) == 1
        assert "invalid_category" in warnings[0]

    def test_whitespace_handling(self):
        """Test that whitespace is stripped before validation."""
        result, warnings = normalise_category("  communications  ")
        assert result == "communications"
        assert warnings == []


class TestNormaliseAnalysisResult:
    """Tests for normalise_analysis_result function."""

    def test_string_fields_normalised(self):
        """Test that all string fields are normalised."""
        result, warnings = normalise_analysis_result({
            "document_type": "LEASE",
            "category": "COMMUNICATIONS",
            "description": "  A test document  ",
            "human_readable_name": "Test Name",
        })
        assert result["document_type"] == "agreement"  # lease -> agreement (synonym)
        assert result["category"] == "communications"
        assert result["description"] == "a test document"
        assert result["human_readable_name"] == "test name"

    def test_warnings_collected(self):
        """Test that warnings are collected from nested normalisation."""
        result, warnings = normalise_analysis_result({
            "document_type": "invalid_type",
            "category": "also_invalid",
        })
        assert result["document_type"] == "other"
        assert result["category"] == "other"
        assert len(warnings) == 2
        assert any("invalid_document_type" in w for w in warnings)
        assert any("invalid_category" in w for w in warnings)

    def test_nested_dict_normalised(self):
        """Test that nested dictionaries are recursively normalised."""
        result, warnings = normalise_analysis_result({
            "truncation": {
                "series_id_hint": "  Truncation Series  ",
            },
        })
        assert result["truncation"]["series_id_hint"] == "truncation series"

    def test_list_values_normalised(self):
        """Test that list values are normalised."""
        result, warnings = normalise_analysis_result({
            "key_parties": ["  Party A  ", "PARTY B"],
        })
        assert result["key_parties"] == ["party a", "party b"]

    def test_non_string_values_unchanged(self):
        """Test that non-string values are not modified."""
        result, warnings = normalise_analysis_result({
            "quality_score": 3,
            "is_cover_communication": True,
            "key_dates": ["2023-01-01"],
        })
        assert result["quality_score"] == 3
        assert result["is_cover_communication"] is True


class TestNormaliseDescription:
    """Tests for normalise_description function."""

    def test_placeholder_detected(self):
        """Test that placeholder descriptions are detected."""
        result, warnings = normalise_description("This is a document")
        assert result == "this is a document"
        assert len(warnings) == 1
        assert "placeholder_description" in warnings[0]

    def test_empty_string_warning(self):
        """Test that empty string triggers warning."""
        result, warnings = normalise_description("")
        assert result == ""
        assert len(warnings) == 1
        assert "placeholder_description" in warnings[0]

    def test_see_file_placeholder(self):
        """Test that 'see file' placeholder is detected."""
        result, warnings = normalise_description("See file")
        assert result == "see file"
        assert len(warnings) == 1
        assert "placeholder_description" in warnings[0]

    def test_valid_description_no_warning(self):
        """Test that valid description does not trigger warning."""
        result, warnings = normalise_description("Service agreement between Acme and Beta")
        assert result == "service agreement between acme and beta"
        assert warnings == []
