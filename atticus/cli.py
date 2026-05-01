"""Command line interface for Atticus Harness."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from atticus.commands.registry import command_by_name, list_commands
from atticus.agents.coordinator import plan_coordinator_work
from atticus.agents.orchestrator import (
    orchestrator_plan_repair,
    orchestrator_tick,
    record_operator_signal,
    report_worker_failure_to_orchestrator,
)
from atticus.agents.repair_planner import (
    ensure_repair_plans_for_matter,
    get_repair_plan,
    list_repair_plans,
    next_repair_plan,
)
from atticus.agents.maintenance import maintenance_report, maintenance_status, maintenance_tick, request_maintenance
from atticus.agents.repair_executor import execute_repair_plan, repair_tick_payload
from atticus.config import DEFAULT_DB_PATH
from atticus.context.diagnostics import build_context_diagnostics
from atticus.core.events import utc_now
from atticus.core.matters import authorized_matter_from_env, require_matter_access
from atticus.core.policies import TaskStatus
from atticus.core.matter_profiles import (
    apply_matter_profile_adaptation,
    propose_matter_profile_adaptation,
    reset_matter_profile_to_default,
)
from atticus.db import repo
from atticus.db.doctor import schema_check_json, verify_schema
from atticus.extraction.local import repair_source_extractions
from atticus.graph.certifications import CertificationBlocked, certify_subject
from atticus.matter_seed import seed_matter_from_inventory, set_provider_policy_for_matter
from atticus.memory.consolidation import consolidate_case_memory
from atticus.memory.extraction import extract_memory_candidates
from atticus.migration.import_old_run import import_candidates
from atticus.migration.reconcile import reconcile_foundation
from atticus.migration.report import build_migration_report
from atticus.providers.budget import budget_status, check_budget
from atticus.providers.live_readiness import LIVE_ENABLE_ENV, probe_live_openrouter
from atticus.providers.model_policy import (
    ModelRoutingPolicy,
    default_smart_model_policy,
    load_model_routing_policy,
    provider_policy_for_route,
    smart_provider_policy_for_route,
)
from atticus.providers.policy import (
    ProviderActual,
    ProviderRequest,
    check_provider_policy,
    record_provider_policy_decision,
)
from atticus.reducer.review_queue import (
    accept_reducer_review,
    enqueue_open_reducer_reviews_for_matter,
    get_reducer_review,
    list_reducer_reviews,
    next_reducer_review,
    reject_reducer_review,
    review_item_summary,
)
from atticus.reducer.reducer import reduce_candidate
from atticus.retrieval.ask import answer_question
from atticus.retrieval.index import DEFAULT_INDEX_NAME, rebuild_search_index
from atticus.scheduler.gates import blocked_task_auto_requeue_allowed, evaluate_task_gates
from atticus.scheduler.capacity import MAX_PARALLEL_AGENT_CAPACITY, agent_capacity
from atticus.scheduler.free_loop import run_free_loop
from atticus.scheduler.lease import LeaseError, acquire_lease
from atticus.scheduler.live_orchestrator import prepare_live_resume
from atticus.scheduler.planner import budget_blockers, select_runnable_tasks
from atticus.skills.registry import list_skills, load_skill
from atticus.scheduler.supervisor_invariants import evaluate_no_silent_idle
from atticus.status.completion import (
    assert_completion_has_next_action,
    build_matter_completion_report,
    explain_why_not_done,
    next_resume_action,
    record_completion_snapshot,
)
from atticus.status.human_attention_cleanup import plan_human_attention_cleanup
from atticus.status.inspect import inspect_record
from atticus.status.report import generate_status
from atticus.status.runbook import export_runbook
from atticus.tools.registry import list_tools
from atticus.validation.gates import run_validation
from atticus.verifier import verify_candidate
from atticus.validation.citation_support import trace_quote_in_source_material, validate_candidate_citation_support
from atticus.workflows.final_gate import create_missing_final_gate_work, final_gate_readiness, plan_final_gate_repairs, record_final_gate_state
from atticus.workflows.registry import list_workflows, load_workflow, plan_workflow
from atticus.workflows.source_led_packet import create_source_led_candidate_for_task
from atticus.workers.outputs import reject_candidate_output
from atticus.workers.runtime import execute_local_work_order
from atticus.workers.work_order import build_work_order
from atticus.work_runs import resume_work_run, summarize_reusable_work


JsonObject = dict[str, object]


class CliArgs(Protocol):
    command: str
    db: str
    type: str
    id: str
    question: str
    matter: str
    index_name: str
    workspace: str
    dry_run: bool
    gate: str
    target_type: str
    target_id: str
    subject_type: str
    subject_id: str
    certification_type: str
    validator: str
    capacity: int
    task_id: str
    candidate_id: str
    worker_id: str
    seconds: int
    lease_id: str
    output_dir: str
    write: bool
    scope_type: str
    scope_id: str
    limit: float | None
    check: float
    provider: str
    model: str
    policy_file: str | None
    smart_defaults: bool
    action: str
    skill_id: str
    layer: str
    stage: str
    task_type: str
    inventory: str
    estimated_cost_usd: float
    actual_provider: str | None
    actual_model: str | None
    allow_fallback: bool
    probe: bool
    probe_result_json: str | None
    write_leases: bool
    worker_prefix: str
    max_ticks: int
    runtime: str
    allow_live: bool
    codex_timeout_seconds: float
    codex_reasoning_effort: str
    extraction_timeout_seconds: float
    add: bool
    severity: str
    reason: str
    json_output: bool
    token_budget: int
    explain: bool
    memory_id: str | None
    session_id: str | None
    status: str | None
    name: str
    goal: str
    expected_value: float
    source_id: list[str] | None
    artifact_id: list[str] | None
    risk_level: str
    legal_complexity: str
    evidence_volume: str
    authority_required: bool
    hostile_review_required: bool
    drafting_finality: str
    contradiction_count: int
    unresolved_uncertainty_count: int
    source_count: int
    extracted_char_count: int
    capability: list[str] | None
    operator_override: str | None
    event_type: str | None
    failure_event_id: str | None
    payload_json: str | None
    work_run_id: str | None
    step_type: str | None
    input_fingerprint: str
    output_fingerprint: str
    reused_from_step_id: str | None
    reused_by_step_id: str | None
    profile_file: str | None
    resume_token: str | None
    why_not_done: bool
    repair_plan_id: str | None
    write_snapshot: bool
    review_id: str | None
    reviewer: str
    format: str
    out: str
    state: bool
    suite: str
    all_open: bool
    authority_id: str | None
    proposition: str
    citation_id: str | None
    quote: str
    explain_breaks: bool
    group_by_policy: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atticus")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize an Atticus SQLite database")
    _ = init.add_argument("--db", default=str(DEFAULT_DB_PATH))

    commands = sub.add_parser("commands", help="list command metadata")
    _ = commands.add_argument("action", choices=["list"])
    _ = commands.add_argument("--json", dest="json_output", action="store_true")

    command = sub.add_parser("command", help="show command metadata")
    _ = command.add_argument("action", choices=["show"])
    _ = command.add_argument("name")
    _ = command.add_argument("--json", dest="json_output", action="store_true")

    status = sub.add_parser("status", help="read-only run status")
    _ = status.add_argument("--db", required=True)
    _ = status.add_argument("--matter")

    matter_health = sub.add_parser("matter-health", help="authoritative matter completion, blocker owners, and next action")
    _ = matter_health.add_argument("--db", required=True)
    _ = matter_health.add_argument("--matter", required=True)
    _ = matter_health.add_argument("--json", dest="json_output", action="store_true")
    _ = matter_health.add_argument("--why-not-done", action="store_true")
    _ = matter_health.add_argument("--write-snapshot", action="store_true")

    next_action = sub.add_parser("next-action", help="show the exact next safe action for an incomplete matter")
    _ = next_action.add_argument("--db", required=True)
    _ = next_action.add_argument("--matter", required=True)
    _ = next_action.add_argument("--json", dest="json_output", action="store_true")

    repair_tick = sub.add_parser("repair-tick", help="execute one bounded deterministic repair continuation tick")
    _ = repair_tick.add_argument("--db", required=True)
    _ = repair_tick.add_argument("--matter", required=True)
    _ = repair_tick.add_argument("--max-repairs", type=int, default=10)
    _ = repair_tick.add_argument("--write", action="store_true", help="apply repairs; dry-run is the default")
    _ = repair_tick.add_argument("--json", dest="json_output", action="store_true")

    repairs = sub.add_parser("repairs", help="list, show, and advance deterministic repair plans")
    _ = repairs.add_argument("action", choices=["list", "show", "next", "apply"])
    _ = repairs.add_argument("--db", required=True)
    _ = repairs.add_argument("--matter", required=True)
    _ = repairs.add_argument("--repair-plan-id")
    _ = repairs.add_argument("--status")
    _ = repairs.add_argument("--dry-run", dest="dry_run", action="store_true")
    _ = repairs.add_argument("--write", action="store_true")
    _ = repairs.add_argument("--json", dest="json_output", action="store_true")

    reducer_review = sub.add_parser("reducer-review", help="list, inspect, accept, or reject manual reducer reviews")
    _ = reducer_review.add_argument("action", choices=["list", "show", "accept", "reject", "next"])
    _ = reducer_review.add_argument("--db", required=True)
    _ = reducer_review.add_argument("--matter", default="")
    _ = reducer_review.add_argument("--candidate-id")
    _ = reducer_review.add_argument("--review-id")
    _ = reducer_review.add_argument("--lease-id", default="")
    _ = reducer_review.add_argument("--reason", default="")
    _ = reducer_review.add_argument("--reviewer", default="operator")
    _ = reducer_review.add_argument("--write", action="store_true")
    _ = reducer_review.add_argument("--json", dest="json_output", action="store_true")

    final_gate = sub.add_parser("final-gate", help="inspect and repair deterministic final gate readiness")
    _ = final_gate.add_argument("action", choices=["readiness", "repair-plan", "create-missing"])
    _ = final_gate.add_argument("--db", required=True)
    _ = final_gate.add_argument("--matter", required=True)
    _ = final_gate.add_argument("--state", action="store_true")
    _ = final_gate.add_argument("--dry-run", dest="dry_run", action="store_true")
    _ = final_gate.add_argument("--write", action="store_true")
    _ = final_gate.add_argument("--json", dest="json_output", action="store_true")

    runbook = sub.add_parser(
        "runbook",
        help="export operator runbook with exact next action, blockers, provider taxonomy, reducer queue, and stale warnings",
        description="Export a matter operator runbook for handoff and no-silent-idle triage.",
    )
    _ = runbook.add_argument("action", choices=["export"])
    _ = runbook.add_argument("--db", required=True, help="SQLite matter ledger to inspect")
    _ = runbook.add_argument("--matter", required=True, help="matter scope to export")
    _ = runbook.add_argument("--out", required=True, help="destination path")
    _ = runbook.add_argument("--format", choices=["md", "json"], default="md")
    _ = runbook.add_argument("--json", dest="json_output", action="store_true", help="also print JSON export metadata")

    supervisor = sub.add_parser("supervisor", help="diagnose no-silent-idle and write deterministic next-action evidence")
    _ = supervisor.add_argument("action", choices=["diagnose-idle"])
    _ = supervisor.add_argument("--db", required=True)
    _ = supervisor.add_argument("--matter", required=True)
    _ = supervisor.add_argument("--write", action="store_true")
    _ = supervisor.add_argument("--json", dest="json_output", action="store_true")

    source_trace = sub.add_parser("source-trace", help="trace a quote or citation to current source chunks/spans")
    _ = source_trace.add_argument("--db", required=True)
    _ = source_trace.add_argument("--matter", required=True)
    _ = source_trace.add_argument("--source-id")
    _ = source_trace.add_argument("--citation-id")
    _ = source_trace.add_argument("--quote", default="")
    _ = source_trace.add_argument("--json", dest="json_output", action="store_true")

    citation_support = sub.add_parser("citation-support", help="verify candidate citation quote/span/proposition support")
    _ = citation_support.add_argument("action", choices=["verify"])
    _ = citation_support.add_argument("--db", required=True)
    _ = citation_support.add_argument("--candidate-id")
    _ = citation_support.add_argument("--matter", default="")
    _ = citation_support.add_argument("--stage", default="")
    _ = citation_support.add_argument("--write", action="store_true")
    _ = citation_support.add_argument("--json", dest="json_output", action="store_true")

    authority = sub.add_parser("authority", help="verify current authority status and proposition support metadata")
    _ = authority.add_argument("action", choices=["verify"])
    _ = authority.add_argument("--db", required=True)
    _ = authority.add_argument("--matter", required=True)
    _ = authority.add_argument("--authority-id")
    _ = authority.add_argument("--proposition", default="")
    _ = authority.add_argument("--all-open", action="store_true")
    _ = authority.add_argument("--write", action="store_true")
    _ = authority.add_argument("--json", dest="json_output", action="store_true")

    provider_health = sub.add_parser("provider-health", help="group provider control-plane health by provider policy")
    _ = provider_health.add_argument("--db", required=True)
    _ = provider_health.add_argument("--matter", required=True)
    _ = provider_health.add_argument("--group-by-policy", action="store_true")
    _ = provider_health.add_argument("--write", action="store_true")
    _ = provider_health.add_argument("--json", dest="json_output", action="store_true")

    cache_health = sub.add_parser("cache-health", help="report prompt/cache observability and likely cache breaks")
    _ = cache_health.add_argument("--db", required=True)
    _ = cache_health.add_argument("--matter", required=True)
    _ = cache_health.add_argument("--explain-breaks", action="store_true")
    _ = cache_health.add_argument("--json", dest="json_output", action="store_true")

    bad_fixtures = sub.add_parser("bad-fixtures", help="run historical bad fixture regression suite")
    _ = bad_fixtures.add_argument("action", choices=["run"])
    _ = bad_fixtures.add_argument("--suite", default="all")
    _ = bad_fixtures.add_argument("--json", dest="json_output", action="store_true")

    inspect = sub.add_parser("inspect", help="read-only record inspection")
    _ = inspect.add_argument("--db", required=True)
    _ = inspect.add_argument("--type", required=True, choices=["run", "task", "source", "artifact", "candidate", "context-pack", "certification"])
    _ = inspect.add_argument("--id", required=True)

    ask = sub.add_parser("ask", help="read-only legal memory query")
    _ = ask.add_argument("question")
    _ = ask.add_argument("--db", required=True)
    _ = ask.add_argument("--matter", default="atticus")

    rebuild_index = sub.add_parser("rebuild-search-index", help="rebuild durable legal-memory search projection")
    _ = rebuild_index.add_argument("--db", required=True)
    _ = rebuild_index.add_argument("--matter", default="atticus")
    _ = rebuild_index.add_argument("--index-name", default=DEFAULT_INDEX_NAME)
    _ = rebuild_index.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = rebuild_index.add_argument("--write", dest="dry_run", action="store_false", help="write rebuilt projection rows and audit record")

    imp = sub.add_parser("import-candidates", help="import legacy material as candidate artifacts")
    _ = imp.add_argument("--workspace", required=True)
    _ = imp.add_argument("--db", required=True)
    _ = imp.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = imp.add_argument("--write", dest="dry_run", action="store_false", help="actually write candidate artifacts and validation tasks")

    source_led = sub.add_parser("source-led-packet", help="generate a deterministic quote-supported candidate from current source chunks")
    _ = source_led.add_argument("action", choices=["create"])
    _ = source_led.add_argument("--db", required=True)
    _ = source_led.add_argument("--matter", required=True)
    _ = source_led.add_argument("--task-id", required=True)
    _ = source_led.add_argument("--source-id", action="append", default=[])
    _ = source_led.add_argument("--max-sources", type=int, default=12)
    _ = source_led.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = source_led.add_argument("--write", dest="dry_run", action="store_false", help="write source chunks if needed and record the candidate packet")
    _ = source_led.add_argument("--json", dest="json_output", action="store_true")

    seed = sub.add_parser("seed-matter", help="seed or repair a matter from a local workspace inventory")
    _ = seed.add_argument("--db", required=True)
    _ = seed.add_argument("--matter", required=True)
    _ = seed.add_argument("--workspace", required=True)
    _ = seed.add_argument("--inventory", required=True)
    _ = seed.add_argument("--provider", required=True)
    _ = seed.add_argument("--model", required=True)
    _ = seed.add_argument("--estimated-cost-usd", dest="estimated_cost_usd", type=float, default=0.0)
    _add_fallback_mode_args(seed)
    _ = seed.add_argument("--write", action="store_true", help="write matter, source, snapshot, tracked-file, and foundation task rows")

    extract_sources = sub.add_parser("extract-sources", help="extract or OCR matter-local sources without provider calls")
    _ = extract_sources.add_argument("--db", required=True)
    _ = extract_sources.add_argument("--matter", required=True)
    _ = extract_sources.add_argument("--workspace", required=True)
    _ = extract_sources.add_argument("--source-id", action="append", default=[])
    _ = extract_sources.add_argument("--timeout-seconds", dest="extraction_timeout_seconds", type=float, default=90.0)
    _ = extract_sources.add_argument("--write", action="store_true", help="write extracted text artifacts and extraction/OCR records")

    validate = sub.add_parser("validate", help="run a durable validation gate")
    _ = validate.add_argument("--db", required=True)
    _ = validate.add_argument("--gate", required=True)
    _ = validate.add_argument("--target-type", required=True)
    _ = validate.add_argument("--target-id", required=True)

    certify = sub.add_parser("certify", help="issue a certification after a passing validation")
    _ = certify.add_argument("--db", required=True)
    _ = certify.add_argument("--subject-type", required=True)
    _ = certify.add_argument("--subject-id", required=True)
    _ = certify.add_argument("--type", "--certification-type", dest="certification_type", required=True)
    _ = certify.add_argument("--validator", default="atticus-cli")

    schedule = sub.add_parser("schedule", help="dependency-aware scheduling preview or write")
    _ = schedule.add_argument("--db", required=True)
    _ = schedule.add_argument("--matter", help="limit scheduler inspection/application to one matter")
    _ = schedule.add_argument("--all-matters", action="store_true", help="explicitly allow global multi-matter scheduling")
    _ = schedule.add_argument("--capacity", type=int, default=MAX_PARALLEL_AGENT_CAPACITY)
    _ = schedule.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = schedule.add_argument("--write", dest="dry_run", action="store_false", help="persist blocked reasons")

    lease = sub.add_parser("lease", help="acquire a fenced task lease without launching a worker")
    _ = lease.add_argument("--db", required=True)
    _ = lease.add_argument("--task-id", required=True)
    _ = lease.add_argument("--worker-id", default="atticus-cli")
    _ = lease.add_argument("--seconds", type=int, default=900)
    _ = lease.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = lease.add_argument("--write", dest="dry_run", action="store_false", help="write the lease")

    work_order = sub.add_parser("work-order", help="build a bounded worker work order; never launches workers")
    _ = work_order.add_argument("--db", required=True)
    _ = work_order.add_argument("--task-id", required=True)
    _ = work_order.add_argument("--lease-id")
    _ = work_order.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = work_order.add_argument("--write-context", dest="dry_run", action="store_false", help="persist the context pack")

    context = sub.add_parser("context", help="read-only context pack diagnostics for legal audit")
    _ = context.add_argument("--db", required=True)
    _ = context.add_argument("--task-id", required=True)
    _ = context.add_argument("--token-budget", type=int, default=32_000)
    _ = context.add_argument("--json", dest="json_output", action="store_true")
    _ = context.add_argument("--explain", action="store_true")

    run_local = sub.add_parser("run-local", help="execute a leased task through the local stub adapter only")
    _ = run_local.add_argument("--db", required=True)
    _ = run_local.add_argument("--task-id", required=True)
    _ = run_local.add_argument("--lease-id", required=True)
    _ = run_local.add_argument("--worker-id", default="atticus-local")
    _ = run_local.add_argument("--output-dir", required=True)
    _ = run_local.add_argument("--write", action="store_true", help="actually record the local candidate output")

    reduce = sub.add_parser("reduce", help="reduce a candidate packet through reducer-only canonical path")
    _ = reduce.add_argument("--db", required=True)
    _ = reduce.add_argument("--candidate-id", required=True)
    _ = reduce.add_argument("--lease-id", required=True)
    _ = reduce.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = reduce.add_argument("--write", dest="dry_run", action="store_false", help="write reducer decision/canonical artifact")

    reject_candidate = sub.add_parser("reject-candidate", help="quarantine a valid but unsuitable candidate packet")
    _ = reject_candidate.add_argument("--db", required=True)
    _ = reject_candidate.add_argument("--candidate-id", required=True)
    _ = reject_candidate.add_argument("--reason", required=True)
    _ = reject_candidate.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = reject_candidate.add_argument("--write", dest="dry_run", action="store_false")

    budget = sub.add_parser("budget", help="view, set, or check budget gates")
    _ = budget.add_argument("--db", required=True)
    _ = budget.add_argument("--scope-type", default="matter")
    _ = budget.add_argument("--scope-id", default="atticus")
    _ = budget.add_argument("--limit", type=float)
    _ = budget.add_argument("--check", type=float, default=0.0)
    _ = budget.add_argument("--write", action="store_true")

    provider_policy = sub.add_parser("provider-policy", help="check provider/model fallback policy")
    _add_provider_policy_args(provider_policy)

    set_provider_policy = sub.add_parser("set-provider-policy", help="set provider/model policy on queued tasks for a matter")
    _ = set_provider_policy.add_argument("--db", required=True)
    _ = set_provider_policy.add_argument("--matter", required=True)
    _ = set_provider_policy.add_argument("--provider")
    _ = set_provider_policy.add_argument("--model")
    _ = set_provider_policy.add_argument("--policy-file")
    _ = set_provider_policy.add_argument("--smart-defaults", action="store_true", help="apply the built-in smart model policy to queued tasks")
    _ = set_provider_policy.add_argument("--estimated-cost-usd", dest="estimated_cost_usd", type=float, default=0.0)
    _add_fallback_mode_args(set_provider_policy)
    _ = set_provider_policy.add_argument("--write", action="store_true", help="write normalized provider policy to queued tasks")

    model_policy = sub.add_parser("model-policy", help="validate or resolve a model routing policy file")
    _ = model_policy.add_argument("action", choices=["validate", "resolve", "decide"])
    _ = model_policy.add_argument("--policy-file")
    _ = model_policy.add_argument("--db")
    _ = model_policy.add_argument("--layer", default="")
    _ = model_policy.add_argument("--stage", default="")
    _ = model_policy.add_argument("--task-type", default="")
    _ = model_policy.add_argument("--task-id", default="")
    _ = model_policy.add_argument("--matter", default="atticus")
    _ = model_policy.add_argument("--risk-level", default="unknown")
    _ = model_policy.add_argument("--legal-complexity", default="unknown")
    _ = model_policy.add_argument("--evidence-volume", default="unknown")
    _ = model_policy.add_argument("--authority-required", action="store_true")
    _ = model_policy.add_argument("--hostile-review-required", action="store_true")
    _ = model_policy.add_argument("--drafting-finality", default="")
    _ = model_policy.add_argument("--contradiction-count", type=int, default=0)
    _ = model_policy.add_argument("--unresolved-uncertainty-count", type=int, default=0)
    _ = model_policy.add_argument("--source-count", type=int, default=0)
    _ = model_policy.add_argument("--extracted-char-count", type=int, default=0)
    _ = model_policy.add_argument("--expected-value", type=float, default=0.0)
    _ = model_policy.add_argument("--capability", action="append", default=[])
    _ = model_policy.add_argument("--operator-override")
    _ = model_policy.add_argument("--json", dest="json_output", action="store_true")

    skill = sub.add_parser("skill", help="list or show bundled worker skills")
    _ = skill.add_argument("action", choices=["list", "show"])
    _ = skill.add_argument("--skill-id")

    tools = sub.add_parser("tools", help="list Atticus legal tools")
    _ = tools.add_argument("action", choices=["list"])
    _ = tools.add_argument("--db")
    _ = tools.add_argument("--matter", default="atticus")
    _ = tools.add_argument("--json", dest="json_output", action="store_true")

    verifier = sub.add_parser("verifier", help="run independent verifier checks against candidates")
    _ = verifier.add_argument("action", choices=["run"])
    _ = verifier.add_argument("--db", required=True)
    _ = verifier.add_argument("--candidate-id", required=True)
    _ = verifier.add_argument("--type", "--verifier-type", dest="type", required=True)
    _ = verifier.add_argument("--write", action="store_true")
    _ = verifier.add_argument("--json", dest="json_output", action="store_true")

    workflow = sub.add_parser("workflow", help="list, show, or run markdown legal workflows")
    _ = workflow.add_argument("action", choices=["list", "show", "run"])
    _ = workflow.add_argument("name", nargs="?")
    _ = workflow.add_argument("--db")
    _ = workflow.add_argument("--matter", default="atticus")
    _ = workflow.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = workflow.add_argument("--write", dest="dry_run", action="store_false")

    coordinator = sub.add_parser("coordinator", help="plan or create self-contained legal coordinator task graphs")
    _ = coordinator.add_argument("action", choices=["plan", "create-tasks"])
    _ = coordinator.add_argument("--db", required=True)
    _ = coordinator.add_argument("--matter", required=True)
    _ = coordinator.add_argument("--goal", required=True)
    _ = coordinator.add_argument("--source-id", action="append", default=[])
    _ = coordinator.add_argument("--artifact-id", action="append", default=[])
    _ = coordinator.add_argument("--write", action="store_true", help="write queued coordinator-created tasks")

    matter_profile = sub.add_parser("matter-profile", help="show, propose, apply, or reset matter-local adaptive stage profiles")
    _ = matter_profile.add_argument("action", choices=["show", "create", "propose", "apply", "reset"])
    _ = matter_profile.add_argument("--db", required=True)
    _ = matter_profile.add_argument("--matter", required=True)
    _ = matter_profile.add_argument("--name", default="Default profile")
    _ = matter_profile.add_argument("--reason", default="operator requested profile")
    _ = matter_profile.add_argument("--goal", default="")
    _ = matter_profile.add_argument("--profile-file")
    _ = matter_profile.add_argument("--json", dest="json_output", action="store_true")
    _ = matter_profile.add_argument("--write", action="store_true")

    orchestrator = sub.add_parser("orchestrator", help="status, tick, failures, or record matter orchestrator state")
    _ = orchestrator.add_argument("action", choices=["show", "upsert", "event", "status", "tick", "failures", "worker-failed", "repair", "signal"])
    _ = orchestrator.add_argument("--db", required=True)
    _ = orchestrator.add_argument("--matter", required=True)
    _ = orchestrator.add_argument("--status", default="idle")
    _ = orchestrator.add_argument("--goal", default="")
    _ = orchestrator.add_argument("--event-type")
    _ = orchestrator.add_argument("--failure-event-id")
    _ = orchestrator.add_argument("--payload-json")
    _ = orchestrator.add_argument("--task-id")
    _ = orchestrator.add_argument("--signal-type", default="attention")
    _ = orchestrator.add_argument("--message", default="")
    _ = orchestrator.add_argument("--priority", default="normal")
    _ = orchestrator.add_argument("--requested-by", default="operator")
    _ = orchestrator.add_argument("--reason", default="")
    _ = orchestrator.add_argument("--capacity", type=int, default=MAX_PARALLEL_AGENT_CAPACITY)
    _ = orchestrator.add_argument("--json", dest="json_output", action="store_true")
    _ = orchestrator.add_argument("--write", action="store_true")

    maintenance = sub.add_parser("maintenance", help="isolated maintenance orchestrator diagnostics, reports, and resume signals")
    _ = maintenance.add_argument("action", choices=["status", "trigger", "tick", "report"])
    _ = maintenance.add_argument("--db", required=True)
    _ = maintenance.add_argument("--matter", default="global")
    _ = maintenance.add_argument("--reason", default="maintenance requested")
    _ = maintenance.add_argument("--triggered-by", default="operator")
    _ = maintenance.add_argument("--maintenance-run-id")
    _ = maintenance.add_argument("--json", dest="json_output", action="store_true")
    _ = maintenance.add_argument("--write", action="store_true")

    work_run = sub.add_parser("work-run", help="record resumable matter work runs and reusable steps")
    _ = work_run.add_argument("action", choices=["start", "complete", "step", "reuse", "reusable", "status", "resume", "export"])
    _ = work_run.add_argument("--db", required=True)
    _ = work_run.add_argument("--matter", required=True)
    _ = work_run.add_argument("--goal", default="")
    _ = work_run.add_argument("--work-run-id")
    _ = work_run.add_argument("--resume-token")
    _ = work_run.add_argument("--step-type")
    _ = work_run.add_argument("--status", default="complete")
    _ = work_run.add_argument("--task-id")
    _ = work_run.add_argument("--input-fingerprint", default="")
    _ = work_run.add_argument("--output-fingerprint", default="")
    _ = work_run.add_argument("--reused-from-step-id")
    _ = work_run.add_argument("--reused-by-step-id")
    _ = work_run.add_argument("--json", dest="json_output", action="store_true")
    _ = work_run.add_argument("--write", action="store_true")

    memory = sub.add_parser("memory", help="list, show, extract, consolidate, or update typed legal memory")
    _ = memory.add_argument("action", choices=["list", "show", "mark-stale", "reject", "export-index", "extract-candidates", "consolidate"])
    _ = memory.add_argument("--db", required=True)
    _ = memory.add_argument("--matter", default="atticus")
    _ = memory.add_argument("--memory-id")
    _ = memory.add_argument("--candidate-id")
    _ = memory.add_argument("--reason", default="")
    _ = memory.add_argument("--write", action="store_true")

    session = sub.add_parser("session", help="list, show, resume, or export sensitive legal sessions")
    _ = session.add_argument("action", choices=["list", "show", "resume", "export"])
    _ = session.add_argument("session_id", nargs="?")
    _ = session.add_argument("--db", required=True)
    _ = session.add_argument("--matter", default="atticus")
    _ = session.add_argument("--status")

    provider_probe = sub.add_parser("provider-probe", help="make a tiny OpenRouter probe before live resume")
    _ = provider_probe.add_argument("--provider", default="openrouter")
    _ = provider_probe.add_argument("--model", required=True)
    _ = provider_probe.add_argument("--allow-fallback", action="store_true")

    live_resume = sub.add_parser("live-resume", help="prepare safe live OpenRouter leases without launching workers")
    _ = live_resume.add_argument("--db", required=True)
    _ = live_resume.add_argument("--matter", help="limit live resume planning/leasing to one matter")
    _ = live_resume.add_argument("--all-matters", action="store_true", help="explicitly allow global multi-matter live resume")
    _ = live_resume.add_argument("--capacity", type=int, default=15)
    _ = live_resume.add_argument("--model", default="deepseek/deepseek-v4-pro", help="OpenRouter model to probe for live resume")
    _ = live_resume.add_argument("--probe", action="store_true", help="run a live OpenRouter probe before planning")
    _ = live_resume.add_argument("--probe-result-json", help="preverified provider probe JSON from provider-probe")
    _ = live_resume.add_argument("--allow-live", action="store_true", help="permit live OpenRouter probe/planning and set the child env gate for this command only")
    _ = live_resume.add_argument("--write", action="store_true", help="write smart provider policy updates and leases")
    _ = live_resume.add_argument("--execute", action="store_true", help="alias for --write; does not launch workers")
    _ = live_resume.add_argument("--write-leases", action="store_true")
    _ = live_resume.add_argument("--worker-prefix", default="atticus-openrouter")
    _ = live_resume.add_argument("--execute-ticks", type=int, default=0, help="run N free-loop ticks after successful live-resume preparation")
    _ = live_resume.add_argument("--max-ticks", type=int, default=1, help="max ticks per execute batch (default: 1)")
    _ = live_resume.add_argument("--output-dir", help="output directory for free-loop workers (required when --execute-ticks > 0)")

    free_loop = sub.add_parser("run-free-loop", help="run bounded autonomous supervisor ticks; live provider calls require --allow-live and provider env gates")
    _ = free_loop.add_argument("--db", required=True)
    _ = free_loop.add_argument("--matter", help="limit supervisor ticks to one matter")
    _ = free_loop.add_argument("--all-matters", action="store_true", help="explicitly allow global multi-matter supervisor ticks")
    _ = free_loop.add_argument("--output-dir", required=True)
    _ = free_loop.add_argument("--capacity", type=int, default=15)
    _ = free_loop.add_argument("--max-ticks", type=int, default=1)
    _ = free_loop.add_argument("--runtime", choices=["openrouter", "local", "codex"], default="openrouter")
    _ = free_loop.add_argument("--allow-live", action="store_true", help="permit live OpenRouter/Codex calls after normal runtime gates")
    _ = free_loop.add_argument("--codex-timeout-seconds", type=float, default=180.0, help="bounded timeout for each live Codex CLI worker call")
    _ = free_loop.add_argument("--codex-reasoning-effort", choices=["low", "medium", "high", "xhigh"], default="low", help="Codex reasoning effort override; prevents inheriting global CLI defaults")

    reconcile = sub.add_parser("reconcile-foundation", help="validate/certify foundation before live resume")
    _ = reconcile.add_argument("--db", required=True)
    _ = reconcile.add_argument("--matter", default="atticus")
    _ = reconcile.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = reconcile.add_argument("--write", dest="dry_run", action="store_false")
    _ = reconcile.add_argument("--validator", default="atticus-cli")

    policy = sub.add_parser("policy-check", help="check provider/model fallback policy")
    _add_provider_policy_args(policy)

    attention = sub.add_parser("human-attention", help="list or add human attention items")
    _ = attention.add_argument("--db", required=True)
    _ = attention.add_argument("--matter")
    _ = attention.add_argument("--add", action="store_true")
    _ = attention.add_argument("--target-type", default="manual")
    _ = attention.add_argument("--target-id", default="manual")
    _ = attention.add_argument("--severity", default="info")
    _ = attention.add_argument("--reason", default="")
    _ = attention.add_argument("--current-only", action="store_true", help="show only current (non-superseded) attention items")
    _ = attention.add_argument("--classify", action="store_true", help="add triage classification per attention item")
    _ = attention.add_argument("--cleanup", action="store_true", help="plan conservative stale/noisy human-attention cleanup")
    _ = attention.add_argument("--provider-probe-passed", action="append", default=[], help="explicit successful provider probe, e.g. openrouter; repeatable")
    _ = attention.add_argument("--write", action="store_true", help="apply human-attention cleanup plan; default is dry-run")
    _ = attention.add_argument("--json", dest="json_output", action="store_true", help="accepted for command symmetry; output is always JSON")

    migrate = sub.add_parser("migrate-report", help="dry-run migration report for legacy workspace")
    _ = migrate.add_argument("--workspace", required=True)
    _ = migrate.add_argument("--db")
    _ = migrate.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = migrate.add_argument("--write", dest="dry_run", action="store_false", help="persist report metadata")

    doctor = sub.add_parser("doctor", help="safety and schema diagnostics")
    _ = doctor.add_argument("--db", required=True)
    _ = doctor.add_argument("--schema", action="store_true", help="run schema drift checks")
    _ = doctor.add_argument("--repair", action="store_true", help="apply additive schema repair")
    _ = doctor.add_argument("--write", action="store_true", help="allow doctor repair to write")
    _ = doctor.add_argument("--json", dest="json_output", action="store_true")

    return parser


def _add_provider_policy_args(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--provider", required=True)
    _ = parser.add_argument("--model", required=True)
    _ = parser.add_argument("--actual-provider")
    _ = parser.add_argument("--actual-model")
    _ = parser.add_argument("--allow-fallback", action="store_true")
    _ = parser.add_argument("--db")
    _ = parser.add_argument("--task-id")


def _add_fallback_mode_args(parser: argparse.ArgumentParser) -> None:
    fallback = parser.add_mutually_exclusive_group()
    _ = fallback.add_argument("--allow-fallback", dest="allow_fallback", action="store_true", default=False)
    _ = fallback.add_argument("--no-fallback", dest="allow_fallback", action="store_false")


def main(argv: list[str] | None = None) -> int:
    args = cast(CliArgs, cast(object, build_parser().parse_args(argv)))

    try:
        return _main(args)
    except (CertificationBlocked, LeaseError, KeyError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _main(args: CliArgs) -> int:
    if args.command == "init":
        repo.initialize_database(args.db)
        with repo.db_connection(args.db) as conn:
            repo.upsert_run(conn, "default", "initialized", "database initialized")
        print(f"initialized {Path(args.db)}")
        return 0

    if args.command == "commands":
        print_json({"commands": [command.as_dict() for command in list_commands() if not command.hidden]})
        return 0

    if args.command == "command":
        print_json(command_by_name(args.name).as_dict())
        return 0

    if args.command == "status":
        with repo.db_connection(args.db, read_only=True) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
        if not schema_check["ok"]:
            print_json(schema_check)
            return 2
        report = generate_status(args.db, matter_scope=args.matter)
        print_json(report.__dict__)
        return 0

    if args.command == "matter-health":
        with repo.db_connection(args.db, read_only=not args.write_snapshot) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            if args.why_not_done:
                payload = explain_why_not_done(conn, args.matter)
            else:
                payload = build_matter_completion_report(conn, args.matter).as_dict()
                payload["next_action"] = next_resume_action(conn, args.matter)
                payload["completion_invariant"] = assert_completion_has_next_action(conn, args.matter).as_dict()
            from atticus.agents.repair_executor import execute_repair_tick
            repair_preview = execute_repair_tick(conn, matter_scope=args.matter, max_repairs=10, write=False).as_dict()
            payload["repair_executor"] = {
                **repair_preview,
                "can_repair_tick_write_make_progress": bool(repair_preview.get("made_progress") or repair_preview.get("attempted")),
                "run_free_loop_can_continue_safely": str(payload.get("next_action", {}).get("owner") if isinstance(payload.get("next_action"), dict) else "") not in {"provider", "operator"},
            }
            if args.write_snapshot:
                payload["completion_snapshot"] = record_completion_snapshot(conn, args.matter).as_dict()
        print_json(_materialize_resume_commands(payload, args.db))
        return 0

    if args.command == "next-action":
        with repo.db_connection(args.db, read_only=True) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = next_resume_action(conn, args.matter)
            payload["completion_invariant"] = assert_completion_has_next_action(conn, args.matter).as_dict()
        print_json(_materialize_resume_commands(payload, args.db))
        return 0

    if args.command == "repair-tick":
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = repair_tick_payload(conn, matter_scope=args.matter, max_repairs=args.max_repairs, write=args.write)
        print_json(_materialize_resume_commands(payload, args.db))
        return 0

    if args.command == "repairs":
        read_only = args.action != "apply" and not args.write
        with repo.db_connection(args.db, read_only=read_only) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            if args.write and args.action in {"list", "next"}:
                _ = ensure_repair_plans_for_matter(conn, matter_scope=args.matter)
            if args.action == "list":
                status = None if args.status in {None, "", "open"} else args.status
                plans = [plan.as_dict() for plan in list_repair_plans(conn, matter_scope=args.matter, status=status)]
                if args.status == "open":
                    plans = [plan for plan in plans if str(plan.get("status")) in {"proposed", "blocked", "requires_human"}]
                payload = {"repair_plans": plans}
            elif args.action == "next":
                plan = next_repair_plan(conn, matter_scope=args.matter)
                payload = {"repair_plan": plan.as_dict() if plan is not None else None}
            elif args.action == "show":
                if not args.repair_plan_id:
                    raise ValueError("repairs show requires --repair-plan-id")
                plan = get_repair_plan(conn, args.repair_plan_id)
                payload = {"repair_plan": plan.as_dict() if plan is not None else None}
            else:
                if not args.repair_plan_id:
                    raise ValueError("repairs apply requires --repair-plan-id")
                if not args.write:
                    plan = get_repair_plan(conn, args.repair_plan_id)
                    payload = {"dry_run": True, "repair_plan": plan.as_dict() if plan is not None else None}
                    print_json(_materialize_resume_commands(payload, args.db))
                    return 0
                execution = execute_repair_plan(conn, repair_plan_id=args.repair_plan_id, max_actions=1, dry_run=False)
                plan = get_repair_plan(conn, args.repair_plan_id)
                payload = {"repair_plan": plan.as_dict()}
                if execution is not None:
                    payload["repair_execution"] = execution
        print_json(_materialize_resume_commands(payload, args.db))
        return 0

    if args.command == "reducer-review":
        read_only = not args.write
        with repo.db_connection(args.db, read_only=read_only) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            if args.action == "list":
                if not args.matter:
                    raise ValueError("reducer-review list requires --matter")
                if args.write:
                    _ = enqueue_open_reducer_reviews_for_matter(conn, matter_scope=args.matter)
                payload = {"reviews": [item.as_dict() for item in list_reducer_reviews(conn, matter_scope=args.matter)]}
            elif args.action == "next":
                if not args.matter:
                    raise ValueError("reducer-review next requires --matter")
                if args.write:
                    _ = enqueue_open_reducer_reviews_for_matter(conn, matter_scope=args.matter)
                item = next_reducer_review(conn, matter_scope=args.matter)
                payload = {"review": item.as_dict() if item is not None else None}
            elif args.action == "show":
                candidate_id = _candidate_id_for_review_args(conn, candidate_id=args.candidate_id, review_id=args.review_id)
                if not candidate_id:
                    raise ValueError("reducer-review show requires --candidate-id or --review-id")
                payload = {"review": review_item_summary(conn, candidate_id=candidate_id)}
            elif args.action == "accept":
                candidate_id = _candidate_id_for_review_args(conn, candidate_id=args.candidate_id, review_id=args.review_id)
                if not candidate_id:
                    raise ValueError("reducer-review accept requires --candidate-id or --review-id")
                lease_id = args.lease_id
                if args.write and not lease_id:
                    task_row = conn.execute("SELECT task_id FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone()
                    if task_row is None:
                        raise ValueError(f"unknown candidate: {candidate_id}")
                    lease_id = acquire_lease(conn, task_id=str(task_row["task_id"]), worker_id=args.reviewer, lease_role="reducer")
                payload = accept_reducer_review(conn, candidate_id=candidate_id, reducer_lease_id=lease_id or "dry-run-review", write=args.write)
                if args.write:
                    _ = conn.execute("UPDATE reducer_review_queue SET reviewer = ? WHERE candidate_id = ?", (args.reviewer, candidate_id))
            else:
                candidate_id = _candidate_id_for_review_args(conn, candidate_id=args.candidate_id, review_id=args.review_id)
                if not candidate_id:
                    raise ValueError("reducer-review reject requires --candidate-id or --review-id")
                payload = reject_reducer_review(conn, candidate_id=candidate_id, reason=args.reason, write=args.write)
                if args.write:
                    _ = conn.execute("UPDATE reducer_review_queue SET reviewer = ? WHERE candidate_id = ?", (args.reviewer, candidate_id))
        print_json(_materialize_resume_commands(payload, args.db))
        return 0

    if args.command == "final-gate":
        read_only = not args.write
        with repo.db_connection(args.db, read_only=read_only) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            if args.action == "readiness":
                payload = final_gate_readiness(conn, args.matter)
                if args.write:
                    payload = record_final_gate_state(conn, args.matter)
            elif args.action == "repair-plan":
                payload = {"repairs": plan_final_gate_repairs(conn, args.matter)}
            else:
                if not args.write:
                    payload = {"dry_run": True, "would_create": plan_final_gate_repairs(conn, args.matter), "readiness": final_gate_readiness(conn, args.matter)}
                else:
                    payload = create_missing_final_gate_work(conn, args.matter)
        print_json(_materialize_resume_commands(payload, args.db))
        return 0

    if args.command == "runbook":
        with repo.db_connection(args.db, read_only=True) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = export_runbook(conn, matter_scope=args.matter, out_path=args.out, db_path=args.db)
            if args.format == "json":
                Path(args.out).write_text(json.dumps(payload["runbook"], indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
                payload["format"] = "json"
            else:
                payload["format"] = "md"
        print_json(payload)
        return 0

    if args.command == "supervisor":
        tick_result: dict[str, object] = {
            "leased_tasks": [],
            "executed_tasks": [],
            "imported_tasks": [],
            "reduced_candidates": [],
            "applied_actions": [],
            "routed_operator_signals": [],
            "worker_errors": [],
            "preflight_groups": [],
            "blocked_repairs": [],
            "terminal_blocks": [],
        }
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = evaluate_no_silent_idle(conn, args.matter, tick_result, write=args.write)
        print_json(_materialize_resume_commands(payload, args.db))
        return 0

    if args.command == "source-trace":
        with repo.db_connection(args.db, read_only=True) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = _source_trace_payload(conn, args)
        print_json(payload)
        return 0

    if args.command == "citation-support":
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = _citation_support_payload(conn, args)
        print_json(payload)
        return 0

    if args.command == "authority":
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = _authority_verify_payload(conn, args)
        print_json(payload)
        return 0

    if args.command == "provider-health":
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = _provider_health_payload(conn, args)
        print_json(payload)
        return 0

    if args.command == "cache-health":
        with repo.db_connection(args.db, read_only=True) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
            if not schema_check["ok"]:
                print_json(schema_check)
                return 2
            payload = _cache_health_payload(conn, args)
        print_json(payload)
        return 0

    if args.command == "bad-fixtures":
        from atticus.testing.bad_fixtures import all_bad_fixtures

        fixtures = [_json_safe(fixture.__dict__) for fixture in all_bad_fixtures()]
        if args.suite != "all":
            fixtures = [fixture for fixture in fixtures if args.suite in {str(fixture.get("category")), str(fixture.get("fixture_id"))}]
        print_json({"suite": args.suite, "fixture_count": len(fixtures), "fixtures": fixtures})
        return 0

    if args.command == "inspect":
        with repo.db_connection(args.db, read_only=True) as conn:
            schema_check = schema_check_json(conn, db_path=args.db)
        if not schema_check["ok"]:
            print_json(schema_check)
            return 2
        print_json(inspect_record(args.db, record_type=args.type, record_id=args.id))
        return 0

    if args.command == "ask":
        answer = answer_question(
            args.db,
            args.question,
            matter_scope=args.matter,
            authorized_matter_scope=authorized_matter_from_env(),
        )
        print_json(
            {
                "answer": answer.answer,
                "trust_level": answer.trust_level,
                "confidence": answer.confidence,
                "citations": [c.as_dict() for c in answer.citations],
                "follow_up_task": answer.follow_up_task,
            }
        )
        return 0

    if args.command == "rebuild-search-index":
        authorized_matter_scope = authorized_matter_from_env()
        if args.dry_run:
            _ = require_matter_access(args.matter, authorized_matter_scope=authorized_matter_scope)
            print_json(
                {
                    "dry_run": True,
                    "index_name": args.index_name,
                    "matter_scope": args.matter,
                    "requires_write": True,
                }
            )
            return 0
        with repo.db_connection(args.db) as conn:
            result = rebuild_search_index(
                conn,
                matter_scope=args.matter,
                authorized_matter_scope=authorized_matter_scope,
                index_name=args.index_name,
            )
        print_json({"dry_run": False, **result})
        return 0

    if args.command == "import-candidates":
        with repo.db_connection(args.db) as conn:
            result = import_candidates(conn, workspace=args.workspace, dry_run=args.dry_run)
        print_json(
            {
                "dry_run": result.dry_run,
                "candidate_count": len(result.candidates),
                "validation_tasks_created": result.validation_tasks_created,
                "candidates": [c.__dict__ for c in result.candidates],
            }
        )
        return 0

    if args.command == "source-led-packet":
        with repo.db_connection(args.db, read_only=args.dry_run) as conn:
            result = create_source_led_candidate_for_task(
                conn,
                matter_scope=args.matter,
                task_id=args.task_id,
                max_sources=args.max_sources,
                source_ids=args.source_id or None,
                write=not args.dry_run,
            )
        print_json(result.as_dict())
        return 0

    if args.command == "seed-matter":
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            result = seed_matter_from_inventory(
                conn,
                matter_scope=args.matter,
                workspace=args.workspace,
                inventory=args.inventory,
                provider=args.provider,
                model=args.model,
                allow_fallback=args.allow_fallback,
                estimated_cost_usd=args.estimated_cost_usd,
                dry_run=not args.write,
            )
        print_json(result.as_dict())
        return 0

    if args.command == "extract-sources":
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            result = repair_source_extractions(
                conn,
                matter_scope=args.matter,
                workspace=args.workspace,
                source_ids=args.source_id or [],
                dry_run=not args.write,
                timeout_seconds=args.extraction_timeout_seconds,
            )
        print_json(result.as_dict())
        return 0

    if args.command == "validate":
        with repo.db_connection(args.db) as conn:
            outcome = run_validation(
                conn,
                gate_name=args.gate,
                target_type=args.target_type,
                target_id=args.target_id,
            )
        print_json(outcome.__dict__)
        return 0 if outcome.passed else 2

    if args.command == "certify":
        with repo.db_connection(args.db) as conn:
            certification_id = certify_subject(
                conn,
                subject_type=args.subject_type,
                subject_id=args.subject_id,
                certification_type=args.certification_type,
                validator=args.validator,
            )
        print_json({"certification_id": certification_id})
        return 0

    if args.command == "schedule":
        if args.dry_run:
            with repo.db_connection(args.db, read_only=True) as conn:
                matter_scope = _resolve_scheduler_matter_scope(conn, matter_scope=args.matter, all_matters=args.all_matters)
                runnable, blocked = _schedule_preview(conn, capacity=args.capacity, matter_scope=matter_scope)
            print_json({"dry_run": True, "runnable": runnable, "blocked": blocked})
        else:
            with repo.db_connection(args.db) as conn:
                matter_scope = _resolve_scheduler_matter_scope(conn, matter_scope=args.matter, all_matters=args.all_matters)
                runnable_rows = select_runnable_tasks(
                    conn,
                    capacity=args.capacity,
                    matter_scope=matter_scope,
                    dry_run=False,
                    allow_decomposition=True,
                )
                runnable = [_task_summary(row) for row in runnable_rows]
            print_json({"dry_run": False, "runnable": runnable})
        return 0

    if args.command == "lease":
        with repo.db_connection(args.db) as conn:
            lease_id = acquire_lease(
                conn,
                task_id=args.task_id,
                worker_id=args.worker_id,
                seconds=args.seconds,
                dry_run=args.dry_run,
            )
        print_json({"dry_run": args.dry_run, "lease_id": lease_id, "task_id": args.task_id})
        return 0

    if args.command == "work-order":
        with repo.db_connection(args.db, read_only=args.dry_run) as conn:
            order = build_work_order(
                conn,
                task_id=args.task_id,
                lease_id=args.lease_id,
                persist_context=not args.dry_run,
            )
        print_json({"dry_run": args.dry_run, "work_order": order.as_dict()})
        return 0

    if args.command == "context":
        with repo.db_connection(args.db, read_only=True) as conn:
            diagnostics = build_context_diagnostics(conn, task_id=args.task_id, token_budget=args.token_budget)
        if args.explain and not args.json_output:
            print(_context_markdown(diagnostics))
        else:
            print_json(diagnostics)
        return 0

    if args.command == "run-local":
        if not args.write:
            print_json(
                {
                    "dry_run": True,
                    "blocked": "run-local requires --write to record a candidate output",
                    "task_id": args.task_id,
                    "lease_id": args.lease_id,
                    "adapter": "local_stub",
                }
            )
            return 0
        with repo.db_connection(args.db) as conn:
            result = execute_local_work_order(
                conn,
                task_id=args.task_id,
                lease_id=args.lease_id,
                worker_id=args.worker_id,
                output_dir=args.output_dir,
            )
        print_json(
            {
                "dry_run": False,
                "candidate_id": result.candidate_id,
                "worker_attempt_id": result.worker_attempt_id,
                "output_path": str(result.output_path),
                "provider_run_id": result.provider_run_id,
                "adapter": result.adapter,
            }
        )
        return 0

    if args.command == "reduce":
        with repo.db_connection(args.db, read_only=args.dry_run) as conn:
            result = reduce_candidate(
                conn,
                candidate_id=args.candidate_id,
                reducer_lease_id=args.lease_id,
                dry_run=args.dry_run,
        )
        print_json(result)
        return 0

    if args.command == "reject-candidate":
        with repo.db_connection(args.db, read_only=args.dry_run) as conn:
            result = reject_candidate_output(
                conn,
                candidate_id=args.candidate_id,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        print_json(result)
        return 0

    if args.command == "budget":
        with repo.db_connection(args.db) as conn:
            if args.limit is not None:
                if not args.write:
                    print_json(
                        {
                            "dry_run": True,
                            "would_set": {
                                "scope_type": args.scope_type,
                                "scope_id": args.scope_id,
                                "limit_usd": args.limit,
                            },
                        }
                    )
                    return 0
                budget_id = repo.add_budget(
                    conn,
                    scope_type=args.scope_type,
                    scope_id=args.scope_id,
                    limit_usd=args.limit,
                )
            else:
                budget_id = None
            decision = check_budget(conn, scope_type=args.scope_type, scope_id=args.scope_id, requested_usd=args.check)
            status = budget_status(conn, scope_type=args.scope_type, scope_id=args.scope_id)
        print_json({"budget_id": budget_id, "decision": decision.__dict__, "status": status.__dict__})
        return 0 if decision.allowed else 2

    if args.command == "set-provider-policy":
        if args.smart_defaults and args.policy_file:
            raise ValueError("set-provider-policy accepts --smart-defaults or --policy-file, not both")
        if args.smart_defaults:
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                result = _set_model_policy_for_matter(
                    conn,
                    matter_scope=args.matter,
                    policy=default_smart_model_policy(),
                    policy_label="built-in:smart-defaults",
                    smart=True,
                    dry_run=not args.write,
                )
            print_json(result)
            return 0
        if args.policy_file:
            policy_path = Path(args.policy_file)
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                result = _set_model_policy_for_matter(
                    conn,
                    matter_scope=args.matter,
                    policy=load_model_routing_policy(policy_path),
                    policy_label=str(policy_path.resolve()),
                    smart=False,
                    dry_run=not args.write,
                )
            print_json(result)
            return 0
        if not args.provider or not args.model:
            raise ValueError("set-provider-policy requires --provider/--model or --policy-file")
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            result = set_provider_policy_for_matter(
                conn,
                matter_scope=args.matter,
                provider=args.provider,
                model=args.model,
                allow_fallback=args.allow_fallback,
                estimated_cost_usd=args.estimated_cost_usd,
                dry_run=not args.write,
            )
        print_json(result.as_dict())
        return 0

    if args.command == "model-policy":
        task_row: Mapping[str, object] | None = None
        if args.policy_file is None and not (args.action == "decide" and args.db and args.task_id):
            raise ValueError("model-policy requires --policy-file, or decide requires --db and --task-id")
        if args.policy_file is not None:
            policy = load_model_routing_policy(Path(args.policy_file))
        else:
            with repo.db_connection(args.db, read_only=True) as conn:
                task_row = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM tasks WHERE task_id = ?", (args.task_id,)).fetchone())
            if task_row is None:
                raise ValueError(f"task not found: {args.task_id}")
            provider_policy = _json_object_arg(str(task_row["provider_policy_json"] or "{}"))
            routing = provider_policy.get("model_routing")
            policy = load_model_routing_policy(cast(Mapping[str, object], routing)) if isinstance(routing, Mapping) else default_smart_model_policy()
            args.stage = args.stage or str(task_row["stage"])
            args.task_type = args.task_type or str(task_row["task_type"])
            args.matter = args.matter or str(task_row["matter_scope"])
            args.expected_value = args.expected_value or float(str(task_row["expected_value"] or 0.0))
        if args.action == "validate":
            print_json({"ok": True, "policy": policy.as_dict()})
            return 0
        if args.action == "decide":
            resolved = smart_provider_policy_for_route(
                policy,
                layer=args.layer,
                stage=args.stage,
                task_type=args.task_type,
                task_id=args.task_id,
                matter_scope=args.matter,
                risk_level=args.risk_level,
                legal_complexity=args.legal_complexity,
                evidence_volume=args.evidence_volume,
                authority_required=args.authority_required,
                hostile_review_required=args.hostile_review_required,
                drafting_finality=args.drafting_finality,
                contradiction_count=args.contradiction_count,
                unresolved_uncertainty_count=args.unresolved_uncertainty_count,
                source_count=args.source_count,
                extracted_char_count=args.extracted_char_count,
                expected_value=args.expected_value,
                requested_capabilities=tuple(args.capability or ()),
                operator_override=args.operator_override,
            )
        else:
            resolved = provider_policy_for_route(
                policy,
                layer=args.layer,
                stage=args.stage,
                task_type=args.task_type,
                task_id=args.task_id,
            )
        print_json({"ok": True, "resolved": resolved})
        return 0

    if args.command == "skill":
        if args.action == "list":
            print_json(
                {
                    "skills": [
                        {
                            "skill_id": skill.skill_id,
                            "path": str(skill.path),
                            "manifest": skill.manifest,
                            "references": list(skill.references),
                            "examples": list(skill.examples),
                        }
                        for skill in list_skills()
                    ]
                }
            )
            return 0
        if not args.skill_id:
            raise ValueError("skill show requires --skill-id")
        print_json(load_skill(args.skill_id).as_work_order_context())
        return 0

    if args.command == "tools":
        tools_payload = {"tools": [tool.metadata.as_dict() for tool in list_tools() if not tool.hidden]}
        print_json(tools_payload)
        return 0

    if args.command == "verifier":
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            result = verify_candidate(
                conn,
                candidate_id=args.candidate_id,
                verifier_type=args.type,
                write=args.write,
            )
        print_json({"dry_run": not args.write, **result.as_dict()})
        return 0 if result.passed else 2

    if args.command == "workflow":
        if args.action == "list":
            print_json({"workflows": [{"name": workflow.name, "frontmatter": workflow.frontmatter} for workflow in list_workflows()]})
            return 0
        if not args.name:
            raise ValueError(f"workflow {args.action} requires NAME")
        if args.action == "show":
            print_json(load_workflow(args.name).as_dict())
            return 0
        if not args.db:
            raise ValueError("workflow run requires --db")
        with repo.db_connection(args.db, read_only=args.dry_run) as conn:
            result = plan_workflow(conn, name=args.name, matter_scope=args.matter, dry_run=args.dry_run)
        print_json(result)
        return 0

    if args.command == "coordinator":
        dry_run = args.action == "plan" or not args.write
        with repo.db_connection(args.db, read_only=dry_run) as conn:
            result = plan_coordinator_work(
                conn,
                matter_scope=args.matter,
                goal=args.goal,
                source_ids=args.source_id or [],
                artifact_ids=args.artifact_id or [],
                dry_run=dry_run,
            )
        print_json(result)
        return 0

    if args.command == "matter-profile":
        if args.action == "show":
            with repo.db_connection(args.db, read_only=True) as conn:
                print_json({"matter_scope": args.matter, "active_profile": repo.get_active_matter_profile(conn, matter_scope=args.matter)})
            return 0
        if args.action == "propose":
            with repo.db_connection(args.db, read_only=True) as conn:
                proposal = propose_matter_profile_adaptation(conn, args.matter, args.goal or args.reason or "matter work", {})
            print_json({"dry_run": True, "proposal": proposal.as_dict()})
            return 0
        if args.action == "apply":
            if not args.profile_file:
                raise ValueError("matter-profile apply requires --profile-file")
            profile_payload = _json_object_arg(Path(args.profile_file).read_text(encoding="utf-8"))
            if isinstance(profile_payload.get("proposal"), Mapping):
                profile_payload = dict(cast(Mapping[str, object], profile_payload["proposal"]))
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                result = apply_matter_profile_adaptation(conn, args.matter, profile_payload, write=args.write)
            print_json(result)
            return 0
        if args.action == "reset":
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                result = reset_matter_profile_to_default(conn, args.matter, write=args.write)
            print_json(result)
            return 0
        if not args.write:
            print_json({"dry_run": True, "matter_scope": args.matter, "profile_name": args.name, "reason": args.reason})
            return 0
        with repo.db_connection(args.db) as conn:
            profile_id = repo.create_matter_profile(conn, matter_scope=args.matter, profile_name=args.name, reason=args.reason)
            active = repo.get_active_matter_profile(conn, matter_scope=args.matter)
        print_json({"dry_run": False, "matter_profile_id": profile_id, "active_profile": active})
        return 0

    if args.command == "orchestrator":
        if args.action in {"show", "status"}:
            with repo.db_connection(args.db, read_only=True) as conn:
                print_json({"matter_scope": args.matter, "orchestrator": repo.get_matter_orchestrator(conn, matter_scope=args.matter)})
            return 0
        if args.action == "tick":
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                result = orchestrator_tick(conn, args.matter, args.capacity, dry_run=not args.write)
            print_json(result)
            return 0
        if args.action == "failures":
            with repo.db_connection(args.db, read_only=True) as conn:
                rows = [
                    _row_to_dict(row)
                    for row in conn.execute(
                        """
                        SELECT *
                        FROM orchestrator_events
                        WHERE matter_scope = ? AND event_type = 'orchestrator.worker_failed'
                        ORDER BY created_at DESC
                        LIMIT 50
                        """,
                        (args.matter,),
                    )
                ]
                error_logs = [
                    _row_to_dict(row)
                    for row in conn.execute(
                        """
                        SELECT *
                        FROM error_logs
                        WHERE matter_scope = ?
                        ORDER BY created_at DESC
                        LIMIT 50
                        """,
                        (args.matter,),
                    )
                ]
            print_json({"matter_scope": args.matter, "failures": rows, "error_logs": error_logs})
            return 0
        if args.action == "worker-failed":
            if not args.task_id:
                raise ValueError("orchestrator worker-failed requires --task-id")
            if not args.write:
                with repo.db_connection(args.db, read_only=True) as conn:
                    task_matter = repo.matter_scope_for_target(conn, target_type="task", target_id=args.task_id)
                if task_matter is None:
                    raise ValueError(f"unknown task: {args.task_id}")
                if task_matter != args.matter:
                    raise ValueError(f"task {args.task_id} belongs to matter {task_matter}, not {args.matter}")
                print_json({"dry_run": True, "matter_scope": args.matter, "task_id": args.task_id, "failure_reason": args.reason})
                return 0
            with repo.db_connection(args.db) as conn:
                event_id = report_worker_failure_to_orchestrator(conn, args.task_id, args.reason or "worker failed", matter_scope=args.matter)
            print_json({"dry_run": False, "orchestrator_event_id": event_id})
            return 0
        if args.action == "repair":
            failure_event_id = args.failure_event_id or args.event_type
            if not failure_event_id:
                raise ValueError("orchestrator repair requires --failure-event-id")
            with repo.db_connection(args.db, read_only=True) as conn:
                result = orchestrator_plan_repair(conn, args.matter, failure_event_id)
            print_json(result)
            return 0
        if args.action == "signal":
            message = args.message or args.reason
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                result = record_operator_signal(
                    conn,
                    args.matter,
                    args.signal_type,
                    message,
                    target_task_id=args.task_id,
                    priority=args.priority,
                    requested_by=args.requested_by,
                    write=args.write,
                )
            print_json(result)
            return 0
        if not args.write:
            print_json({"dry_run": True, "matter_scope": args.matter, "action": args.action, "status": args.status, "goal": args.goal})
            return 0
        with repo.db_connection(args.db) as conn:
            if args.action == "upsert":
                orchestrator_id = repo.upsert_matter_orchestrator(conn, matter_scope=args.matter, status=args.status or "idle", current_goal=args.goal)
                result = repo.get_matter_orchestrator(conn, matter_scope=args.matter)
                print_json({"dry_run": False, "orchestrator_id": orchestrator_id, "orchestrator": result})
                return 0
            current = repo.get_matter_orchestrator(conn, matter_scope=args.matter)
            if current is None:
                raise ValueError("orchestrator event requires an existing orchestrator; run orchestrator upsert first")
            if not args.event_type:
                raise ValueError("orchestrator event requires --event-type")
            event_id = repo.record_orchestrator_event(
                conn,
                orchestrator_id=str(current["orchestrator_id"]),
                event_type=args.event_type,
                payload=_json_object_arg(args.payload_json),
            )
        print_json({"dry_run": False, "orchestrator_event_id": event_id})
        return 0

    if args.command == "maintenance":
        if args.action == "status":
            with repo.db_connection(args.db, read_only=True) as conn:
                schema_check = schema_check_json(conn, db_path=args.db)
                if not schema_check["ok"]:
                    print_json(schema_check)
                    return 2
                result = maintenance_status(conn, matter_scope=None if args.matter == "global" else args.matter)
            print_json(result)
            return 0
        if args.action == "trigger":
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                result = request_maintenance(
                    conn,
                    matter_scope=args.matter,
                    reason=args.reason,
                    triggered_by=args.triggered_by,
                    write=args.write,
                )
            print_json(result)
            return 0
        if args.action == "tick":
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                if not args.write:
                    schema_check = schema_check_json(conn, db_path=args.db)
                    if not schema_check["ok"]:
                        print_json(schema_check)
                        return 2
                result = maintenance_tick(
                    conn,
                    matter_scope=args.matter,
                    maintenance_run_id=args.maintenance_run_id,
                    write=args.write,
                )
            print_json(result)
            return 0 if result.get("resume_signal", {}).get("status") != "blocked_by_user_intervention" else 2
        if args.action == "report":
            if not args.maintenance_run_id:
                raise ValueError("maintenance report requires --maintenance-run-id")
            with repo.db_connection(args.db, read_only=True) as conn:
                schema_check = schema_check_json(conn, db_path=args.db)
                if not schema_check["ok"]:
                    print_json(schema_check)
                    return 2
                result = maintenance_report(conn, maintenance_run_id=args.maintenance_run_id)
            print_json(result)
            return 0

    if args.command == "work-run":
        if args.action == "status":
            with repo.db_connection(args.db, read_only=True) as conn:
                rows = [
                    _row_to_dict(row)
                    for row in conn.execute(
                        "SELECT * FROM work_runs WHERE matter_scope = ? ORDER BY updated_at DESC LIMIT 25",
                        (args.matter,),
                    )
                ]
            print_json({"matter_scope": args.matter, "work_runs": rows})
            return 0
        if args.action == "resume":
            if not args.resume_token:
                raise ValueError("work-run resume requires --resume-token")
            with repo.db_connection(args.db) as conn:
                result = resume_work_run(conn, args.resume_token, matter_scope=args.matter)
            print_json(result)
            return 0 if result.get("ok") is True else 2
        if args.action == "export":
            if not args.work_run_id:
                raise ValueError("work-run export requires --work-run-id")
            with repo.db_connection(args.db, read_only=True) as conn:
                run = conn.execute("SELECT * FROM work_runs WHERE work_run_id = ? AND matter_scope = ?", (args.work_run_id, args.matter)).fetchone()
                if run is None:
                    raise ValueError(f"work run not found in matter {args.matter}: {args.work_run_id}")
                steps = [_row_to_dict(row) for row in conn.execute("SELECT * FROM work_run_steps WHERE work_run_id = ? ORDER BY created_at", (args.work_run_id,))]
            print_json({"work_run": _row_to_dict(run), "steps": steps})
            return 0
        if args.action == "reusable":
            with repo.db_connection(args.db, read_only=True) as conn:
                if args.step_type:
                    reusable = repo.find_reusable_work_step(conn, matter_scope=args.matter, step_type=args.step_type, input_fingerprint=args.input_fingerprint)
                    print_json({"matter_scope": args.matter, "reusable_step": reusable})
                else:
                    print_json(summarize_reusable_work(conn, args.matter, args.goal))
            return 0
        if not args.write:
            print_json({"dry_run": True, "matter_scope": args.matter, "action": args.action, "goal": args.goal})
            return 0
        with repo.db_connection(args.db) as conn:
            if args.action == "start":
                work_run_id = repo.start_work_run(conn, matter_scope=args.matter, goal=args.goal)
                run = conn.execute("SELECT * FROM work_runs WHERE work_run_id = ?", (work_run_id,)).fetchone()
                print_json({"dry_run": False, "work_run": _row_to_dict(run) if run is not None else {"work_run_id": work_run_id}})
                return 0
            if args.action == "complete":
                if not args.work_run_id:
                    raise ValueError("work-run complete requires --work-run-id")
                repo.update_work_run_status(conn, work_run_id=args.work_run_id, status="complete", matter_scope=args.matter)
                print_json({"dry_run": False, "work_run_id": args.work_run_id, "status": "complete"})
                return 0
            if args.action == "step":
                if not args.work_run_id or not args.step_type:
                    raise ValueError("work-run step requires --work-run-id and --step-type")
                step_id = repo.record_work_run_step(
                    conn,
                    work_run_id=args.work_run_id,
                    step_type=args.step_type,
                    status=args.status or "complete",
                    task_id=args.task_id,
                    input_fingerprint=args.input_fingerprint,
                    output_fingerprint=args.output_fingerprint,
                    expected_matter_scope=args.matter,
                )
                print_json({"dry_run": False, "work_run_step_id": step_id})
                return 0
            if not args.reused_from_step_id:
                raise ValueError("work-run reuse requires --reused-from-step-id")
            reuse_id = repo.record_work_reuse(
                conn,
                matter_scope=args.matter,
                reused_from_step_id=args.reused_from_step_id,
                reused_by_step_id=args.reused_by_step_id,
            )
        print_json({"dry_run": False, "reuse_record_id": reuse_id})
        return 0

    if args.command == "memory":
        with repo.db_connection(args.db, read_only=not args.write) as conn:
            if args.action == "list":
                print_json({"matter_scope": args.matter, "memories": repo.list_legal_memories(conn, matter_scope=args.matter)})
                return 0
            if args.action == "extract-candidates":
                if not args.candidate_id:
                    raise ValueError("memory extract-candidates requires --candidate-id")
                result = extract_memory_candidates(
                    conn,
                    candidate_id=args.candidate_id,
                    matter_scope=args.matter,
                    dry_run=not args.write,
                )
                print_json(result)
                return 0
            if args.action == "consolidate":
                result = consolidate_case_memory(conn, matter_scope=args.matter, dry_run=not args.write)
                print_json(result)
                return 0
            if not args.memory_id:
                raise ValueError(f"memory {args.action} requires --memory-id")
            if args.action == "show":
                memory = repo.get_legal_memory(conn, memory_id=args.memory_id, matter_scope=args.matter)
                if memory is None:
                    raise KeyError(f"memory not found: {args.memory_id}")
                print_json(memory)
                return 0
            if args.action == "mark-stale":
                if not args.write:
                    print_json({"dry_run": True, "memory_id": args.memory_id, "would_mark_stale": True, "reason": args.reason})
                    return 0
                repo.mark_legal_memory_stale(conn, memory_id=args.memory_id, matter_scope=args.matter, reason=args.reason or "operator marked stale")
                print_json({"dry_run": False, "memory_id": args.memory_id, "stale": True})
                return 0
            if args.action == "reject":
                if not args.write:
                    print_json({"dry_run": True, "memory_id": args.memory_id, "would_reject": True, "reason": args.reason})
                    return 0
                _ = conn.execute(
                    "UPDATE legal_memories SET status = 'rejected', updated_at = ? WHERE memory_id = ? AND matter_scope = ?",
                    (utc_now(), args.memory_id, args.matter),
                )
                _ = repo.emit_event(conn, "legal_memory.rejected", matter_scope=args.matter, payload={"memory_id": args.memory_id, "reason": args.reason})
                print_json({"dry_run": False, "memory_id": args.memory_id, "status": "rejected"})
                return 0
            if args.action == "export-index":
                memories = repo.list_legal_memories(conn, matter_scope=args.matter)
                print_json({"matter_scope": args.matter, "memory_index": [{"memory_id": item["memory_id"], "type": item["type"], "name": item["name"], "stale": item["stale"]} for item in memories]})
                return 0

    if args.command == "session":
        authorized_matter_scope = authorized_matter_from_env()
        matter_scope = require_matter_access(args.matter, authorized_matter_scope=authorized_matter_scope)
        with repo.db_connection(args.db, read_only=True) as conn:
            if args.action == "list":
                print_json(
                    {
                        "matter_scope": matter_scope,
                        "sessions": repo.list_sessions(conn, matter_scope=matter_scope, status=args.status),
                    }
                )
                return 0
            if not args.session_id:
                raise ValueError(f"session {args.action} requires SESSION_ID")
            export = repo.export_session_for_matter(conn, session_id=args.session_id, matter_scope=matter_scope)
            if args.action == "show":
                print_json(export)
                return 0
            if args.action == "export":
                print_json(export)
                return 0
            if args.action == "resume":
                print_json(
                    {
                        **export,
                        "resume": {
                            "provider_replay": False,
                            "note": "session resume is transcript-only; provider work must be started through normal gated commands",
                        },
                    }
                )
                return 0

    if args.command in {"provider-policy", "policy-check"}:
        actual = None
        if args.actual_provider or args.actual_model:
            actual = ProviderActual(args.actual_provider or args.provider, args.actual_model or args.model)
        request = ProviderRequest(args.provider, args.model, allow_fallback=args.allow_fallback)
        if args.db:
            with repo.db_connection(args.db) as conn:
                decision = record_provider_policy_decision(conn, requested=request, actual=actual, task_id=args.task_id)
        else:
            decision = check_provider_policy(request, actual=actual)
        print_json(decision.__dict__)
        return 0 if decision.allowed else 2

    if args.command == "provider-probe":
        result = probe_live_openrouter({"provider": args.provider, "model": args.model, "allow_fallback": args.allow_fallback})
        print_json(result)
        return 0 if result.get("ok") is True else 2

    if args.command == "live-resume":
        env = dict(os.environ)
        write = bool(args.write or args.execute or args.write_leases)
        if args.allow_live:
            env[LIVE_ENABLE_ENV] = "1"
        probe_result: object
        if args.probe_result_json:
            try:
                probe_result = json.loads(args.probe_result_json)
            except json.JSONDecodeError as exc:
                probe_result = {"ok": False, "reason": f"probe_result_json must be valid JSON: {exc}"}
        elif args.probe:
            probe_result = probe_live_openrouter(
                {"provider": "openrouter", "model": args.model, "allow_fallback": False},
                env=env,
            )
        else:
            probe_result = {"ok": False, "reason": "live-resume requires --probe or --probe-result-json"}
        with repo.db_connection(args.db, read_only=not write) as conn:
            matter_scope = _resolve_scheduler_matter_scope(conn, matter_scope=args.matter, all_matters=args.all_matters)
            policy_plan = _ensure_live_resume_smart_provider_policy(
                conn,
                matter_scope=matter_scope,
                dry_run=not write,
            )
            plan = prepare_live_resume(
                conn,
                capacity=args.capacity,
                env=env,
                matter_scope=matter_scope,
                probe_result=probe_result,
                write_leases=write,
                worker_prefix=args.worker_prefix,
            )
            plan["provider_policy_plan"] = policy_plan
            plan["live_env_gate"] = {
                "name": LIVE_ENABLE_ENV,
                "established_for_child_commands": bool(args.allow_live),
                "enabled": env.get(LIVE_ENABLE_ENV) == "1",
            }
            plan["next"] = _classify_live_resume_next_action(
                conn,
                plan=plan,
                db=args.db,
                matter_scope=matter_scope,
                output_dir="OUT",
                capacity=args.capacity,
            )
            execute_output: dict[str, object] = {}
            if args.execute_ticks and args.execute_ticks > 0 and write and plan.get("ready") is True and args.allow_live:
                output_dir = args.output_dir or "OUT"
                try:
                    tick_result = run_free_loop(
                        conn,
                        output_dir=output_dir,
                        capacity=args.capacity or 15,
                        max_ticks=args.execute_ticks,
                        runtime="openrouter",
                        allow_live=True,
                        env=env,
                        matter_scope=matter_scope,
                    )
                    plan["free_loop_ticks"] = tick_result
                    plan["_auto_continued"] = True
                except Exception as exc:
                    plan["free_loop_error"] = str(exc)
                    plan["_auto_continued"] = False
        print_json(plan)
        return 0 if plan["ready"] else 2

    if args.command == "run-free-loop":
        with repo.db_connection(args.db) as conn:
            matter_scope = _resolve_scheduler_matter_scope(conn, matter_scope=args.matter, all_matters=args.all_matters)
            result = run_free_loop(
                conn,
                output_dir=args.output_dir,
                capacity=args.capacity,
                max_ticks=args.max_ticks,
                runtime=args.runtime,
                allow_live=args.allow_live,
                env=dict(os.environ),
                codex_timeout_seconds=args.codex_timeout_seconds,
                codex_reasoning_effort=args.codex_reasoning_effort,
                matter_scope=matter_scope,
            )
        print_json(result)
        return 0 if result.get("ok") is True else 2

    if args.command == "reconcile-foundation":
        with repo.db_connection(args.db) as conn:
            result = reconcile_foundation(
                conn,
                matter_scope=args.matter,
                dry_run=args.dry_run,
                validator=args.validator,
            )
        print_json(result)
        return 0 if result["ready_for_live_resume"] else 2

    if args.command == "human-attention":
        with repo.db_connection(args.db, read_only=not (bool(getattr(args, "write", False)) or bool(getattr(args, "add", False)))) as conn:
            if args.cleanup:
                if not args.matter:
                    raise ValueError("human-attention --cleanup requires --matter")
                print_json(
                    plan_human_attention_cleanup(
                        conn,
                        matter_scope=args.matter,
                        provider_probe_passed=cast(list[str], args.provider_probe_passed),
                        write=bool(args.write),
                    )
                )
                return 0
            if args.add:
                matter_scope = cast(str | None, args.matter)
                if matter_scope is None and repo.matter_scope_for_target(conn, target_type=args.target_type, target_id=args.target_id) is None:
                    raise ValueError("human-attention --add requires --matter when target matter cannot be inferred")
                attention_id = repo.record_human_attention(
                    conn,
                    target_type=args.target_type,
                    target_id=args.target_id,
                    severity=args.severity,
                    reason=args.reason,
                    matter_scope=matter_scope,
                )
                print_json({"attention_id": attention_id})
            else:
                matter_filter = ""
                params: tuple[object, ...] = ()
                if args.matter:
                    matter_filter = "AND matter_scope = ?"
                    params = (args.matter,)
                rows = [
                    _row_to_dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM human_attention WHERE status = 'open' {matter_filter} ORDER BY attention_id DESC LIMIT 50",
                        params,
                    )
                ]
                if args.current_only:
                    rows = [row for row in rows if row.get("superseded_by") is None]
                if args.classify:
                    from atticus.status.completion import triage_human_attention
                    for row in rows:
                        row["classification"] = triage_human_attention(dict(row))
                print_json({"items": rows})
        return 0

    if args.command == "migrate-report":
        if args.db:
            with repo.db_connection(args.db) as conn:
                report = build_migration_report(
                    conn,
                    workspace=args.workspace,
                    dry_run=args.dry_run,
                    persist=not args.dry_run,
                )
        else:
            report = build_migration_report(None, workspace=args.workspace, dry_run=args.dry_run)
        print_json(report.as_dict())
        return 0

    if args.command == "doctor":
        if args.repair:
            if not args.write:
                with repo.db_connection(args.db, read_only=True) as conn:
                    schema_check = schema_check_json(conn, db_path=args.db)
                print_json({"dry_run": True, "would_repair": not schema_check["ok"], **schema_check})
                return 0 if schema_check["ok"] else 2
            with repo.db_connection(args.db, read_only=False) as conn:
                schema_check = schema_check_json(conn, db_path=args.db)
            print_json({"dry_run": False, "repaired": schema_check["ok"], **schema_check})
            return 0 if schema_check["ok"] else 2

        with repo.db_connection(args.db, read_only=True) as conn:
            check = verify_schema(conn)
            schema_check = check.as_dict(db_path=args.db)
            if not check.ok:
                print_json({"diagnostic_only": True, **schema_check})
                return 2
            expired = [
                _row_str(row, "lease_id")
                for row in conn.execute(
                    "SELECT lease_id FROM leases WHERE status = 'active' AND expires_at <= ? ORDER BY lease_id",
                    (utc_now(),),
                )
            ]
            tables = {
                name: _count_table(conn, name)
                for name in ("events", "runs", "sources", "artifacts", "tasks", "leases", "human_attention")
            }
            schema_row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
            schema_version = _row_str(schema_row, "value")
        print_json(
            {
                "ok": True,
                "diagnostic_only": True,
                "schema_check": schema_check,
                "schema_version": schema_version,
                "tables": tables,
                "expired_leases": expired,
                "safety": {
                    "openclaw_started": False,
                    "live_workers_started": False,
                    "external_legal_actions_enabled": False,
                },
            }
        )
        return 0

    return 1


def _schedule_preview(conn: sqlite3.Connection, *, capacity: int, matter_scope: str | None = None) -> tuple[list[JsonObject], list[JsonObject]]:
    capacity_requested = agent_capacity(capacity)
    runnable: list[JsonObject] = []
    blocked: list[JsonObject] = []
    params: tuple[object, ...] = ()
    matter_clause = ""
    if matter_scope:
        matter_clause = " AND matter_scope = ?"
        params = (matter_scope,)
    task_rows = cast(Iterable[sqlite3.Row], conn.execute(
        f"""
        SELECT * FROM tasks
        WHERE status IN ('queued', 'ready', 'blocked')
        {matter_clause}
        ORDER BY expected_value DESC, created_at ASC
        """,
        params,
    ))
    for task in task_rows:
        result = evaluate_task_gates(conn, cast(Mapping[str, object], cast(object, task)))
        blockers = result.reasons + budget_blockers(conn, task)
        if blockers:
            blocked.append({"task_id": _row_str(task, "task_id"), "title": _row_str(task, "title"), "reasons": blockers})
        elif str(task["status"]) == str(TaskStatus.BLOCKED) and not blocked_task_auto_requeue_allowed(cast(Mapping[str, object], cast(object, task))):
            blocked.append(
                {
                    "task_id": _row_str(task, "task_id"),
                    "title": _row_str(task, "title"),
                    "reasons": _blocked_reasons_for_preview(cast(Mapping[str, object], cast(object, task))),
                }
            )
        elif len(runnable) < capacity_requested:
            runnable.append(_task_summary(cast(Mapping[str, object], cast(object, task))))
    return runnable, blocked


def _resolve_scheduler_matter_scope(conn: sqlite3.Connection, *, matter_scope: str | None, all_matters: bool) -> str | None:
    if matter_scope:
        return matter_scope
    rows = conn.execute(
        """
        SELECT DISTINCT matter_scope
        FROM tasks
        WHERE status IN ('queued', 'ready', 'blocked', 'leased', 'reducer_pending')
        ORDER BY matter_scope
        """
    ).fetchall()
    active = [str(row["matter_scope"]) for row in rows]
    if len(active) > 1 and not all_matters:
        raise ValueError(
            "multiple active matters are present; pass --matter <matter_scope> or --all-matters explicitly"
        )
    return None


def _blocked_reasons_for_preview(task: Mapping[str, object]) -> list[str]:
    try:
        raw = json.loads(str(task["blocked_reasons_json"] or "[]"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return ["malformed blocked_reasons_json"]
    if not isinstance(raw, list):
        return ["malformed blocked_reasons_json"]
    reasons = [str(item) for item in cast(list[object], raw) if str(item)]
    return reasons or ["blocked by prior terminal runtime failure"]


def _set_model_policy_for_matter(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    policy: ModelRoutingPolicy,
    policy_label: str,
    smart: bool,
    dry_run: bool,
) -> JsonObject:
    rows = [
        cast(Mapping[str, object], row)
        for row in conn.execute(
            """
            SELECT task_id, stage, task_type, expected_value, provider_policy_json
            FROM tasks
            WHERE matter_scope = ? AND status = 'queued'
            ORDER BY task_id
            """,
            (matter_scope,),
        )
    ]
    changed: list[str] = []
    resolved: list[JsonObject] = []
    for row in rows:
        stage = str(row["stage"])
        task_type = str(row["task_type"])
        task_id = str(row["task_id"])
        if smart:
            provider_policy = smart_provider_policy_for_route(
                policy,
                layer="worker",
                stage=stage,
                task_type=task_type,
                task_id=task_id,
                matter_scope=matter_scope,
                expected_value=float(str(row["expected_value"] or 0.0)),
            )
        else:
            provider_policy = provider_policy_for_route(policy, layer="worker", stage=stage, task_type=task_type, task_id=task_id)
        resolved.append({"task_id": str(row["task_id"]), "provider_policy": provider_policy})
        if str(row["provider_policy_json"] or "{}") != json.dumps(provider_policy, sort_keys=True, separators=(",", ":")):
            changed.append(str(row["task_id"]))
        if not dry_run:
            _ = conn.execute(
                "UPDATE tasks SET provider_policy_json = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(provider_policy, sort_keys=True, separators=(",", ":")), utc_now(), str(row["task_id"])),
            )
    if not dry_run and rows:
        _ = repo.emit_event(
            conn,
            "model_policy.set",
            matter_scope=matter_scope,
            payload={"policy_file": policy_label, "smart": smart, "task_ids": [str(row["task_id"]) for row in rows]},
        )
    return {
        "dry_run": dry_run,
        "matter_scope": matter_scope,
        "policy_file": policy_label,
        "smart": smart,
        "tasks_matched": len(rows),
        "tasks_updated": 0 if dry_run else len(changed),
        "task_ids": [str(row["task_id"]) for row in rows],
        "resolved": resolved,
    }


def _ensure_live_resume_smart_provider_policy(
    conn: sqlite3.Connection,
    *,
    matter_scope: str | None,
    dry_run: bool,
) -> JsonObject:
    """Apply smart OpenRouter policy to live-resume candidates with unset policy only."""

    matter_clause = "AND matter_scope = ?" if matter_scope else ""
    params: tuple[object, ...] = (matter_scope,) if matter_scope else ()
    rows = [
        cast(Mapping[str, object], row)
        for row in conn.execute(
            f"""
            SELECT task_id, matter_scope, stage, task_type, expected_value, provider_policy_json
            FROM tasks
            WHERE status IN ('queued', 'ready', 'blocked')
            {matter_clause}
            ORDER BY expected_value DESC, created_at ASC
            """,
            params,
        )
    ]
    policy = default_smart_model_policy()
    changed: list[str] = []
    resolved: list[JsonObject] = []
    skipped: list[JsonObject] = []
    for row in rows:
        task_id = str(row["task_id"])
        try:
            current = json.loads(str(row["provider_policy_json"] or "{}"))
        except (json.JSONDecodeError, TypeError) as exc:
            skipped.append({"task_id": task_id, "reason": f"malformed provider_policy_json: {exc}"})
            continue
        if not isinstance(current, Mapping):
            skipped.append({"task_id": task_id, "reason": "provider_policy_json is not a JSON object"})
            continue
        current_policy = {str(key): value for key, value in cast(Mapping[object, object], current).items()}
        if str(current_policy.get("provider") or "").strip() and str(current_policy.get("model") or "").strip():
            skipped.append({"task_id": task_id, "reason": "provider policy already set"})
            continue
        provider_policy = smart_provider_policy_for_route(
            policy,
            layer="worker",
            stage=str(row["stage"]),
            task_type=str(row["task_type"]),
            task_id=task_id,
            matter_scope=str(row["matter_scope"]),
            expected_value=float(str(row["expected_value"] or 0.0)),
        )
        resolved.append({"task_id": task_id, "provider_policy": provider_policy})
        changed.append(task_id)
        if not dry_run:
            _ = conn.execute(
                "UPDATE tasks SET provider_policy_json = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(provider_policy, sort_keys=True, separators=(",", ":")), utc_now(), task_id),
            )
    if changed and not dry_run:
        _ = repo.emit_event(
            conn,
            "live_resume.provider_policy.smart_defaults",
            matter_scope=matter_scope or "multi",
            payload={"task_ids": changed},
        )
    return {
        "dry_run": dry_run,
        "smart_defaults": True,
        "tasks_scanned": len(rows),
        "tasks_would_update": len(changed),
        "tasks_updated": 0 if dry_run else len(changed),
        "task_ids": changed,
        "resolved": resolved,
        "skipped": skipped,
    }


def _classify_live_resume_next_action(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, object],
    db: str,
    matter_scope: str | None,
    output_dir: str,
    capacity: int,
) -> JsonObject:
    matter_arg = f" --matter {matter_scope}" if matter_scope else " --all-matters"
    run_command = (
        f"ATTICUS_ENABLE_LIVE_OPENROUTER=1 python -m atticus.cli run-free-loop --db {db}{matter_arg} "
        f"--output-dir {output_dir} --capacity {capacity} --max-ticks 1 --runtime openrouter --allow-live"
    )
    if plan.get("ready") is True:
        return {"classification": "scheduler_can_continue", "command": run_command}

    reducer = conn.execute(
        f"SELECT task_id, stage FROM tasks WHERE status = ? {'AND matter_scope = ?' if matter_scope else ''} ORDER BY updated_at ASC LIMIT 1",
        ((TaskStatus.REDUCER_PENDING, matter_scope) if matter_scope else (TaskStatus.REDUCER_PENDING,)),
    ).fetchone()
    if reducer is not None:
        review_exists = conn.execute(
            f"SELECT 1 FROM reducer_review_queue WHERE status = 'open' {'AND matter_scope = ?' if matter_scope else ''} LIMIT 1",
            (matter_scope,) if matter_scope else (),
        ).fetchone()
        if review_exists is not None:
            return {
                "classification": "reducer_review_required",
                "command": f"python -m atticus.cli reducer-review next --db {db}{matter_arg}",
                "task_id": _row_str(reducer, "task_id"),
            }
        reducer_stage = _row_str(reducer, "stage")
        if reducer_stage not in {"S6", "S7", "S8", "S9"}:
            return {
                "classification": "scheduler_can_continue",
                "command": f"ATTICUS_ENABLE_LIVE_OPENROUTER=1 python -m atticus.cli run-free-loop --db {db}{matter_arg} --output-dir {output_dir} --capacity {capacity} --max-ticks 1 --runtime openrouter --allow-live",
                "task_id": _row_str(reducer, "task_id"),
                "note": f"reducer-pending task in stage {reducer_stage} can be auto-reduced",
            }
        return {
            "classification": "reducer_review_required",
            "command": f"python -m atticus.cli reducer-review next --db {db}{matter_arg}",
            "task_id": _row_str(reducer, "task_id"),
            "note": "reducer_pending in high-risk stage but no review queue entry; run 'reducer-review list --write' first",
        }

    human = conn.execute(
        f"SELECT attention_id, reason FROM human_attention WHERE status = 'open' {'AND matter_scope = ?' if matter_scope else ''} ORDER BY attention_id DESC LIMIT 1",
        (matter_scope,) if matter_scope else (),
    ).fetchone()
    if human is not None:
        return {
            "classification": "human_attention_terminal",
            "command": f"python -m atticus.cli human-attention --db {db}{matter_arg}",
            "attention_id": _row_str(human, "attention_id"),
            "reason": _row_str(human, "reason"),
        }

    blocked_tasks = plan.get("blocked_tasks")
    blocked_text = json.dumps(blocked_tasks if isinstance(blocked_tasks, list) else [], sort_keys=True).lower()
    if "local/stub" in blocked_text or "provider must be openrouter" in blocked_text or "deterministic source-led" in blocked_text:
        return {
            "classification": "stale_local_stub_or_source_led_repair_required",
            "command": f"python -m atticus.cli repairs next --db {db}{matter_arg}",
        }

    return {
        "classification": "provider_or_readiness_blocker",
        "command": f"python -m atticus.cli live-resume --db {db}{matter_arg} --capacity {capacity} --allow-live --probe --write",
    }


def _task_summary(row: Mapping[str, object]) -> JsonObject:
    return {
        "task_id": str(row["task_id"]),
        "title": str(row["title"]),
        "stage": str(row["stage"]),
        "task_type": str(row["task_type"]),
        "expected_value": int(float(str(row["expected_value"]))),
    }


def _row_value(row: sqlite3.Row | None, key: str, default: object = "") -> object:
    if row is None:
        return default
    return cast(object, row[key])


def _row_str(row: sqlite3.Row | None, key: str) -> str:
    return str(_row_value(row, key))


def _row_to_dict(row: sqlite3.Row) -> JsonObject:
    return {str(key): _row_value(row, str(key)) for key in row.keys()}


def _source_trace_payload(conn: sqlite3.Connection, args: CliArgs) -> dict[str, object]:
    if args.source_id and args.quote:
        span = trace_quote_in_source_material(conn, source_id=args.source_id, quote=args.quote)
        return {
            "matter_scope": args.matter,
            "source_id": args.source_id,
            "quote": args.quote,
            "found": bool(span),
            "trace": span,
            "proof_policy": "chunk/span trace is proof metadata; original source snapshot remains the fact source",
        }
    if args.citation_id:
        row = conn.execute(
            """
            SELECT *
            FROM citation_support_results
            WHERE matter_scope = ? AND citation_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (args.matter, args.citation_id),
        ).fetchone()
        return {"matter_scope": args.matter, "citation_id": args.citation_id, "trace": _row_to_dict(row) if row is not None else None}
    raise ValueError("source-trace requires --source-id and --quote, or --citation-id")


def _citation_support_payload(conn: sqlite3.Connection, args: CliArgs) -> dict[str, object]:
    candidate_ids: list[str] = []
    if args.candidate_id:
        candidate_ids = [args.candidate_id]
    elif args.matter:
        stage_clause = "AND t.stage = ?" if args.stage else ""
        params: tuple[object, ...] = (args.matter, args.stage) if args.stage else (args.matter,)
        rows = conn.execute(
            f"""
            SELECT co.candidate_id
            FROM candidate_outputs co
            JOIN tasks t ON t.task_id = co.task_id
            WHERE t.matter_scope = ? {stage_clause}
              AND co.status IN ('candidate', 'accepted')
            ORDER BY co.created_at DESC
            LIMIT 50
            """,
            params,
        ).fetchall()
        candidate_ids = [str(row["candidate_id"]) for row in rows]
    if not candidate_ids:
        raise ValueError("citation-support verify requires --candidate-id or --matter")
    results = []
    for candidate_id in candidate_ids:
        summary = validate_candidate_citation_support(conn, candidate_id, persist_results=bool(args.write))
        results.append({"candidate_id": candidate_id, "passed": summary.passed, "details": summary.details})
    return {"write": args.write, "verified_count": len(results), "results": results}


def _authority_verify_payload(conn: sqlite3.Connection, args: CliArgs) -> dict[str, object]:
    authority_ids: list[str]
    if args.all_open:
        rows = conn.execute(
            """
            SELECT authority_id
            FROM legal_authorities
            WHERE matter_scope = ? AND status != 'rejected'
            ORDER BY updated_at DESC, authority_id
            LIMIT 50
            """,
            (args.matter,),
        ).fetchall()
        authority_ids = [str(row["authority_id"]) for row in rows]
    elif args.authority_id:
        authority_ids = [args.authority_id]
    else:
        raise ValueError("authority verify requires --authority-id or --all-open")
    results: list[dict[str, object]] = []
    proposition_hash = _sha256_normalized(args.proposition)
    for authority_id in authority_ids:
        current = conn.execute(
            """
            SELECT *
            FROM authority_verifications
            WHERE matter_scope = ? AND authority_id = ?
              AND currentness_status = 'current'
              AND proposition_supported = 1
            ORDER BY checked_at DESC
            LIMIT 1
            """,
            (args.matter, authority_id),
        ).fetchone()
        result = {
            "authority_id": authority_id,
            "current": current is not None,
            "proposition_supported": current is not None,
            "requires_human_review": current is None,
            "proposition_hash": proposition_hash,
        }
        if args.write and current is None:
            verification_id = f"authority-verification-{authority_id}-{uuid4().hex}"
            _ = conn.execute(
                """
                INSERT INTO authority_verifications(
                  authority_verification_id, matter_scope, authority_id, jurisdiction,
                  jurisdiction_status, binding_status, currentness_status, proposition_supported,
                  proposition_hash, verification_method, source_url_or_reference,
                  checked_by, checked_at, details_json
                )
                VALUES (?, ?, ?, '', 'unchecked', '', 'unknown', 0, ?, 'operator_required', '', 'atticus-cli', ?, ?)
                """,
                (
                    verification_id,
                    args.matter,
                    authority_id,
                    proposition_hash,
                    utc_now(),
                    json.dumps({"proposition": args.proposition, "status": "human_review_required"}, sort_keys=True),
                ),
            )
            result["verification_id"] = verification_id
        results.append(result)
    return {"write": args.write, "matter_scope": args.matter, "results": results}


def _provider_health_payload(conn: sqlite3.Connection, args: CliArgs) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT provider_policy_json, COUNT(*) AS tasks, GROUP_CONCAT(task_id) AS task_ids
        FROM tasks
        WHERE matter_scope = ? AND status IN ('queued', 'ready', 'blocked')
        GROUP BY provider_policy_json
        ORDER BY tasks DESC
        """,
        (args.matter,),
    ).fetchall()
    groups: list[dict[str, object]] = []
    for row in rows:
        policy_raw = str(row["provider_policy_json"] or "{}")
        policy = _json_object_arg(policy_raw) if policy_raw.strip().startswith("{") else {}
        fingerprint = _sha256_normalized(policy_raw)
        provider = str(policy.get("provider") or policy.get("requested_provider") or "")
        model = str(policy.get("model") or policy.get("requested_model") or "")
        failure_summary = _provider_failure_summary(
            conn,
            matter_scope=args.matter,
            policy_fingerprint=fingerprint,
            task_ids=[task_id for task_id in str(row["task_ids"] or "").split(",") if task_id],
        )
        status = "blocked" if int(failure_summary["failure_count"]) else "not_probed"
        group = {
            "provider_policy_fingerprint": fingerprint,
            "requested_provider": provider,
            "requested_model": model,
            "task_count": int(row["tasks"] or 0),
            "status": status,
            "failure_taxonomy": failure_summary["failure_taxonomy"],
            "failure_count": failure_summary["failure_count"],
            "latest_error_type": failure_summary["latest_error_type"],
            "synthetic_probe_only": True,
        }
        if args.write:
            _ = conn.execute(
                """
                INSERT INTO provider_health_checks(provider_health_check_id, matter_scope, provider_policy_fingerprint,
                  requested_provider, requested_model, status, failure_taxonomy, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"provider-health-{uuid4().hex}",
                    args.matter,
                    fingerprint,
                    provider,
                    model,
                    status,
                    str(group["failure_taxonomy"]),
                    json.dumps(group, sort_keys=True),
                    utc_now(),
                ),
            )
        groups.append(group)
    return {"matter_scope": args.matter, "group_by_policy": args.group_by_policy, "groups": groups}


def _provider_failure_summary(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    policy_fingerprint: str,
    task_ids: list[str],
) -> dict[str, object]:
    task_clause = ""
    params: list[object] = [matter_scope, f"%{policy_fingerprint[:12]}%"]
    if task_ids:
        task_clause = f" OR (target_type = 'task' AND target_id IN ({','.join('?' for _ in task_ids)}))"
        params.extend(task_ids)
    rows = conn.execute(
        f"""
        SELECT error_type, message, payload_json, created_at
        FROM error_logs
        WHERE matter_scope = ? AND (message LIKE ?{task_clause})
        ORDER BY created_at DESC
        """,
        tuple(params),
    ).fetchall()
    if not rows:
        return {"failure_taxonomy": "no_live_probe", "failure_count": 0, "latest_error_type": ""}
    taxonomy = _provider_failure_taxonomy(rows)
    latest = rows[0]
    return {
        "failure_taxonomy": taxonomy,
        "failure_count": len(rows),
        "latest_error_type": str(latest["error_type"] or ""),
    }


def _provider_failure_taxonomy(rows: list[sqlite3.Row]) -> str:
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if isinstance(payload, Mapping):
            provider_failure_class = str(payload.get("provider_failure_class") or payload.get("failure_taxonomy") or "")
            if provider_failure_class:
                return provider_failure_class
    text = " ".join(f"{row['error_type']} {row['message']}" for row in rows).lower()
    if any(marker in text for marker in ("401", "403", "unauthorized", "auth")):
        return "auth"
    if any(marker in text for marker in ("402", "billing", "credit", "payment", "quota")):
        return "billing"
    if any(marker in text for marker in ("timeout", "network", "invalid json", "json message", "http 5")):
        return "transient"
    return "provider_control_plane"


def _cache_health_payload(conn: sqlite3.Connection, args: CliArgs) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT reason, possible_cache_break, COUNT(*) AS n,
               SUM(cache_hit_tokens) AS hit_tokens,
               SUM(cache_write_tokens) AS write_tokens,
               SUM(cache_miss_tokens) AS miss_tokens
        FROM prompt_cache_observations
        WHERE matter_scope = ?
        GROUP BY reason, possible_cache_break
        ORDER BY n DESC
        LIMIT 50
        """,
        (args.matter,),
    ).fetchall()
    return {
        "matter_scope": args.matter,
        "explain_breaks": args.explain_breaks,
        "observations": [_row_to_dict(row) for row in rows],
        "note": "cache health is operational telemetry only and never legal proof",
    }


def _sha256_normalized(value: str) -> str:
    import hashlib

    return hashlib.sha256(" ".join(value.split()).encode("utf-8")).hexdigest()


def _count_table(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
    return int(str(_row_value(row, "n", 0)))


def _json_object_arg(value: str | None) -> JsonObject:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, Mapping):
        raise ValueError("JSON argument must be an object")
    return {str(key): item for key, item in cast(Mapping[object, object], parsed).items()}


def _context_markdown(diagnostics: Mapping[str, object]) -> str:
    lines = [
        f"# Atticus context diagnostics: {diagnostics.get('task_id')}",
        "",
        f"- Matter: `{diagnostics.get('matter_scope')}`",
        f"- Context pack: `{diagnostics.get('context_pack_id')}`",
        f"- Estimated tokens: `{diagnostics.get('estimated_tokens')}` / `{diagnostics.get('token_budget')}`",
        f"- Result schema: `{diagnostics.get('result_schema_version')}`",
        "",
        "| Section | Kind | Tokens | Cache | Reason |",
        "|---|---|---:|---|---|",
    ]
    for section in cast(list[Mapping[str, object]], diagnostics.get("sections") or []):
        lines.append(
            "| {name} | {kind} | {tokens} | {cache} | {reason} |".format(
                name=section.get("name", ""),
                kind=section.get("kind", ""),
                tokens=section.get("estimated_tokens", ""),
                cache=section.get("cache_scope", ""),
                reason=str(section.get("inclusion_reason", "")).replace("|", "\\|"),
            )
        )
    return "\n".join(lines)


def _materialize_resume_commands(value: object, db_path: str) -> object:
    if isinstance(value, str):
        result = value.replace("--db DB", f"--db {db_path}")
        result = result.replace("--output-dir OUT", f"--output-dir {Path(db_path).parent / 'OUT'}")
        return result
    if isinstance(value, Mapping):
        return {str(key): _materialize_resume_commands(item, db_path) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_materialize_resume_commands(item, db_path) for item in value]
    return value


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(str(item) for item in value)
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


def _candidate_id_for_review_args(conn: sqlite3.Connection, *, candidate_id: str | None, review_id: str | None) -> str:
    if candidate_id:
        return candidate_id
    if review_id:
        item = get_reducer_review(conn, review_id)
        return item.candidate_id if item is not None else ""
    return ""


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
