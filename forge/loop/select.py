"""Task selection."""

from __future__ import annotations

from forge.loop.task import TaskPacket


RISK = {"low": 0.0, "medium": 2.0, "high": 6.0}
VALUE = {"low": 1.0, "medium": 3.0, "high": 5.0}


def select_task(tasks: list[TaskPacket]) -> TaskPacket:
    if not tasks:
        raise ValueError("no candidate tasks available")
    return max(tasks, key=_score)


def _score(task: TaskPacket) -> float:
    if task.score:
        return task.score
    complexity = min(task.estimated_diff_lines / 200.0, 5.0)
    return VALUE.get(task.value, 3.0) + 2.0 - RISK.get(task.risk, 2.0) - complexity
