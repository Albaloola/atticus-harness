"""Controlled vocabulary enforcement and string normalisation."""

from __future__ import annotations

from atticus.evidence_ingest.prompts import DOCUMENT_TYPES, CATEGORIES

SYNONYM_MAP: dict[str, str] = {
    "lease": "agreement",
    "tenancy agreement": "agreement",
    "eviction_notice": "notice",
    "ntq": "notice",
    "bank stmt": "financial_statement",
    "payment recpt": "receipt",
}


def normalise_string(value: str) -> str:
    """Normalise a string value.

    Args:
        value: The string to normalise.

    Returns:
        The normalised string.
    """
    normalised = value.lower().strip()
    return SYNONYM_MAP.get(normalised, normalised)


def normalise_document_type(doc_type: str) -> tuple[str, list[str]]:
    """Normalise and validate a document type.

    Args:
        doc_type: The document type to normalise.

    Returns:
        A tuple of (normalised_type, warnings).
    """
    warnings: list[str] = []
    normalised = normalise_string(doc_type)
    if normalised not in DOCUMENT_TYPES:
        warnings.append(f"invalid_document_type: {doc_type}")
        return ("other", warnings)
    return (normalised, warnings)


def normalise_category(category: str) -> tuple[str, list[str]]:
    """Normalise and validate a category.

    Args:
        category: The category to normalise.

    Returns:
        A tuple of (normalised_category, warnings).
    """
    warnings: list[str] = []
    normalised = normalise_string(category)
    if normalised not in CATEGORIES:
        warnings.append(f"invalid_category: {category}")
        return ("other", warnings)
    return (normalised, warnings)


def normalise_analysis_result(result: dict) -> tuple[dict, list[str]]:
    """Normalise all string fields in an analysis result.

    Args:
        result: The analysis result dictionary to normalise.

    Returns:
        A tuple of (normalised_result, warnings).
    """
    warnings: list[str] = []
    normalised: dict = {}

    for key, value in result.items():
        if isinstance(value, str):
            normalised[key], new_warnings = _normalise_field(key, value)
            warnings.extend(new_warnings)
        elif isinstance(value, dict):
            normalised[key], new_warnings = normalise_analysis_result(value)
            warnings.extend(new_warnings)
        elif isinstance(value, list):
            normalised[key], new_warnings = _normalise_list(key, value)
            warnings.extend(new_warnings)
        else:
            normalised[key] = value

    return (normalised, warnings)


def _normalise_field(field_name: str, value: str) -> tuple[str, list[str]]:
    """Normalise a single field based on its name.

    Args:
        field_name: The name of the field.
        value: The value to normalise.

    Returns:
        A tuple of (normalised_value, warnings).
    """
    if field_name == "document_type":
        return normalise_document_type(value)
    elif field_name == "category":
        return normalise_category(value)
    elif field_name == "description":
        return normalise_description(value)
    else:
        return (normalise_string(value), [])


def _normalise_list(field_name: str, values: list) -> tuple[list, list[str]]:
    """Normalise a list of values.

    Args:
        field_name: The name of the field.
        values: The list to normalise.

    Returns:
        A tuple of (normalised_list, warnings).
    """
    warnings: list[str] = []
    normalised: list = []

    for item in values:
        if isinstance(item, str):
            norm_item, item_warnings = _normalise_field(field_name, item)
            warnings.extend(item_warnings)
            normalised.append(norm_item)
        elif isinstance(item, dict):
            norm_item, item_warnings = normalise_analysis_result(item)
            warnings.extend(item_warnings)
            normalised.append(norm_item)
        else:
            normalised.append(item)

    return (normalised, warnings)


def normalise_filename(filename: str) -> str:
    """Normalise a filename.

    Args:
        filename: The filename to normalise.

    Returns:
        The normalised filename.
    """
    return normalise_string(filename)


def normalise_description(description: str) -> tuple[str, list[str]]:
    """Normalise a description and check for placeholders.

    Args:
        description: The description to normalise.

    Returns:
        A tuple of (normalised_description, warnings).
    """
    warnings: list[str] = []
    normalised = description.lower().strip()

    placeholders: list[str] = ["this is a document", "see file", ""]
    if normalised in placeholders:
        warnings.append(f"placeholder_description: {description}")

    return (normalised, warnings)
