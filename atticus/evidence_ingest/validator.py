from __future__ import annotations

from collections import defaultdict
from typing import Any


try:
    from atticus.evidence_ingest.normaliser import (
        normalise_category,
        normalise_description,
        normalise_document_type,
        normalise_filename,
    )
    NORMALISER_AVAILABLE = True
except ImportError:
    NORMALISER_AVAILABLE = False
    normalise_category = None
    normalise_description = None
    normalise_document_type = None
    normalise_filename = None


class ValidationError:
    """Validation error code constants."""
    MISSING_FILE = "missing_file"
    FILENAME_COLLISION = "filename_collision"
    ORPHAN_DUPLICATE_TARGET = "orphan_duplicate_target"
    SERIES_INCOMPLETE = "series_incomplete"
    CIRCULAR_REFERENCE = "circular_reference"
    HASH_MISMATCH = "hash_mismatch"
    INVALID_VOCABULARY = "invalid_vocabulary"
    NORMALISATION_NOT_APPLIED = "normalisation_not_applied"
    PLACEHOLDER_DESCRIPTION = "placeholder_description"


def validate_coverage(scan_results: list[dict], resolution_plan: dict) -> list[str]:
    """Check every file from scan appears exactly once in plan.

    Args:
        scan_results: List of dicts containing scanned file info with 'path' keys.
        resolution_plan: Dict containing 'sources' list with 'original_path' keys.

    Returns:
        List of error messages for files missing or duplicated in plan.
    """
    errors: list[str] = []

    scanned_files: set[str] = set()
    for item in scan_results:
        path = item.get("path") or item.get("file")
        if path:
            scanned_files.add(path)

    plan_files: dict[str, int] = defaultdict(int)
    sources = resolution_plan.get("sources", [])
    for source in sources:
        orig_path = source.get("original_path")
        if orig_path:
            plan_files[orig_path] += 1

    for scanned in scanned_files:
        count = plan_files.get(scanned, 0)
        if count == 0:
            errors.append(f"{ValidationError.MISSING_FILE}: File '{scanned}' not found in resolution plan")
        elif count > 1:
            errors.append(f"{ValidationError.MISSING_FILE}: File '{scanned}' appears {count} times in resolution plan")

    return errors


def validate_filename_collisions(resolution_plan: dict) -> list[str]:
    """Check no two sources map to same stored_path.

    Args:
        resolution_plan: Dict containing 'sources' list with 'stored_path' keys.

    Returns:
        List of error messages for filename collisions.
    """
    errors: list[str] = []

    stored_paths: dict[str, list[str]] = defaultdict(list)
    sources = resolution_plan.get("sources", [])
    for source in sources:
        stored_path = source.get("stored_path")
        source_id = source.get("source_id", "unknown")
        if stored_path:
            stored_paths[stored_path].append(source_id)

    for stored_path, source_ids in stored_paths.items():
        if len(source_ids) > 1:
            errors.append(
                f"{ValidationError.FILENAME_COLLISION}: "
                f"Stored path '{stored_path}' mapped by multiple sources: {', '.join(source_ids)}"
            )

    return errors


def validate_duplicate_integrity(resolution_plan: dict) -> list[str]:
    """Check every duplicate_of target exists as a source_id.

    Args:
        resolution_plan: Dict containing 'sources' list with 'source_id' and 'duplicate_of' keys.

    Returns:
        List of error messages for orphan duplicate targets.
    """
    errors: list[str] = []

    sources = resolution_plan.get("sources", [])
    valid_source_ids: set[str] = {s.get("source_id") for s in sources if s.get("source_id")}

    for source in sources:
        duplicate_of = source.get("duplicate_of")
        source_id = source.get("source_id", "unknown")
        if duplicate_of and duplicate_of not in valid_source_ids:
            errors.append(
                f"{ValidationError.ORPHAN_DUPLICATE_TARGET}: "
                f"Source '{source_id}' duplicates non-existent target '{duplicate_of}'"
            )

    return errors


def validate_series_integrity(resolution_plan: dict) -> list[str]:
    """Check every part_of_series group accounts for all parts.

    Args:
        resolution_plan: Dict containing 'sources' list with 'part_of_series' keys.
        Each part_of_series should be a dict with 'series_id' and 'parts' (list of expected source_ids).

    Returns:
        List of error messages for incomplete series.
    """
    errors: list[str] = []

    series_groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"expected": set(), "actual": set()})
    sources = resolution_plan.get("sources", [])

    for source in sources:
        part_of_series = source.get("part_of_series")
        if part_of_series:
            series_id = part_of_series.get("series_id")
            parts = part_of_series.get("parts", [])
            source_id = source.get("source_id", "unknown")

            if series_id:
                if parts:
                    series_groups[series_id]["expected"].update(parts)
                series_groups[series_id]["actual"].add(source_id)

    for series_id, group in series_groups.items():
        missing = group["expected"] - group["actual"]
        if missing:
            errors.append(
                f"{ValidationError.SERIES_INCOMPLETE}: "
                f"Series '{series_id}' missing parts: {', '.join(sorted(missing))}"
            )

    return errors


def validate_no_circular_references(resolution_plan: dict) -> list[str]:
    """Check no A->B and B->A duplicate chains exist.

    Args:
        resolution_plan: Dict containing 'sources' list with 'source_id' and 'duplicate_of' keys.

    Returns:
        List of error messages for circular references.
    """
    errors: list[str] = []

    sources = resolution_plan.get("sources", [])
    duplicate_map: dict[str, str] = {}
    for source in sources:
        source_id = source.get("source_id")
        duplicate_of = source.get("duplicate_of")
        if source_id and duplicate_of:
            duplicate_map[source_id] = duplicate_of

    for source_id, target_id in duplicate_map.items():
        if target_id in duplicate_map and duplicate_map[target_id] == source_id:
            if source_id < target_id:  # Report only once per pair
                errors.append(
                    f"{ValidationError.CIRCULAR_REFERENCE}: "
                    f"Circular duplicate reference between '{source_id}' and '{target_id}'"
                )

    return errors


def validate_hash_integrity(scan_results: list[dict], resolution_plan: dict) -> list[str]:
    """Check SHA-256 matches for each file.

    Args:
        scan_results: List of dicts with 'path' and 'sha256' keys.
        resolution_plan: Dict containing 'sources' list with 'original_path' and 'sha256' keys.

    Returns:
        List of error messages for hash mismatches.
    """
    errors: list[str] = []

    scan_hashes: dict[str, str] = {}
    for item in scan_results:
        path = item.get("path")
        sha256 = item.get("sha256")
        if path and sha256:
            scan_hashes[path] = sha256

    sources = resolution_plan.get("sources", [])
    for source in sources:
        orig_path = source.get("original_path")
        plan_hash = source.get("sha256")
        source_id = source.get("source_id", "unknown")

        if orig_path and plan_hash:
            scan_hash = scan_hashes.get(orig_path)
            if scan_hash and scan_hash != plan_hash:
                errors.append(
                    f"{ValidationError.HASH_MISMATCH}: "
                    f"Hash mismatch for '{source_id}' (path: {orig_path})"
                )

    return errors


def validate_vocabulary_compliance(resolution_plan: dict) -> list[str]:
    """Check all categories and document_types are in controlled lists.

    Args:
        resolution_plan: Dict containing 'sources' list with 'category' and 'document_type' keys.
        Controlled vocabularies are imported from prompts module.

    Returns:
        List of error messages for invalid vocabulary usage.
    """
    errors: list[str] = []

    from atticus.evidence_ingest.prompts import CATEGORIES, DOCUMENT_TYPES
    valid_categories: set[str] = set(CATEGORIES)
    valid_document_types: set[str] = set(DOCUMENT_TYPES)

    sources = resolution_plan.get("sources", [])
    for source in sources:
        source_id = source.get("source_id", "unknown")

        category = source.get("category")
        if category and category not in valid_categories:
            errors.append(
                f"{ValidationError.INVALID_VOCABULARY}: "
                f"Invalid category '{category}' for source '{source_id}'"
            )

        document_type = source.get("document_type")
        if document_type and document_type not in valid_document_types:
            errors.append(
                f"{ValidationError.INVALID_VOCABULARY}: "
                f"Invalid document_type '{document_type}' for source '{source_id}'"
            )

    return errors


def validate_normalisation(resolution_plan: dict) -> list[str]:
    """Check all string fields are normalised.

    Args:
        resolution_plan: Dict containing 'sources' list with string fields.

    Returns:
        List of error messages for fields where normalisation not applied.
    """
    errors: list[str] = []

    if not NORMALISER_AVAILABLE:
        return errors

    sources = resolution_plan.get("sources", [])
    for source in sources:
        source_id = source.get("source_id", "unknown")

        filename = source.get("stored_path")
        if filename and normalise_filename and filename != normalise_filename(filename):
            errors.append(
                f"{ValidationError.NORMALISATION_NOT_APPLIED}: "
                f"Filename not normalised for source '{source_id}'"
            )

        category = source.get("category")
        if category and normalise_category:
            normalised_category, _ = normalise_category(category)
            if category != normalised_category:
                errors.append(
                    f"{ValidationError.NORMALISATION_NOT_APPLIED}: "
                    f"Category not normalised for source '{source_id}'"
                )

        document_type = source.get("document_type")
        if document_type and normalise_document_type:
            normalised_type, _ = normalise_document_type(document_type)
            if document_type != normalised_type:
                errors.append(
                    f"{ValidationError.NORMALISATION_NOT_APPLIED}: "
                    f"Document type not normalised for source '{source_id}'"
                )

        description = source.get("description")
        if description and normalise_description:
            normalised_desc, _ = normalise_description(description)
            if description != normalised_desc:
                errors.append(
                    f"{ValidationError.NORMALISATION_NOT_APPLIED}: "
                    f"Description not normalised for source '{source_id}'"
                )

    return errors


def validate_descriptions(resolution_plan: dict) -> list[str]:
    """Check no placeholder descriptions exist.

    Args:
        resolution_plan: Dict containing 'sources' list with 'description' keys.

    Returns:
        List of error messages for placeholder descriptions.
    """
    errors: list[str] = []

    placeholder_patterns: list[str] = [
        "placeholder",
        "todo",
        "tbd",
        "to be determined",
        "to be filled",
        "unknown",
        "n/a",
        "na",
        "none",
        "no description",
        "desc",
        "...",
        "___",
        "---",
    ]

    sources = resolution_plan.get("sources", [])
    for source in sources:
        source_id = source.get("source_id", "unknown")
        description = source.get("description", "")

        if not description:
            errors.append(
                f"{ValidationError.PLACEHOLDER_DESCRIPTION}: "
                f"Empty description for source '{source_id}'"
            )
            continue

        desc_lower = description.lower().strip()
        for pattern in placeholder_patterns:
            if pattern in desc_lower:
                errors.append(
                    f"{ValidationError.PLACEHOLDER_DESCRIPTION}: "
                    f"Placeholder description for source '{source_id}': '{description}'"
                )
                break

    return errors


def run_all_validations(scan_results: list[dict], resolution_plan: dict) -> dict:
    """Run all validators and return aggregated results.

    Args:
        scan_results: List of dicts containing scanned file info.
        resolution_plan: Dict containing the resolution plan with sources.

    Returns:
        Dict with 'status', 'errors', and 'error_codes' keys.
    """
    all_errors: list[str] = []

    all_errors.extend(validate_coverage(scan_results, resolution_plan))
    all_errors.extend(validate_filename_collisions(resolution_plan))
    all_errors.extend(validate_duplicate_integrity(resolution_plan))
    all_errors.extend(validate_series_integrity(resolution_plan))
    all_errors.extend(validate_no_circular_references(resolution_plan))
    all_errors.extend(validate_hash_integrity(scan_results, resolution_plan))
    all_errors.extend(validate_vocabulary_compliance(resolution_plan))
    all_errors.extend(validate_normalisation(resolution_plan))
    all_errors.extend(validate_descriptions(resolution_plan))

    error_codes: list[str] = []
    for error in all_errors:
        code = error.split(":")[0] if ":" in error else error
        if code not in error_codes:
            error_codes.append(code)

    status = "ALL_CLEAR" if not all_errors else "EXCEPTIONS"

    return {
        "status": status,
        "errors": all_errors,
        "error_codes": error_codes,
    }
