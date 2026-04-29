"""Policy gate orchestration."""

from __future__ import annotations

from forge.audit.packet import GateResult
from forge.config import ForgeConfig
from forge.gates.diff_limits import check_diff_limits
from forge.gates.paths import check_paths
from forge.gates.secrets import scan_diff_for_secrets, scan_forbidden_commands
from forge.audit.packet import DiffStats


def run_policy_gates(*, changed_files: list[str], diff: str, stats: DiffStats, engine_output: str, config: ForgeConfig, allowed_paths: list[str] | None = None) -> list[GateResult]:
    return [
        check_paths(changed_files, config.forbidden_paths, allowed_paths),
        check_diff_limits(stats, config.diff_limits),
        scan_diff_for_secrets(diff),
        scan_forbidden_commands(diff + "\n" + engine_output),
    ]
