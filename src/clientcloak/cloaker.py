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


def _strip_corporate_suffix(name: str) -> str:
    """Strip trailing corporate suffixes like Inc., LLC, Corp., etc."""
    return re.sub(
        r"\s+(?:Inc\.?|LLC|Corp\.?|Corporation|Ltd\.?|LLP|L\.P\.?|LP|"
        r"P\.C\.?|PC|Co\.?|Company|Group|Partners|Associates|"
        r"Enterprises|Holdings|International|Foundation|Technologies|"
        r"Solutions|Services|Systems)\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()


def sanitize_filename(filename: str, cloak_replacements: dict[str, str]) -> str:
    """
    Apply cloak replacements to a filename, case-insensitively.

    Replaces occurrences of original party names found in *filename* with
    their bracketed placeholder labels, using the same ``original -> placeholder``
    dictionary used for document content.  Replacements are applied
    longest-original-first so that "Acme Corporation" is matched before "Acme".

    Spaces in original names are treated flexibly: they match underscores,
    hyphens, dots, or no separator at all (CamelCase). Corporate suffixes
    (Inc., LLC, etc.) are also tried as optional so "MakingReign" matches
    "Making Reign Inc.".

    Args:
        filename: The original filename (stem + extension, or just the stem).
        cloak_replacements: Mapping of ``original_name -> "[Label]"`` as built
            during the cloaking pipeline.

    Returns:
        The filename with all recognised party names replaced by their labels.
    """
    # Build all variants: full name + name without corporate suffix.
    # Longer originals are tried first to prevent partial matches.
    variants: list[tuple[str, str]] = []
    for original, placeholder in cloak_replacements.items():
        variants.append((original, placeholder))
        stripped = _strip_corporate_suffix(original)
        if stripped != original and stripped:
            variants.append((stripped, placeholder))
    variants.sort(key=lambda kv: len(kv[0]), reverse=True)

    for original, placeholder in variants:
        # Build a pattern where spaces optionally match filename separators
        # (underscore, hyphen, dot) or no separator (CamelCase).
        parts = re.escape(original).split(r"\ ")  # escaped spaces
        flexible_pattern = r"[\s_\-.]?".join(parts)
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

    Extracts all text from the document, runs entity detection (regex and,
    when available, GLiNER), and filters out entities that match the
    configured party names.

    Args:
        input_path: Path to the .docx file to scan for entities.
        config: A :class:`CloakConfig` providing party names and the
            GLiNER confidence threshold.

    Returns:
        A list of :class:`DetectedEntity` instances sorted by count.
    """
    from .detector import detect_entities

    doc = load_document(input_path)
    text_fragments = extract_all_text(doc)
    full_text = "\n".join(text for text, _source in text_fragments)

    # Collect all party names (primary + aliases) for filtering
    party_names: list[str] = [config.party_a_name, config.party_b_name]
    for alias in config.party_a_aliases:
        party_names.append(alias.name)
    for alias in config.party_b_aliases:
        party_names.append(alias.name)

    return detect_entities(
        text=full_text,
        party_names=party_names,
        gliner_threshold=config.gliner_threshold,
    )
