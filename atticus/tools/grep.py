from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from atticus.tools.registry import HarnessTool, ToolContext, ToolResult, register_tool


@register_tool
class GrepTool(HarnessTool):
    """Search file contents for a regex pattern across a directory."""

    @property
    def name(self) -> str:
        return "Grep"

    @property
    def description(self) -> str:
        return "Search file contents for a regex pattern across a directory."

    def can_handle(self, stage: str) -> bool:
        """Check if tool is available in the given stage.

        Args:
            stage: The harness stage to check.

        Returns:
            True if tool is available in resolve, harvest, review, or repair stages.
        """
        allowed_stages = {
            "resolve",
            "harvest",
            "review",
            "repair",
            "evidence-ingest-resolve",
        }
        return stage in allowed_stages

    def invoke(self, params: dict, context: ToolContext) -> ToolResult:
        """Execute the Grep tool with given parameters and context.

        Args:
            params: Tool parameters including:
                - pattern (required): Regex pattern to search.
                - path (optional): Directory to search (defaults to context.workspace_path).
                - include (optional): File glob to include (e.g., "*.py").
                - max_results (optional): Max results to return (default 100).
            context: Tool execution context.

        Returns:
            ToolResult containing the output and metadata.
        """
        pattern = params.get("pattern")
        if not pattern or not isinstance(pattern, str):
            return ToolResult(
                content=[],
                metadata={"error": "pattern is required"},
                success=False,
                error="pattern is required",
            )

        if pattern.startswith("-"):
            return ToolResult(
                content={"matches": [], "returncode": -1},
                metadata={"pattern": pattern},
                success=False,
                error="pattern must not start with '-'",
            )

        search_path = params.get("path")
        if not search_path:
            search_path = str(context.workspace_path)

        include = params.get("include")
        max_results = params.get("max_results", 100)
        if max_results is not None:
            max_results = int(max_results)

        results = self._search(pattern, search_path, include, max_results)

        results = self._sort_by_mtime(results, Path(search_path))

        return ToolResult(
            content=results,
            metadata={
                "pattern": pattern,
                "search_path": search_path,
                "result_count": len(results),
            },
            success=True,
            error=None,
        )

    def _search(
        self, pattern: str, search_path: str, include: str | None, max_results: int
    ) -> list[dict]:
        """Search for pattern using available tools, with fallback to Python regex.

        Args:
            pattern: Regex pattern to search.
            search_path: Directory to search.
            include: File glob to include.
            max_results: Max results to return.

        Returns:
            List of match dictionaries with path, line_number, line, and match.
        """
        results = self._try_ripgrep(pattern, search_path, include, max_results)
        if results is not None:
            return results

        results = self._try_grep(pattern, search_path, include, max_results)
        if results is not None:
            return results

        return self._search_python(pattern, search_path, include, max_results)

    def _try_ripgrep(
        self, pattern: str, search_path: str, include: str | None, max_results: int
    ) -> list[dict] | None:
        """Search using ripgrep (rg) if available.

        Args:
            pattern: Regex pattern to search.
            search_path: Directory to search.
            include: File glob to include.
            max_results: Max results to return.

        Returns:
            List of match dictionaries or None if ripgrep not available.
        """
        try:
            cmd = ["rg", "--no-heading", "--line-number", "--color", "never"]
            if include:
                cmd.extend(["--glob", include])
            cmd.extend([pattern, search_path])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode not in (0, 1):
                return None

            return self._parse_grep_output(result.stdout, pattern, max_results)
        except (subprocess.SubprocessError, FileNotFoundError):
            return None

    def _try_grep(
        self, pattern: str, search_path: str, include: str | None, max_results: int
    ) -> list[dict] | None:
        """Search using grep if available.

        Args:
            pattern: Regex pattern to search.
            search_path: Directory to search.
            include: File glob to include.
            max_results: Max results to return.

        Returns:
            List of match dictionaries or None if grep not available.
        """
        try:
            cmd = ["grep", "-rn"]
            if include:
                cmd.extend(["--include", include])
            cmd.extend([pattern, search_path])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode not in (0, 1):
                return None

            return self._parse_grep_output(result.stdout, pattern, max_results)
        except (subprocess.SubprocessError, FileNotFoundError):
            return None

    def _parse_grep_output(
        self, output: str, pattern: str, max_results: int
    ) -> list[dict]:
        """Parse grep/ripgrep output into match dictionaries.

        Args:
            output: Output from grep/ripgrep command.
            pattern: Original regex pattern.
            max_results: Max results to return.

        Returns:
            List of match dictionaries.
        """
        matches = []
        for line in output.split("\n"):
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) >= 3:
                file_path, line_num, content = parts[0], parts[1], parts[2]
                matches.append(
                    {
                        "path": file_path,
                        "line_number": int(line_num),
                        "line": content,
                        "match": pattern,
                    }
                )
                if len(matches) >= max_results:
                    break
        return matches

    def _search_python(
        self, pattern: str, search_path: str, include: str | None, max_results: int
    ) -> list[dict]:
        """Search using Python regex as fallback.

        Args:
            pattern: Regex pattern to search.
            search_path: Directory to search.
            include: File glob to include.
            max_results: Max results to return.

        Returns:
            List of match dictionaries.
        """
        import fnmatch

        regex = re.compile(pattern)
        matches = []
        search_path_obj = Path(search_path)

        for file_path in self._walk_directory(search_path_obj, include):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        if regex.search(line):
                            matches.append(
                                {
                                    "path": str(file_path),
                                    "line_number": line_num,
                                    "line": line.rstrip("\n"),
                                    "match": pattern,
                                }
                            )
                            if len(matches) >= max_results:
                                return self._sort_by_mtime(matches, search_path_obj)
            except (UnicodeDecodeError, PermissionError, IsADirectoryError):
                continue

        return self._sort_by_mtime(matches, search_path_obj)

    def _walk_directory(
        self, search_path: Path, include: str | None
    ) -> list[Path]:
        """Walk directory and return files matching include pattern.

        Args:
            search_path: Directory to search.
            include: File glob to include.

        Returns:
            List of file paths.
        """
        import fnmatch

        files = []
        for root, _dirs, filenames in os.walk(search_path):
            for filename in filenames:
                if include and not fnmatch.fnmatch(filename, include):
                    continue
                files.append(Path(root) / filename)
        return files

    def _sort_by_mtime(self, matches: list[dict], search_path: Path) -> list[dict]:
        """Sort matches by file modification time (newest first).

        Args:
            matches: List of match dictionaries.
            search_path: Base search path for relative path calculation.

        Returns:
            Sorted list of match dictionaries.
        """
        def get_mtime(match: dict) -> float:
            try:
                return Path(match["path"]).stat().st_mtime
            except OSError:
                return 0.0

        return sorted(matches, key=get_mtime, reverse=True)
