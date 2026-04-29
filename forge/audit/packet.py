"""Serializable audit packet data structures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0

    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass
class GateResult:
    name: str
    passed: bool
    details: str = ""
    command: str = ""
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


@dataclass
class EngineResult:
    engine: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    changed_files: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    git_diff: str = ""


@dataclass
class ReviewerVerdict:
    role: str
    verdict: str
    confidence: float
    risk_level: str
    blocking_issues: list[str] = field(default_factory=list)
    non_blocking_issues: list[str] = field(default_factory=list)
    recommended_repairs: list[str] = field(default_factory=list)
    files_of_concern: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiffStats:
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0


@dataclass
class AuditPacket:
    iteration_id: str
    timestamp_start: str
    timestamp_end: str = ""
    target_repo: str = ""
    base_branch: str = ""
    worktree_path: str = ""
    branch_name: str = ""
    task: dict[str, Any] = field(default_factory=dict)
    engine: dict[str, Any] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    diff_stats: DiffStats = field(default_factory=DiffStats)
    commands_run: list[dict[str, Any]] = field(default_factory=list)
    gate_results: list[dict[str, Any]] = field(default_factory=list)
    reviewer_verdicts: list[dict[str, Any]] = field(default_factory=list)
    final_decision: str = "failed"
    commit_sha: str = ""
    risk_score: float = 0.0
    usage: dict[str, Any] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "total_cost_usd": 0.0})
    cost: dict[str, Any] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "total_cost_usd": 0.0})
    cleanup: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
