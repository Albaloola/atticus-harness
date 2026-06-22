"""Token and page budget enforcement for the Read tool.

Design principles (learned from Claude Code reports):
1. Use actual tokenizer (tiktoken) when available - char-based estimation is wildly inaccurate
2. Stream/chunk reads - count tokens as we go, stop when budget is hit
3. Circuit breaker pattern - stop after N consecutive failures
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Text file extensions that can have text extracted
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".kts",
    ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".svg", ".css", ".scss", ".less",
    ".sql", ".r", ".jl", ".m", ".mm", ".pl", ".pm",
    ".pdf", ".docx",
}

# Image file extensions
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".ico", ".ppm", ".pgm", ".pbm",
}


# ---------------------------------------------------------------------------
# Tiktoken (optional dependency, graceful fallback)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=128)
def _get_tokenizer():
    """Lazily load tiktoken tokenizer.

    Uses LRU cache to avoid reloading. Returns:
        tiktoken.Encoding object, or None if not available.
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        logger.info("tiktoken tokenizer loaded (cl100k_base)")
        return enc
    except ImportError:
        logger.debug("tiktoken not available; using char-based estimation")
        return None


def count_tokens(text: str) -> int:
    """Count tokens in text using available method.

    Args:
        text: The text to count tokens for.

    Returns:
        Number of tokens.
    """
    tokenizer = _get_tokenizer()
    if tokenizer:
        return len(tokenizer.encode(text))
    # Fallback: improved char-based estimation
    return _estimate_tokens_chars(text)


def _estimate_tokens_chars(text: str) -> int:
    """Estimate tokens using character count with language-aware heuristics.

    Based on analysis from Claude Code reports:
    - English prose: ~4 chars/token
    - Code: ~3 chars/token
    - Structured data (JSON/XML): ~2.5 chars/token

    Args:
        text: Text to estimate.

    Returns:
        Estimated token count.
    """
    if not text:
        return 0

    char_count = len(text)

    # Detect if likely code/structured data
    code_indicators = sum(1 for c in text[:500] if c in '{}[]()=<>;:')
    if code_indicators > 20:
        return int(char_count / 3.0)  # Code

    # Detect if likely structured data
    struct_indicators = sum(1 for c in text[:500] if c in '{}[]"\'')
    if struct_indicators > 30:
        return int(char_count / 2.5)  # JSON/XML

    # Default: English prose
    return int(char_count / 4.0)


# ---------------------------------------------------------------------------
# Type-aware token estimation (from Claude Code tokenEstimation.ts)
# ---------------------------------------------------------------------------

def estimate_tokens_for_file(file_path: Path, content: str | None = None) -> int:
    """Estimate token count using file-type-aware ratios.

    Based on Claude Code's bytesPerTokenForFileType():
    - JSON/XML: 2 chars/token (dense structured data)
    - Code files: 3 chars/token
    - Everything else: 4 chars/token (prose/PDF)

    Args:
        file_path: Path to the file.
        content: Optional pre-read content (avoids re-reading).

    Returns:
        Estimated token count.
    """
    if content is not None:
        # Use content directly
        return count_tokens(content)

    # Get ratio by file extension
    ext = file_path.suffix.lower()
    ratio = _chars_per_token_for_ext(ext)

    try:
        size = file_path.stat().st_size
        return int(size / ratio)
    except OSError:
        return 0


def _chars_per_token_for_ext(ext: str) -> float:
    """Return chars-per-token ratio based on file extension."""
    # Dense structured data
    if ext in (".json", ".jsonl", ".xml", ".csv"):
        return 2.0
    # Code files
    if ext in (
        ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp",
        ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".kts",
    ):
        return 3.0
    # Default: prose/PDF
    return 4.0


# ---------------------------------------------------------------------------
# Streaming read with byte budget
# ---------------------------------------------------------------------------

def read_file_streaming(
    file_path: Path,
    max_bytes: int = 8000,  # ~2000 tokens * 4
) -> tuple[str, int]:
    """Read file in chunks, stop when byte budget is hit.

    Streams file and counts tokens as we go (if tokenizer available).
    This is more accurate than reading entire file then truncating.

    Args:
        file_path: Path to the file.
        max_bytes: Maximum bytes to read (~4 * max_tokens).

    Returns:
        Tuple of (text_read, tokens_used).
    """
    if not file_path.exists():
        return "", 0

    tokenizer = _get_tokenizer()

    content_parts: list[str] = []
    bytes_read = 0
    tokens_used = 0

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            while bytes_read < max_bytes:
                chunk = f.read(1024)  # Read in 1KB chunks
                if not chunk:
                    break

                chunk_bytes = len(chunk.encode("utf-8"))
                if bytes_read + chunk_bytes > max_bytes:
                    remaining_bytes = max_bytes - bytes_read
                    # Truncate chunk to fit remaining byte budget
                    # For ASCII-heavy content, chars ≈ bytes. For multi-byte,
                    # we truncate conservatively and re-check on next iteration.
                    chunk = chunk[:remaining_bytes]
                    chunk_bytes = len(chunk.encode("utf-8"))

                content_parts.append(chunk)
                bytes_read += chunk_bytes

                # If tokenizer available, count tokens incrementally
                if tokenizer:
                    tokens_used = len(tokenizer.encode("".join(content_parts)))

    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"Failed to stream read {file_path}: {e}")
        return "", 0

    text = "".join(content_parts)

    # Final token count
    if tokenizer:
        tokens_used = len(tokenizer.encode(text))
    else:
        tokens_used = estimate_tokens_for_file(file_path, text)

    return text, tokens_used


# ---------------------------------------------------------------------------
# Token truncation (for when we need to truncate, not stream)
# ---------------------------------------------------------------------------

def truncate_text_to_tokens(
    text: str,
    max_tokens: int = 2000,
    max_bytes: int | None = None,
) -> tuple[str, int]:
    """Truncate text to approximately max_tokens tokens.

    Uses streaming approach: encode incrementally and stop when budget is hit.
    Also supports byte-based truncation as a fallback.

    Args:
        text: The text to truncate.
        max_tokens: Maximum number of tokens allowed.
        max_bytes: Optional maximum bytes (if provided, uses byte budget).

    Returns:
        Tuple of (truncated_text, tokens_used).
    """
    if not text:
        return "", 0

    tokenizer = _get_tokenizer()

    # If tokenizer available, use incremental encoding
    if tokenizer:
        return _truncate_with_tokenizer(text, max_tokens, tokenizer)

    # Fallback: byte-based or char-based
    if max_bytes:
        return _truncate_bytes_fallback(text, max_bytes)

    return _truncate_chars_fallback(text, max_tokens)


def _truncate_with_tokenizer(
    text: str,
    max_tokens: int,
    tokenizer,
) -> tuple[str, int]:
    """Truncate using actual tokenizer with binary search for efficiency."""
    # Quick check: if text is likely under budget, return as-is
    quick_estimate = len(text) / 4.0
    if quick_estimate <= max_tokens:
        actual = len(tokenizer.encode(text))
        if actual <= max_tokens:
            return text, actual

    # Binary search to find truncation point
    lo, hi = 0, len(text)
    best_text = ""
    best_tokens = 0

    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid]
        tokens = len(tokenizer.encode(candidate))

        if tokens <= max_tokens:
            best_text = candidate
            best_tokens = tokens
            lo = mid + 1
        else:
            hi = mid - 1

    return best_text, best_tokens


def _truncate_bytes_fallback(
    text: str,
    max_bytes: int,
) -> tuple[str, int]:
    """Truncate based on byte count."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, len(encoded) // 4  # Rough token estimate

    truncated_bytes = encoded[:max_bytes]
    # Try to decode, handling unfinished characters
    try:
        truncated = truncated_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Drop the last potentially incomplete character
        truncated = truncated_bytes[:-1].decode("utf-8")

    return truncated, len(truncated_bytes) // 4


def _truncate_chars_fallback(
    text: str,
    max_tokens: int,
) -> tuple[str, int]:
    """Fallback truncation using char estimation."""
    chars_per_token = _chars_per_token_for_ext(".txt")  # Default ratio
    max_chars = int(max_tokens * chars_per_token)
    if len(text) <= max_chars:
        return text, int(len(text) / chars_per_token)

    return text[:max_chars], max_tokens


# ---------------------------------------------------------------------------
# PDF utilities (stubs with optional pdf2image)
# ---------------------------------------------------------------------------

def extract_pages_from_pdf(
    pdf_path: Path,
    max_pages: int = 2,
) -> list[bytes]:
    """Extract up to max_pages pages from a multi-page image PDF.

    Args:
        pdf_path: Path to the PDF file.
        max_pages: Maximum number of pages to extract.

    Returns:
        List of page images as bytes (PNG format).
    """
    pages: list[bytes] = []

    try:
        from pdf2image import convert_from_path
        images = convert_from_path(pdf_path, dpi=150)
        for i, img in enumerate(images[:max_pages]):
            import io

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            pages.append(buf.getvalue())
    except ImportError:
        logger.warning(
            "pdf2image not available; stub implementation for extract_pages_from_pdf. "
            "Install pdf2image and poppler-utils for full functionality."
        )
    except Exception as e:
        logger.warning(f"Failed to extract pages from PDF {pdf_path}: {e}")

    return pages


def is_text_file(path: Path) -> bool:
    """Check if file is text-extractable based on extension."""
    return path.suffix.lower() in TEXT_EXTENSIONS


def is_image_file(path: Path) -> bool:
    """Check if file is an image based on extension."""
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_multi_page_image_pdf(path: Path) -> bool:
    """Check if PDF has multiple pages and each page is an image."""
    logger.debug(
        "is_multi_page_image_pdf is a stub; install pdf2image for full functionality"
    )
    return False


# ---------------------------------------------------------------------------
# Circuit Breaker (from Claude Code autoCompact.ts)
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Circuit breaker to stop retrying after N consecutive failures per file.

    From Claude Code cache optimization report:
    Problem: 1,279 sessions had 50+ consecutive autocompact failures,
    wasting ~250,000 API calls/day.

    Solution: Track failure counts per file. After N consecutive failures
    for a given file, stop trying that file. Also tracks a global count
    to trigger a full circuit break if too many distinct files fail.
    """

    def __init__(
        self,
        max_failures: int = 3,  # Claude Code's MAX_CONSECUTIVE_FAILURES = 3
        max_global_failures: int = 50,  # Stop entirely after 50 distinct file failures
        reset_on_success: bool = True,
    ):
        """Initialize circuit breaker.

        Args:
            max_failures: Max consecutive failures per file before skipping it.
            max_global_failures: Max distinct file failures before global trip.
            reset_on_success: Whether to reset per-file count on success.
        """
        self.max_failures = max_failures
        self.max_global_failures = max_global_failures
        self.reset_on_success = reset_on_success
        self._failure_counts: dict[str, int] = {}  # per-file failure count
        self._tripped_paths: set[str] = set()  # files that hit max_failures
        self.global_failure_count = 0
        self.globally_tripped = False

    def should_skip(self, file_path: str) -> bool:
        """Check if a file should be skipped due to repeated failures.

        Args:
            file_path: Path to the file.

        Returns:
            True if the file should be skipped.
        """
        return file_path in self._tripped_paths or self.globally_tripped

    def record_success(self, file_path: str) -> None:
        """Record a successful operation for a file.

        Resets the per-file failure count for that file.

        Args:
            file_path: Path to the file.
        """
        if self.reset_on_success:
            self._failure_counts.pop(file_path, None)
            self._tripped_paths.discard(file_path)

    def record_failure(self, file_path: str) -> None:
        """Record a failed operation for a file.

        Increments per-file failure count. If it reaches max_failures,
        the file is added to the tripped set and won't be retried.

        Args:
            file_path: Path to the file.
        """
        count = self._failure_counts.get(file_path, 0) + 1
        self._failure_counts[file_path] = count

        if count >= self.max_failures:
            self._tripped_paths.add(file_path)
            logger.warning(
                f"Circuit breaker tripped for {file_path} "
                f"after {count} consecutive failures"
            )

        # Increment global count only once per distinct file
        if count == self.max_failures:
            self.global_failure_count += 1
            if self.global_failure_count >= self.max_global_failures:
                self.globally_tripped = True
                logger.warning(
                    f"Circuit breaker globally tripped after "
                    f"{self.global_failure_count} distinct file failures"
                )

    def check(self) -> bool:
        """Check if circuit breaker is globally tripped.

        Returns:
            True if the circuit breaker has been globally tripped.
        """
        return self.globally_tripped

    def reset(self) -> None:
        """Manually reset the entire circuit breaker."""
        self._failure_counts.clear()
        self._tripped_paths.clear()
        self.global_failure_count = 0
        self.globally_tripped = False
