"""Atticus memory directory — file-based persistent memory system.

Port of Claude Code's memdir/ to Python. Core exports:

- ``load_memory_from_dir`` — load .md memory files from a directory
- ``find_relevant_memories`` — find memories matching a query
- ``build_memory_prompt`` — format memories as a system prompt section
- ``get_default_memdir_path`` — resolve default memory directory
- ``MemoryType`` / ``MemoryEntry`` — type definitions
"""

from __future__ import annotations

from .memdir import (
    build_memory_prompt,
    find_relevant_memories,
    get_default_memdir_path,
    load_memory_from_dir,
)
from .memory_types import MemoryEntry, MemoryType

__all__ = [
    "MemoryType",
    "MemoryEntry",
    "load_memory_from_dir",
    "find_relevant_memories",
    "build_memory_prompt",
    "get_default_memdir_path",
]
