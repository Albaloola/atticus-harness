"""Evidence scanning module.

Scans source directories and produces raw_inventory.json with file metadata.
Uses parallel SHA-256 hashing for large file sets (5000+ files).
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from atticus.tools.glob import GlobTool
from atticus.tools.registry import ToolContext

logger = logging.getLogger(__name__)


def _compute_sha256(file_path: Path) -> tuple[str, str]:
    """Compute SHA-256 hash of a single file.

    Args:
        file_path: Path to the file to hash.

    Returns:
        Tuple of (file_path_str, sha256_hex).
    """
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)  # 64KB chunks
                if not chunk:
                    break
                sha256_hash.update(chunk)
        return str(file_path), sha256_hash.hexdigest()
    except OSError as e:
        logger.warning(f"Failed to hash {file_path}: {e}")
        return str(file_path), ""


def _compute_sha256_batch(
    file_paths: list[Path], max_workers: int = 8
) -> dict[str, str]:
    """Compute SHA-256 for multiple files in parallel.

    Args:
        file_paths: List of file paths to hash.
        max_workers: Maximum parallel workers (default 8).

    Returns:
        Dictionary mapping file path strings to their SHA-256 hex digests.
    """
    if not file_paths:
        return {}

    results: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_compute_sha256, p): p for p in file_paths}

        for future in as_completed(futures):
            file_path_str, sha256_hex = future.result()
            results[file_path_str] = sha256_hex

    return results


def scan_source_directory(
    source_dir: Path,
    workspace: Path,
    context: ToolContext,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Scan source directory and produce raw_inventory.json.

    Scans files in parallel using GlobTool, computes SHA-256 in parallel batches.

    Args:
        source_dir: Path to the source directory to scan.
        workspace: Workspace root path for output.
        context: ToolContext for tool invocations.
        max_workers: Maximum parallel workers for hashing (default 8).

    Returns:
        Dictionary with scan results including file list and metadata.
    """
    from atticus.evidence_ingest.provenance import ProvenanceLogger

    provenance = ProvenanceLogger(workspace, context)

    results: list[dict[str, Any]] = []
    scanned_paths: list[Path] = []

    # Use GlobTool to find all files
    glob_tool = GlobTool()
    glob_result = glob_tool.invoke(
        {"pattern": "**/*", "path": str(source_dir)},
        context,
    )

    if glob_result.success:
        if isinstance(glob_result.content, list):
            file_strs = glob_result.content
        elif isinstance(glob_result.content, str):
            file_strs = json.loads(glob_result.content)
        else:
            file_strs = []
    else:
        file_strs = []

    # Filter to files only
    for file_path_str in file_strs:
        file_path = Path(file_path_str)
        if file_path.is_file():
            scanned_paths.append(file_path)

    logger.info(f"Found {len(scanned_paths)} files in {source_dir}")

    # Batch SHA-256 computation
    hashes = _compute_sha256_batch(scanned_paths, max_workers=max_workers)

    for file_path in scanned_paths:
        fp_str = str(file_path)
        try:
            rel_path = file_path.relative_to(source_dir)
        except ValueError:
            rel_path = file_path.name

        entry = {
            "path": str(rel_path),
            "absolute_path": fp_str,
            "file": str(rel_path),
            "sha256": hashes.get(fp_str, ""),
            "size_bytes": file_path.stat().st_size if file_path.exists() else 0,
            "extension": file_path.suffix.lower(),
        }
        results.append(entry)

    output = {
        "source_dir": str(source_dir),
        "files": results,
        "count": len(results),
    }

    # Write raw_inventory.json
    output_path = workspace / "02-registers" / "raw_inventory.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)

    provenance.log("scan", source_dir=str(source_dir), file_count=len(results))

    return output


# -- Compatibility wrappers for old test APIs --
# Tests call scan_directory(source_dir, tool_context) with workspace
# extracted from the ToolContext rather than passed separately.

def scan_directory(source_dir: Path, context: ToolContext) -> dict[str, Any]:
    """Backward-compatible scan: workspace is derived from ToolContext.

    Tests import from atticus.evidence_ingest.scanner via
        scan_source_directory as scan_directory
    so we provide this alias that extracts workspace from context.
    """
    return scan_source_directory(
        source_dir=source_dir,
        workspace=context.workspace_path,
        context=context,
    )
