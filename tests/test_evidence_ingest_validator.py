from atticus.evidence_ingest.validator import (
    ValidationError,
    run_all_validations,
    validate_coverage,
    validate_filename_collisions,
    validate_duplicate_integrity,
    validate_series_integrity,
    validate_no_circular_references,
    validate_hash_integrity,
    validate_vocabulary_compliance,
    validate_normalisation,
    validate_descriptions,
)


def make_scan_results(paths_with_hashes=None):
    """Create scan_results list from dict of path->sha256."""
    if paths_with_hashes is None:
        return []
    return [{"path": p, "sha256": h} for p, h in paths_with_hashes.items()]


def make_resolution_plan(sources):
    """Create resolution_plan dict with sources list."""
    return {"sources": sources}


def make_source(source_id, **kwargs):
    """Create a source dict with defaults."""
    source = {"source_id": source_id}
    source.update(kwargs)
    return source


class TestValidateCoverage:
    def test_missing_file_returns_error(self):
        scan_results = make_scan_results({"/path/file1.pdf": "abc123"})
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file2.pdf")
        ])
        errors = validate_coverage(scan_results, resolution_plan)
        assert len(errors) == 1
        assert ValidationError.MISSING_FILE in errors[0]
        assert "file1.pdf" in errors[0]

    def test_duplicate_file_returns_error(self):
        scan_results = make_scan_results({"/path/file1.pdf": "abc123"})
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file1.pdf"),
            make_source("src_2", original_path="/path/file1.pdf"),
        ])
        errors = validate_coverage(scan_results, resolution_plan)
        assert len(errors) == 1
        assert ValidationError.MISSING_FILE in errors[0]
        assert "appears 2 times" in errors[0]

    def test_all_files_present_no_errors(self):
        scan_results = make_scan_results({
            "/path/file1.pdf": "abc123",
            "/path/file2.pdf": "def456",
        })
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file1.pdf"),
            make_source("src_2", original_path="/path/file2.pdf"),
        ])
        errors = validate_coverage(scan_results, resolution_plan)
        assert errors == []


class TestValidateFilenameCollisions:
    def test_collision_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", stored_path="evidence/file.pdf"),
            make_source("src_2", stored_path="evidence/file.pdf"),
        ])
        errors = validate_filename_collisions(resolution_plan)
        assert len(errors) == 1
        assert ValidationError.FILENAME_COLLISION in errors[0]
        assert "file.pdf" in errors[0]
        assert "src_1" in errors[0]
        assert "src_2" in errors[0]

    def test_no_collision_no_errors(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", stored_path="evidence/file1.pdf"),
            make_source("src_2", stored_path="evidence/file2.pdf"),
        ])
        errors = validate_filename_collisions(resolution_plan)
        assert errors == []


class TestValidateDuplicateIntegrity:
    def test_orphan_duplicate_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", duplicate_of="non_existent_id"),
        ])
        errors = validate_duplicate_integrity(resolution_plan)
        assert len(errors) == 1
        assert ValidationError.ORPHAN_DUPLICATE_TARGET in errors[0]
        assert "src_1" in errors[0]
        assert "non_existent_id" in errors[0]

    def test_valid_duplicate_no_errors(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1"),
            make_source("src_2", duplicate_of="src_1"),
        ])
        errors = validate_duplicate_integrity(resolution_plan)
        assert errors == []

    def test_no_duplicate_field_no_errors(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1"),
            make_source("src_2"),
        ])
        errors = validate_duplicate_integrity(resolution_plan)
        assert errors == []


class TestValidateSeriesIntegrity:
    def test_missing_parts_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", part_of_series={
                "series_id": "series_1",
                "parts": ["src_1", "src_2", "src_3"],
            }),
        ])
        errors = validate_series_integrity(resolution_plan)
        assert len(errors) == 1
        assert ValidationError.SERIES_INCOMPLETE in errors[0]
        assert "series_1" in errors[0]
        assert "src_2" in errors[0]
        assert "src_3" in errors[0]

    def test_complete_series_no_errors(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", part_of_series={
                "series_id": "series_1",
                "parts": ["src_1", "src_2"],
            }),
            make_source("src_2", part_of_series={
                "series_id": "series_1",
                "parts": ["src_1", "src_2"],
            }),
        ])
        errors = validate_series_integrity(resolution_plan)
        assert errors == []

    def test_no_series_no_errors(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1"),
            make_source("src_2"),
        ])
        errors = validate_series_integrity(resolution_plan)
        assert errors == []


class TestValidateNoCircularReferences:
    def test_circular_reference_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", duplicate_of="src_2"),
            make_source("src_2", duplicate_of="src_1"),
        ])
        errors = validate_no_circular_references(resolution_plan)
        assert len(errors) == 1
        assert ValidationError.CIRCULAR_REFERENCE in errors[0]
        assert "src_1" in errors[0]
        assert "src_2" in errors[0]

    def test_no_circular_reference_no_errors(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1"),
            make_source("src_2", duplicate_of="src_1"),
        ])
        errors = validate_no_circular_references(resolution_plan)
        assert errors == []


class TestValidateHashIntegrity:
    def test_hash_mismatch_returns_error(self):
        scan_results = make_scan_results({"/path/file1.pdf": "correct_hash"})
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file1.pdf", sha256="wrong_hash"),
        ])
        errors = validate_hash_integrity(scan_results, resolution_plan)
        assert len(errors) == 1
        assert ValidationError.HASH_MISMATCH in errors[0]
        assert "src_1" in errors[0]

    def test_hash_match_no_errors(self):
        scan_results = make_scan_results({"/path/file1.pdf": "same_hash"})
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file1.pdf", sha256="same_hash"),
        ])
        errors = validate_hash_integrity(scan_results, resolution_plan)
        assert errors == []

    def test_missing_scan_hash_no_error(self):
        scan_results = make_scan_results({})
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file1.pdf", sha256="some_hash"),
        ])
        errors = validate_hash_integrity(scan_results, resolution_plan)
        assert errors == []


class TestValidateVocabularyCompliance:
    def test_invalid_category_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", category="invalid_category"),
        ])
        errors = validate_vocabulary_compliance(resolution_plan)
        assert len(errors) == 1
        assert ValidationError.INVALID_VOCABULARY in errors[0]
        assert "invalid_category" in errors[0]

    def test_invalid_document_type_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", document_type="invalid_type"),
        ])
        errors = validate_vocabulary_compliance(resolution_plan)
        assert len(errors) == 1
        assert ValidationError.INVALID_VOCABULARY in errors[0]
        assert "invalid_type" in errors[0]

    def test_valid_vocabulary_no_errors(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", category="correspondence", document_type="email"),
            make_source("src_2", category="other", document_type="video"),
        ])
        errors = validate_vocabulary_compliance(resolution_plan)
        assert errors == []

    def test_empty_vocabulary_no_errors(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1"),
        ])
        errors = validate_vocabulary_compliance(resolution_plan)
        assert errors == []


class TestValidateNormalisation:
    def test_unnormalised_filename_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", stored_path="  FileName  "),
        ])
        errors = validate_normalisation(resolution_plan)
        if errors:
            assert ValidationError.NORMALISATION_NOT_APPLIED in errors[0]

    def test_normalised_filename_no_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", stored_path="filename"),
        ])
        errors = validate_normalisation(resolution_plan)
        filename_errors = [e for e in errors if "Filename" in e]
        assert filename_errors == []


class TestValidateDescriptions:
    def test_placeholder_description_returns_error(self):
        placeholders = ["placeholder", "todo", "tbd", "unknown", "n/a", "none", "..."]
        for placeholder in placeholders:
            resolution_plan = make_resolution_plan([
                make_source("src_1", description=placeholder),
            ])
            errors = validate_descriptions(resolution_plan)
            assert len(errors) == 1, f"Failed for placeholder: {placeholder}"
            assert ValidationError.PLACEHOLDER_DESCRIPTION in errors[0]

    def test_empty_description_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", description=""),
        ])
        errors = validate_descriptions(resolution_plan)
        assert len(errors) == 1
        assert ValidationError.PLACEHOLDER_DESCRIPTION in errors[0]

    def test_valid_description_no_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1", description="Medical report from St. Mary's Hospital dated 2023-05-15"),
        ])
        errors = validate_descriptions(resolution_plan)
        assert errors == []

    def test_no_description_returns_error(self):
        resolution_plan = make_resolution_plan([
            make_source("src_1"),
        ])
        errors = validate_descriptions(resolution_plan)
        assert len(errors) == 1
        assert ValidationError.PLACEHOLDER_DESCRIPTION in errors[0]


class TestRunAllValidations:
    def test_all_clear_when_no_errors(self):
        scan_results = make_scan_results({
            "/path/file1.pdf": "hash1",
            "/path/file2.pdf": "hash2",
        })
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file1.pdf", sha256="hash1",
                       stored_path="file1.pdf", category="correspondence",
                       document_type="email", description="medical report for john doe"),
            make_source("src_2", original_path="/path/file2.pdf", sha256="hash2",
                       stored_path="file2.pdf", category="other",
                       document_type="video", description="police incident report number 12345"),
        ])
        result = run_all_validations(scan_results, resolution_plan)
        assert result["status"] == "ALL_CLEAR"
        assert result["errors"] == []
        assert result["error_codes"] == []

    def test_exceptions_when_errors_exist(self):
        scan_results = make_scan_results({"/path/file1.pdf": "hash1"})
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file1.pdf", sha256="wrong_hash",
                       description="placeholder"),
            make_source("src_2", original_path="/path/missing.pdf"),
        ])
        result = run_all_validations(scan_results, resolution_plan)
        assert result["status"] == "EXCEPTIONS"
        assert len(result["errors"]) > 0
        assert len(result["error_codes"]) > 0

    def test_error_codes_are_unique(self):
        scan_results = make_scan_results({"/path/file1.pdf": "hash1"})
        resolution_plan = make_resolution_plan([
            make_source("src_1", original_path="/path/file1.pdf", sha256="wrong_hash"),
            make_source("src_2", original_path="/path/file1.pdf", sha256="also_wrong"),
        ])
        result = run_all_validations(scan_results, resolution_plan)
        assert result["status"] == "EXCEPTIONS"
        assert len(result["error_codes"]) == len(set(result["error_codes"]))
