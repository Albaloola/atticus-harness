"""Tests for token budget utilities in atticus.tools.token_budget."""

import pytest
from pathlib import Path

from atticus.tools.token_budget import (
    truncate_text_to_tokens,
    is_text_file,
    is_image_file,
    is_multi_page_image_pdf,
)


class TestTruncateTextToTokens:
    """Tests for truncate_text_to_tokens function."""

    def test_truncation_to_2000_tokens(self):
        """Test truncation of text exceeding 2000 tokens."""
        # Create text that exceeds 2000 tokens (2000 * 4 chars = 8000 chars)
        long_text = "a" * 10000
        truncated, tokens_used = truncate_text_to_tokens(long_text, max_tokens=2000)

        assert len(truncated) <= 8000  # 2000 * 4 chars_per_token
        assert tokens_used <= 2000
        assert truncated == "a" * 8000

    def test_text_under_budget_no_truncation(self):
        """Test that text under budget is not truncated."""
        short_text = "Hello, world!"
        truncated, tokens_used = truncate_text_to_tokens(short_text, max_tokens=2000)

        assert truncated == short_text
        assert tokens_used == int(len(short_text) / 4.0)

    def test_returns_tuple_of_text_and_tokens(self):
        """Test that function returns (truncated_text, tokens_used) tuple."""
        text = "Sample text for testing"
        result = truncate_text_to_tokens(text)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], int)

    def test_empty_string(self):
        """Test truncation of empty string."""
        truncated, tokens_used = truncate_text_to_tokens("")

        assert truncated == ""
        assert tokens_used == 0

    def test_custom_max_bytes(self):
        """Test truncation with max_bytes parameter."""
        text = "b" * 6000
        truncated, tokens_used = truncate_text_to_tokens(
            text, max_tokens=1000, max_bytes=6000
        )

        assert len(truncated) <= 6000  # 1000 * 6 bytes
        assert truncated == "b" * 6000


class TestIsTextFile:
    """Tests for is_text_file function."""

    def test_txt_file_returns_true(self):
        """Test that .txt files are identified as text files."""
        assert is_text_file(Path("document.txt")) is True

    def test_pdf_file_returns_true(self):
        """Test that .pdf files are identified as text files."""
        assert is_text_file(Path("document.pdf")) is True

    def test_docx_file_returns_true(self):
        """Test that .docx files are identified as text files."""
        assert is_text_file(Path("document.docx")) is True

    def test_jpg_file_returns_false(self):
        """Test that .jpg files are not identified as text files."""
        assert is_text_file(Path("image.jpg")) is False

    def test_png_file_returns_false(self):
        """Test that .png files are not identified as text files."""
        assert is_text_file(Path("image.png")) is False

    def test_case_insensitive_extension(self):
        """Test that extension checking is case insensitive."""
        assert is_text_file(Path("document.PDF")) is True
        assert is_text_file(Path("document.TXT")) is True


class TestIsImageFile:
    """Tests for is_image_file function."""

    def test_jpg_file_returns_true(self):
        """Test that .jpg files are identified as image files."""
        assert is_image_file(Path("image.jpg")) is True

    def test_png_file_returns_true(self):
        """Test that .png files are identified as image files."""
        assert is_image_file(Path("image.png")) is True

    def test_jpeg_file_returns_true(self):
        """Test that .jpeg files are identified as image files."""
        assert is_image_file(Path("image.jpeg")) is True

    def test_txt_file_returns_false(self):
        """Test that .txt files are not identified as image files."""
        assert is_image_file(Path("document.txt")) is False

    def test_pdf_file_returns_false(self):
        """Test that .pdf files are not identified as image files."""
        assert is_image_file(Path("document.pdf")) is False

    def test_case_insensitive_extension(self):
        """Test that extension checking is case insensitive."""
        assert is_image_file(Path("image.JPG")) is True
        assert is_image_file(Path("image.PNG")) is True


class TestIsMultiPageImagePdf:
    """Tests for is_multi_page_image_pdf function (stub)."""

    def test_returns_bool(self):
        """Test that function returns a boolean value."""
        result = is_multi_page_image_pdf(Path("document.pdf"))
        assert isinstance(result, bool)

    def test_with_nonexistent_file(self):
        """Test with a path that doesn't exist (stub always returns False)."""
        result = is_multi_page_image_pdf(Path("/nonexistent/document.pdf"))
        assert isinstance(result, bool)
        assert result is False
