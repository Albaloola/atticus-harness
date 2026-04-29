"""Dry-run-first legal coordinator planning.

The coordinator creates matter-scoped task graphs. It never calls providers,
never creates leases, and never writes canonical legal memory or artifacts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import re
import sqlite3
from typing import cast

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.model_decision import ModelDecision
from atticus.providers.model_policy import default_smart_model_policy, smart_provider_policy_for_route

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


@dataclass(frozen=True)
class AdaptivePlan:
    matter_scope: str
    goal: str
    selected_stages: tuple[str, ...]
    skipped_stages: tuple[dict[str, str], ...]
    required_workers: tuple[str, ...]
    required_gates: tuple[str, ...]
    model_decisions: tuple[ModelDecision, ...]
    tasks: tuple[TaskSpec, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "matter_scope": self.matter_scope,
            "goal": self.goal,
            "selected_stages": list(self.selected_stages),
            "skipped_stages": list(self.skipped_stages),
            "required_workers": list(self.required_workers),
            "required_gates": list(self.required_gates),
            "model_decisions": [decision.__dict__ for decision in self.model_decisions],
            "tasks": [task.__dict__ for task in self.tasks],
            "reasons": list(self.reasons),
        }


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
    explicit_sources = _dedupe(source_ids)
    artifacts = _dedupe(artifact_ids)
    _validate_dependencies(conn, matter_scope=matter_scope, source_ids=explicit_sources, artifact_ids=artifacts)
    sources = explicit_sources or _default_source_dependencies_for_goal(conn, matter_scope=matter_scope, goal=clean_goal)

    templates, profile_skips = _templates_allowed_by_active_profile(
        conn,
        matter_scope=matter_scope,
        templates=_templates_for_goal(clean_goal),
    )
    goal_slug = _safe_component(clean_goal.lower())[:72] or "legal-work"
    dependency_slug = _dependency_slug(sources=sources, artifacts=artifacts)
    task_id_by_key = {
        template.key: _safe_component(f"{matter_scope}-coord-{goal_slug}{dependency_slug}-{template.key}") for template in templates
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
                    provider_policy=dict(cast(Mapping[str, object], task["provider_policy"])),
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
        "profile_skipped_tasks": list(profile_skips),
        "existing_task_ids": sorted(existing),
        "created_task_ids": created,
        "external_actions": "blocked",
        "provider_calls": 0,
        "leases_created": 0,
        "candidate_outputs_created": 0,
        "canonical_writes": 0,
    }


def plan_adaptive_work(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    goal: str,
    source_ids: Iterable[str] = (),
    artifact_ids: Iterable[str] = (),
    prior_work_state: Mapping[str, object] | None = None,
) -> AdaptivePlan:
    """Return an explicit adaptive plan without writing tasks.

    The existing coordinator already chooses different worker templates by
    goal. This wrapper exposes that decision as an auditable data model and
    records why stages are selected or skipped.
    """

    clean_goal = " ".join(goal.split()).strip()
    if not clean_goal:
        raise ValueError("adaptive plan goal is required")
    explicit_sources = _dedupe(source_ids)
    artifacts = _dedupe(artifact_ids)
    _validate_dependencies(conn, matter_scope=matter_scope, source_ids=explicit_sources, artifact_ids=artifacts)
    sources = explicit_sources or _default_source_dependencies_for_goal(conn, matter_scope=matter_scope, goal=clean_goal)
    templates, profile_skips = _templates_allowed_by_active_profile(
        conn,
        matter_scope=matter_scope,
        templates=_templates_for_goal(clean_goal),
    )
    goal_slug = _safe_component(clean_goal.lower())[:72] or "legal-work"
    dependency_slug = _dependency_slug(sources=sources, artifacts=artifacts)
    task_id_by_key = {template.key: _safe_component(f"{matter_scope}-adapt-{goal_slug}{dependency_slug}-{template.key}") for template in templates}
    task_payloads = [
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
    selected = tuple(sorted({str(task["stage"]) for task in task_payloads}))
    active_enabled_stages = _active_profile_enabled_stages(conn, matter_scope=matter_scope)
    skipped = tuple(
        {
            "stage": stage.value,
            "reason": (
                "disabled by active matter profile"
                if active_enabled_stages is not None and stage.value not in active_enabled_stages
                else _skip_reason(stage.value, selected, clean_goal, prior_work_state or {})
            ),
        }
        for stage in LegalStage
        if stage.value not in selected
    )
    decisions = tuple(
        cast(ModelDecision, cast(Mapping[str, object], task["provider_policy"]).get("model_decision"))
        for task in task_payloads
        if isinstance(cast(Mapping[str, object], task["provider_policy"]).get("model_decision"), ModelDecision)
    )
    # smart_provider_policy_for_route serializes decisions to dictionaries; rebuild
    # the dataclass shape for this plan surface.
    rebuilt_decisions: list[ModelDecision] = []
    for task in task_payloads:
        raw = cast(Mapping[str, object], task["provider_policy"]).get("model_decision")
        if isinstance(raw, Mapping):
            rebuilt_decisions.append(ModelDecision(**{key: raw[key] for key in ModelDecision.__dataclass_fields__ if key in raw}))
    task_specs = tuple(
        TaskSpec(
            task_id=str(task["task_id"]),
            title=str(task["title"]),
            task_type=str(task["task_type"]),
            instructions=str(task["instructions"]),
            matter_scope=matter_scope,
            stage=LegalStage(str(task["stage"])),
            status=TaskStatus.QUEUED,
            source_dependencies=list(sources),
            artifact_dependencies=list(artifacts),
            task_dependencies=_string_list(task, "task_dependencies"),
            validation_gates=_string_list(task, "validation_gates"),
            required_certifications=[
                {"subject_type": "matter", "subject_id": matter_scope, "certification_type": cert}
                for cert in _string_list(task, "required_certifications")
            ],
            provider_policy=dict(cast(Mapping[str, object], task["provider_policy"])),
        )
        for task in task_payloads
    )
    reasons = tuple([*_adaptive_reasons(clean_goal, templates, prior_work_state or {}), *profile_skips])
    return AdaptivePlan(
        matter_scope=matter_scope,
        goal=clean_goal,
        selected_stages=selected,
        skipped_stages=skipped,
        required_workers=tuple(template.role for template in templates),
        required_gates=tuple(sorted({gate for template in templates for gate in template.validation_gates})),
        model_decisions=tuple(rebuilt_decisions) or decisions,
        tasks=task_specs,
        reasons=reasons,
    )


def _templates_for_goal(goal: str) -> tuple[CoordinatorTaskTemplate, ...]:
    lowered = goal.lower()
    if _contains_any(lowered, ("draft", "complaint", "letter", "correspondence", "pleading", "submission", "witness statement")):
        return _drafting_templates()
    if _contains_any(lowered, ("organize evidence", "organise evidence", "rename", "renaming", "order evidence", "bundle order", "evidence bundle", "sort evidence", "sorted evidence")):
        return _evidence_organization_templates()
    if _source_inventory_goal(lowered):
        return _source_inventory_templates()
    if _contains_any(lowered, ("chronology", "timeline", "sequence", "events")):
        return _chronology_templates()
    if _contains_any(lowered, ("authority", "case law", "statute", "legal research", "law map", "research")):
        return _authority_templates()
    if _contains_any(lowered, ("sar", "disclosure", "gdpr", "productions", "bundle")):
        return _disclosure_templates()
    return _evidence_triage_templates()


def _source_inventory_goal(goal: str) -> bool:
    return _contains_any(
        goal,
        (
            "source inventory",
            "inventory",
            "extract sources",
            "extraction",
            "ocr",
            "source triage",
            "source qa",
            "deduplicate sources",
            "source dedup",
        ),
    )


def _default_source_dependencies_for_goal(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    goal: str,
) -> tuple[str, ...]:
    """Bind matter-local sources when an operator asks for matter-level work.

    The coordinator is usually invoked with a matter and a goal, not a hand
    listed source set. If sources already exist for the matter, an empty source
    dependency list creates empty-context legal work that can only say "no
    evidence supplied" and then dead-end downstream gates. Binding existing
    matter sources keeps the work order self-contained while preserving matter
    isolation and explicit citation allow-lists.
    """

    del goal  # reserved for future goal-specific source selection.
    return tuple(
        str(row["source_id"])
        for row in conn.execute(
            """
            SELECT source_id
            FROM sources
            WHERE matter_scope = ? AND stale = 0
            ORDER BY source_id
            """,
            (matter_scope,),
        )
    )


def _dependency_slug(*, sources: tuple[str, ...], artifacts: tuple[str, ...]) -> str:
    if not sources and not artifacts:
        return ""
    material = "\n".join(("source:" + source_id for source_id in sources)) + "\n" + "\n".join(
        "artifact:" + artifact_id for artifact_id in artifacts
    )
    return f"-ctx-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def _source_inventory_templates() -> tuple[CoordinatorTaskTemplate, ...]:
    return (
        CoordinatorTaskTemplate(
            key="source-inventory",
            role="source_inventory_worker",
            task_type="source_inventory",
            title="Inventory matter sources and extraction gaps",
            stage=LegalStage.S0_SOURCE_INVENTORY,
            deliverable="A matter-local source inventory with custody notes, extraction status, and missing-source gaps.",
            validation_gates=("source_inventory", "chain_of_custody", "cross_matter_isolation"),
            risk_focus=("missing source", "generated artifact treated as evidence", "cross-matter contamination"),
        ),
        CoordinatorTaskTemplate(
            key="extraction-qa",
            role="extraction_qa_worker",
            task_type="extraction_qa",
            title="Check extraction and OCR coverage",
            stage=LegalStage.S1_EXTRACTION,
            deliverable="An extraction/OCR coverage check tied back to source IDs and extraction provenance.",
            validation_gates=("extraction_coverage", "citation_integrity", "stale_dependency"),
            depends_on=("source-inventory",),
            risk_focus=("bad OCR", "missing text", "unsupported extracted quote"),
        ),
    )


def _evidence_organization_templates() -> tuple[CoordinatorTaskTemplate, ...]:
    return (
        CoordinatorTaskTemplate(
            key="evidence-organization",
            role="production_organizer",
            task_type="evidence_organization_plan",
            title="Organize evidence bundle naming and order",
            stage=LegalStage.S3_PRODUCTION_STATUS,
            deliverable=(
                "A candidate organization map that groups sources by evidence type, provenance, chronology, "
                "and issue; proposes stable display names and bundle order without renaming source files."
            ),
            validation_gates=("production_mapping", "chain_of_custody", "cross_matter_isolation", "stale_dependency"),
            required_certifications=("source_inventory", "extraction_coverage"),
            risk_focus=("custody break", "mislabelled source", "generated artifact treated as evidence", "privacy leak"),
        ),
    )


def _drafting_templates() -> tuple[CoordinatorTaskTemplate, ...]:
    return (
        CoordinatorTaskTemplate(
            key="evidence-map",
            role="evidence_worker",
            task_type="evidence_issue_map",
            title="Map evidence and issues before drafting",
            stage=LegalStage.S2_EVIDENCE_REGISTRY,
            deliverable=(
                "A cited issue and evidence map identifying what can and cannot safely be alleged. "
                "Return the primary proposed artifact as artifact_type='evidence_registry'."
            ),
            validation_gates=("claim_evidence_support", "stale_dependency", "cross_matter_isolation"),
            risk_focus=("unsupported allegation", "missing source", "stale evidence"),
        ),
        CoordinatorTaskTemplate(
            key="production-map",
            role="production_organizer",
            task_type="production_mapping",
            title="Map source production order and evidence bundle identity",
            stage=LegalStage.S3_PRODUCTION_STATUS,
            deliverable=(
                "A cited production/source crosswalk tying each source ID to stable bundle identity, display name, "
                "evidence type, and order. Return the primary proposed artifact as artifact_type='production_crosswalk'."
            ),
            validation_gates=("production_mapping", "chain_of_custody", "cross_matter_isolation", "stale_dependency"),
            depends_on=("evidence-map",),
            risk_focus=("generated artifact treated as evidence", "custody break", "mislabelled source"),
        ),
        CoordinatorTaskTemplate(
            key="chronology",
            role="chronology_worker",
            task_type="chronology_event_extraction",
            title="Extract cited chronology before drafting",
            stage=LegalStage.S4_BASELINE_CHRONOLOGY,
            deliverable=(
                "A date-ordered candidate chronology with source citations for every event, uncertainty for unclear "
                "dates, and contradictions separated from facts."
            ),
            validation_gates=("chronology_citations", "date_precision", "stale_dependency"),
            depends_on=("production-map",),
            risk_focus=("unclear date", "missing locator", "source conflict"),
        ),
        CoordinatorTaskTemplate(
            key="issue-route-map",
            role="issue_mapper",
            task_type="issue_route_map",
            title="Map issues, routes, remedies, and proof gaps",
            stage=LegalStage.S5_ISSUE_ROUTE_MAP,
            deliverable=(
                "A cited issue-route map separating university complaint, debt/accommodation, welfare/support, "
                "disability/discrimination, and urgent protection routes."
            ),
            validation_gates=("claim_evidence_support", "chronology_citations", "cross_matter_isolation", "stale_dependency"),
            depends_on=("chronology",),
            risk_focus=("wrong forum", "unsupported remedy", "missing proof route"),
        ),
        CoordinatorTaskTemplate(
            key="authority-map",
            role="research_worker",
            task_type="authority_map",
            title="Map authorities and legal issues before drafting",
            stage=LegalStage.S6_AUTHORITY_LAW_MAP,
            deliverable="A jurisdiction-aware authority map with each proposition marked verified, uncertain, or needs research.",
            validation_gates=("authority_jurisdiction", "authority_citation_format", "stale_dependency"),
            depends_on=("issue-route-map",),
            risk_focus=("wrong jurisdiction", "uncited legal proposition", "outdated law"),
        ),
        CoordinatorTaskTemplate(
            key="draft",
            role="drafting_worker",
            task_type="draft_preparation",
            title="Prepare candidate legal draft",
            stage=LegalStage.S8_DRAFT_PREPARATION,
            deliverable="A candidate draft that separates evidence-backed text from uncertain or strategic options.",
            validation_gates=("claim_evidence_support", "legal_citation_integrity", "privacy_redaction", "stale_dependency"),
            depends_on=("evidence-map", "chronology", "authority-map"),
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
    stage = str(template.stage)
    provider_policy = smart_provider_policy_for_route(
        default_smart_model_policy(),
        layer=_model_layer_for_template(template),
        stage=stage,
        task_type=template.task_type,
        task_id=task_id,
        matter_scope=matter_scope,
        authority_required=template.stage == LegalStage.S6_AUTHORITY_LAW_MAP or "authority_support" in template.validation_gates,
        hostile_review_required=template.verifier_required or "hostile_review" in template.validation_gates,
        source_count=len(source_ids),
        requested_capabilities=tuple(template.validation_gates),
    )
    return {
        "task_id": task_id,
        "matter_scope": matter_scope,
        "status": str(TaskStatus.QUEUED),
        "stage": stage,
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
        "provider_policy": provider_policy,
    }


def _model_layer_for_template(template: CoordinatorTaskTemplate) -> str:
    if template.role == "reducer":
        return "reducer"
    if template.role == "hostile_reviewer" or template.task_type in {"hostile_opponent_review", "hostile_review"}:
        return "hostile_review"
    if template.verifier_required:
        return "verifier"
    return "worker"


def _templates_allowed_by_active_profile(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    templates: tuple[CoordinatorTaskTemplate, ...],
) -> tuple[tuple[CoordinatorTaskTemplate, ...], tuple[str, ...]]:
    enabled_stages = _active_profile_enabled_stages(conn, matter_scope=matter_scope)
    if enabled_stages is None:
        return templates, ()
    kept: list[CoordinatorTaskTemplate] = []
    skipped: list[str] = []
    for template in templates:
        if template.stage.value in enabled_stages:
            kept.append(template)
        else:
            skipped.append(f"active matter profile disabled {template.stage.value}; skipped {template.task_type}")
    kept_keys = {template.key for template in kept}
    filtered = tuple(template for template in kept if all(dep in kept_keys for dep in template.depends_on))
    dropped_for_dependency = tuple(template for template in kept if any(dep not in kept_keys for dep in template.depends_on))
    skipped.extend(
        f"active matter profile removed dependency for {template.task_type}; skipped until prerequisite stage is enabled"
        for template in dropped_for_dependency
    )
    return filtered, tuple(skipped)


def _active_profile_enabled_stages(conn: sqlite3.Connection, *, matter_scope: str) -> set[str] | None:
    active = repo.get_active_matter_profile(conn, matter_scope=matter_scope)
    if active is None:
        return None
    stage_rows = active.get("stages")
    if not isinstance(stage_rows, list | tuple):
        return None
    return {
        str(stage["stage"])
        for stage in cast(list[Mapping[str, object]], stage_rows)
        if bool(stage.get("enabled"))
    }


def _skip_reason(stage: str, selected: tuple[str, ...], goal: str, prior_work_state: Mapping[str, object]) -> str:
    if stage in selected:
        return ""
    if stage in {"S6", "S7", "S8", "S9"} and not any(term in goal.lower() for term in ("authority", "draft", "complaint", "filing", "hostile", "review")):
        return "high-risk legal/review stage not needed for this goal"
    if bool(prior_work_state.get("reset_to_default")):
        return "profile reset requested; use default coordinator path before adding adaptive stages"
    return "not selected by matter-local adaptive planner"


def _adaptive_reasons(
    goal: str,
    templates: tuple[CoordinatorTaskTemplate, ...],
    prior_work_state: Mapping[str, object],
) -> list[str]:
    reasons = [f"selected {len(templates)} worker templates for goal"]
    if any(template.task_type == "authority_map" for template in templates):
        reasons.append("authority question requires S6 authority mapping and verifier review")
    if any(template.task_type == "hostile_opponent_review" for template in templates):
        reasons.append("drafting or high-risk review goal requires hostile review")
    if int(prior_work_state.get("contradiction_count") or 0) > 0:
        reasons.append("contradiction-heavy matter requires contradiction-aware Pro review")
    if "draft" not in goal.lower() and all(template.stage not in {LegalStage.S8_DRAFT_PREPARATION, LegalStage.S9_FINAL_QUALITY_GATE} for template in templates):
        reasons.append("no final draft intent detected, so S8/S9 are not spun up")
    reasons.append("external actions remain blocked and canonical writes remain reducer-only")
    return reasons


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
        "When using extracted or OCR source_materials, cite the source_id as source evidence rather than the generated extraction artifact unless citation_targets explicitly allows that artifact. "
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
