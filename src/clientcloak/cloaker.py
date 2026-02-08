"""
Core cloaking logic for ClientCloak.

Orchestrates the full cloaking pipeline: security scan, text replacement,
metadata stripping, comment processing, and mapping file generation.

The heavy lifting (cross-run replacement, ZIP-level XML surgery, etc.) lives
in the dedicated handler modules. This module simply wires them together in
the correct order.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .comments import process_comments
from .docx_handler import (
    extract_all_text,
    load_document,
    replace_text_in_document,
    save_document,
)
from .mapping import create_mapping, get_cloak_replacements, save_mapping
from .metadata import inspect_metadata, strip_metadata
from .models import CloakConfig, CloakResult, DetectedEntity, MappingFile
from .security import scan_document

logger = logging.getLogger(__name__)


def _build_cloak_replacements(config: CloakConfig) -> dict[str, str]:
    """
    Build the original -> placeholder replacement dictionary from a config.

    This is the same logic used inside :func:`cloak_document` (step 3),
    extracted so callers can access the replacement dict independently
    (e.g. for filename sanitization).
    """
    mappings: dict[str, str] = {
        f"[{config.party_a_label}]": config.party_a_name,
        f"[{config.party_b_label}]": config.party_b_name,
    }
    for alias in config.party_a_aliases:
        mappings[f"[{alias.label}]"] = alias.name
    for alias in config.party_b_aliases:
        mappings[f"[{alias.label}]"] = alias.name
    mappings.update(config.additional_replacements)

    return {original: placeholder for placeholder, original in mappings.items()}


def sanitize_filename(filename: str, cloak_replacements: dict[str, str]) -> str:
    """
    Apply cloak replacements to a filename, case-insensitively.

    Replaces occurrences of original party names found in *filename* with
    their bracketed placeholder labels, using the same ``original -> placeholder``
    dictionary used for document content.  Replacements are applied
    longest-original-first so that "Acme Corporation" is matched before "Acme".

    Spaces in original names are treated flexibly: they also match underscores,
    hyphens, and dots that are commonly used as word separators in filenames.

    Args:
        filename: The original filename (stem + extension, or just the stem).
        cloak_replacements: Mapping of ``original_name -> "[Label]"`` as built
            during the cloaking pipeline.

    Returns:
        The filename with all recognised party names replaced by their labels.
    """
    # Sort by length of original (descending) so longer names match first,
    # preventing partial replacements (e.g. "Acme Corp" before "Acme").
    for original, placeholder in sorted(
        cloak_replacements.items(), key=lambda kv: len(kv[0]), reverse=True
    ):
        # Build a pattern where each literal space in the original name also
        # matches common filename separators: underscore, hyphen, dot.
        parts = re.escape(original).split(r"\ ")  # escaped spaces
        flexible_pattern = r"[\s_\-.]".join(parts)
        pattern = re.compile(flexible_pattern, re.IGNORECASE)
        filename = pattern.sub(placeholder, filename)
    return filename


def sanitize_filename_for_config(filename: str, config: CloakConfig) -> str:
    """
    Convenience wrapper: sanitize a filename using replacements derived from a
    :class:`CloakConfig`.

    Args:
        filename: The original filename (or stem) to sanitize.
        config: The cloaking configuration that defines party names and labels.

    Returns:
        The filename with party names replaced by their bracketed labels.
    """
    return sanitize_filename(filename, _build_cloak_replacements(config))


def cloak_document(
    input_path: str | Path,
    output_path: str | Path,
    mapping_path: str | Path,
    config: CloakConfig,
) -> CloakResult:
    """
    Run the full cloaking pipeline on a .docx document.

    Steps:
        1. Load the document.
        2. Run a security scan and collect findings.
        3. Build a replacement dictionary from party names and any additional
           replacements in the config.
        4. Apply text replacements throughout the document.
        5. Save the document. If ``config.strip_metadata`` is True, strip
           metadata from the saved file (ZIP-level operation).
        6. Process comments according to ``config.comment_mode``.
        7. Persist the mapping file for later uncloaking.
        8. Return a :class:`CloakResult` with everything that happened.

    Args:
        input_path: Path to the original .docx file.
        output_path: Path where the cloaked .docx will be written.
        mapping_path: Path where the JSON mapping file will be saved.
        config: A :class:`CloakConfig` controlling what gets replaced and how.

    Returns:
        A :class:`CloakResult` summarising the operation.

    Raises:
        FileNotFoundError: If input_path does not exist.
        docx_handler.DocumentLoadError: If the document cannot be loaded.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    mapping_path = Path(mapping_path)

    # --- 1. Load document ---
    doc = load_document(input_path)

    # --- 2. Security scan ---
    findings = scan_document(doc)
    if findings:
        logger.info("Security scan found %d issue(s).", len(findings))

    # --- 3. Build replacement dict (original -> placeholder) ---
    # The mapping file stores placeholder -> original. We build that first,
    # then invert it for the replacement pass.
    # Labels are wrapped in square brackets — the standard convention in
    # legal templates (e.g., [Customer], [Vendor], [Licensor]).
    mappings: dict[str, str] = {
        f"[{config.party_a_label}]": config.party_a_name,
        f"[{config.party_b_label}]": config.party_b_name,
    }
    for alias in config.party_a_aliases:
        mappings[f"[{alias.label}]"] = alias.name
        logger.info("Party A alias: %s -> [%s]", alias.name, alias.label)
    for alias in config.party_b_aliases:
        mappings[f"[{alias.label}]"] = alias.name
        logger.info("Party B alias: %s -> [%s]", alias.name, alias.label)
    # Additional replacements are already in placeholder -> original form.
    mappings.update(config.additional_replacements)

    logger.info("Total mappings to apply: %d", len(mappings))

    # Invert: original -> placeholder for text replacement.
    cloak_replacements: dict[str, str] = {
        original: placeholder for placeholder, original in mappings.items()
    }

    # --- 3b. Sanitize the output filename ---
    # Party names may appear in the filename (e.g. "Acme_BigCo_NDA.docx").
    # Replace them with bracketed labels so the filename does not leak
    # party identities.
    sanitized_stem = sanitize_filename(output_path.stem, cloak_replacements)
    output_path = output_path.with_name(sanitized_stem + output_path.suffix)

    # --- 4. Apply replacements ---
    replacement_count = replace_text_in_document(doc, cloak_replacements)
    logger.info("Applied %d text replacement(s).", replacement_count)

    # --- 5. Save document, then optionally strip metadata ---
    save_document(doc, output_path)

    metadata_report = None
    if config.strip_metadata:
        metadata_report = strip_metadata(
            input_path=output_path,
            output_path=output_path,
            preserve_comments=True,  # comments are handled separately below
        )
        logger.info("Metadata stripped from output document.")
    else:
        # Still capture metadata for the report without removing it.
        metadata_report = inspect_metadata(output_path)

    # --- 6. Process comments ---
    # Build content replacements for SANITIZE mode (same original -> placeholder).
    comment_author_mapping = process_comments(
        input_path=output_path,
        output_path=output_path,
        mode=config.comment_mode,
        content_replacements=cloak_replacements if cloak_replacements else None,
    )

    # --- 7. Build and save mapping file ---
    party_labels = {
        "party_a": config.party_a_label,
        "party_b": config.party_b_label,
    }
    mapping_file = create_mapping(
        original_file=input_path.name,
        mappings=mappings,
        party_labels=party_labels,
        comment_authors=comment_author_mapping if comment_author_mapping else None,
    )
    save_mapping(mapping_file, mapping_path)
    logger.info("Mapping file saved to %s.", mapping_path)

    # --- 8. Return result ---
    return CloakResult(
        mapping=mapping_file,
        security_findings=findings,
        metadata_report=metadata_report,
        replacements_applied=replacement_count,
        output_path=str(output_path),
    )


def preview_entities(
    input_path: str | Path,
    config: CloakConfig,
) -> list[DetectedEntity]:
    """
    Preview entities detected in a document before cloaking.

    This is a Phase 2 feature (GLiNER integration). Currently returns an
    empty list as a stub.

    When implemented, this will:
        1. Extract all text from the document via :func:`extract_all_text`.
        2. Run GLiNER entity detection on the extracted text.
        3. Filter out entities that match the configured party names (since
           those are already covered by explicit config).
        4. Return the remaining detected entities with suggested placeholders.

    Args:
        input_path: Path to the .docx file to scan for entities.
        config: A :class:`CloakConfig` providing party names and the
            GLiNER confidence threshold.

    Returns:
        A list of :class:`DetectedEntity` instances. Currently always empty.
    """
    # Phase 2: GLiNER entity detection will go here.
    # Stub implementation — load the document to validate the path, but
    # return an empty list until the detection model is integrated.
    _ = load_document(input_path)
    _ = config  # will use party names and gliner_threshold in Phase 2
    return []
