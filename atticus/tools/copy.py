"""Copy tool for Atticus harness with SHA preservation."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from pathlib import Path
from typing import Any

from atticus.tools.registry import (
    HarnessTool,
    ToolContext,
    ToolResult,
    register_tool,
)


def compute_sha256(file_path: Path, chunk_size: int = 8192) -> str:
    """Compute SHA-256 hash of a file.

    Args:
        file_path: Path to the file to hash.
        chunk_size: Size of chunks to read in bytes.

    Returns:
        Hexadecimal SHA-256 hash string.
    """
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


@register_tool
class CopyTool(HarnessTool):
    """Copy a file or directory, preserving SHA-256 for provenance."""

    @property
    def name(self) -> str:
        """Tool name."""
        return "Copy"

    @property
    def description(self) -> str:
        """Tool description."""
        return "Copy a file or directory, preserving SHA-256 for provenance."

    def can_handle(self, stage: str) -> bool:
        """Check if tool is available in the given stage.

        Args:
            stage: The harness stage to check.

        Returns:
            True if tool is available in execute, repair, or register stages.
        """
        return any(s in stage for s in ["execute", "repair", "register"])

    def invoke(self, params: dict, context: ToolContext) -> ToolResult:
        """Execute the copy tool with given parameters and context.

        Args:
            params: Tool parameters including src, dst, and preserve_sha.
            context: Tool execution context.

        Returns:
            ToolResult containing the output and metadata.
        """
        try:
            src = params.get("src")
            dst = params.get("dst")
            preserve_sha = params.get("preserve_sha", True)

            if not src or not isinstance(src, str):
                return ToolResult(
                    content={},
                    metadata={},
                    success=False,
                    error="src parameter is required and must be a string",
                )

            if not dst or not isinstance(dst, str):
                return ToolResult(
                    content={},
                    metadata={},
                    success=False,
                    error="dst parameter is required and must be a string",
                )

            src_path = Path(src).resolve()
            dst_path = Path(dst).resolve()

            if not src_path.exists():
                return ToolResult(
                    content={"src": str(src_path), "dst": str(dst_path), "sha256": None},
                    metadata={"src": str(src_path), "dst": str(dst_path), "sha256": None},
                    success=False,
                    error=f"Source path does not exist: {src_path}",
                )

            dst_path.parent.mkdir(parents=True, exist_ok=True)

            sha256_hex: str | None = None

            if src_path.is_dir():
                shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, dst_path)

                if preserve_sha:
                    src_sha256 = compute_sha256(src_path)
                    dst_sha256 = compute_sha256(dst_path)
                    if src_sha256 != dst_sha256:
                        return ToolResult(
                            content={"src": str(src_path), "dst": str(dst_path), "sha256": None},
                            metadata={"src": str(src_path), "dst": str(dst_path), "sha256": None},
                            success=False,
                            error=f"SHA-256 mismatch after copy: {src_sha256} != {dst_sha256}",
                        )
                    sha256_hex = src_sha256

            if context.provenance_logger and sha256_hex:
                log_entry = {
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "operation": "copy",
                    "from": str(src_path),
                    "to": str(dst_path),
                    "sha256": sha256_hex,
                    "reason": params.get("reason", "initial ingest"),
                }
                if hasattr(context.provenance_logger, "log"):
                    context.provenance_logger.log(log_entry)
                elif callable(context.provenance_logger):
                    context.provenance_logger(log_entry)

            return ToolResult(
                content={"src": str(src_path), "dst": str(dst_path), "sha256": sha256_hex},
                metadata={"src": str(src_path), "dst": str(dst_path), "sha256": sha256_hex},
                success=True,
                error=None,
            )

        except Exception as e:
            return ToolResult(
                content={},
                metadata={},
                success=False,
                error=str(e),
            )
