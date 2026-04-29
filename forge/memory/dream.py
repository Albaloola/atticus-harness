"""Dreamer memory update pass."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from forge.audit.writer import latest_audit
from forge.memory.backlog import read_backlog, write_backlog


def run_dreamer(repo: Path) -> dict[str, object]:
    report_path = latest_audit(repo)
    observations: list[str] = []
    backlog_items = read_backlog(repo)
    if report_path is None:
        observations.append("No audit reports found yet.")
    else:
        report_raw = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report_raw, Mapping):
            raise RuntimeError("Latest audit report must be a JSON object")
        report = cast(Mapping[str, object], report_raw)
        decision = str(report.get("final_decision") or "unknown")
        task_raw = report.get("task")
        task = cast(Mapping[str, object], task_raw) if isinstance(task_raw, Mapping) else {}
        title = str(task.get("title") or "unknown task")
        observations.append(f"Last audit {report_path.parent.name} ended with {decision}: {title}")
        if decision not in {"committed", "repaired_then_committed"}:
            backlog_items.append({"title": f"Repair failed Forge task: {title}", "source": str(report_path), "risk": "low"})
    write_backlog(repo, backlog_items[-200:])
    return {
        "new_observations": observations,
        "new_backlog_items": backlog_items[-5:],
        "failed_patterns": [],
        "risky_files": [],
        "recommended_next_tasks": backlog_items[-3:],
    }
