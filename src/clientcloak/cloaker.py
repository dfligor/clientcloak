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
from .detector import _SUFFIX_PATTERN
from .docx_handler import (
    extract_all_text,
    load_document,
    replace_text_in_document,
    replace_text_in_xml,
    save_document,
)
from .mapping import create_mapping, get_cloak_replacements, save_mapping
from .metadata import inspect_metadata, strip_metadata
from .models import CloakConfig, CloakResult, DetectedEntity, MappingFile
from .security import scan_document

logger = logging.getLogger(__name__)


def _build_mappings_and_replacements(
    config: CloakConfig,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Build both the mapping dict and the cloak replacement dict from a config.

    Returns a 2-tuple of:
    - **mappings**: ``placeholder -> original`` (e.g. ``"[Vendor]" -> "Acme Inc."``)
      used for the mapping file.
    - **cloak_replacements**: ``original -> placeholder`` (e.g. ``"Acme Inc." -> "[Vendor]"``)
      used for text replacement in the document, comments, and filenames.

    This is the single source of truth for replacement-building logic,
    called by both :func:`build_cloak_replacements` and :func:`cloak_document`.
    """
    mappings: dict[str, str] = {}
    if config.party_a_name:
        mappings[f"[{config.party_a_label}]"] = config.party_a_name
    if config.party_b_name:
        mappings[f"[{config.party_b_label}]"] = config.party_b_name
    for alias in config.party_a_aliases:
        mappings[f"[{alias.label}]"] = alias.name
    for alias in config.party_b_aliases:
        mappings[f"[{alias.label}]"] = alias.name
    mappings.update(config.additional_replacements)

    cloak_replacements = {
        original: placeholder for placeholder, original in mappings.items()
    }

    # Add defined-term short forms (e.g. "Acme" for "Acme Wireless, Inc.").
    # These map to the SAME placeholder as the primary name and must be
    # injected after inversion since the {placeholder: original} dict can
    # only hold one original per key.
    for sf in config.party_a_short_forms:
        if sf and sf not in cloak_replacements:
            cloak_replacements[sf] = f"[{config.party_a_label}]"
    for sf in config.party_b_short_forms:
        if sf and sf not in cloak_replacements:
            cloak_replacements[sf] = f"[{config.party_b_label}]"

    cloak_replacements = _expand_content_replacements(cloak_replacements)
    return mappings, cloak_replacements


def build_cloak_replacements(config: CloakConfig) -> dict[str, str]:
    """
    Build the ``original -> placeholder`` replacement dictionary from a config.

    Convenience wrapper around :func:`_build_mappings_and_replacements` for
    callers that only need the replacement dict (e.g. filename sanitization).
    """
    _mappings, cloak_replacements = _build_mappings_and_replacements(config)
    return cloak_replacements


# Keep the old name as an alias for backwards compatibility.
_build_cloak_replacements = build_cloak_replacements


def _strip_corporate_suffix(name: str) -> str:
    """Strip trailing corporate suffixes like Inc., LLC, Corp., GmbH, etc.

    Handles both "Name LLC" and "Name, LLC" (comma-separated) forms.
    """
    return re.sub(
        rf",?\s+(?:{_SUFFIX_PATTERN})\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()


def _make_short_placeholder(
    base_placeholder: str, existing_placeholders: set[str]
) -> str:
    """Create a ``-Short`` variant of a bracketed placeholder.

    Given ``[Vendor]``, returns ``[Vendor-Short]``.  If that already exists in
    *existing_placeholders*, tries ``[Vendor-Short-2]``, ``[Vendor-Short-3]``,
    etc., until a unique name is found.
    """
    label = base_placeholder.strip("[]")
    candidate = f"[{label}-Short]"
    if candidate not in existing_placeholders:
        return candidate
    n = 2
    while f"[{label}-Short-{n}]" in existing_placeholders:
        n += 1
    return f"[{label}-Short-{n}]"


_INITIALISM_STOP_WORDS = frozenset({"the", "of", "and", "a", "an", "in", "for", "to", "at", "by"})


def _generate_initialisms(name: str) -> list[str]:
    """Generate plausible initialisms/acronyms from a company name.

    "Bank of New York Mellon" -> ["BNYM", "BNY"]
    Only returns initialisms of 2+ characters.
    """
    # Strip corporate suffix first
    core = _strip_corporate_suffix(name)
    if not core:
        return []
    words = core.split()
    significant = [w for w in words if w.lower() not in _INITIALISM_STOP_WORDS]
    if len(significant) < 2:
        return []

    # Full initialism: first letter of each significant word
    full = "".join(w[0].upper() for w in significant)
    results = [full]

    # Also try dropping the last word (common abbreviation pattern)
    if len(significant) > 2:
        shorter = "".join(w[0].upper() for w in significant[:-1])
        if len(shorter) >= 2:
            results.append(shorter)

    return results


def _expand_content_replacements(
    cloak_replacements: dict[str, str],
    document_text: str | None = None,
) -> dict[str, str]:
    """Expand replacements with suffix-stripped variants.

    For each party name like "BigOrg Group PBC", iteratively strips corporate
    suffixes to produce all shortened forms::

        "BigOrg Group PBC" -> "BigOrg Group" -> "BigOrg"

    Each intermediate form is added as a replacement mapping to the same
    placeholder, so shortened references and defined terms in the document
    body, comments, and filenames are all caught.

    If *document_text* is provided, also generates initialisms/acronyms from
    company names and adds them as replacements when they appear in the text.

    Existing entries are never overwritten, and stripping stops when the
    name is fully consumed (single word with a suffix, e.g. "Partners").
    """
    expanded = dict(cloak_replacements)
    for original, placeholder in cloak_replacements.items():
        current = original
        while True:
            stripped = _strip_corporate_suffix(current)
            if not stripped or stripped == current:
                break
            if stripped not in expanded:
                expanded[stripped] = placeholder
            current = stripped

    # Generate initialisms and add if they appear in the document
    if document_text:
        doc_upper = document_text.upper()
        for original, placeholder in cloak_replacements.items():
            for initialism in _generate_initialisms(original):
                if initialism not in expanded and initialism in doc_upper:
                    expanded[initialism] = placeholder

    return expanded


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
    return sanitize_filename(filename, build_cloak_replacements(config))


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
    # Uses the shared helper so the logic is defined in one place.
    # mappings: placeholder -> original (for the mapping file)
    # cloak_replacements: original -> placeholder (for text replacement)
    mappings, cloak_replacements = _build_mappings_and_replacements(config)

    for alias in config.party_a_aliases:
        logger.info("Party A alias: %s -> [%s]", alias.name, alias.label)
    for alias in config.party_b_aliases:
        logger.info("Party B alias: %s -> [%s]", alias.name, alias.label)
    logger.info("Total mappings to apply: %d", len(mappings))

    # --- 3b. Sanitize the output filename ---
    # Party names may appear in the filename (e.g. "Acme_BigCo_NDA.docx").
    # Replace them with bracketed labels so the filename does not leak
    # party identities.
    sanitized_stem = sanitize_filename(output_path.stem, cloak_replacements)
    output_path = output_path.with_name(sanitized_stem + output_path.suffix)

    # --- 3c. Assign distinct placeholders to suffix-stripped variants ---
    # After expansion, variants like "AiSim" (from "AiSim Inc.") share the
    # same placeholder "[Company]".  Give each variant its own "-Short"
    # placeholder so the mapping file can restore them independently.
    # This runs AFTER filename sanitization (which benefits from shared
    # placeholders) but BEFORE the document replacement pass.
    base_originals = set(mappings.values())
    existing_placeholders = set(mappings.keys())
    for original in list(cloak_replacements.keys()):
        if original not in base_originals:
            old_placeholder = cloak_replacements[original]
            new_placeholder = _make_short_placeholder(old_placeholder, existing_placeholders)
            cloak_replacements[original] = new_placeholder
            mappings[new_placeholder] = original
            existing_placeholders.add(new_placeholder)

    # --- 4. Apply replacements ---
    replacement_count = replace_text_in_document(doc, cloak_replacements)
    logger.info("Applied %d text replacement(s).", replacement_count)

    # --- 5. Save document, then optionally strip metadata ---
    save_document(doc, output_path)

    # --- 5b. Replace text in tracked changes (XML-level) ---
    # python-docx doesn't expose runs inside <w:ins>/<w:del> elements.
    # This catches originals in tracked changes, text boxes, footnotes.
    xml_count = replace_text_in_xml(output_path, cloak_replacements)
    if xml_count:
        replacement_count += xml_count
        logger.info("Applied %d XML-level replacement(s) (tracked changes, etc.).", xml_count)

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
    # Expand replacements with suffix-stripped variants so shortened names
    # (e.g. "Making Reign" for "Making Reign Inc.") are also caught.
    # Pass full document text for initialism detection.
    text_fragments = extract_all_text(doc)
    full_text = "\n".join(t for t, _src in text_fragments)
    comment_replacements = _expand_content_replacements(cloak_replacements, document_text=full_text)
    comment_author_mapping = process_comments(
        input_path=output_path,
        output_path=output_path,
        mode=config.comment_mode,
        content_replacements=comment_replacements if comment_replacements else None,
    )

    # --- 7. Build and save mapping file ---
    party_labels = {
        "party_a": config.party_a_label,
        "party_b": config.party_b_label,
    }
    # process_comments returns {original_author: label}.
    # MappingFile stores {label: original_author} (uncloaking direction).
    comment_authors_for_mapping = {
        label: original for original, label in comment_author_mapping.items()
    } if comment_author_mapping else None
    mapping_file = create_mapping(
        original_file=input_path.name,
        mappings=mappings,
        party_labels=party_labels,
        comment_authors=comment_authors_for_mapping,
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
    party_names: list[str] = [n for n in (config.party_a_name, config.party_b_name) if n]
    for alias in config.party_a_aliases:
        party_names.append(alias.name)
    for alias in config.party_b_aliases:
        party_names.append(alias.name)

    return detect_entities(
        text=full_text,
        party_names=party_names,
        gliner_threshold=config.gliner_threshold,
        use_gliner=config.use_gliner,
    )
