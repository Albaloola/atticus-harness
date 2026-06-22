"""Evidence ingest CLI subcommand handler.

Provides the `evidence-ingest` command group with subcommands for
scanning, analysing, resolving, planning, executing, and registering evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from atticus.tools.registry import ToolContext

# Module function imports – these modules are implemented separately.
from atticus.evidence_ingest.scanner import scan_source_directory
from atticus.evidence_ingest.analyser import analyse_scanned_files
from atticus.evidence_ingest.resolver import resolve_analysis_results
from atticus.evidence_ingest.executor import execute_resolution_plan
from atticus.evidence_ingest.register import register_evidence

# Validator for plan validation subcommand
from atticus.evidence_ingest.validator import run_all_validations


JsonObject = dict[str, object]


def _print_json(result: object) -> None:
    """Print result as JSON to stdout.

    Args:
        result: Object to serialise as JSON.
    """
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _handle_scan(args: argparse.Namespace) -> int:
    """Handle the scan subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()
        source_dir = Path(args.source_dir).resolve()

        context = ToolContext(
            stage="evidence-ingest-scan",
            workspace_path=workspace,
        )

        result: JsonObject = scan_source_directory(
            source_dir=source_dir,
            workspace=workspace,
            context=context,
        )
        _print_json(result)
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _handle_analyse(args: argparse.Namespace) -> int:
    """Handle the analyse subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()
        source_dir = Path(args.source_dir).resolve()

        context = ToolContext(
            stage="evidence-ingest-analyse",
            workspace_path=workspace,
        )

        result: JsonObject = analyse_scanned_files(
            source_dir=source_dir,
            workspace=workspace,
            context=context,
            provider=getattr(args, "provider", None),
            model=getattr(args, "model", None),
        )
        _print_json(result)
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _handle_resolve(args: argparse.Namespace) -> int:
    """Handle the resolve subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()

        context = ToolContext(
            stage="evidence-ingest-resolve",
            workspace_path=workspace,
        )

        result: JsonObject = resolve_analysis_results(
            workspace=workspace,
            context=context,
            provider=getattr(args, "provider", None),
            model=getattr(args, "model", None),
        )
        _print_json(result)
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _handle_plan_validate(args: argparse.Namespace) -> int:
    """Handle the plan validate subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()

        # Load scan results and resolution plan
        scan_path = workspace / "02-registers" / "raw_inventory.json"
        plan_path = workspace / "02-registers" / "resolution_plan.json"

        if not scan_path.exists():
            raise FileNotFoundError(f"Scan results not found: {scan_path}")
        if not plan_path.exists():
            raise FileNotFoundError(f"Resolution plan not found: {plan_path}")

        with open(scan_path, "r", encoding="utf-8") as f:
            scan_results = json.load(f)
        with open(plan_path, "r", encoding="utf-8") as f:
            resolution_plan = json.load(f)

        result = run_all_validations(scan_results, resolution_plan)
        _print_json(result)
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _handle_plan_inspect(args: argparse.Namespace) -> int:
    """Handle the plan inspect subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()
        plan_path = workspace / "02-registers" / "resolution_plan.json"

        if not plan_path.exists():
            raise FileNotFoundError(f"Resolution plan not found: {plan_path}")

        with open(plan_path, "r", encoding="utf-8") as f:
            plan = json.load(f)

        flagged = plan.get("needs_human_review", [])
        _print_json({"flagged_items": flagged, "count": len(flagged)})
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _handle_plan_override(args: argparse.Namespace) -> int:
    """Handle the plan override subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()
        plan_path = workspace / "02-registers" / "resolution_plan.json"

        if not plan_path.exists():
            raise FileNotFoundError(f"Resolution plan not found: {plan_path}")

        with open(plan_path, "r", encoding="utf-8") as f:
            plan = json.load(f)

        # Apply overrides based on --source-id and --override-json
        source_id = args.source_id
        override_json = args.override_json

        overrides = json.loads(override_json) if override_json else {}

        if source_id:
            # Find and update the specific source
            sources = plan.get("sources", [])
            updated = False
            for source in sources:
                if source.get("source_id") == source_id:
                    source.update(overrides)
                    updated = True
                    break
            if not updated:
                raise ValueError(f"Source '{source_id}' not found in plan")

        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, sort_keys=True)

        _print_json({"status": "overridden", "source_id": source_id, "overrides": overrides})
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _handle_plan_accept(args: argparse.Namespace) -> int:
    """Handle the plan accept subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()
        plan_path = workspace / "02-registers" / "resolution_plan.json"
        accept_path = workspace / "02-registers" / "plan_accepted.flag"

        if not plan_path.exists():
            raise FileNotFoundError(f"Resolution plan not found: {plan_path}")

        # Validate before accepting
        scan_path = workspace / "02-registers" / "raw_inventory.json"
        if scan_path.exists():
            with open(scan_path, "r", encoding="utf-8") as f:
                scan_results = json.load(f)
            with open(plan_path, "r", encoding="utf-8") as f:
                resolution_plan = json.load(f)

            validation = run_all_validations(scan_results, resolution_plan)
            if validation["status"] != "ALL_CLEAR":
                _print_json({"status": "rejected", "validation": validation})
                return 1
        else:
            with open(plan_path, "r", encoding="utf-8") as f:
                resolution_plan = json.load(f)

        # Add acceptance metadata to the plan itself
        import datetime as _dt
        resolution_plan["metadata"] = resolution_plan.get("metadata", {})
        resolution_plan["metadata"]["accepted_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        resolution_plan["metadata"]["accepted_by"] = "human"

        # Save updated plan and create acceptance flag
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(resolution_plan, f, indent=2, sort_keys=True)

        accept_path.parent.mkdir(parents=True, exist_ok=True)
        with open(accept_path, "w", encoding="utf-8") as f:
            json.dump({
                "accepted": True,
                "timestamp": resolution_plan["metadata"]["accepted_at"],
            }, f, indent=2)

        _print_json({"status": "accepted", "flag_file": str(accept_path)})
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _handle_execute(args: argparse.Namespace) -> int:
    """Handle the execute subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()
        dry_run = getattr(args, "dry_run", True)

        context = ToolContext(
            stage="evidence-ingest-execute",
            workspace_path=workspace,
        )

        result: JsonObject = execute_resolution_plan(
            workspace=workspace,
            context=context,
            dry_run=dry_run,
        )
        _print_json(result)
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _handle_register(args: argparse.Namespace) -> int:
    """Handle the register subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    try:
        workspace = Path(args.workspace).resolve()
        db_path = Path(args.db).resolve() if args.db else None

        context = ToolContext(
            stage="evidence-ingest-register",
            workspace_path=workspace,
            db_path=db_path,
        )

        result: JsonObject = register_evidence(
            workspace=workspace,
            context=context,
            db_path=db_path,
            provider=getattr(args, "provider", None),
            model=getattr(args, "model", None),
        )
        _print_json(result)
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def add_evidence_ingest_subparser(subparsers: Any) -> None:
    """Add the evidence-ingest command group to the main parser.

    Args:
        subparsers: Subparsers object from the main argument parser.
    """
    # Main evidence-ingest parser
    parser = subparsers.add_parser(
        "evidence-ingest",
        help="Evidence ingestion: scan, analyse, resolve, plan, execute, register",
    )
    evidence_sub = parser.add_subparsers(dest="evidence_action", required=True)

    # scan subcommand
    scan_parser = evidence_sub.add_parser(
        "scan",
        help="Scan source directory and produce raw_inventory.json",
    )
    _ = scan_parser.add_argument("--workspace", required=True, help="Workspace root path")
    _ = scan_parser.add_argument("--source-dir", required=True, help="Source directory to scan")
    scan_parser.set_defaults(func=_handle_scan)

    # analyse subcommand
    analyse_parser = evidence_sub.add_parser(
        "analyse",
        help="AI-driven analysis of scanned files",
    )
    _ = analyse_parser.add_argument("--workspace", required=True, help="Workspace root path")
    _ = analyse_parser.add_argument("--source-dir", required=True, help="Source directory to analyse")
    _ = analyse_parser.add_argument("--provider", default=None, help="AI provider name")
    _ = analyse_parser.add_argument("--model", default=None, help="AI model name")
    analyse_parser.set_defaults(func=_handle_analyse)

    # resolve subcommand
    resolve_parser = evidence_sub.add_parser(
        "resolve",
        help="AI-driven resolution plan for analysed files",
    )
    _ = resolve_parser.add_argument("--workspace", required=True, help="Workspace root path")
    _ = resolve_parser.add_argument("--provider", default=None, help="AI provider name")
    _ = resolve_parser.add_argument("--model", default=None, help="AI model name")
    resolve_parser.set_defaults(func=_handle_resolve)

    # plan subcommand group
    plan_parser = evidence_sub.add_parser(
        "plan",
        help="Resolution plan operations: validate, inspect, override, accept",
    )
    plan_sub = plan_parser.add_subparsers(dest="plan_action", required=True)

    # plan validate
    plan_validate = plan_sub.add_parser(
        "validate",
        help="Validate resolution plan against scan results",
    )
    _ = plan_validate.add_argument("--workspace", required=True, help="Workspace root path")
    plan_validate.set_defaults(func=_handle_plan_validate)

    # plan inspect
    plan_inspect = plan_sub.add_parser(
        "inspect",
        help="Inspect flagged items in resolution plan",
    )
    _ = plan_inspect.add_argument("--workspace", required=True, help="Workspace root path")
    plan_inspect.set_defaults(func=_handle_plan_inspect)

    # plan override
    plan_override = plan_sub.add_parser(
        "override",
        help="Override specific decisions in resolution plan",
    )
    _ = plan_override.add_argument("--workspace", required=True, help="Workspace root path")
    _ = plan_override.add_argument("--source-id", default=None, help="Source ID to override")
    _ = plan_override.add_argument("--override-json", default=None, help="JSON string of overrides")
    plan_override.set_defaults(func=_handle_plan_override)

    # plan accept
    plan_accept = plan_sub.add_parser(
        "accept",
        help="Accept resolution plan for execution",
    )
    _ = plan_accept.add_argument("--workspace", required=True, help="Workspace root path")
    plan_accept.set_defaults(func=_handle_plan_accept)

    # execute subcommand
    execute_parser = evidence_sub.add_parser(
        "execute",
        help="Execute approved resolution plan",
    )
    _ = execute_parser.add_argument("--workspace", required=True, help="Workspace root path")
    _ = execute_parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    _ = execute_parser.add_argument("--write", dest="dry_run", action="store_false", help="Apply changes")
    execute_parser.set_defaults(func=_handle_execute)

    # register subcommand
    register_parser = evidence_sub.add_parser(
        "register",
        help="Generate registry and call seed-matter",
    )
    _ = register_parser.add_argument("--workspace", required=True, help="Workspace root path")
    _ = register_parser.add_argument("--db", required=True, help="Database path")
    _ = register_parser.add_argument("--provider", default=None, help="AI provider name")
    _ = register_parser.add_argument("--model", default=None, help="AI model name")
    register_parser.set_defaults(func=_handle_register)
