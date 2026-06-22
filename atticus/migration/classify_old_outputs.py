"""Classify legacy workspace files for candidate-only migration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LegacyClassification:
    artifact_type: str
    trust_status: str
    confidence: float
    matched_rule: str


NOISE_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "_inspection_tmp",
}
NOISE_SUFFIXES = {".pyc", ".bak", ".tmp", ".swp"}


def classify_legacy_path(path: str | Path) -> tuple[str, str]:
    classified = classify_legacy_file(path)
    return classified.artifact_type, classified.trust_status


def classify_legacy_file(path: str | Path) -> LegacyClassification:
    p = Path(path)
    name = p.name.lower()
    text = str(p).lower()
    parts = {part.lower() for part in p.parts}

    if parts & NOISE_PARTS or p.suffix.lower() in NOISE_SUFFIXES or name in {".keep"}:
        return LegacyClassification("duplicate_noise", "rejected", 0.95, "infrastructure/noise")

    if "error.json" == name:
        if _has_adjacent_useful_output(p):
            return LegacyClassification("failed_useful", "rough_note", 0.85, "failed task with adjacent useful output")
        return LegacyClassification("failed_no_output", "rejected", 0.9, "failed task without adjacent output")

    if (
        "source_hashes_manifest" in name
        or "source_index" in name
        or ("source" in name and "inventory" in name)
        or "source_expansion" in name
    ):
        artifact_type = "source_index" if "source_index" in name else "source_inventory"
        return LegacyClassification(artifact_type, "candidate", 0.9, "source inventory/hash pattern")
    if name.startswith("manifest") or name.endswith("_manifest.csv") or "evidence_index" in name:
        return LegacyClassification("evidence_registry", "candidate", 0.9, "manifest/evidence index pattern")
    if "sha256" in name or "hash" in name:
        return LegacyClassification("source_inventory", "candidate", 0.82, "hash manifest pattern")
    if any(term in text for term in ("ocr", "extracted", "transcript", "transcription", "native_message", "production-text")):
        return LegacyClassification("extraction_record", "candidate", 0.86, "extraction/ocr/transcript pattern")
    if any(term in text for term in ("production", "crosswalk", "cross_reference", "xref", "oa_jr_dedup")):
        return LegacyClassification("production_crosswalk", "candidate", 0.88, "production/crosswalk pattern")
    if "duplicate" in name or "dedup" in name:
        return LegacyClassification("duplicate_noise", "candidate", 0.75, "duplicate report pattern")
    if any(term in name for term in ("chronology", "timeline", "event", "date")):
        return LegacyClassification("chronology_fragment", "candidate", 0.82, "chronology/timeline pattern")
    if "authority" in name or "/research/" in text or "rule_by_rule" in name or "procedure" in name:
        return LegacyClassification("authority_note", "rough_note", 0.82, "authority/research pattern")
    if any(term in text for term in ("hostile", "opposition_silk", "red_team", "attack_memo", "verdict")):
        return LegacyClassification("hostile_review", "rough_note", 0.86, "hostile review pattern")
    if "/draft" in text or "draft" in name or "submissions" in name:
        return LegacyClassification("draft", "rough_note", 0.84, "draft pattern")
    if any(term in name for term in ("analysis", "audit", "assessment", "gap", "matrix", "map", "synthesis", "status", "route", "risk", "contradiction", "coverage", "sufficiency")):
        return LegacyClassification("analysis", "rough_note", 0.72, "analysis/work-product pattern")
    if "case/work" in text and p.suffix.lower() in {".md", ".txt", ".csv", ".json"}:
        return LegacyClassification("analysis", "rough_note", 0.55, "generic legacy work product")
    return LegacyClassification("legacy_note", "unverified_legacy", 0.35, "fallback")


def _has_adjacent_useful_output(path: Path) -> bool:
    useful = {"result.md", "result.raw.json", "proposed_tasks.json", "audit_note.md"}
    parent = path.parent
    return any((parent / name).exists() and (parent / name).stat().st_size > 0 for name in useful)
