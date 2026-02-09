"""
Regex-based entity detection with pluggable GLiNER backend.

Detects structured PII (emails, phone numbers, SSNs, EINs, dollar amounts,
addresses, URLs) using regex patterns. Also detects party names from legal
preambles using defined-term patterns. Designed so GLiNER can be added later
as an additional backend — both feed the same DetectedEntity model.
"""

from __future__ import annotations

import re
from collections import Counter

from .models import DetectedEntity

# Regex patterns for structured PII. Each key becomes the entity_type.
ENTITY_PATTERNS: dict[str, str] = {
    "EMAIL": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "PHONE": r"(?<![A-Za-z0-9])(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
    "EIN": r"\b\d{2}-\d{7}\b",
    "AMOUNT": r"\$[\d,]+(?:\.\d{2})?\b",
    "ADDRESS": r"\d+\s+[A-Za-z\s\.]+(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|Way|Lane|Ln)\.?[,\s]+(?:(?:Suite|Ste|Apt|Unit)\s+\d+[,\s]+)?[A-Za-z\s]+,\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?",
    "URL": r"(?:https?://)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s,)]*)?(?<![.,)])",
}

# Pre-compile for performance
_COMPILED_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern) for name, pattern in ENTITY_PATTERNS.items()
}

# Context-based person name patterns (require surrounding context to avoid false positives).
# Each pattern should have a single capture group for the person's name.
_PERSON_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:^|\n)\s*Name:\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)\s*$", re.MULTILINE),
    re.compile(r"(?:^|\n)\s*By:\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)\s*$", re.MULTILINE),
    re.compile(r"(?:^|\n)\s*Signature:\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)\s*$", re.MULTILINE),
    re.compile(r"(?:^|\n)\s*Attn:\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)", re.MULTILINE),
]


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

    URL matches that are substrings of detected EMAIL matches are filtered
    out (e.g., ``softwareexperts.io`` from ``michael@softwareexperts.io``).

    Args:
        text: The full document text to scan.

    Returns:
        List of DetectedEntity instances, one per unique match text per type.
    """
    entities: list[DetectedEntity] = []
    email_texts: set[str] = set()

    # First pass: collect all matches by type
    matches_by_type: dict[str, Counter] = {}
    for entity_type, pattern in _COMPILED_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            matches_by_type[entity_type] = Counter(matches)

    # Collect email texts for URL dedup
    if "EMAIL" in matches_by_type:
        email_texts = set(matches_by_type["EMAIL"].keys())

    for entity_type, counts in matches_by_type.items():
        idx = 0
        for match_text, count in counts.most_common():
            # Filter out URL matches that are substrings of detected emails
            if entity_type == "URL" and email_texts:
                if any(match_text in email for email in email_texts):
                    continue
            idx += 1
            entities.append(DetectedEntity(
                text=match_text,
                entity_type=entity_type,
                confidence=1.0,
                count=count,
                suggested_placeholder=generate_placeholder(entity_type, idx),
            ))

    # Context-based person name detection
    person_counts: Counter = Counter()
    for pattern in _PERSON_PATTERNS:
        for match in pattern.finditer(text):
            person_counts[match.group(1).strip()] += 1
    for idx, (name, count) in enumerate(person_counts.most_common(), 1):
        entities.append(DetectedEntity(
            text=name,
            entity_type="PERSON",
            confidence=1.0,
            count=count,
            suggested_placeholder=generate_placeholder("PERSON", idx),
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


# ---------------------------------------------------------------------------
# Party name detection from legal preambles
# ---------------------------------------------------------------------------

# Corporate suffixes used to identify company names in preamble text.
# Compound / longer patterns come before shorter ones so the regex
# alternation matches the most specific form first.
_CORPORATE_SUFFIXES = (
    # --- Compound forms (must precede their shorter components) ---
    r"Pty\.?\s+Ltd\.?",          # Australia
    r"Public\s+Benefit\s+Corporation",
    # --- Period-spaced abbreviations (before plain versions) ---
    r"L\.L\.L\.P\.?",
    r"P\.L\.L\.C\.?",
    r"P\.L\.C\.?",
    r"L\.L\.C\.?",
    r"L\.L\.P\.?",
    r"L\.P\.?",
    r"P\.C\.?",
    r"S\.R\.L\.?",
    r"S\.p\.A\.?",
    r"S\.A\.?",
    r"G\.P\.?",
    r"N\.A\.?",
    r"B\.V\.?",
    r"K\.K\.?",
    r"G\.K\.?",
    # --- Multi-word plain forms ---
    r"Corporation",
    r"Company",
    r"International",
    # --- Standard US/UK ---
    r"Inc\.?",
    r"LLC",
    r"LLLP",
    r"LLP",
    r"LP",
    r"Corp\.?",
    r"Ltd\.?",
    r"Limited",
    r"PLLC",
    r"PLC",
    r"PBC",
    r"PC",
    r"Co\.?",
    r"GP",
    r"NA",
    # --- International ---
    r"GmbH",
    r"SAS",
    r"SRL",
    r"SpA",
    r"BV",
    r"SA",
    r"FSB",
    r"DST",
    r"Pty\.?",                   # standalone (after Pty Ltd above)
    r"Oyj",                      # Finland (public) — before Oy
    r"Oy",                       # Finland
    # --- Generic descriptive suffixes ---
    r"Group",
    r"Partners",
    r"Associates",
    r"Enterprises",
    r"Holdings",
    r"Foundation",
    r"Technologies",
    r"Solutions",
    r"Services",
    r"Systems",
)

_SUFFIX_PATTERN = "|".join(_CORPORATE_SUFFIXES)

# Pattern 1: Defined-term — "Company Name Inc. (the "Label")" or ("Label")
# Handles both straight quotes (" ") and curly quotes (\u201c \u201d).
_DEFINED_TERM_RE = re.compile(
    rf'((?:[A-Z][A-Za-z&\-\']+ )*(?:{_SUFFIX_PATTERN}))(?:,?)\s+'
    r'(?:\((?:the\s+)?["\u201c]([^"\u201d]+)["\u201d]\))',
    re.UNICODE,
)

# Pattern 2: "Dear Name," — catches addressee in letter-format agreements.
_DEAR_RE = re.compile(
    rf'Dear\s+((?:[A-Z][A-Za-z&\-\']+ )*(?:{_SUFFIX_PATTERN})),',
    re.UNICODE,
)


# Default role labels assigned when the defined term is the company name itself.
_DEFAULT_ROLE_LABELS = ("Company", "Counterparty")


def _label_resembles_name(label: str, name: str) -> bool:
    """Check if a defined-term label is derived from the company name itself.

    Returns True when the label is essentially the company name (or a
    shortened form of it), which would defeat the purpose of cloaking.

    Catches exact matches and leading-word subsets::

        name="AiSim Inc."          label="AiSim"       -> True  (exact core)
        name="BigOrg Group PBC"    label="BigOrg"       -> True  (leading word)
        name="BigOrg Group PBC"    label="BigOrg Group" -> True  (exact core)
        name="AiSim Inc."          label="Licensee"     -> False (real role)
    """
    # Strip suffix to get the core name, e.g. "AiSim Inc." -> "AiSim"
    suffix_pattern = re.compile(
        r"\s+(?:" + _SUFFIX_PATTERN + r")\s*$",
        re.IGNORECASE,
    )
    core = suffix_pattern.sub("", name).strip()
    # Compare case-insensitively, ignoring whitespace differences
    label_norm = re.sub(r"\s+", " ", label.strip()).lower()
    core_norm = re.sub(r"\s+", " ", core).lower()
    name_norm = re.sub(r"\s+", " ", name.strip()).lower()

    # Exact match with core or full name
    if label_norm in (core_norm, name_norm) or core_norm == label_norm:
        return True

    # Label is a leading-word subset of the core name.
    # e.g., "BigOrg" is the first word of "BigOrg Group".
    label_words = label_norm.split()
    core_words = core_norm.split()
    if label_words and len(label_words) < len(core_words):
        if core_words[: len(label_words)] == label_words:
            return True

    return False


def detect_party_names(text: str) -> list[dict[str, str]]:
    """
    Detect company/party names from a legal preamble using defined-term patterns.

    Searches the first ~2000 characters of the text for:

    1. **Defined-term pattern** — ``Company Name Inc. (the "Label")`` with
       corporate suffix filtering (Inc., LLC, Corp., Ltd., LLP, etc.).
       Handles both straight and curly quotes.

    2. **"Dear Name," pattern** — catches addressee in letter-format
       agreements, also requiring a corporate suffix.

    If a defined-term label appears to be the company name itself (e.g.,
    ``AiSim Inc. (the "AiSim")``), it is replaced with a generic role
    label ("Company", "Counterparty") since using the name as the label
    would defeat the purpose of cloaking.

    Args:
        text: The preamble text to scan (typically first ~2000 chars).

    Returns:
        A list of dicts, each with ``"name"`` and ``"label"`` keys.
        E.g., ``[{"name": "Making Reign Inc.", "label": "Company"}]``
    """
    preamble = text[:2000]
    results: list[dict[str, str]] = []
    seen_names: set[str] = set()
    role_index = 0  # tracks which default role label to assign next

    # Pattern 1: Defined terms
    for match in _DEFINED_TERM_RE.finditer(preamble):
        name = match.group(1).strip()
        label = match.group(2).strip()
        if name.lower() not in seen_names:
            seen_names.add(name.lower())
            # If the label IS the company name, replace with a role label
            if _label_resembles_name(label, name):
                label = _DEFAULT_ROLE_LABELS[min(role_index, len(_DEFAULT_ROLE_LABELS) - 1)]
            role_index += 1
            results.append({"name": name, "label": label})

    # Pattern 2: "Dear Name,"
    for match in _DEAR_RE.finditer(preamble):
        name = match.group(1).strip()
        if name.lower() not in seen_names:
            seen_names.add(name.lower())
            results.append({"name": name, "label": "Addressee"})

    return results
