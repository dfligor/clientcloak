"""
Tests for clientcloak.comments: inspect and process comments in .docx files.

Covers KEEP, STRIP, and SANITIZE modes, plus author restoration for uncloaking.
"""

import zipfile
import pytest
from pathlib import Path
from xml.etree import ElementTree as ET

from clientcloak.comments import (
    generate_initials,
    inspect_comments,
    process_comments,
    restore_comment_authors,
)
from clientcloak.models import CommentMode
from tests.conftest import make_docx_with_comments


# ===================================================================
# Helpers
# ===================================================================

def _read_comments_xml(docx_path: Path) -> ET.Element | None:
    """Parse word/comments.xml from a .docx ZIP."""
    with zipfile.ZipFile(docx_path, "r") as zf:
        if "word/comments.xml" not in zf.namelist():
            return None
        return ET.fromstring(zf.read("word/comments.xml"))


def _get_comment_authors_from_xml(docx_path: Path) -> list[str]:
    """Extract all comment author names from the XML."""
    root = _read_comments_xml(docx_path)
    if root is None:
        return []
    ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    return [
        el.get(f"{{{ns_w}}}author", "")
        for el in root.findall(f"{{{ns_w}}}comment")
    ]


# ===================================================================
# generate_initials
# ===================================================================

class TestGenerateInitials:
    """Tests for generate_initials()."""

    @pytest.mark.parametrize(
        "label, expected",
        [
            ("Reviewer A", "RA"),
            ("Outside Counsel", "OC"),
            ("John", "J"),
            ("A B C", "ABC"),
        ],
    )
    def test_generates_correct_initials(self, label, expected):
        assert generate_initials(label) == expected


# ===================================================================
# inspect_comments
# ===================================================================

class TestInspectComments:
    """Tests for inspect_comments()."""

    def test_inspect_with_comments(self, tmp_path):
        path = make_docx_with_comments(
            tmp_path / "commented.docx",
            "Agreement text.",
            [
                {"author": "Jane Smith", "initials": "JS", "text": "Review this clause"},
                {"author": "Bob Jones", "initials": "BJ", "text": "Needs clarification"},
                {"author": "Jane Smith", "initials": "JS", "text": "Another note"},
            ],
        )
        comments, authors = inspect_comments(path)
        assert len(comments) == 3
        assert len(authors) == 2  # two unique authors

        # Check author labels
        assert authors[0].name == "Jane Smith"
        assert authors[0].suggested_label == "Reviewer A"
        assert authors[1].name == "Bob Jones"
        assert authors[1].suggested_label == "Reviewer B"

        # Check comment count per author
        assert authors[0].comment_count == 2
        assert authors[1].comment_count == 1

    def test_inspect_without_comments(self, tmp_path):
        from tests.conftest import make_simple_docx
        path = make_simple_docx(tmp_path / "no_comments.docx", ["Text without comments"])
        comments, authors = inspect_comments(path)
        assert len(comments) == 0
        assert len(authors) == 0

    def test_comment_text_extraction(self, tmp_path):
        path = make_docx_with_comments(
            tmp_path / "text_check.docx",
            "Body",
            [{"author": "Alice", "initials": "A", "text": "Specific comment text here"}],
        )
        comments, _ = inspect_comments(path)
        assert comments[0].text == "Specific comment text here"
        assert comments[0].author == "Alice"


# ===================================================================
# process_comments: KEEP mode
# ===================================================================

class TestProcessCommentsKeep:
    """Tests for process_comments() in KEEP mode."""

    def test_keep_preserves_all_comments(self, tmp_path):
        path = make_docx_with_comments(
            tmp_path / "to_keep.docx",
            "Agreement text.",
            [
                {"author": "Jane Smith", "initials": "JS", "text": "Comment one"},
                {"author": "Bob Jones", "initials": "BJ", "text": "Comment two"},
            ],
        )
        output_path = tmp_path / "kept.docx"
        result = process_comments(path, output_path, CommentMode.KEEP)

        assert result == {}  # KEEP returns empty mapping

        # Verify comments are still there with original authors
        authors = _get_comment_authors_from_xml(output_path)
        assert "Jane Smith" in authors
        assert "Bob Jones" in authors


# ===================================================================
# process_comments: STRIP mode
# ===================================================================

class TestProcessCommentsStrip:
    """Tests for process_comments() in STRIP mode."""

    def test_strip_removes_all_comments(self, tmp_path):
        path = make_docx_with_comments(
            tmp_path / "to_strip.docx",
            "Agreement text.",
            [
                {"author": "Jane", "initials": "J", "text": "Comment one"},
                {"author": "Bob", "initials": "B", "text": "Comment two"},
            ],
        )
        output_path = tmp_path / "stripped.docx"
        result = process_comments(path, output_path, CommentMode.STRIP)

        assert result == {}  # STRIP returns empty mapping

        # Verify comments are gone
        root = _read_comments_xml(output_path)
        ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        if root is not None:
            comments = root.findall(f"{{{ns_w}}}comment")
            assert len(comments) == 0


# ===================================================================
# process_comments: SANITIZE mode
# ===================================================================

class TestProcessCommentsSanitize:
    """Tests for process_comments() in SANITIZE mode."""

    def test_sanitize_replaces_authors(self, tmp_path):
        path = make_docx_with_comments(
            tmp_path / "to_sanitize.docx",
            "Agreement text.",
            [
                {"author": "Jane Smith", "initials": "JS", "text": "A comment"},
                {"author": "Bob Jones", "initials": "BJ", "text": "Another"},
            ],
        )
        output_path = tmp_path / "sanitized.docx"
        mapping = process_comments(path, output_path, CommentMode.SANITIZE)

        assert len(mapping) == 2
        assert "Jane Smith" in mapping
        assert "Bob Jones" in mapping

        # Verify authors in the output are anonymized
        authors = _get_comment_authors_from_xml(output_path)
        assert "Jane Smith" not in authors
        assert "Bob Jones" not in authors
        # Should be Reviewer A, Reviewer B
        assert mapping["Jane Smith"] in authors
        assert mapping["Bob Jones"] in authors

    def test_sanitize_with_explicit_mapping(self, tmp_path):
        path = make_docx_with_comments(
            tmp_path / "explicit.docx",
            "Text.",
            [{"author": "Jane Smith", "initials": "JS", "text": "Comment"}],
        )
        output_path = tmp_path / "sanitized_explicit.docx"
        mapping = process_comments(
            path,
            output_path,
            CommentMode.SANITIZE,
            author_mapping={"Jane Smith": "Outside Counsel"},
        )

        assert mapping["Jane Smith"] == "Outside Counsel"
        authors = _get_comment_authors_from_xml(output_path)
        assert "Outside Counsel" in authors

    def test_sanitize_replaces_content(self, tmp_path):
        path = make_docx_with_comments(
            tmp_path / "to_sanitize.docx",
            "Agreement between Acme Corp and BigCo.",
            [
                {
                    "author": "Jane Smith",
                    "initials": "JS",
                    "text": "Acme Corp should review this clause",
                },
            ],
        )
        output_path = tmp_path / "sanitized.docx"
        mapping = process_comments(
            path,
            output_path,
            CommentMode.SANITIZE,
            content_replacements={"Acme Corp": "[VENDOR]"},
        )

        # Author should be anonymized
        assert "Jane Smith" in mapping

        # Verify content replacement in comments XML
        root = _read_comments_xml(output_path)
        ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        for t_el in root.iter(f"{{{ns_w}}}t"):
            if t_el.text:
                assert "Acme Corp" not in t_el.text

    def test_sanitize_content_case_insensitive(self, tmp_path):
        """Content replacements should match regardless of case."""
        path = make_docx_with_comments(
            tmp_path / "case.docx",
            "Body text.",
            [
                {
                    "author": "Jane",
                    "initials": "J",
                    "text": "ACME CORP should review this",
                },
            ],
        )
        output_path = tmp_path / "sanitized_case.docx"
        process_comments(
            path,
            output_path,
            CommentMode.SANITIZE,
            content_replacements={"Acme Corp": "[VENDOR]"},
        )

        root = _read_comments_xml(output_path)
        ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        texts = [t.text for t in root.iter(f"{{{ns_w}}}t") if t.text]
        full_text = " ".join(texts)
        assert "ACME" not in full_text.upper() or "[VENDOR]" in full_text

    def test_sanitize_content_longest_first(self, tmp_path):
        """Longer originals should be matched before shorter substrings."""
        path = make_docx_with_comments(
            tmp_path / "longest.docx",
            "Body text.",
            [
                {
                    "author": "Jane",
                    "initials": "J",
                    "text": "Contact Acme Corporation for details",
                },
            ],
        )
        output_path = tmp_path / "sanitized_longest.docx"
        process_comments(
            path,
            output_path,
            CommentMode.SANITIZE,
            content_replacements={
                "Acme Corporation": "[VENDOR]",
                "Acme": "[VENDOR-SHORT]",
            },
        )

        root = _read_comments_xml(output_path)
        ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        texts = [t.text for t in root.iter(f"{{{ns_w}}}t") if t.text]
        full_text = " ".join(texts)
        assert "[VENDOR]" in full_text
        # Should not have matched the shorter variant
        assert "[VENDOR-SHORT]" not in full_text


# ===================================================================
# restore_comment_authors (uncloaking)
# ===================================================================

class TestRestoreCommentAuthors:
    """Tests for restore_comment_authors()."""

    def test_restore_reverses_anonymization(self, tmp_path):
        # First, sanitize to anonymize authors
        path = make_docx_with_comments(
            tmp_path / "original.docx",
            "Agreement text.",
            [
                {"author": "Jane Smith", "initials": "JS", "text": "A comment"},
                {"author": "Bob Jones", "initials": "BJ", "text": "Another"},
            ],
        )
        sanitized_path = tmp_path / "sanitized.docx"
        mapping = process_comments(path, sanitized_path, CommentMode.SANITIZE)

        # Verify authors are anonymized
        authors = _get_comment_authors_from_xml(sanitized_path)
        assert "Jane Smith" not in authors
        assert "Bob Jones" not in authors

        # Now restore
        restored_path = tmp_path / "restored.docx"
        # Invert the mapping: {original -> label} becomes {label -> original}
        reverse_mapping = {label: original for original, label in mapping.items()}
        restore_comment_authors(sanitized_path, restored_path, reverse_mapping)

        # Verify original authors are back
        restored_authors = _get_comment_authors_from_xml(restored_path)
        assert "Jane Smith" in restored_authors
        assert "Bob Jones" in restored_authors

    def test_restore_no_op_with_empty_mapping(self, tmp_path):
        path = make_docx_with_comments(
            tmp_path / "original.docx",
            "Text.",
            [{"author": "Alice", "initials": "A", "text": "Comment"}],
        )
        # restore_comment_authors with empty mapping should be a no-op
        # (it returns early without creating output)
        restore_comment_authors(path, tmp_path / "output.docx", {})
        assert not (tmp_path / "output.docx").exists()
