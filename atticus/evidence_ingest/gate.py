"""Quality gate orchestration for evidence ingest pipeline."""

from __future__ import annotations

import json
import time
from typing import Any

from atticus.evidence_ingest import normaliser, prompts
from atticus.evidence_ingest.validator import run_all_validations

HIGH_CONFIDENCE: float = 0.9
MEDIUM_CONFIDENCE: float = 0.7


def run_quality_gate(
    scan_results: list[dict],
    resolution_plan: dict,
) -> dict[str, Any]:
    """Run quality gate checks on scan results and resolution plan.

    Args:
        scan_results: List of scan result dictionaries from evidence analysis.
        resolution_plan: Resolution plan dictionary with duplicate groups,
            truncation groups, recategorisations, renames, and needs_human_review.

    Returns:
        Gate result dictionary with status, validation results, confidence
            classification, quarantine status, and plan path.
    """
    result: dict[str, Any] = {
        "status": "ALL_CLEAR",
        "validation": {"status": "ALL_CLEAR", "errors": []},
        "confidence": {
            "auto_approved": [],
            "needs_glance": [],
            "needs_human_review": [],
        },
        "quarantined": False,
        "plan_path": resolution_plan.get("plan_path", ""),
    }

    retry_count: int = 0
    max_retries: int = 1

    while retry_count <= max_retries:
        validation_result = run_all_validations(scan_results, resolution_plan)
        result["validation"] = validation_result

        if validation_result["status"] == "ALL_CLEAR":
            break

        if retry_count < max_retries:
            corrected_plan = _call_ai_correction(
                resolution_plan,
                validation_result.get("errors", []),
            )
            if corrected_plan:
                resolution_plan = corrected_plan
            retry_count += 1
        else:
            result["status"] = "BLOCKED"
            result["quarantined"] = True
            return result

    # Classify confidence based on resolution plan sources plus scan_results fallback
    confidence_result = _classify_confidence(scan_results, resolution_plan)
    result["confidence"] = confidence_result

    if confidence_result["needs_human_review"]:
        result["status"] = "PARTIAL"
    elif confidence_result["needs_glance"]:
        result["status"] = "PARTIAL"

    return result


def accept_plan(
    resolution_plan: dict,
    accepted_by: str = "human",
) -> dict[str, Any]:
    """Accept a resolution plan by adding acceptance metadata.

    Args:
        resolution_plan: The resolution plan dictionary to accept.
        accepted_by: Identifier of who/what accepted the plan. Defaults to "human".

    Returns:
        Updated resolution plan with acceptance metadata added.
    """
    if "metadata" not in resolution_plan:
        resolution_plan["metadata"] = {}

    resolution_plan["metadata"]["accepted_at"] = time.time()
    resolution_plan["metadata"]["accepted_by"] = accepted_by

    plan_path = resolution_plan.get("plan_path", "")
    if plan_path:
        _save_plan(resolution_plan, plan_path)

    return resolution_plan


def _call_ai_correction(
    plan: dict,
    validation_errors: list[str],
) -> dict | None:
    """Call AI to correct a resolution plan based on validation errors.

    Args:
        plan: The original resolution plan.
        validation_errors: List of validation error messages.

    Returns:
        Corrected resolution plan dictionary, or None if correction fails.
    """
    prompt = (
        prompts.VALIDATOR_FEEDBACK_PROMPT
        .replace("{validation_errors}", "\n".join(validation_errors))
        .replace("{original_plan}", json.dumps(plan, indent=2))
    )

    try:
        from atticus.evidence_ingest.analyser import _call_ai_provider

        # Call AI provider with the correction prompt
        corrected_json = _call_ai_provider(
            "correction_request",
            prompt,
            provider=None,
            model=None,
        )
        if isinstance(corrected_json, dict):
            return corrected_json
    except Exception:
        pass

    return None


def _classify_confidence(
    resolution_plan_or_results: dict | list,
    resolution_plan: dict | None = None,
) -> dict[str, list[str]]:
    """Classify sources in resolution plan by confidence thresholds.

    Old API (tests): _classify_confidence(scan_results, resolution_plan)
    New API (internal): _classify_confidence(resolution_plan)

    Args:
        resolution_plan: Resolution plan dictionary with sources.

    Returns:
        Dictionary with source IDs classified by confidence level.
    """
    classification: dict[str, list[str]] = {
        "auto_approved": [],
        "needs_glance": [],
        "needs_human_review": [],
    }

    # Detect old calling convention
    if resolution_plan is not None:
        scan_results = resolution_plan_or_results
        sources = []
        for item in scan_results:
            if isinstance(item, dict):
                sources.append(item)
        if not resolution_plan.get("sources"):
            resolution_plan = dict(resolution_plan, sources=sources)
    else:
        resolution_plan = resolution_plan_or_results

    sources = resolution_plan.get("sources", [])
    for item in sources:
        source_id = item.get("source_id", "")
        confidence = item.get("confidence", 0.0)

        if isinstance(confidence, str):
            confidence = _confidence_string_to_float(confidence)

        if confidence >= HIGH_CONFIDENCE:
            classification["auto_approved"].append(source_id)
        elif confidence >= MEDIUM_CONFIDENCE:
            classification["needs_glance"].append(source_id)
        else:
            classification["needs_human_review"].append(source_id)

    return classification


def _confidence_string_to_float(confidence: str) -> float:
    """Convert confidence string to float value.

    Args:
        confidence: Confidence string ("high", "medium", "low").

    Returns:
        Float confidence value.
    """
    mapping: dict[str, float] = {
        "high": 0.9,
        "medium": 0.7,
        "low": 0.3,
    }
    return mapping.get(confidence.lower(), 0.0)


def _save_plan(plan: dict, plan_path: str) -> None:
    """Save resolution plan to file.

    Args:
        plan: The resolution plan dictionary to save.
        plan_path: Path to save the plan to.
    """
    import json

    with open(plan_path, "w") as f:
        json.dump(plan, f, indent=2)
