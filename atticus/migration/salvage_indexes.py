"""Discovery of candidate salvage files."""

from __future__ import annotations

from pathlib import Path

from atticus.migration.classify_old_outputs import classify_legacy_file

SUPPORTED_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv", ".txt", ".md", ".sha256"}


def iter_candidate_files(workspace: str | Path) -> list[tuple[Path, str, str]]:
    root = Path(workspace)
    results: list[tuple[Path, str, str]] = []
    if not root.exists():
        raise FileNotFoundError(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        classified = classify_legacy_file(path)
        artifact_type, trust_status = classified.artifact_type, classified.trust_status
        if trust_status == "rejected":
            continue
        if artifact_type == "legacy_note" and trust_status == "unverified_legacy":
            continue
        results.append((path, artifact_type, trust_status))
    return results


def iter_classified_files(workspace: str | Path) -> list[tuple[Path, object]]:
    root = Path(workspace)
    if not root.exists():
        raise FileNotFoundError(root)
    results: list[tuple[Path, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        results.append((path, classify_legacy_file(path)))
    return results
