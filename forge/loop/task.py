"""Task packet model for bounded Forge work."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json


@dataclass
class TaskPacket:
    id: str
    title: str
    reason: str
    risk: str = "low"
    value: str = "medium"
    estimated_diff_lines: int = 200
    allowed_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    score: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_builder_prompt(self) -> str:
        task_json = json.dumps(self.as_dict(), indent=2, sort_keys=True)
        return f"""# Role

You are the builder agent inside Forge.

You are working inside an isolated git worktree.

You must complete exactly one task.

# Task

```json
{task_json}
```

# Rules

1. Only modify allowed paths.
2. Do not modify forbidden paths.
3. Keep the diff small.
4. Add or update tests when possible.
5. Do not disable tests.
6. Do not remove safety checks.
7. Do not touch secrets or environment files.
8. Do not make external network calls.
9. Do not auto-merge or push.
10. Stop after the task is complete.

# Required output

At the end, write a short implementation summary:

- What changed
- Why it changed
- Tests run
- Any risks or follow-up items
"""
