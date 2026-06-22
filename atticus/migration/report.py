"""Dry-run migration reporting for legacy workspaces."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import hashlib
import sqlite3

from atticus.db import repo
from atticus.migration.salvage_indexes import iter_classified_files


@dataclass(frozen=True)
class MigrationReport:
    dry_run: bool
    workspace: str
    total_files: int
    by_classification: dict[str, int]
    by_trust_status: dict[str, int]
    candidate_count: int
    rough_note_count: int
    rejected_count: int
    examples: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "workspace": self.workspace,
            "total_files": self.total_files,
            "by_classification": self.by_classification,
            "by_trust_status": self.by_trust_status,
            "candidate_count": self.candidate_count,
            "rough_note_count": self.rough_note_count,
            "rejected_count": self.rejected_count,
            "examples": self.examples,
            "stance": "legacy material is candidate/rough-note only; no old artifact is certified",
        }


def build_migration_report(
    conn: sqlite3.Connection | None,
    *,
    workspace: str | Path,
    dry_run: bool = True,
    persist: bool = False,
    limit_examples: int = 25,
) -> MigrationReport:
    root = Path(workspace)
    classified = iter_classified_files(root)
    by_classification = Counter(item.artifact_type for _, item in classified)
    by_trust = Counter(item.trust_status for _, item in classified)
    examples: list[dict[str, object]] = []
    for path, item in classified[:limit_examples]:
        stat = path.stat()
        examples.append(
            {
                "path": str(path),
                "relative_path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
                "size_bytes": stat.st_size,
                "mtime": int(stat.st_mtime),
                "extension": path.suffix.lower(),
                "sha256": _sha256(path) if stat.st_size <= 10_000_000 else "",
                "classification": item.artifact_type,
                "trust_status": item.trust_status,
                "confidence": item.confidence,
                "matched_rule": item.matched_rule,
                "requires_human_attention": item.trust_status in {"rough_note", "unverified_legacy", "rejected"},
            }
        )
    report = MigrationReport(
        dry_run=dry_run,
        workspace=str(root),
        total_files=len(classified),
        by_classification=dict(sorted(by_classification.items())),
        by_trust_status=dict(sorted(by_trust.items())),
        candidate_count=by_trust.get("candidate", 0),
        rough_note_count=by_trust.get("rough_note", 0),
        rejected_count=by_trust.get("rejected", 0),
        examples=examples,
    )
    if persist and conn is not None:
        _ = repo.record_migration_report(conn, workspace_path=str(root), dry_run=dry_run, summary=report.as_dict())
    return report


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
