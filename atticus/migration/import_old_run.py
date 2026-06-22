"""Safe candidate import from legacy Atticus/OpenClaw workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.migration.salvage_indexes import iter_candidate_files


@dataclass(frozen=True)
class CandidateImport:
    path: str
    artifact_type: str
    trust_status: str


@dataclass(frozen=True)
class ImportResult:
    dry_run: bool
    candidates: list[CandidateImport]
    validation_tasks_created: int = 0


def import_candidates(
    conn: sqlite3.Connection,
    *,
    workspace: str | Path,
    dry_run: bool = True,
) -> ImportResult:
    candidates = [
        CandidateImport(str(path), artifact_type, trust_status)
        for path, artifact_type, trust_status in iter_candidate_files(workspace)
    ]
    validation_tasks = 0
    if not dry_run:
        for candidate in candidates:
            artifact_id = repo.add_artifact_from_file(
                conn,
                Path(candidate.path),
                artifact_type=candidate.artifact_type,
                trust_status=candidate.trust_status,
                imported_from=str(workspace),
            )
            if candidate.trust_status in {"candidate", "rough_note", "unverified_legacy"}:
                validation_tasks += 1
                repo.add_task(
                    conn,
                    TaskSpec(
                        task_id=f"validate-{artifact_id}",
                        matter_scope="atticus",
                        stage=_validation_stage(candidate.artifact_type),
                        status=TaskStatus.QUEUED,
                        task_type="legacy_validation",
                        title=f"Validate legacy {candidate.artifact_type}: {Path(candidate.path).name}",
                        artifact_dependencies=[artifact_id],
                        validation_gates=["foundation", "stale_dependency"],
                        provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.02},
                        cost_limit_usd=0.25,
                    ),
                )
    return ImportResult(dry_run=dry_run, candidates=candidates, validation_tasks_created=validation_tasks)


def _validation_stage(artifact_type: str) -> LegalStage:
    if artifact_type in {"source_inventory", "source_index"}:
        return LegalStage.S0_SOURCE_INVENTORY
    if artifact_type in {"extraction_record"}:
        return LegalStage.S1_EXTRACTION
    if artifact_type in {"evidence_registry"}:
        return LegalStage.S2_EVIDENCE_REGISTRY
    if artifact_type in {"production_crosswalk"}:
        return LegalStage.S3_PRODUCTION_STATUS
    if artifact_type in {"chronology_fragment"}:
        return LegalStage.S4_BASELINE_CHRONOLOGY
    if artifact_type in {"authority_note"}:
        return LegalStage.S6_AUTHORITY_LAW_MAP
    if artifact_type in {"hostile_review"}:
        return LegalStage.S7_HOSTILE_REVIEW
    if artifact_type in {"draft"}:
        return LegalStage.S8_DRAFT_PREPARATION
    return LegalStage.S2_EVIDENCE_REGISTRY
