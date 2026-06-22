"""Task lifecycle management — types, IDs, state tracking, and handles.

Ported from Claude Code's Task.ts, adapted for atticus-harness.
Defines task types, statuses, ID generation, and lifecycle state
independently of core/policies.py and core/tasks.py.
"""

from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable


class TaskLifecycleType(StrEnum):
    """Task type with prefixed string values matching Claude Code types."""

    LOCAL_BASH = "local_bash"
    LOCAL_AGENT = "local_agent"
    REMOTE_AGENT = "remote_agent"
    IN_PROCESS_TEAMMATE = "in_process_teammate"
    LOCAL_WORKFLOW = "local_workflow"
    MONITOR_MCP = "monitor_mcp"
    DREAM = "dream"


class TaskLifecycleStatus(StrEnum):
    """Lifecycle status — PENDING through terminal states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


TASK_ID_PREFIXES: dict[TaskLifecycleType, str] = {
    TaskLifecycleType.LOCAL_BASH: "b",
    TaskLifecycleType.LOCAL_AGENT: "a",
    TaskLifecycleType.REMOTE_AGENT: "r",
    TaskLifecycleType.IN_PROCESS_TEAMMATE: "t",
    TaskLifecycleType.LOCAL_WORKFLOW: "w",
    TaskLifecycleType.MONITOR_MCP: "m",
    TaskLifecycleType.DREAM: "d",
}

TASK_ID_ALPHABET: str = "0123456789abcdefghijklmnopqrstuvwxyz"
"""36-character alphabet for compact, case-insensitive random IDs (base-36)."""


def generate_task_id(task_type: TaskLifecycleType, id_length: int = 8) -> str:
    """Generate a prefixed random task ID using CSPRNG randomness.

    Uses :func:`secrets.token_bytes` for cryptographically-secure randomness
    and maps each byte to a character from :data:`TASK_ID_ALPHABET`.

    Args:
        task_type: The type of task, used to select the single-character prefix.
        id_length: Number of random characters after the prefix (default 8).

    Returns:
        A string like ``'b3k7m9x2'`` (bash task) or ``'a9f2x1p0'`` (agent task).

    Raises:
        KeyError: If *task_type* is not registered in :data:`TASK_ID_PREFIXES`.
    """
    prefix = TASK_ID_PREFIXES[task_type]
    raw = secrets.token_bytes(id_length)
    chars = [TASK_ID_ALPHABET[b % len(TASK_ID_ALPHABET)] for b in raw]
    return f"{prefix}{''.join(chars)}"


_TERMINAL_STATUSES: frozenset[TaskLifecycleStatus] = frozenset(
    {TaskLifecycleStatus.COMPLETED, TaskLifecycleStatus.FAILED, TaskLifecycleStatus.KILLED}
)


def is_terminal_task_status(status: TaskLifecycleStatus) -> bool:
    """Return ``True`` if *status* is a terminal (finished) state.

    Terminal states are :attr:`TaskLifecycleStatus.COMPLETED`,
    :attr:`TaskLifecycleStatus.FAILED`, and :attr:`TaskLifecycleStatus.KILLED`.
    """
    return status in _TERMINAL_STATUSES


@dataclass(frozen=True)
class TaskHandle:
    """Lightweight reference to a tracked task with optional cleanup callback.

    Attributes:
        task_id: The prefixed task identifier (e.g. ``'a3k7m9x2'``).
        cleanup: An optional no-argument callable invoked when the handle
            is released (e.g. to kill a subprocess or close resources).
    """

    task_id: str
    cleanup: Callable[[], None] | None = None


@dataclass(frozen=True)
class TaskState:
    """Immutable snapshot of a task's full lifecycle state.

    Instances are typically created via :func:`create_task_state` rather
    than directly so that ``start_time`` and ``output_file`` are populated
    automatically.
    """

    id: str
    type: TaskLifecycleType
    status: TaskLifecycleStatus = TaskLifecycleStatus.PENDING
    description: str = ""
    tool_use_id: str | None = None
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None
    output_file: str | None = None
    output_offset: int = 0
    notified: bool = False


def create_task_state(
    task_id: str,
    task_type: TaskLifecycleType,
    description: str,
    tool_use_id: str | None = None,
    output_dir: str | None = None,
) -> TaskState:
    """Create an immutable :class:`TaskState` with ``start_time`` set to now.

    If *output_dir* is provided, the ``output_file`` field is set to a
    predictable path under that directory using a UUID-based name.

    Args:
        task_id: The prefixed task identifier (e.g. from :func:`generate_task_id`).
        task_type: The type of task.
        description: Human-readable task summary.
        tool_use_id: Optional external tool-use correlation ID.
        output_dir: Optional directory for writing task output.  When supplied,
            ``output_file`` is set to ``<output_dir>/output_<uuid>.txt``.

    Returns:
        A frozen :class:`TaskState` with ``start_time`` pinned to the current
        UTC time and ``output_file`` resolved if *output_dir* was given.
    """
    output_file: str | None = None
    if output_dir is not None:
        output_file = os.path.join(output_dir, f"output_{uuid.uuid4()}.txt")

    return TaskState(
        id=task_id,
        type=task_type,
        description=description,
        tool_use_id=tool_use_id,
        start_time=datetime.now(timezone.utc),
        output_file=output_file,
    )
