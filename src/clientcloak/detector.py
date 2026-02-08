"""
Regex-based entity detection with pluggable GLiNER backend.

Detects structured PII (emails, phone numbers, SSNs, EINs, dollar amounts)
using regex patterns. Designed so GLiNER can be added later as an additional
backend — both feed the same DetectedEntity model.
"""

from __future__ import annotations

import re
from collections import Counter

from .models import DetectedEntity

# Regex patterns for structured PII. Each key becomes the entity_type.
ENTITY_PATTERNS: dict[str, str] = {
    "EMAIL": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "PHONE": r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
    "EIN": r"\b\d{2}-\d{7}\b",
    "AMOUNT": r"\$[\d,]+(?:\.\d{2})?\b",
}

# Pre-compile for performance
_COMPILED_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern) for name, pattern in ENTITY_PATTERNS.items()
}


def generate_placeholder(entity_type: str, index: int) -> str:
    """
    Generate a bracketed placeholder for a detected entity.

    Uses title-cased type name with a numeric suffix:
    ``[Email-1]``, ``[Phone-2]``, ``[Ssn-1]``, ``[Amount-3]``.

    Args:
        entity_type: The entity type string (e.g. "EMAIL", "PHONE").
        index: 1-based index for this entity within its type.

    Returns:
        A bracketed placeholder string.
    """
    # Title-case the type for readability: EMAIL -> Email, SSN -> Ssn
    label = entity_type.capitalize()
    return f"[{label}-{index}]"


def deduplicate_entities(entities: list[DetectedEntity]) -> list[DetectedEntity]:
    """
    Merge duplicate entities (same text and type), summing counts and
    keeping the highest confidence score.

    Args:
        entities: List of detected entities, possibly with duplicates.

    Returns:
        Deduplicated list with merged counts and max confidence.
    """
    seen: dict[tuple[str, str], DetectedEntity] = {}
    for entity in entities:
        key = (entity.text, entity.entity_type)
        if key in seen:
            existing = seen[key]
            seen[key] = existing.model_copy(update={
                "count": existing.count + entity.count,
                "confidence": max(existing.confidence, entity.confidence),
            })
        else:
            seen[key] = entity
    return list(seen.values())


def detect_entities_regex(text: str) -> list[DetectedEntity]:
    """
    Run all regex patterns against the text and return detected entities.

    Each unique match is returned with its occurrence count and a generated
    placeholder. Confidence is always 1.0 for regex matches.

    Args:
        text: The full document text to scan.

    Returns:
        List of DetectedEntity instances, one per unique match text per type.
    """
    entities: list[DetectedEntity] = []
    for entity_type, pattern in _COMPILED_PATTERNS.items():
        matches = pattern.findall(text)
        if not matches:
            continue
        counts = Counter(matches)
        for idx, (match_text, count) in enumerate(counts.most_common(), start=1):
            entities.append(DetectedEntity(
                text=match_text,
                entity_type=entity_type,
                confidence=1.0,
                count=count,
                suggested_placeholder=generate_placeholder(entity_type, idx),
            ))
    return entities


def detect_entities(
    text: str,
    party_names: list[str] | None = None,
    gliner_threshold: float = 0.5,
) -> list[DetectedEntity]:
    """
    Detect entities in text using all available backends.

    This is the single public entry point. Callers never need to know
    which backend produced the results.

    Steps:
        1. Always run regex-based detection.
        2. Try to import and run GLiNER (Phase 2 — graceful skip if not
           installed).
        3. Merge and deduplicate results from all backends.
        4. Filter out entities whose text matches a known party name.
        5. Return sorted by count descending.

    Args:
        text: The full document text to scan.
        party_names: Optional list of party names to exclude from results
            (these are already handled by party config).
        gliner_threshold: Confidence threshold for GLiNER results.

    Returns:
        List of DetectedEntity instances sorted by count (descending).
    """
    # 1. Regex detection (always runs)
    entities = detect_entities_regex(text)

    # 2. GLiNER detection (Phase 2 — skip if not installed)
    try:
        from gliner import GLiNER  # type: ignore[import-not-found]  # noqa: F401
        # Phase 2: GLiNER inference will go here.
        # gliner_entities = _run_gliner(text, gliner_threshold)
        # entities.extend(gliner_entities)
    except ImportError:
        pass

    # 3. Deduplicate
    entities = deduplicate_entities(entities)

    # 4. Filter out known party names
    if party_names:
        lower_names = {name.lower() for name in party_names if name}
        entities = [
            e for e in entities
            if e.text.lower() not in lower_names
        ]

    # 5. Sort by count descending, then by text for stability
    entities.sort(key=lambda e: (-e.count, e.text))

    return entities
