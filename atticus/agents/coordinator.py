"""Dry-run-first legal coordinator planning.

The coordinator creates matter-scoped task graphs. It never calls providers,
never creates leases, and never writes canonical legal memory or artifacts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
import sqlite3
from typing import cast

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class CoordinatorTaskTemplate:
    key: str
    role: str
    task_type: str
    title: str
    stage: LegalStage
    deliverable: str
    validation_gates: tuple[str, ...]
    required_certifications: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    verifier_required: bool = False
    risk_focus: tuple[str, ...] = ()


def plan_coordinator_work(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    goal: str,
    source_ids: Iterable[str] = (),
    artifact_ids: Iterable[str] = (),
    dry_run: bool = True,
) -> dict[str, object]:
    """Plan or create self-contained worker tasks for a legal goal."""

    clean_goal = " ".join(goal.split()).strip()
    if not clean_goal:
        raise ValueError("coordinator goal is required")
    sources = _dedupe(source_ids)
    artifacts = _dedupe(artifact_ids)
    _validate_dependencies(conn, matter_scope=matter_scope, source_ids=sources, artifact_ids=artifacts)

    templates = _templates_for_goal(clean_goal)
    goal_slug = _safe_component(clean_goal.lower())[:72] or "legal-work"
    task_id_by_key = {
        template.key: _safe_component(f"{matter_scope}-coord-{goal_slug}-{template.key}") for template in templates
    }
    tasks = [
        _task_from_template(
            template,
            task_id=task_id_by_key[template.key],
            matter_scope=matter_scope,
            goal=clean_goal,
            source_ids=sources,
            artifact_ids=artifacts,
            task_dependencies=[task_id_by_key[key] for key in template.depends_on],
        )
        for template in templates
    ]

    existing = _existing_task_ids(conn, (task["task_id"] for task in tasks))
    created: list[str] = []
    if not dry_run:
        repo.ensure_matter(conn, matter_scope)
        for task in tasks:
            task_id = str(task["task_id"])
            if task_id in existing:
                continue
            task_dependencies = _string_list(task, "task_dependencies")
            required_certifications = _string_list(task, "required_certifications")
            validation_gates = _string_list(task, "validation_gates")
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=task_id,
                    title=str(task["title"]),
                    task_type=str(task["task_type"]),
                    instructions=str(task["instructions"]),
                    matter_scope=matter_scope,
                    stage=LegalStage(str(task["stage"])),
                    status=TaskStatus.QUEUED,
                    source_dependencies=list(sources),
                    artifact_dependencies=list(artifacts),
                    task_dependencies=task_dependencies,
                    required_certifications=[
                        {"subject_type": "matter", "subject_id": matter_scope, "certification_type": str(cert)}
                        for cert in required_certifications
                    ],
                    validation_gates=validation_gates,
                    staleness_rules={
                        "source_scope": matter_scope,
                        "coordinator_goal": clean_goal,
                        "stale_dependency_blocks": True,
                    },
                ),
            )
            created.append(task_id)
        _ = repo.emit_event(
            conn,
            "coordinator.tasks_created",
            matter_scope=matter_scope,
            payload={
                "goal": clean_goal,
                "task_ids": created,
                "existing_task_ids": sorted(existing),
                "external_actions": "blocked",
            },
        )

    return {
        "dry_run": dry_run,
        "matter_scope": matter_scope,
        "goal": clean_goal,
        "tasks": tasks,
        "existing_task_ids": sorted(existing),
        "created_task_ids": created,
        "external_actions": "blocked",
        "provider_calls": 0,
        "leases_created": 0,
        "candidate_outputs_created": 0,
        "canonical_writes": 0,
    }


def _templates_for_goal(goal: str) -> tuple[CoordinatorTaskTemplate, ...]:
    lowered = goal.lower()
    if _contains_any(lowered, ("draft", "complaint", "letter", "correspondence", "pleading", "submission", "witness statement")):
        return _drafting_templates()
    if _contains_any(lowered, ("chronology", "timeline", "sequence", "events")):
        return _chronology_templates()
    if _contains_any(lowered, ("authority", "case law", "statute", "legal research", "law map", "research")):
        return _authority_templates()
    if _contains_any(lowered, ("sar", "disclosure", "gdpr", "productions", "bundle")):
        return _disclosure_templates()
    return _evidence_triage_templates()


def _drafting_templates() -> tuple[CoordinatorTaskTemplate, ...]:
    return (
        CoordinatorTaskTemplate(
            key="evidence-map",
            role="evidence_worker",
            task_type="evidence_issue_map",
            title="Map evidence and issues before drafting",
            stage=LegalStage.S2_EVIDENCE_REGISTRY,
            deliverable="A cited issue and evidence map identifying what can and cannot safely be alleged.",
            validation_gates=("claim_evidence_support", "stale_dependency", "cross_matter_isolation"),
            risk_focus=("unsupported allegation", "missing source", "stale evidence"),
        ),
        CoordinatorTaskTemplate(
            key="draft",
            role="drafting_worker",
            task_type="draft_preparation",
            title="Prepare candidate legal draft",
            stage=LegalStage.S8_DRAFT_PREPARATION,
            deliverable="A candidate draft that separates evidence-backed text from uncertain or strategic options.",
            validation_gates=("claim_evidence_support", "legal_citation_integrity", "privacy_redaction", "stale_dependency"),
            depends_on=("evidence-map",),
            risk_focus=("overstatement", "uncited claim", "forum mismatch", "privacy leak"),
        ),
        CoordinatorTaskTemplate(
            key="citation-audit",
            role="citation_auditor",
            task_type="citation_audit",
            title="Audit every factual and legal citation in the draft",
            stage=LegalStage.S7_HOSTILE_REVIEW,
            deliverable="A pass/fail citation audit listing unsupported, weak, stale, or fabricated citations.",
            validation_gates=("citation_integrity", "fabricated_citation", "cross_matter_isolation"),
            depends_on=("draft",),
            verifier_required=True,
            risk_focus=("fabricated citation", "unsupported factual assertion", "unsupported legal proposition"),
        ),
        CoordinatorTaskTemplate(
            key="hostile-review",
            role="hostile_reviewer",
            task_type="hostile_opponent_review",
            title="Hostile opponent review",
            stage=LegalStage.S7_HOSTILE_REVIEW,
            deliverable="A sceptical opponent-style review of weaknesses, overstatement, missing proof, and procedural risk.",
            validation_gates=("hostile_review", "overstatement_risk", "authority_support"),
            depends_on=("draft",),
            verifier_required=True,
            risk_focus=("weak claim", "bad fact", "opponent answer", "remedy risk"),
        ),
        CoordinatorTaskTemplate(
            key="privacy-redaction-audit",
            role="privacy_reviewer",
            task_type="privacy_redaction_audit",
            title="Privacy and redaction audit",
            stage=LegalStage.S9_FINAL_QUALITY_GATE,
            deliverable="A privacy and redaction review identifying sensitive data, privilege, and unnecessary disclosure.",
            validation_gates=("privacy_redaction", "privilege_check"),
            depends_on=("draft",),
            verifier_required=True,
            risk_focus=("personal data", "privilege", "sensitive third-party material"),
        ),
        CoordinatorTaskTemplate(
            key="final-quality-gate",
            role="reducer",
            task_type="final_quality_gate",
            title="Final quality gate for candidate draft",
            stage=LegalStage.S9_FINAL_QUALITY_GATE,
            deliverable="A reducer-facing final QA checklist; do not certify or file the draft automatically.",
            validation_gates=("claim_evidence_support", "citation_integrity", "hostile_review", "privacy_redaction"),
            required_certifications=("citation_audit", "hostile_review", "privacy_redaction_audit"),
            depends_on=("citation-audit", "hostile-review", "privacy-redaction-audit"),
            verifier_required=True,
            risk_focus=("uncertified final text", "external action", "canonical write bypass"),
        ),
    )


def _chronology_templates() -> tuple[CoordinatorTaskTemplate, ...]:
    return (
        CoordinatorTaskTemplate(
            key="event-extraction",
            role="chronology_worker",
            task_type="chronology_event_extraction",
            title="Extract cited chronology events",
            stage=LegalStage.S4_BASELINE_CHRONOLOGY,
            deliverable="A date-ordered candidate chronology with citations for every event and uncertainty for unclear dates.",
            validation_gates=("chronology_citations", "date_precision", "stale_dependency"),
            risk_focus=("unclear date", "missing locator", "source conflict"),
        ),
        CoordinatorTaskTemplate(
            key="chronology-consistency-audit",
            role="citation_auditor",
            task_type="chronology_consistency_audit",
            title="Audit chronology consistency",
            stage=LegalStage.S7_HOSTILE_REVIEW,
            deliverable="A hostile audit of chronology conflicts, gaps, unsupported dates, and stale assumptions.",
            validation_gates=("chronology_citations", "contradiction_detection", "cross_matter_isolation"),
            depends_on=("event-extraction",),
            verifier_required=True,
            risk_focus=("contradiction", "unsupported event", "wrong date precision"),
        ),
    )


def _authority_templates() -> tuple[CoordinatorTaskTemplate, ...]:
    return (
        CoordinatorTaskTemplate(
            key="authority-map",
            role="research_worker",
            task_type="authority_map",
            title="Map authorities and legal issues",
            stage=LegalStage.S6_AUTHORITY_LAW_MAP,
            deliverable="A jurisdiction-aware authority map with each proposition marked verified, uncertain, or needs research.",
            validation_gates=("authority_jurisdiction", "authority_citation_format", "stale_dependency"),
            risk_focus=("wrong jurisdiction", "uncited legal proposition", "outdated law"),
        ),
        CoordinatorTaskTemplate(
            key="authority-audit",
            role="procedural_reviewer",
            task_type="authority_audit",
            title="Audit authority support",
            stage=LegalStage.S7_HOSTILE_REVIEW,
            deliverable="A hostile authority audit that challenges each legal proposition and flags unsupported or stale law.",
            validation_gates=("authority_support", "hostile_review", "jurisdiction_check"),
            depends_on=("authority-map",),
            verifier_required=True,
            risk_focus=("unsupported law", "stale authority", "forum mismatch"),
        ),
    )


def _disclosure_templates() -> tuple[CoordinatorTaskTemplate, ...]:
    return (
        CoordinatorTaskTemplate(
            key="disclosure-map",
            role="evidence_worker",
            task_type="disclosure_obligation_map",
            title="Map disclosure obligations and evidence gaps",
            stage=LegalStage.S3_PRODUCTION_STATUS,
            deliverable="A cited disclosure and production map identifying missing records, privilege, and redaction needs.",
            validation_gates=("production_mapping", "privacy_redaction", "stale_dependency"),
            risk_focus=("missing record", "privilege", "personal data"),
        ),
        CoordinatorTaskTemplate(
            key="redaction-audit",
            role="privacy_reviewer",
            task_type="privacy_redaction_audit",
            title="Audit privacy and redaction risk",
            stage=LegalStage.S9_FINAL_QUALITY_GATE,
            deliverable="A privacy audit identifying redaction duties, third-party data, privilege, and safe next tasks.",
            validation_gates=("privacy_redaction", "privilege_check", "cross_matter_isolation"),
            depends_on=("disclosure-map",),
            verifier_required=True,
            risk_focus=("over-disclosure", "privileged material", "third-party data"),
        ),
    )


def _evidence_triage_templates() -> tuple[CoordinatorTaskTemplate, ...]:
    return (
        CoordinatorTaskTemplate(
            key="evidence-triage",
            role="evidence_worker",
            task_type="evidence_triage",
            title="Triage evidence and immediate gaps",
            stage=LegalStage.S2_EVIDENCE_REGISTRY,
            deliverable="A cited evidence triage separating confirmed facts, gaps, contradictions, and urgent follow-up tasks.",
            validation_gates=("claim_evidence_support", "contradiction_detection", "stale_dependency"),
            risk_focus=("missing evidence", "contradiction", "uncertain posture"),
        ),
        CoordinatorTaskTemplate(
            key="citation-audit",
            role="citation_auditor",
            task_type="citation_audit",
            title="Audit evidence triage citations",
            stage=LegalStage.S7_HOSTILE_REVIEW,
            deliverable="A pass/fail audit of the triage citations and any unsupported factual claims.",
            validation_gates=("citation_integrity", "fabricated_citation", "cross_matter_isolation"),
            depends_on=("evidence-triage",),
            verifier_required=True,
            risk_focus=("fabricated citation", "weak support", "uncited fact"),
        ),
    )


def _task_from_template(
    template: CoordinatorTaskTemplate,
    *,
    task_id: str,
    matter_scope: str,
    goal: str,
    source_ids: tuple[str, ...],
    artifact_ids: tuple[str, ...],
    task_dependencies: list[str],
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "matter_scope": matter_scope,
        "status": str(TaskStatus.QUEUED),
        "stage": str(template.stage),
        "role": template.role,
        "task_type": template.task_type,
        "title": template.title,
        "instructions": _instructions_for(
            template,
            matter_scope=matter_scope,
            goal=goal,
            source_ids=source_ids,
            artifact_ids=artifact_ids,
            task_dependencies=task_dependencies,
        ),
        "source_dependencies": list(source_ids),
        "artifact_dependencies": list(artifact_ids),
        "task_dependencies": task_dependencies,
        "validation_gates": list(template.validation_gates),
        "required_certifications": list(template.required_certifications),
        "verifier_required": template.verifier_required,
        "risk_focus": list(template.risk_focus),
    }


def _instructions_for(
    template: CoordinatorTaskTemplate,
    *,
    matter_scope: str,
    goal: str,
    source_ids: tuple[str, ...],
    artifact_ids: tuple[str, ...],
    task_dependencies: list[str],
) -> str:
    verifier = (
        "This is an independent verifier task: attack weak work, do not rubber-stamp, and return pass/fail findings. "
        if template.verifier_required
        else ""
    )
    return (
        f"Coordinator role: {template.role}. matter_scope: {matter_scope}. Legal stage: {template.stage}. "
        f"Coordinator goal: {goal}. Task title: {template.title}. Task deliverable: {template.deliverable} "
        f"Bounded source dependencies: {', '.join(source_ids) if source_ids else 'none supplied'}. "
        f"Bounded artifact dependencies: {', '.join(artifact_ids) if artifact_ids else 'none supplied'}. "
        f"Task dependencies: {', '.join(task_dependencies) if task_dependencies else 'none'}. "
        f"Validation gates: {', '.join(template.validation_gates) if template.validation_gates else 'none'}. "
        f"Risk focus: {', '.join(template.risk_focus) if template.risk_focus else 'general legal reliability'}. "
        f"{verifier}"
        "This work order is self-contained; do not rely on unstated prior conversation. "
        "Workers produce candidate packets only. Reducers are the only canonical writers. "
        "Use only matter-scoped sources, artifacts, authorities, active legal memory, and dependencies provided in the work order. "
        "Separate fact, law, procedure, inference, drafting note, contradiction, risk, uncertainty, and redaction concerns. "
        "Cite every factual, legal, procedural, contradiction, or risk finding to an allowed context target; otherwise mark it uncertain or needs_research. "
        "Do not invent evidence, authorities, quotations, dates, amounts, deadlines, remedies, procedural posture, or user instructions. "
        "Memory may orient work but is not proof. Flag stale evidence, weak support, missing certification, privacy risk, and contradiction. "
        "Propose follow-up tasks for gaps rather than hiding uncertainty. "
        "Do not send, file, serve, upload, email, contact, message, or perform external legal actions."
    )


def _validate_dependencies(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_ids: tuple[str, ...],
    artifact_ids: tuple[str, ...],
) -> None:
    _validate_ids(
        conn,
        table="sources",
        id_column="source_id",
        matter_scope=matter_scope,
        ids=source_ids,
        label="source",
    )
    _validate_ids(
        conn,
        table="artifacts",
        id_column="artifact_id",
        matter_scope=matter_scope,
        ids=artifact_ids,
        label="artifact",
    )


def _validate_ids(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    matter_scope: str,
    ids: tuple[str, ...],
    label: str,
) -> None:
    if not ids:
        return
    rows = conn.execute(
        f"SELECT {id_column} FROM {table} WHERE {id_column} IN ({','.join('?' for _ in ids)}) AND matter_scope = ?",
        (*ids, matter_scope),
    ).fetchall()
    found = {str(row[id_column]) for row in rows}
    missing = [item for item in ids if item not in found]
    if missing:
        raise ValueError(f"{label} dependencies are missing or outside matter scope {matter_scope}: {', '.join(missing)}")


def _existing_task_ids(conn: sqlite3.Connection, task_ids: Iterable[object]) -> set[str]:
    ids = tuple(str(item) for item in task_ids)
    if not ids:
        return set()
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE task_id IN (%s)" % ",".join("?" for _ in ids),
        ids,
    ).fetchall()
    return {str(row["task_id"]) for row in rows}


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = str(item).strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return tuple(result)


def _string_list(mapping: Mapping[str, object], field: str) -> list[str]:
    value = mapping.get(field, [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value)]


def _safe_component(value: str) -> str:
    return _SAFE_ID_RE.sub("-", value.strip()).strip(".-") or "coord"
