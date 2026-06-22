"""Type definitions for the memory directory system.

Defines the MemoryType enum for categorizing memories and the MemoryEntry
dataclass that represents a single persistent memory record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class MemoryType(str, Enum):
    """Categories of persistent memory entries.

    Mirrors Claude Code's memory types:
    - USER: Explicit user instructions and preferences
    - FEEDBACK: Agent self-feedback and lessons learned
    - PROJECT: Project-level context and conventions
    - REFERENCE: Reference material and documentation notes
    """

    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


@dataclass
class MemoryEntry:
    """A single memory record stored as a markdown file.

    Attributes:
        type: The MemoryType category of this entry.
        content: The full markdown content of the memory file.
        source_path: Absolute path to the .md file this was loaded from.
        created_at: Timestamp when the memory was originally created.
        age_days: Computed age in days (derived from created_at vs now).
        relevance_score: Numeric relevance score (0.0-1.0+) set by scoring.
        tags: Optional tags for categorization and filtering.
        priority: Numeric priority level (higher = more important).
    """

    type: MemoryType
    content: str
    source_path: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    age_days: int = field(init=False)
    relevance_score: float = 0.0
    tags: list[str] = field(default_factory=list)
    priority: int = 0

    def __post_init__(self) -> None:
        """Compute age_days from created_at and current time."""
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - self.created_at
        self.age_days = delta.days
