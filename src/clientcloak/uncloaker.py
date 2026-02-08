"""
Core uncloaking logic for ClientCloak.

Restores a cloaked document to its original form by reversing all placeholder
substitutions using the mapping file generated during cloaking.

The mapping file stores placeholder -> original values, which is exactly the
replacement direction needed for uncloaking. Comment author mappings are also
reversed so that anonymous reviewer labels are restored to real names.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .comments import restore_comment_authors
from .docx_handler import load_document, replace_text_in_document, save_document
from .mapping import load_mapping

logger = logging.getLogger(__name__)


def uncloak_document(
    input_path: str | Path,
    output_path: str | Path,
    mapping_path: str | Path,
) -> int:
    """
    Reverse the cloaking process on a redlined document.

    Steps:
        1. Load the cloaked/redlined document.
        2. Load the mapping file generated during cloaking.
        3. Build a replacement dictionary from the mapping. The mapping
           already stores placeholder -> original, which is the direction
           needed for uncloaking. Comment author labels are included as
           additional replacements.
        4. Apply all replacements throughout the document.
        5. Save the uncloaked document.

    Args:
        input_path: Path to the cloaked .docx file to restore.
        output_path: Path where the uncloaked .docx will be written.
        mapping_path: Path to the JSON mapping file from the cloaking step.

    Returns:
        The total number of individual text replacements made.

    Raises:
        FileNotFoundError: If input_path or mapping_path does not exist.
        docx_handler.DocumentLoadError: If the document cannot be loaded.
        pydantic.ValidationError: If the mapping file is malformed.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    mapping_path = Path(mapping_path)

    # --- 1. Load document ---
    doc = load_document(input_path)

    # --- 2. Load mapping ---
    mapping = load_mapping(mapping_path)
    logger.info(
        "Loaded mapping for '%s' with %d mapping(s) and %d comment author(s).",
        mapping.original_file,
        len(mapping.mappings),
        len(mapping.comment_authors),
    )

    # --- 3. Build replacements dict (placeholder -> original) ---
    # The mapping file already stores this in the correct direction.
    replacements: dict[str, str] = dict(mapping.mappings)

    # Include comment author labels -> original author names.
    # These are stored as anonymous_label -> original_author in the mapping.
    if mapping.comment_authors:
        replacements.update(mapping.comment_authors)

    # --- 4. Apply replacements ---
    # Use match_case=False so originals are restored verbatim (e.g.
    # "BigCo LLC" not "Bigco Llc" from title-case "Licensee").
    replacement_count = replace_text_in_document(doc, replacements, match_case=False)
    logger.info("Applied %d uncloak replacement(s).", replacement_count)

    # --- 5. Save ---
    save_document(doc, output_path)

    # --- 6. Restore comment authors ---
    if mapping.comment_authors:
        restore_comment_authors(output_path, output_path, mapping.comment_authors)
        logger.info("Restored %d comment author(s).", len(mapping.comment_authors))

    logger.info("Uncloaked document saved to %s.", output_path)

    return replacement_count
