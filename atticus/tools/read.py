"""Read tool for Atticus harness with token/page limits."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from atticus.tools.registry import HarnessTool, ToolResult, register_tool
from atticus.tools.token_budget import (
    read_file_streaming,
    count_tokens,
    CircuitBreaker,
    is_text_file,
    is_image_file,
    extract_pages_from_pdf,
    estimate_tokens_for_file,
)


def extract_text_from_docx(path: Path) -> str:
    """Extract text from a DOCX file."""
    try:
        from docx import Document
        doc = Document(str(path))
        return "\n".join(para.text for para in doc.paragraphs)
    except ImportError:
        return f"[DOCX extraction unavailable - python-docx not installed] File: {path}"


def extract_text_from_pdf(path: Path) -> str:
    """Extract text from a PDF file."""
    try:
        import PyPDF2
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        return f"[PDF extraction unavailable - PyPDF2 not installed] File: {path}"


@register_tool
class ReadTool(HarnessTool):
    """Read a file (text or image) with optional token/page limits."""

    @property
    def name(self) -> str:
        return "Read"

    @property
    def description(self) -> str:
        return "Read a file (text or image) with optional token/page limits."

    def can_handle(self, stage: str) -> bool:
        """Read tool is available in all stages."""
        return True

    def invoke(self, params: dict, context: Any) -> ToolResult:
        """Execute the Read tool.

        Args:
            params: Tool parameters including:
                - path (required): File path to read.
                - max_tokens (optional): Max tokens to return.
                - max_pages (optional): Max pages for multi-page image PDFs.
            context: Tool execution context.

        Returns:
            ToolResult with file content and metadata.
        """
        path_param = params.get("path")
        if not path_param or not isinstance(path_param, str):
            return ToolResult(
                content="",
                metadata={"error": "path is required and must be a string"},
                success=False,
                error="path is required",
            )

        path = Path(path_param).resolve()

        if not path.exists():
            return ToolResult(
                content="",
                metadata={"path": str(path)},
                success=False,
                error=f"File not found: {path}",
            )

        if not path.is_file():
            return ToolResult(
                content="",
                metadata={"path": str(path)},
                success=False,
                error=f"Not a file: {path}",
            )

        # Check read permission
        try:
            with open(path, "rb") as f:
                pass
        except PermissionError:
            return ToolResult(
                content="",
                metadata={"path": str(path)},
                success=False,
                error=f"Permission denied: {path}",
            )

        stage = getattr(context, "stage", "unknown")
        suffix = path.suffix.lower()

        # For analyse stage: use streaming read with byte budget
        if stage == "evidence-ingest-analyse":
            max_tokens = params.get("max_tokens", 2000)
            # Convert tokens to bytes (rough: 4 chars = 1 token, 1 char ≈ 1 byte for UTF-8 ASCII)
            max_bytes = max_tokens * 4  # Rough estimate
            text, tokens_used = read_file_streaming(path, max_bytes=max_bytes)
            return ToolResult(
                content=text,
                metadata={
                    "path": str(path),
                    "stage": stage,
                    "tokens_used": tokens_used,
                    "truncated": tokens_used >= max_tokens,
                },
                success=True,
                error=None,
            )

        # For non-analyse stages: read full file
        if is_text_file(path):
            if suffix == ".docx":
                text = extract_text_from_docx(path)
            elif suffix == ".pdf":
                text = extract_text_from_pdf(path)
            else:
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except UnicodeDecodeError:
                    with open(path, "r", encoding="latin-1") as f:
                        text = f.read()
            tokens_used = count_tokens(text) if text else 0
            return ToolResult(
                content=text,
                metadata={
                    "path": str(path),
                    "stage": stage,
                    "tokens_used": tokens_used,
                    "size": len(text),
                },
                success=True,
                error=None,
            )

        if is_image_file(path):
            return self._read_image(path)

        # Fallback: try to read as text
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            tokens_used = count_tokens(text) if text else 0
            return ToolResult(
                content=text,
                metadata={
                    "path": str(path),
                    "stage": stage,
                    "tokens_used": tokens_used,
                    "size": len(text),
                },
                success=True,
                error=None,
            )
        except (UnicodeDecodeError, OSError) as e:
            return ToolResult(
                content="",
                metadata={"path": str(path), "error": str(e)},
                success=False,
                error=f"Cannot read file: {e}",
            )

    def _read_image(self, path: Path) -> ToolResult:
        """Read image file and return base64 encoded content."""
        try:
            with open(path, "rb") as f:
                image_bytes = f.read()
            encoded = base64.b64encode(image_bytes).decode("ascii")
            return ToolResult(
                content=encoded,
                metadata={
                    "path": str(path),
                    "size": len(image_bytes),
                    "encoding": "base64",
                    "mime_type": self._get_mime_type(path.suffix.lower()),
                },
                success=True,
                error=None,
            )
        except Exception as e:
            return ToolResult(
                content="",
                metadata={"path": str(path)},
                success=False,
                error=f"Failed to read image: {e}",
            )

    def _get_mime_type(self, suffix: str) -> str:
        """Get MIME type for a file extension."""
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".webp": "image/webp",
            ".ico": "image/x-icon",
        }
        return mime_types.get(suffix, "application/octet-stream")
