"""
Comment inspection, anonymization, and sanitization for .docx files.

Comments in a .docx file live in ``word/comments.xml`` as a flat list of
``<w:comment>`` elements:

.. code-block:: xml

    <w:comments xmlns:w="...">
        <w:comment w:id="0" w:author="John Smith" w:initials="JS"
                   w:date="2026-01-15T10:30:00Z">
            <w:p><w:r><w:t>Comment text</w:t></w:r></w:p>
        </w:comment>
    </w:comments>

The main document body (``word/document.xml``) references comments via:
    - ``<w:commentRangeStart w:id="0"/>``
    - ``<w:commentRangeEnd w:id="0"/>``
    - ``<w:commentReference w:id="0"/>``

When stripping comments we must remove *both* the entries in comments.xml
and the range/reference markers in document.xml, otherwise Word may show
repair prompts.

All functions operate on the .docx as a ZIP archive (``zipfile`` module).
When modifying, the input ZIP is read in full and a new ZIP is written to
the output path, so input == output is safe.
"""

import re
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from .models import CommentAuthor, CommentInfo, CommentMode

# ---------------------------------------------------------------------------
# XML namespaces
# ---------------------------------------------------------------------------

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Register the prefix so ET.tostring preserves "w:" in output.
ET.register_namespace("w", _NS_W)

# Convenience tags (fully qualified)
_TAG_COMMENT = f"{{{_NS_W}}}comment"
_TAG_COMMENTS = f"{{{_NS_W}}}comments"
_TAG_P = f"{{{_NS_W}}}p"
_TAG_R = f"{{{_NS_W}}}r"
_TAG_T = f"{{{_NS_W}}}t"
_TAG_COMMENT_RANGE_START = f"{{{_NS_W}}}commentRangeStart"
_TAG_COMMENT_RANGE_END = f"{{{_NS_W}}}commentRangeEnd"
_TAG_COMMENT_REFERENCE = f"{{{_NS_W}}}commentReference"

# Attribute keys are in the Word namespace
_ATTR_ID = f"{{{_NS_W}}}id"
_ATTR_AUTHOR = f"{{{_NS_W}}}author"
_ATTR_INITIALS = f"{{{_NS_W}}}initials"
_ATTR_DATE = f"{{{_NS_W}}}date"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inspect_comments(
    doc_path: Path,
) -> tuple[list[CommentInfo], list[CommentAuthor]]:
    """
    Extract all comments and unique authors from a .docx file.

    Parses ``word/comments.xml`` directly from the ZIP archive and returns
    structured data for each comment and each unique author.  Authors are
    assigned suggested labels ("Reviewer A", "Reviewer B", ...) in the
    order they are first encountered.

    The file is **not** modified.

    Args:
        doc_path: Path to the .docx file to inspect.

    Returns:
        A 2-tuple of:
        - A list of :class:`CommentInfo` instances (one per comment).
        - A list of :class:`CommentAuthor` instances (one per unique author).

    Raises:
        FileNotFoundError: If doc_path does not exist.
        zipfile.BadZipFile: If the file is not a valid ZIP / .docx.
    """
    doc_path = Path(doc_path)

    with zipfile.ZipFile(doc_path, "r") as zf:
        if "word/comments.xml" not in zf.namelist():
            return [], []

        xml_data = zf.read("word/comments.xml")

    root = ET.fromstring(xml_data)

    comments: list[CommentInfo] = []
    author_order: list[str] = []  # preserves first-seen order
    author_counts: dict[str, int] = {}
    author_initials_map: dict[str, str] = {}

    for idx, comment_el in enumerate(root.findall(_TAG_COMMENT)):
        comment_id = comment_el.get(_ATTR_ID, "")
        author = comment_el.get(_ATTR_AUTHOR, "")
        initials = comment_el.get(_ATTR_INITIALS, "")
        date = comment_el.get(_ATTR_DATE, "")
        text = _extract_comment_text(comment_el)

        comments.append(
            CommentInfo(
                id=comment_id,
                author=author,
                author_initials=initials,
                date=date,
                text=text,
                paragraph_index=idx,
            )
        )

        # Track unique authors in first-seen order
        if author not in author_counts:
            author_order.append(author)
            author_counts[author] = 0
            author_initials_map[author] = initials
        author_counts[author] += 1

    # Build author list with suggested labels
    authors: list[CommentAuthor] = []
    for i, name in enumerate(author_order):
        label = _reviewer_label(i)
        authors.append(
            CommentAuthor(
                name=name,
                initials=author_initials_map[name],
                comment_count=author_counts[name],
                suggested_label=label,
            )
        )

    return comments, authors


def process_comments(
    input_path: Path,
    output_path: Path,
    mode: CommentMode,
    author_mapping: dict[str, str] | None = None,
    content_replacements: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    Process comments in a .docx file according to the specified mode.

    Reads the input ZIP fully into memory and writes a new ZIP to
    *output_path*, so ``input_path == output_path`` is safe.

    Modes:
        **KEEP**
            Leave comments completely untouched.

        **STRIP**
            Remove all ``<w:comment>`` elements from ``word/comments.xml``
            **and** remove ``commentRangeStart``, ``commentRangeEnd``, and
            ``commentReference`` markers from ``word/document.xml``.

        **SANITIZE**
            Replace the ``w:author`` and ``w:initials`` attributes on every
            ``<w:comment>`` element **and** apply *content_replacements* to
            the text runs inside each comment.  If *author_mapping* is
            provided it is used directly (original name -> replacement label);
            otherwise a default mapping is generated ("Reviewer A", ...).

    Args:
        input_path: Path to the source .docx file.
        output_path: Path for the processed .docx file.
        mode: The :class:`CommentMode` to apply.
        author_mapping: Optional dict of ``original_author -> replacement_label``.
            Used by ANONYMIZE and SANITIZE modes.  If ``None``, a default
            mapping is generated from the authors present in the file.
        content_replacements: Optional dict of ``original_text -> replacement_text``
            applied to comment body text in SANITIZE mode only.

    Returns:
        The effective author mapping that was applied (original name -> label).
        Empty dict if mode is STRIP.

    Raises:
        FileNotFoundError: If input_path does not exist.
        zipfile.BadZipFile: If the file is not a valid ZIP / .docx.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    author_mapping = author_mapping or {}
    content_replacements = content_replacements or {}

    input_bytes = input_path.read_bytes()
    effective_mapping: dict[str, str] = {}

    buf = BytesIO()
    with zipfile.ZipFile(BytesIO(input_bytes), "r") as zin, \
         zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:

        for item in zin.infolist():
            raw = zin.read(item.filename)

            if mode == CommentMode.KEEP:
                pass  # Leave comments untouched

            elif mode == CommentMode.STRIP:
                if item.filename == "word/comments.xml":
                    raw = _strip_all_comments(raw)
                elif item.filename == "word/document.xml":
                    raw = _strip_comment_references(raw)

            elif mode == CommentMode.SANITIZE:
                if item.filename == "word/comments.xml":
                    raw, effective_mapping = _anonymize_comments(
                        raw,
                        author_mapping,
                        content_replacements,
                    )

            zout.writestr(item, raw)

    output_path.write_bytes(buf.getvalue())
    return effective_mapping


def restore_comment_authors(
    input_path: Path,
    output_path: Path,
    author_mapping: dict[str, str],
) -> None:
    """
    Restore original author names in comments during uncloaking.

    Reads ``word/comments.xml`` from the ZIP and replaces anonymous labels
    (e.g. "Reviewer A") back to the original author names using the mapping
    from the mapping file.  Uses regex on raw XML to preserve namespace
    prefixes.

    Args:
        input_path: Path to the cloaked .docx file.
        output_path: Path for the restored .docx file.
        author_mapping: Dict of ``anonymous_label -> original_author``
            (as stored in the mapping file's ``comment_authors`` field).
    """
    if not author_mapping:
        return

    input_path = Path(input_path)
    output_path = Path(output_path)

    input_bytes = input_path.read_bytes()

    buf = BytesIO()
    with zipfile.ZipFile(BytesIO(input_bytes), "r") as zin, \
         zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:

        for item in zin.infolist():
            raw = zin.read(item.filename)

            if item.filename == "word/comments.xml":
                xml_str = raw.decode("utf-8")

                # Reverse mapping: label -> original name
                def _restore_attrs(match: re.Match) -> str:
                    tag = match.group(0)
                    author_match = re.search(r'w:author="([^"]*)"', tag)
                    if author_match:
                        label = author_match.group(1)
                        if label in author_mapping:
                            original_name = author_mapping[label]
                            original_initials = generate_initials(original_name)
                            tag = re.sub(
                                r'w:author="[^"]*"',
                                f'w:author="{original_name}"',
                                tag,
                            )
                            tag = re.sub(
                                r'w:initials="[^"]*"',
                                f'w:initials="{original_initials}"',
                                tag,
                            )
                    return tag

                xml_str = re.sub(r"<w:comment\s[^>]*?>", _restore_attrs, xml_str)
                raw = xml_str.encode("utf-8")

            zout.writestr(item, raw)

    output_path.write_bytes(buf.getvalue())


def generate_initials(label: str) -> str:
    """
    Generate initials from a reviewer label.

    Takes the first letter of each word and uppercases it.

    Examples:
        >>> generate_initials("Reviewer A")
        'RA'
        >>> generate_initials("Outside Counsel")
        'OC'

    Args:
        label: A human-readable label such as "Reviewer A".

    Returns:
        Uppercase initials string.
    """
    return "".join(word[0].upper() for word in label.split() if word)


# ---------------------------------------------------------------------------
# Internal: STRIP mode
# ---------------------------------------------------------------------------

def _strip_all_comments(xml_data: bytes) -> bytes:
    """
    Remove all ``<w:comment>`` elements from ``word/comments.xml``.

    Uses regex removal on the raw XML to avoid re-serializing through
    ElementTree, which would mangle namespace prefixes and cause Word
    to report "unreadable content".

    Preserves the ``<w:comments>`` wrapper so the file remains well-formed.

    Args:
        xml_data: Raw bytes of ``word/comments.xml``.

    Returns:
        Cleaned XML bytes.
    """
    xml_str = xml_data.decode("utf-8")
    xml_str = re.sub(r"<w:comment\s[^>]*>[\s\S]*?</w:comment>", "", xml_str)
    return xml_str.encode("utf-8")


def _strip_comment_references(xml_data: bytes) -> bytes:
    """
    Remove all comment range markers and references from ``word/document.xml``.

    Uses regex removal on the raw XML to avoid re-serializing through
    ElementTree, which would mangle namespace prefixes (e.g. ``mc:`` becomes
    ``ns0:``) and cause Word to report "unreadable content".

    Targets self-closing elements:
        - ``<w:commentRangeStart ... />``
        - ``<w:commentRangeEnd ... />``
        - ``<w:commentReference ... />``

    Args:
        xml_data: Raw bytes of ``word/document.xml``.

    Returns:
        Cleaned XML bytes with comment markers removed.
    """
    xml_str = xml_data.decode("utf-8")
    xml_str = re.sub(r"<w:commentRangeStart\b[^>]*?/>", "", xml_str)
    xml_str = re.sub(r"<w:commentRangeEnd\b[^>]*?/>", "", xml_str)
    xml_str = re.sub(r"<w:commentReference\b[^>]*?/>", "", xml_str)
    return xml_str.encode("utf-8")


# ---------------------------------------------------------------------------
# Internal: ANONYMIZE / SANITIZE mode
# ---------------------------------------------------------------------------

def _anonymize_comments(
    xml_data: bytes,
    author_mapping: dict[str, str],
    content_replacements: dict[str, str],
) -> tuple[bytes, dict[str, str]]:
    """
    Anonymize comment authors and optionally sanitize comment text.

    Uses ElementTree for read-only parsing to build the author mapping,
    then applies changes via regex on the raw XML to preserve namespace
    prefixes and avoid Word "unreadable content" errors.

    Args:
        xml_data: Raw bytes of ``word/comments.xml``.
        author_mapping: Explicit original_name -> replacement_label mapping.
            May be partial or empty; missing authors get auto-generated labels.
        content_replacements: Text substitutions to apply to comment body
            text (SANITIZE mode).  Empty dict skips text replacement.

    Returns:
        A 2-tuple of:
        - The modified XML bytes.
        - The effective author mapping used (including auto-generated entries).
    """
    # Step 1: Parse read-only to build the effective author mapping
    root = ET.fromstring(xml_data)
    effective_mapping = dict(author_mapping)
    auto_index = len(author_mapping)

    for comment_el in root.findall(_TAG_COMMENT):
        original_author = comment_el.get(_ATTR_AUTHOR, "")

        if original_author and original_author not in effective_mapping:
            label = _reviewer_label(auto_index)
            effective_mapping[original_author] = label
            auto_index += 1

    # Step 2: Apply changes via regex on raw XML to preserve namespaces
    xml_str = xml_data.decode("utf-8")

    def _replace_attrs(match: re.Match) -> str:
        tag = match.group(0)
        author_match = re.search(r'w:author="([^"]*)"', tag)
        if author_match:
            original_author = author_match.group(1)
            if original_author in effective_mapping:
                new_label = effective_mapping[original_author]
                new_initials = generate_initials(new_label)
                tag = re.sub(r'w:author="[^"]*"', f'w:author="{new_label}"', tag)
                tag = re.sub(r'w:initials="[^"]*"', f'w:initials="{new_initials}"', tag)
        return tag

    xml_str = re.sub(r"<w:comment\s[^>]*?>", _replace_attrs, xml_str)

    # Apply content replacements (SANITIZE mode)
    # Case-insensitive, longest-first to prevent partial-match clobbering.
    if content_replacements:
        sorted_replacements = sorted(
            content_replacements.items(), key=lambda kv: len(kv[0]), reverse=True
        )
        for original, replacement in sorted_replacements:
            xml_str = re.sub(
                re.escape(original), lambda _, r=replacement: r, xml_str,
                flags=re.IGNORECASE,
            )

    return xml_str.encode("utf-8"), effective_mapping


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_comment_text(comment_el: ET.Element) -> str:
    """
    Extract the plain-text content from a ``<w:comment>`` element.

    Concatenates the text of all ``<w:t>`` descendants, joining paragraphs
    with newlines and runs with no separator (matching Word's rendering).

    Args:
        comment_el: A ``<w:comment>`` element.

    Returns:
        The comment's plain-text content.
    """
    paragraphs: list[str] = []

    for p_el in comment_el.findall(_TAG_P):
        runs: list[str] = []
        for t_el in p_el.iter(_TAG_T):
            if t_el.text:
                runs.append(t_el.text)
        paragraphs.append("".join(runs))

    return "\n".join(paragraphs)


def _reviewer_label(index: int) -> str:
    """
    Generate a reviewer label from a zero-based index.

    Uses uppercase letters: 0 -> "Reviewer A", 25 -> "Reviewer Z",
    26 -> "Reviewer AA", etc.

    Args:
        index: Zero-based index of the author in first-seen order.

    Returns:
        A label string such as "Reviewer A" or "Reviewer AA".
    """
    if index < 26:
        suffix = chr(ord("A") + index)
    else:
        # For more than 26 authors, use double letters: AA, AB, ...
        first = chr(ord("A") + (index // 26) - 1)
        second = chr(ord("A") + (index % 26))
        suffix = first + second

    return f"Reviewer {suffix}"
