"""Auditable command registry for the Atticus CLI surface."""

from __future__ import annotations

from functools import lru_cache

from atticus.commands.types import CommandDef


@lru_cache(maxsize=1)
def list_commands() -> tuple[CommandDef, ...]:
    return tuple(sorted(_commands(), key=lambda command: command.name))


def command_by_name(name: str) -> CommandDef:
    for command in list_commands():
        if command.name == name:
            return command
    for command in list_commands():
        if name in command.aliases:
            return command
    raise KeyError(f"unknown command: {name}")


def _commands() -> tuple[CommandDef, ...]:
    return (
        CommandDef("ask", "Read-only legal memory query.", read_only_safe=True),
        CommandDef("budget", "View, set, or check budget gates.", supports_dry_run=True),
        CommandDef("bad-fixtures", "Run historical bad fixture regression suites.", read_only_safe=True),
        CommandDef("cache-health", "Report prompt/cache observability and explain cache breaks.", read_only_safe=True),
        CommandDef("certify", "Issue a certification after validation.", requires_write=True),
        CommandDef("citation-support", "Verify candidate citation quote/span/proposition support.", supports_dry_run=True),
        CommandDef("command", "Show command metadata.", read_only_safe=True),
        CommandDef("commands", "List command metadata.", read_only_safe=True),
        CommandDef("context", "Read-only context diagnostics for legal audit.", read_only_safe=True),
        CommandDef("coordinator", "Plan or create self-contained legal coordinator task graphs.", supports_dry_run=True),
        CommandDef("doctor", "Safety and schema diagnostics.", read_only_safe=True),
        CommandDef("extract-sources", "Extract or OCR matter-local sources without provider calls.", supports_dry_run=True),
        CommandDef("final-gate", "Inspect and repair deterministic final gate readiness.", supports_dry_run=True),
        CommandDef("human-attention", "List or add human attention items.", supports_dry_run=False),
        CommandDef("import-candidates", "Import legacy material as candidate artifacts.", supports_dry_run=True),
        CommandDef("init", "Initialize an Atticus SQLite database.", requires_write=True),
        CommandDef("inspect", "Read-only record inspection.", read_only_safe=True),
        CommandDef("lease", "Acquire a fenced task lease.", supports_dry_run=True, requires_write=True),
        CommandDef("live-resume", "Prepare safe live OpenRouter leases.", supports_dry_run=True),
        CommandDef("memory", "List, show, extract, consolidate, reject, or mark stale typed legal memory.", supports_dry_run=True),
        CommandDef("maintenance", "Run isolated control-plane maintenance diagnostics and reports.", supports_dry_run=True),
        CommandDef("matter-health", "Authoritative matter completion, blocker owners, and next action.", read_only_safe=True),
        CommandDef("matter-profile", "Show, propose, apply, or reset matter-local adaptive stage profiles.", supports_dry_run=True),
        CommandDef("migrate-report", "Build migration report for legacy workspace.", supports_dry_run=True),
        CommandDef("model-policy", "Validate or resolve a model routing policy file.", read_only_safe=True),
        CommandDef("next-action", "Show the exact next safe action for an incomplete matter.", read_only_safe=True),
        CommandDef("orchestrator", "Status, tick, failures, and repair proposals for matter orchestrators.", supports_dry_run=True),
        CommandDef("policy-check", "Check provider/model fallback policy.", read_only_safe=True, aliases=("provider-policy",)),
        CommandDef("provider-probe", "Make a tiny OpenRouter probe before live resume.", requires_live=True),
        CommandDef("provider-policy", "Check provider/model fallback policy.", read_only_safe=True, aliases=("policy-check",)),
        CommandDef("provider-health", "Group provider control-plane health by provider policy.", supports_dry_run=True),
        CommandDef("rebuild-search-index", "Rebuild durable legal-memory search projection.", supports_dry_run=True),
        CommandDef("reconcile-foundation", "Validate or certify foundation before live resume.", supports_dry_run=True),
        CommandDef("reduce", "Reduce a candidate through reducer-only canonical path.", supports_dry_run=True, requires_write=True),
        CommandDef("reject-candidate", "Quarantine a candidate packet.", supports_dry_run=True),
        CommandDef("repairs", "List, show, and advance deterministic repair plans.", supports_dry_run=True),
        CommandDef("reducer-review", "List, inspect, accept, or reject manual reducer reviews.", supports_dry_run=True),
        CommandDef("runbook", "Export operator runbook with exact next action, blockers, provider taxonomy, reducer queue, and stale warnings.", read_only_safe=True),
        CommandDef("run-free-loop", "Run bounded autonomous supervisor ticks.", requires_live=False, requires_write=True),
        CommandDef("run-local", "Execute a leased task through local stub adapter.", requires_write=True),
        CommandDef("schedule", "Dependency-aware scheduling preview or write.", supports_dry_run=True),
        CommandDef("seed-matter", "Seed or repair a matter from local inventory.", supports_dry_run=True),
        CommandDef("set-provider-policy", "Set provider/model policy on queued tasks.", supports_dry_run=True),
        CommandDef("skill", "List or show bundled worker skills.", read_only_safe=True),
        CommandDef("session", "List, show, resume, or export matter-scoped legal sessions.", read_only_safe=True),
        CommandDef("status", "Read-only run status.", read_only_safe=True),
        CommandDef("source-trace", "Trace quotes and citations to current source chunks/spans.", read_only_safe=True),
        CommandDef("supervisor", "Diagnose no-silent-idle next-action enforcement.", supports_dry_run=True),
        CommandDef("tools", "List Atticus legal tools.", read_only_safe=True),
        CommandDef("authority", "Verify authority currentness and proposition-support metadata.", supports_dry_run=True),
        CommandDef("validate", "Run a durable validation gate.", requires_write=True),
        CommandDef("verifier", "Run independent verifier checks against candidate packets.", supports_dry_run=True),
        CommandDef("workflow", "List, show, or run markdown legal workflows.", command_type="workflow", supports_dry_run=True),
        CommandDef("work-order", "Build a bounded worker work order.", supports_dry_run=True),
        CommandDef("work-run", "Start, resume, export, and reuse durable matter work runs.", supports_dry_run=True),
    )
