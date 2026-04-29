"""Diff size gates."""

from __future__ import annotations

from forge.audit.packet import DiffStats, GateResult
from forge.config import DiffLimits


def check_diff_limits(stats: DiffStats, limits: DiffLimits) -> GateResult:
    total = stats.lines_added + stats.lines_deleted
    failures: list[str] = []
    if stats.files_changed > limits.max_files_changed:
        failures.append(f"files changed {stats.files_changed} > {limits.max_files_changed}")
    if total > limits.max_diff_lines:
        failures.append(f"diff lines {total} > {limits.max_diff_lines}")
    if stats.lines_deleted > limits.max_deleted_lines:
        failures.append(f"deleted lines {stats.lines_deleted} > {limits.max_deleted_lines}")
    return GateResult(name="diff limits", passed=not failures, details="\n".join(failures) if failures else "diff within configured limits")
