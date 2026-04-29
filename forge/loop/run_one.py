"""One complete Forge autonomous improvement iteration."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
from typing import Any

from forge.audit.packet import AuditPacket, DiffStats, EngineResult, GateResult, ReviewerVerdict
from forge.audit.writer import write_audit_packet
from forge.config import MODEL_FLASH, ForgeConfig, load_config
from forge.council.domain import domain_review
from forge.council.judge import judge
from forge.council.minimalist import minimalist_review
from forge.council.reviewer import review_diff
from forge.council.security import security_review
from forge.engines.claude_code_style import ClaudeCodeStyleEngine
from forge.engines.shell import ShellEngine
from forge.gates.commands import run_gate_commands
from forge.gates.policy import run_policy_gates
from forge.loop.finalise import commit_approved_work, delete_rejected_branch
from forge.loop.propose import propose_tasks
from forge.loop.select import select_task
from forge.loop.task import TaskPacket
from forge.memory.store import append_decision, append_failed_attempt
from forge.state import ensure_forge_dirs, now_iso, update_state
from forge.worktrees.cleanup import cleanup_worktree
from forge.worktrees.diff import changed_files, collect_diff, diff_stats
from forge.worktrees.manager import WorktreeInfo, create_worktree, current_branch, ensure_clean_main_worktree, ensure_git_repo


def run_one(
    repo: Path,
    *,
    policy: str = "default",
    engine_command: str | None = None,
    shell_engine_command: str | None = None,
    offline_review: bool = False,
    require_openrouter_key: bool = True,
) -> AuditPacket:
    repo = repo.resolve()
    config = load_config(repo, policy=policy, engine_command=engine_command)
    ensure_forge_dirs(repo)
    if require_openrouter_key and not offline_review and not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for Forge reviewer/model operations")
    ensure_git_repo(repo)
    ensure_clean_main_worktree(repo)
    task = select_task(propose_tasks(repo, config))
    started = now_iso()
    packet = AuditPacket(
        iteration_id=task.id,
        timestamp_start=started,
        target_repo=str(repo),
        base_branch=current_branch(repo),
        task=task.as_dict(),
        engine={"name": "claude_code_style", "model": MODEL_FLASH, "provider": "openrouter"},
    )
    update_state(repo, running=True, current_task=task.as_dict(), last_iteration=task.id)
    worktree: WorktreeInfo | None = None
    engine_result = EngineResult(engine="not_started", exit_code=1)
    all_gates: list[GateResult] = []
    verdicts: list[ReviewerVerdict] = []
    diff = ""
    test_output = ""
    committed = False
    state_failure_count: int | None = None
    try:
        worktree = create_worktree(repo, task_title=task.title)
        packet.branch_name = worktree.branch_name
        packet.worktree_path = str(worktree.path)
        update_state(repo, last_branch=worktree.branch_name)
        engine = ShellEngine(shell_engine_command, timeout_seconds=config.engine_timeout_seconds) if shell_engine_command else ClaudeCodeStyleEngine(config.engine_command, config.engine_timeout_seconds)
        engine_result = engine.run(task, worktree.path)
        prompt_path = worktree.path / ".forge_task.md"
        if prompt_path.exists():
            prompt_path.unlink()
        diff, files, stats, all_gates, verdicts, final_judge, test_output = _evaluate_worktree(
            worktree.path,
            task=task,
            config=config,
            engine_result=engine_result,
            offline_review=offline_review,
        )
        repair_attempts = 0
        while final_judge.verdict != "approve" and _needs_repair(verdicts) and repair_attempts < config.loop.max_repair_attempts:
            repair_attempts += 1
            repair_task = TaskPacket(
                id=task.id,
                title=f"Repair {task.title}",
                reason=f"{task.reason}\n\nRepair feedback:\n{_repair_feedback(verdicts)}",
                risk=task.risk,
                value=task.value,
                estimated_diff_lines=task.estimated_diff_lines,
                allowed_paths=task.allowed_paths,
                forbidden_paths=task.forbidden_paths,
                required_checks=task.required_checks,
                success_criteria=task.success_criteria,
                score=task.score,
            )
            repair_result = engine.run(repair_task, worktree.path)
            prompt_path = worktree.path / ".forge_task.md"
            if prompt_path.exists():
                prompt_path.unlink()
            engine_result = _merge_engine_results(engine_result, repair_result, label=f"repair-{repair_attempts}")
            diff, files, stats, all_gates, verdicts, final_judge, test_output = _evaluate_worktree(
                worktree.path,
                task=task,
                config=config,
                engine_result=engine_result,
                offline_review=offline_review,
            )
        packet.gate_results = [asdict(gate) for gate in all_gates]
        packet.commands_run = [asdict(gate) for gate in all_gates if gate.command]
        packet.reviewer_verdicts = [asdict(verdict) for verdict in verdicts]
        packet.risk_score = _risk_score(verdicts, stats)
        packet.usage = _usage_summary(verdicts)
        packet.cost = dict(packet.usage)
        packet.changed_files = files
        packet.diff_stats = stats
        if final_judge.verdict == "approve" and config.auto_commit:
            packet.commit_sha = commit_approved_work(worktree.path, task)
            packet.final_decision = "repaired_then_committed" if repair_attempts else "committed"
            committed = True
        else:
            packet.final_decision = "rejected"
        packet.timestamp_end = now_iso()
        append_decision(repo, f"- {packet.timestamp_end}: {packet.final_decision} `{packet.branch_name}` for {task.title}")
        if packet.final_decision not in {"committed", "repaired_then_committed"}:
            append_failed_attempt(repo, {"iteration_id": task.id, "branch": packet.branch_name, "task": task.title, "decision": packet.final_decision, "timestamp": packet.timestamp_end})
            state_failure_count = 1
        else:
            state_failure_count = 0
        return packet
    except Exception as exc:
        packet.final_decision = "failed"
        packet.timestamp_end = now_iso()
        packet.reviewer_verdicts = [asdict(ReviewerVerdict(role="system", verdict="reject", confidence=1.0, risk_level="high", blocking_issues=[str(exc)]))]
        append_failed_attempt(repo, {"iteration_id": task.id, "task": task.title, "decision": "failed", "error": str(exc), "timestamp": packet.timestamp_end})
        state_failure_count = 1
        raise
    finally:
        if worktree is not None:
            packet.cleanup = cleanup_worktree(repo, worktree.path)
            if not committed:
                delete_rejected_branch(repo, worktree.branch_name)
        if not packet.timestamp_end:
            packet.timestamp_end = now_iso()
        write_audit_packet(repo, packet, engine_result=engine_result, diff=diff, test_output=test_output)
        update_state(
            repo,
            running=False,
            current_task=None,
            last_iteration=packet.iteration_id,
            last_branch=packet.branch_name,
            last_commit_sha=packet.commit_sha,
            consecutive_failures=state_failure_count if state_failure_count is not None else 0,
        )


def _test_output(gates: list[GateResult]) -> str:
    chunks: list[str] = []
    for gate in gates:
        if gate.command:
            chunks.append(f"$ {gate.command}\nexit: {'0' if gate.passed else '1'}\n{gate.stdout}\n{gate.stderr}")
    return "\n\n".join(chunks)


def _evaluate_worktree(
    worktree: Path,
    *,
    task: TaskPacket,
    config: ForgeConfig,
    engine_result: EngineResult,
    offline_review: bool,
) -> tuple[str, list[str], DiffStats, list[GateResult], list[ReviewerVerdict], ReviewerVerdict, str]:
    engine_output = f"{engine_result.stdout}\n{engine_result.stderr}"
    command_gates = run_gate_commands(worktree, task.required_checks or config.required_checks)
    diff = collect_diff(worktree)
    files = changed_files(worktree)
    stats = diff_stats(worktree)
    policy_gates = run_policy_gates(changed_files=files, diff=diff, stats=stats, engine_output=engine_output, config=config, allowed_paths=task.allowed_paths)
    gates = [GateResult(name="engine exit", passed=engine_result.exit_code == 0, details=f"exit {engine_result.exit_code}"), *policy_gates, *command_gates]
    deterministic_passed = all(gate.passed for gate in gates)
    general = review_diff(task=task, changed_files=files, diff=diff, gate_results=gates, config=config, offline=offline_review or not deterministic_passed)
    security = security_review(diff=diff, changed_files=files, engine_output=engine_output)
    minimalist = minimalist_review(stats=stats, changed_files=files)
    verdicts = [general, security, minimalist]
    if config.policy_name == "atticus" or any(path.startswith("atticus/") for path in files):
        verdicts.append(domain_review(diff=diff, changed_files=files))
    final_judge = judge(gates, verdicts)
    verdicts.append(final_judge)
    return diff, files, stats, gates, verdicts, final_judge, _test_output(gates)


def _needs_repair(verdicts: list[ReviewerVerdict]) -> bool:
    return any(verdict.verdict == "repair" or verdict.recommended_repairs for verdict in verdicts if verdict.role != "security_reviewer")


def _repair_feedback(verdicts: list[ReviewerVerdict]) -> str:
    lines: list[str] = []
    for verdict in verdicts:
        for issue in verdict.blocking_issues + verdict.recommended_repairs:
            lines.append(f"- {verdict.role}: {issue}")
    return "\n".join(lines) or "- Keep the diff small and satisfy all failed gates."


def _merge_engine_results(first: EngineResult, second: EngineResult, *, label: str) -> EngineResult:
    return EngineResult(
        engine=first.engine,
        exit_code=second.exit_code,
        stdout=f"{first.stdout}\n\n[{label} stdout]\n{second.stdout}".strip(),
        stderr=f"{first.stderr}\n\n[{label} stderr]\n{second.stderr}".strip(),
        duration_seconds=first.duration_seconds + second.duration_seconds,
        changed_files=second.changed_files,
        new_files=second.new_files,
        deleted_files=second.deleted_files,
        git_diff=second.git_diff,
    )


def _risk_score(verdicts: list[ReviewerVerdict], stats: object) -> float:
    score = 0.0
    for verdict in verdicts:
        if verdict.risk_level == "medium":
            score += 1.5
        if verdict.risk_level == "high":
            score += 4.0
        score += len(verdict.blocking_issues) * 2.0
    files_changed = getattr(stats, "files_changed", 0)
    return round(score + float(files_changed) * 0.2, 2)


def _usage_summary(verdicts: list[ReviewerVerdict]) -> dict[str, Any]:
    summary: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "total_cost_usd": 0.0}
    for verdict in verdicts:
        usage = verdict.usage if isinstance(verdict.usage, dict) else {}
        summary["prompt_tokens"] += _usage_int(usage, "prompt_tokens")
        summary["completion_tokens"] += _usage_int(usage, "completion_tokens")
        summary["total_tokens"] += _usage_int(usage, "total_tokens")
        summary["cached_tokens"] += _usage_cached_tokens(usage)
        summary["total_cost_usd"] += _usage_float(usage, "total_cost_usd") or _usage_float(usage, "total_cost") or _usage_float(usage, "cost")
    return summary


def _usage_int(usage: dict[str, Any], key: str) -> int:
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _usage_float(usage: dict[str, Any], key: str) -> float:
    try:
        return float(usage.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _usage_cached_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        return _usage_int(details, "cached_tokens") or _usage_int(details, "cache_read_tokens")
    return _usage_int(usage, "cached_tokens") or _usage_int(usage, "cache_read_input_tokens")
