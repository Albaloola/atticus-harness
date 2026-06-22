"""Per-task progress event tracking module.

Ports the progress event concept from Claude Code's Tool.ts, providing
in-memory storage and querying of per-task tool execution progress events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable
from uuid import uuid4

class ProgressEventType(Enum):
    """Kinds of progress events tracked during tool execution."""

    BASH_PROGRESS = "bash_progress"
    AGENT_PROGRESS = "agent_progress"
    MCP_PROGRESS = "mcp_progress"
    TASK_OUTPUT = "task_output"
    SKILL_PROGRESS = "skill_progress"
    WEB_SEARCH_PROGRESS = "web_search_progress"
    HOOK_PROGRESS = "hook_progress"

@dataclass(frozen=True)
class ProgressEvent:
    """Base progress event emitted during a tool call.

    Attributes:
        event_id: Unique identifier for this event.
        tool_use_id: Identifier of the tool invocation this event belongs to.
        event_type: Kind of progress being reported.
        timestamp: UTC timestamp when the event was created.
        data: Arbitrary payload attached to the event.
    """

    event_id: str = field(default_factory=lambda: str(uuid4()))
    tool_use_id: str = ""
    event_type: ProgressEventType = ProgressEventType.TASK_OUTPUT
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BashProgress:
    """Progress event for a running or completed bash command.

    Attributes:
        tool_use_id: Identifier of the tool invocation.
        command: The shell command being executed.
        stdout_lines: Accumulated stdout output lines so far.
        stderr_lines: Accumulated stderr output lines so far.
        exit_code: Command exit code, or None if still running.
    """

    tool_use_id: str
    command: str = ""
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    exit_code: int | None = None


@dataclass(frozen=True)
class AgentProgress:
    """Progress event emitted by a sub-agent.

    Attributes:
        tool_use_id: Identifier of the tool invocation.
        agent_id: Identifier of the sub-agent (e.g. explore, librarian).
        message: Human-readable status message.
        status: Machine-readable status value (e.g. 'running', 'done', 'error').
    """

    tool_use_id: str
    agent_id: str = ""
    message: str = ""
    status: str = ""

ToolCallProgress = Callable[[ProgressEvent], None]
"""Callback signature for receiving progress events."""

class ProgressTracker:
    """In-memory store for per-task progress events.

    Events are grouped by task_id so multiple concurrent tasks can each have
    their own progress stream.  Thread safety is not guaranteed — callers
    managing concurrent tasks should synchronize externally if needed.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[ProgressEvent]] = {}

    def add_event(self, task_id: str, event: ProgressEvent) -> None:
        """Record a progress event for *task_id*.

        Args:
            task_id: The task this event belongs to.
            event: The progress event to store.
        """
        _ = self._events.setdefault(task_id, [])
        self._events[task_id].append(event)

    def clear_task(self, task_id: str) -> None:
        """Remove all events associated with *task_id*."""
        _ = self._events.pop(task_id, None)

    def get_events(self, task_id: str) -> list[ProgressEvent]:
        """Return all recorded events for *task_id*, newest first."""
        events = self._events.get(task_id, [])
        return list(reversed(events))

    def get_latest_event(self, task_id: str) -> ProgressEvent | None:
        """Return the most recent event for *task_id*, or None."""
        events = self._events.get(task_id)
        if events:
            return events[-1]
        return None

    def all_tasks(self) -> list[str]:
        """Return task IDs that have at least one recorded event."""
        return list(self._events.keys())

    def to_dict(self) -> dict[str, list[dict[str, object]]]:
        """Serialize all tracked events to a JSON-friendly dict.

        Returns:
            Mapping of task_id → list of event dicts.
        """
        return {
            task_id: [
                {
                    "event_id": e.event_id,
                    "tool_use_id": e.tool_use_id,
                    "event_type": e.event_type.value,
                    "timestamp": e.timestamp.isoformat(),
                    "data": e.data,
                }
                for e in events
            ]
            for task_id, events in self._events.items()
        }

_tracker = ProgressTracker()


def get_tracker() -> ProgressTracker:
    return _tracker


def filter_tool_progress(events: list[ProgressEvent]) -> list[ProgressEvent]:
    """Drop hook-progress events from *events*.

    Hook progress events represent lifecycle hooks rather than actual tool
    execution.  This helper strips them so callers can focus on tool-level
    progress.

    Args:
        events: The unfiltered list of progress events.

    Returns:
        A new list with all :attr:`ProgressEventType.HOOK_PROGRESS` entries
        removed.
    """
    return [e for e in events if e.event_type != ProgressEventType.HOOK_PROGRESS]


def build_progress_report(
    task_id: str, events: list[ProgressEvent]
) -> dict[str, object]:
    """Summarise a collection of progress events into a compact report.

    Args:
        task_id: The task identifier the events belong to.
        events: The events to summarise.

    Returns:
        A dictionary with keys ``task_id``, ``event_count``,
        ``latest_status``, ``has_errors``, and ``types_seen``.
    """
    latest: ProgressEvent | None = events[-1] if events else None
    types_seen = sorted({e.event_type.value for e in events})

    # Determine latest_status — use the last event type name, or 'unknown'
    latest_status = latest.event_type.value if latest else "unknown"

    # Determine has_errors by checking exit_code in BashProgress-like data
    # or looking for explicit error markers in data dicts.
    has_errors = any(
        (e.data.get("exit_code") not in (None, 0))
        or (e.data.get("is_error", False))
        for e in events
    )

    return {
        "task_id": task_id,
        "event_count": len(events),
        "latest_status": latest_status,
        "has_errors": has_errors,
        "types_seen": types_seen,
    }
