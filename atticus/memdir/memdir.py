"""Core memory directory operations: load, score, format, and age-track memories.

Port of Claude Code's memdir.ts to Python. All operations are file-based
with no external dependencies.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from .memory_types import MemoryEntry, MemoryType

PathLike = Union[str, Path]


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML-like frontmatter from the first lines of a memory file.

    Supports both `key: value` lines (with optional `---` separator) and
    bare key-value pairs at the top of the file.
    """
    lines = content.strip().split("\n")
    frontmatter: dict[str, str] = {}
    in_frontmatter = False
    separator_seen = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter and stripped == "---":
            separator_seen = True
            break
        if in_frontmatter or (i < 10 and not separator_seen):
            m = re.match(
                r"^(\w[\w_-]*)\s*:\s*(.+)", stripped, re.UNICODE
            )
            if m:
                frontmatter[m.group(1)] = m.group(2).strip()
                in_frontmatter = True
                continue
            if in_frontmatter and stripped:
                break
        if not in_frontmatter and i >= 10:
            break

    return frontmatter


def _extract_type_from_filename(filepath: Path) -> MemoryType | None:
    """Attempt to determine MemoryType from filename convention.

    Expected format: ``{type}__{description}.md``
    e.g. ``user__case_strategy_notes.md``
    """
    name = filepath.stem
    parts = name.split("__", 1)
    if len(parts) == 2:
        type_str = parts[0].lower()
        try:
            return MemoryType(type_str)
        except ValueError:
            pass
    return None


def _parse_content(content: str) -> str:
    """Strip frontmatter lines to get the body content."""
    lines = content.strip().split("\n")
    if lines and lines[0].strip() == "---":
        try:
            end_idx = lines.index("---", 1)
            return "\n".join(lines[end_idx + 1 :]).strip()
        except ValueError:
            pass

    body_start = 0
    for i, line in enumerate(lines[:10]):
        stripped = line.strip()
        if not stripped:
            body_start = i + 1
            continue
        if re.match(r"^\w[\w_-]*\s*:\s*.+", stripped):
            body_start = i + 1
            continue
        if stripped == "---":
            body_start = i + 1
            continue
        break

    return "\n".join(lines[body_start:]).strip()


def load_memory_from_dir(
    dir_path: PathLike, max_age_days: int = 90
) -> list[MemoryEntry]:
    """Walk a directory and load all valid ``.md`` memory files.

    Frontmatter values: ``type: user|feedback|project|reference``
    If no frontmatter, falls back to filename convention.

    Entries older than *max_age_days* are excluded. Age is computed from
    the file modification time when no ``created_at`` is in frontmatter.

    Returns:
        List of MemoryEntry objects, one per valid .md file found.
    """
    path = Path(dir_path)
    if not path.is_dir():
        return []

    entries: list[MemoryEntry] = []
    now = datetime.now(timezone.utc)

    for md_file in sorted(path.rglob("*.md")):
        raw = md_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(raw)
        body = _parse_content(raw)

        mem_type = None
        if "type" in fm:
            try:
                mem_type = MemoryType(fm["type"].lower())
            except ValueError:
                pass

        if mem_type is None:
            mem_type = _extract_type_from_filename(md_file)
        if mem_type is None:
            continue

        created_at = now
        if "created_at" in fm:
            for fmt in (
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    created_at = datetime.strptime(
                        fm["created_at"], fmt
                    ).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
        else:
            created_at = datetime.fromtimestamp(
                md_file.stat().st_mtime, tz=timezone.utc
            )

        tags: list[str] = []
        if "tags" in fm:
            tags = [t.strip() for t in fm["tags"].split(",") if t.strip()]

        priority = 0
        if "priority" in fm:
            try:
                priority = int(fm["priority"])
            except ValueError:
                pass

        entry = MemoryEntry(
            type=mem_type,
            content=body,
            source_path=str(md_file.resolve()),
            created_at=created_at,
            relevance_score=0.0,
            tags=tags,
            priority=priority,
        )

        if entry.age_days > max_age_days:
            continue

        entries.append(entry)

    return entries


def find_relevant_memories(
    dir_path: PathLike,
    query: str,
    max_results: int = 5,
    max_age_days: int = 90,
) -> list[MemoryEntry]:
    """Find memories relevant to *query* using simple TF keyword scoring.

    Loads all non-stale memories from *dir_path*, scores each by how many
    query tokens appear in the content, and returns the top *max_results*
    sorted by relevance_score (descending).

    Returns:
        List of MemoryEntry objects with relevance_score populated.
    """
    entries = load_memory_from_dir(dir_path, max_age_days=max_age_days)
    if not entries or not query.strip():
        return entries[:max_results]

    query_lower = query.lower()
    query_tokens = [
        t for t in re.split(r"\W+", query_lower) if len(t) > 2
    ]

    if not query_tokens:
        return entries[:max_results]

    for entry in entries:
        content_lower = entry.content.lower()
        score = 0.0
        for token in query_tokens:
            count = content_lower.count(token)
            if count:
                score += 1.0 + (0.3 * (count - 1))
        entry.relevance_score = score

    scored = [e for e in entries if e.relevance_score > 0]
    scored.sort(key=lambda e: e.relevance_score, reverse=True)
    return scored[:max_results]


def build_memory_prompt(
    memories: list[MemoryEntry],
    section_title: str = "## Active Memory",
) -> str:
    """Build a system-prompt section from active memories.

    Formats each memory as markdown with type badges, source paths, and
    age warnings. Includes recall instruction sections and a drift caveat
    that mirrors Claude Code's memdir prompt style.
    """
    if not memories:
        return ""

    lines: list[str] = [section_title, ""]

    recent = [m for m in memories if m.age_days <= 7]
    older = [m for m in memories if m.age_days > 7]

    if recent:
        type_badges = {
            MemoryType.USER: "`[USER]`",
            MemoryType.FEEDBACK: "`[FEEDBACK]`",
            MemoryType.PROJECT: "`[PROJECT]`",
            MemoryType.REFERENCE: "`[REF]`",
        }
        lines.append(
            "You have written and can recall the following memories:"
        )
        lines.append("")

        for mem in recent:
            badge = type_badges.get(mem.type, "`[?]`")
            rel_path = _relative_path(mem.source_path)
            age_note = (
                f" ({mem.age_days}d old)"
                if mem.age_days > 1
                else " (today)"
            )
            lines.append(f"- {badge} {rel_path}{age_note}")
            for line in mem.content.strip().split("\n")[:5]:
                lines.append(f"  {line}")
            if mem.content.strip().count("\n") >= 5:
                lines.append("  ...")
            lines.append("")

    if older:
        lines.append("Searching past context (grep suggestions):")
        lines.append("")
        for mem in older:
            rel_path = _relative_path(mem.source_path)
            lines.append(
                f"- `grep -r \"<keyword>\" {rel_path}`"
                f"  ({mem.age_days}d old)"
            )
        lines.append("")

    if recent:
        lines.append("Trusting what you recall:")
        lines.append("- Prefer recent (≤7d) memories over stale material")
        lines.append(
            "- If a memory contradicts current context, trust the current"
            " context"
        )
        lines.append("")

    lines.append("> ⚠️ Memory drift caveat: memories older than 7 days may")
    lines.append(
        "> be stale. Validate against current codebase state before relying"
        " on them."
    )

    return "\n".join(lines)


def get_default_memdir_path() -> Path:
    """Return the default memory directory path.

    Matches Claude Code's convention: ``~/.atticus/memories/``
    """
    return Path.home() / ".atticus" / "memories"


def age_track_memory(entry: MemoryEntry, max_age_days: int = 90) -> bool:
    """Check if a memory entry is stale (exceeds *max_age_days*).

    Returns:
        ``True`` if the entry should be considered stale, ``False`` otherwise.
    """
    return entry.age_days > max_age_days


def _relative_path(source_path: str) -> str:
    """Shrink source_path for display when under home directory."""
    try:
        return str(Path(source_path).relative_to(Path.home()))
    except ValueError:
        return source_path
