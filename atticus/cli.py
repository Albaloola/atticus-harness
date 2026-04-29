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

from atticus.commands.registry import command_by_name, list_commands
from atticus.agents.coordinator import plan_coordinator_work
from atticus.config import DEFAULT_DB_PATH
from atticus.context.diagnostics import build_context_diagnostics
from atticus.core.events import utc_now
from atticus.core.matters import authorized_matter_from_env, require_matter_access
from atticus.db import repo
from atticus.extraction.local import repair_source_extractions
from atticus.graph.certifications import CertificationBlocked, certify_subject
from atticus.matter_seed import seed_matter_from_inventory, set_provider_policy_for_matter
from atticus.memory.consolidation import consolidate_case_memory
from atticus.memory.extraction import extract_memory_candidates
from atticus.migration.import_old_run import import_candidates
from atticus.migration.reconcile import reconcile_foundation
from atticus.migration.report import build_migration_report
from atticus.providers.budget import budget_status, check_budget
from atticus.providers.live_readiness import probe_live_openrouter
from atticus.providers.model_policy import (
    load_model_routing_policy,
    provider_policy_for_route,
)
from atticus.providers.policy import (
    ProviderActual,
    ProviderRequest,
    check_provider_policy,
    record_provider_policy_decision,
)
from atticus.reducer.reducer import reduce_candidate
from atticus.retrieval.ask import answer_question
from atticus.retrieval.index import DEFAULT_INDEX_NAME, rebuild_search_index
from atticus.scheduler.gates import evaluate_task_gates
from atticus.scheduler.free_loop import run_free_loop
from atticus.scheduler.lease import LeaseError, acquire_lease
from atticus.scheduler.live_orchestrator import prepare_live_resume
from atticus.scheduler.planner import budget_blockers, select_runnable_tasks
from atticus.skills.registry import list_skills, load_skill
from atticus.status.inspect import inspect_record
from atticus.status.report import generate_status
from atticus.tools.registry import list_tools
from atticus.validation.gates import run_validation
from atticus.verifier import verify_candidate
from atticus.workflows.registry import list_workflows, load_workflow, plan_workflow
from atticus.workers.outputs import reject_candidate_output
from atticus.workers.runtime import execute_local_work_order
from atticus.workers.work_order import build_work_order


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
    source_id: list[str] | None
    artifact_id: list[str] | None


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
    _ = schedule.add_argument("--capacity", type=int, default=5)
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
    _ = set_provider_policy.add_argument("--estimated-cost-usd", dest="estimated_cost_usd", type=float, default=0.0)
    _add_fallback_mode_args(set_provider_policy)
    _ = set_provider_policy.add_argument("--write", action="store_true", help="write normalized provider policy to queued tasks")

    model_policy = sub.add_parser("model-policy", help="validate or resolve a model routing policy file")
    _ = model_policy.add_argument("action", choices=["validate", "resolve"])
    _ = model_policy.add_argument("--policy-file", required=True)
    _ = model_policy.add_argument("--layer", default="")
    _ = model_policy.add_argument("--stage", default="")
    _ = model_policy.add_argument("--task-type", default="")
    _ = model_policy.add_argument("--task-id", default="")

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
    _ = live_resume.add_argument("--capacity", type=int, default=15)
    _ = live_resume.add_argument("--model", default="deepseek/deepseek-v4-pro", help="OpenRouter model to probe for live resume")
    _ = live_resume.add_argument("--probe", action="store_true", help="run a live OpenRouter probe before planning")
    _ = live_resume.add_argument("--probe-result-json", help="preverified provider probe JSON from provider-probe")
    _ = live_resume.add_argument("--write-leases", action="store_true")
    _ = live_resume.add_argument("--worker-prefix", default="atticus-openrouter")

    free_loop = sub.add_parser("run-free-loop", help="run bounded OpenRouter-free autonomous supervisor ticks")
    _ = free_loop.add_argument("--db", required=True)
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
    _ = attention.add_argument("--add", action="store_true")
    _ = attention.add_argument("--target-type", default="manual")
    _ = attention.add_argument("--target-id", default="manual")
    _ = attention.add_argument("--severity", default="info")
    _ = attention.add_argument("--reason", default="")

    migrate = sub.add_parser("migrate-report", help="dry-run migration report for legacy workspace")
    _ = migrate.add_argument("--workspace", required=True)
    _ = migrate.add_argument("--db")
    _ = migrate.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = migrate.add_argument("--write", dest="dry_run", action="store_false", help="persist report metadata")

    doctor = sub.add_parser("doctor", help="safety and schema diagnostics")
    _ = doctor.add_argument("--db", required=True)

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
        report = generate_status(args.db)
        print_json(report.__dict__)
        return 0

    if args.command == "inspect":
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
                runnable, blocked = _schedule_preview(conn, capacity=args.capacity)
            print_json({"dry_run": True, "runnable": runnable, "blocked": blocked})
        else:
            with repo.db_connection(args.db) as conn:
                runnable_rows = select_runnable_tasks(conn, capacity=args.capacity)
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
        if args.policy_file:
            with repo.db_connection(args.db, read_only=not args.write) as conn:
                result = _set_model_policy_for_matter_from_file(
                    conn,
                    matter_scope=args.matter,
                    policy_file=args.policy_file,
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
        if args.policy_file is None:
            raise ValueError("model-policy requires --policy-file")
        policy = load_model_routing_policy(Path(args.policy_file))
        if args.action == "validate":
            print_json({"ok": True, "policy": policy.as_dict()})
            return 0
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
        with repo.db_connection(args.db, read_only=True) as conn:
            if args.action == "list":
                print_json(
                    {
                        "matter_scope": args.matter,
                        "sessions": repo.list_sessions(conn, matter_scope=args.matter, status=args.status),
                    }
                )
                return 0
            if not args.session_id:
                raise ValueError(f"session {args.action} requires SESSION_ID")
            export = repo.export_session(conn, session_id=args.session_id)
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
        with repo.db_connection(args.db, read_only=not args.write_leases) as conn:
            plan = prepare_live_resume(
                conn,
                capacity=args.capacity,
                env=env,
                probe_result=probe_result,
                write_leases=args.write_leases,
                worker_prefix=args.worker_prefix,
            )
        print_json(plan)
        return 0 if plan["ready"] else 2

    if args.command == "run-free-loop":
        with repo.db_connection(args.db) as conn:
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
        with repo.db_connection(args.db) as conn:
            if args.add:
                attention_id = repo.record_human_attention(
                    conn,
                    target_type=args.target_type,
                    target_id=args.target_id,
                    severity=args.severity,
                    reason=args.reason,
                )
                print_json({"attention_id": attention_id})
            else:
                rows = [
                    _row_to_dict(row)
                    for row in conn.execute(
                        "SELECT * FROM human_attention WHERE status = 'open' ORDER BY attention_id DESC LIMIT 50"
                    )
                ]
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
        with repo.db_connection(args.db, read_only=True) as conn:
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


def _schedule_preview(conn: sqlite3.Connection, *, capacity: int) -> tuple[list[JsonObject], list[JsonObject]]:
    capacity_requested = max(0, capacity)
    runnable: list[JsonObject] = []
    blocked: list[JsonObject] = []
    task_rows = cast(Iterable[sqlite3.Row], conn.execute(
        """
        SELECT * FROM tasks
        WHERE status IN ('queued', 'ready', 'blocked')
        ORDER BY expected_value DESC, created_at ASC
        """
    ))
    for task in task_rows:
        result = evaluate_task_gates(conn, cast(Mapping[str, object], cast(object, task)))
        blockers = result.reasons + budget_blockers(conn, task)
        if blockers:
            blocked.append({"task_id": _row_str(task, "task_id"), "title": _row_str(task, "title"), "reasons": blockers})
        elif len(runnable) < capacity_requested:
            runnable.append(_task_summary(cast(Mapping[str, object], cast(object, task))))
    return runnable, blocked


def _set_model_policy_for_matter_from_file(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    policy_file: str,
    dry_run: bool,
) -> JsonObject:
    policy = load_model_routing_policy(Path(policy_file))
    rows = [
        cast(Mapping[str, object], row)
        for row in conn.execute(
            """
            SELECT task_id, stage, task_type, provider_policy_json
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
        provider_policy = provider_policy_for_route(
            policy,
            layer="worker",
            stage=str(row["stage"]),
            task_type=str(row["task_type"]),
            task_id=str(row["task_id"]),
        )
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
            payload={"policy_file": str(Path(policy_file).resolve()), "task_ids": [str(row["task_id"]) for row in rows]},
        )
    return {
        "dry_run": dry_run,
        "matter_scope": matter_scope,
        "policy_file": str(Path(policy_file).resolve()),
        "tasks_matched": len(rows),
        "tasks_updated": 0 if dry_run else len(changed),
        "task_ids": [str(row["task_id"]) for row in rows],
        "resolved": resolved,
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


def _count_table(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
    return int(str(_row_value(row, "n", 0)))


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


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
