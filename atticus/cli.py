"""Command line interface for Atticus Harness."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from atticus.config import DEFAULT_DB_PATH
from atticus.db import repo
from atticus.graph.certifications import CertificationBlocked, certify_subject
from atticus.migration.import_old_run import import_candidates
from atticus.migration.reconcile import reconcile_foundation
from atticus.migration.report import build_migration_report
from atticus.providers.budget import budget_status, check_budget
from atticus.providers.live_readiness import probe_live_openrouter
from atticus.providers.policy import (
    ProviderActual,
    ProviderRequest,
    check_provider_policy,
    record_provider_policy_decision,
)
from atticus.reducer.reducer import reduce_candidate
from atticus.retrieval.ask import answer_question
from atticus.scheduler.gates import evaluate_task_gates
from atticus.scheduler.lease import LeaseError, acquire_lease, expire_leases
from atticus.scheduler.live_orchestrator import prepare_live_resume
from atticus.scheduler.planner import _budget_blockers, select_runnable_tasks
from atticus.status.inspect import inspect_record
from atticus.status.report import generate_status
from atticus.validation.gates import run_validation
from atticus.workers.runtime import execute_local_work_order
from atticus.workers.work_order import build_work_order


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atticus")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize an Atticus SQLite database")
    init.add_argument("--db", default=str(DEFAULT_DB_PATH))

    status = sub.add_parser("status", help="read-only run status")
    status.add_argument("--db", required=True)

    inspect = sub.add_parser("inspect", help="read-only record inspection")
    inspect.add_argument("--db", required=True)
    inspect.add_argument("--type", required=True, choices=["run", "task", "source", "artifact", "candidate", "context-pack", "certification"])
    inspect.add_argument("--id", required=True)

    ask = sub.add_parser("ask", help="read-only legal memory query")
    ask.add_argument("question")
    ask.add_argument("--db", required=True)

    imp = sub.add_parser("import-candidates", help="import legacy material as candidate artifacts")
    imp.add_argument("--workspace", required=True)
    imp.add_argument("--db", required=True)
    imp.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    imp.add_argument("--write", dest="dry_run", action="store_false", help="actually write candidate artifacts and validation tasks")

    validate = sub.add_parser("validate", help="run a durable validation gate")
    validate.add_argument("--db", required=True)
    validate.add_argument("--gate", required=True)
    validate.add_argument("--target-type", required=True)
    validate.add_argument("--target-id", required=True)

    certify = sub.add_parser("certify", help="issue a certification after a passing validation")
    certify.add_argument("--db", required=True)
    certify.add_argument("--subject-type", required=True)
    certify.add_argument("--subject-id", required=True)
    certify.add_argument("--type", "--certification-type", dest="certification_type", required=True)
    certify.add_argument("--validator", default="atticus-cli")

    schedule = sub.add_parser("schedule", help="dependency-aware scheduling preview or write")
    schedule.add_argument("--db", required=True)
    schedule.add_argument("--capacity", type=int, default=5)
    schedule.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    schedule.add_argument("--write", dest="dry_run", action="store_false", help="persist blocked reasons")

    lease = sub.add_parser("lease", help="acquire a fenced task lease without launching a worker")
    lease.add_argument("--db", required=True)
    lease.add_argument("--task-id", required=True)
    lease.add_argument("--worker-id", default="atticus-cli")
    lease.add_argument("--seconds", type=int, default=900)
    lease.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    lease.add_argument("--write", dest="dry_run", action="store_false", help="write the lease")

    work_order = sub.add_parser("work-order", help="build a bounded worker work order; never launches workers")
    work_order.add_argument("--db", required=True)
    work_order.add_argument("--task-id", required=True)
    work_order.add_argument("--lease-id")
    work_order.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    work_order.add_argument("--write-context", dest="dry_run", action="store_false", help="persist the context pack")

    run_local = sub.add_parser("run-local", help="execute a leased task through the local stub adapter only")
    run_local.add_argument("--db", required=True)
    run_local.add_argument("--task-id", required=True)
    run_local.add_argument("--lease-id", required=True)
    run_local.add_argument("--worker-id", default="atticus-local")
    run_local.add_argument("--output-dir", required=True)
    run_local.add_argument("--write", action="store_true", help="actually record the local candidate output")

    reduce = sub.add_parser("reduce", help="reduce a candidate packet through reducer-only canonical path")
    reduce.add_argument("--db", required=True)
    reduce.add_argument("--candidate-id", required=True)
    reduce.add_argument("--lease-id", required=True)
    reduce.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    reduce.add_argument("--write", dest="dry_run", action="store_false", help="write reducer decision/canonical artifact")

    budget = sub.add_parser("budget", help="view, set, or check budget gates")
    budget.add_argument("--db", required=True)
    budget.add_argument("--scope-type", default="matter")
    budget.add_argument("--scope-id", default="atticus")
    budget.add_argument("--limit", type=float)
    budget.add_argument("--check", type=float, default=0.0)
    budget.add_argument("--write", action="store_true")

    provider_policy = sub.add_parser("provider-policy", help="check provider/model fallback policy")
    _add_provider_policy_args(provider_policy)

    provider_probe = sub.add_parser("provider-probe", help="make a tiny OpenRouter probe before live resume")
    provider_probe.add_argument("--provider", default="openrouter")
    provider_probe.add_argument("--model", required=True)
    provider_probe.add_argument("--allow-fallback", action="store_true")

    live_resume = sub.add_parser("live-resume", help="prepare safe live OpenRouter leases without launching workers")
    live_resume.add_argument("--db", required=True)
    live_resume.add_argument("--capacity", type=int, default=15)
    live_resume.add_argument("--model", default="deepseek/deepseek-v4-pro", help="OpenRouter model to probe for live resume")
    live_resume.add_argument("--probe", action="store_true", help="run a live OpenRouter probe before planning")
    live_resume.add_argument("--probe-result-json", help="preverified provider probe JSON from provider-probe")
    live_resume.add_argument("--write-leases", action="store_true")
    live_resume.add_argument("--worker-prefix", default="atticus-openrouter")

    reconcile = sub.add_parser("reconcile-foundation", help="validate/certify foundation before live resume")
    reconcile.add_argument("--db", required=True)
    reconcile.add_argument("--matter", default="atticus")
    reconcile.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    reconcile.add_argument("--write", dest="dry_run", action="store_false")
    reconcile.add_argument("--validator", default="atticus-cli")

    policy = sub.add_parser("policy-check", help="check provider/model fallback policy")
    _add_provider_policy_args(policy)

    attention = sub.add_parser("human-attention", help="list or add human attention items")
    attention.add_argument("--db", required=True)
    attention.add_argument("--add", action="store_true")
    attention.add_argument("--target-type", default="manual")
    attention.add_argument("--target-id", default="manual")
    attention.add_argument("--severity", default="info")
    attention.add_argument("--reason", default="")

    migrate = sub.add_parser("migrate-report", help="dry-run migration report for legacy workspace")
    migrate.add_argument("--workspace", required=True)
    migrate.add_argument("--db")
    migrate.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    migrate.add_argument("--write", dest="dry_run", action="store_false", help="persist report metadata")

    doctor = sub.add_parser("doctor", help="safety and schema diagnostics")
    doctor.add_argument("--db", required=True)

    return parser


def _add_provider_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--actual-provider")
    parser.add_argument("--actual-model")
    parser.add_argument("--allow-fallback", action="store_true")
    parser.add_argument("--db")
    parser.add_argument("--task-id")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        return _main(args)
    except (CertificationBlocked, LeaseError, KeyError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _main(args: argparse.Namespace) -> int:
    if args.command == "init":
        repo.initialize_database(args.db)
        with repo.db_connection(args.db) as conn:
            repo.upsert_run(conn, "default", "initialized", "database initialized")
        print(f"initialized {Path(args.db)}")
        return 0

    if args.command == "status":
        report = generate_status(args.db)
        print_json(report.__dict__)
        return 0

    if args.command == "inspect":
        print_json(inspect_record(args.db, record_type=args.type, record_id=args.id))
        return 0

    if args.command == "ask":
        answer = answer_question(args.db, args.question)
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
        with repo.db_connection(args.db) as conn:
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
                    dict(row)
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
        with repo.db_connection(args.db) as conn:
            expired = expire_leases(conn)
            tables = {
                name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                for name in ("events", "runs", "sources", "artifacts", "tasks", "leases", "human_attention")
            }
            schema_version = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"]
        print_json(
            {
                "ok": True,
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


def _schedule_preview(conn: Any, *, capacity: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runnable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for task in conn.execute(
        """
        SELECT * FROM tasks
        WHERE status IN ('queued', 'ready')
        ORDER BY expected_value DESC, created_at ASC
        """
    ):
        result = evaluate_task_gates(conn, task)
        blockers = result.reasons + _budget_blockers(conn, task)
        if blockers:
            blocked.append({"task_id": task["task_id"], "title": task["title"], "reasons": blockers})
        elif len(runnable) < capacity:
            runnable.append(_task_summary(task))
    return runnable, blocked


def _task_summary(row: Any) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "title": row["title"],
        "stage": row["stage"],
        "task_type": row["task_type"],
        "expected_value": row["expected_value"],
    }


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
