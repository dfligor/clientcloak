"""
Regex-based entity detection with pluggable GLiNER backend.

Detects structured PII (emails, phone numbers, SSNs, EINs, dollar amounts,
addresses, URLs) using regex patterns. Also detects party names from legal
preambles using defined-term patterns. Designed so GLiNER can be added later
as an additional backend — both feed the same DetectedEntity model.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections import Counter

from .models import DetectedEntity

logger = logging.getLogger(__name__)

# Full US state names for ADDRESS pattern matching.
_US_STATES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
    "Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|"
    "Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|"
    "Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
    "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    "Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|"
    "Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia"
)

# Postal code patterns for address detection (US, Canadian, UK, Japanese, generic).
_POSTAL_CODE = (
    r'(?:'
    r'\d{5}(?:-\d{4})?'                       # US ZIP / ZIP+4
    r'|[A-Z]\d[A-Z]\s?\d[A-Z]\d'              # Canadian (K1A 0B1)
    r'|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}'    # UK (SW1A 1AA)
    r'|\d{3}-\d{4}'                            # Japanese (100-0001)
    r'|\d{4,6}'                                # Generic 4-6 digit (AU, DE, etc.)
    r')'
)

# Regex patterns for structured PII. Each key becomes the entity_type.
ENTITY_PATTERNS: dict[str, str] = {
    "EMAIL": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "PHONE": r"(?<![A-Za-z0-9])(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
    "EIN": r"\b\d{2}-\d{7}\b",
    "AMOUNT": r"\$[\d,]+(?:\.\d{2})?\b",
    "ADDRESS": (
        r"\d+\s+[A-Za-z\s\.]+(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|Way|Lane|Ln)\.?"
        r"[,\s]+(?:(?:Suite|Ste|Apt|Unit)\s+\d+[,\s]+)?"
        r"[A-Za-z\s]+,\s+(?:" + _US_STATES + r"|[A-Z]{2})[,\s]+" + _POSTAL_CODE
    ),
    "URL": r"(?:https?://)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s,)]*)?(?<![.,)])",
}

# Pre-compile for performance
_COMPILED_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern) for name, pattern in ENTITY_PATTERNS.items()
}

# Context-aware bare-number pattern for amounts without $ prefix.
_BARE_AMOUNT_RE = re.compile(
    r'(?:exceed|up to|maximum of|total of|aggregate of|limit of|not to exceed|'
    r'in the amount of|principal amount of|sum of)\s+'
    r'([\d,]{4,}(?:\.\d{2})?)\s*(?:shares?|units?|dollars?|pounds?)?',
    re.IGNORECASE,
)

# Context-based person name patterns (require surrounding context to avoid false positives).
# Each pattern should have a single capture group for the person's name.

# Base name fragment: "FirstName [M.] LastName" — now accepts mixed/ALL-CAPS
_NAME_FRAG = r'([A-Z][A-Za-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][A-Za-z]+)'

_PERSON_PATTERNS: list[re.Pattern[str]] = [
    # Label-prefixed: "Name: John Smith", "By: John Smith", etc.
    re.compile(rf'(?:^|\n)\s*(?:Name|By|Signature|Attn):\s+{_NAME_FRAG}\s*$', re.MULTILINE),
    # Parenthetical defined-term: "Hugh F. Johnston (the "Representative")"
    re.compile(rf'\b{_NAME_FRAG}\s*\((?:the\s+)?["\u201c]'),
    # Signature block: underscores or /s/ followed by name
    re.compile(rf'(?:_{{3,}}|/s/)\s*\n?\s*{_NAME_FRAG}'),
    # Between/among pattern: "between John Smith," or "between John Smith and"
    re.compile(rf'\b(?:between|among)\s+{_NAME_FRAG}\s*[,;]', re.IGNORECASE),
    # Role-keyword context: "and John Smith as Representative"
    re.compile(rf'\band\s+{_NAME_FRAG}\s+as\s+[A-Z]', re.IGNORECASE),
]

_PERSON_FALSE_POSITIVE_WORDS = frozenset({
    "New", "York", "North", "South", "East", "West", "United", "States",
    "Stock", "Market", "Trust", "Federal", "National", "District",
    "San", "Los", "Las", "Fort", "Santa", "Saint", "Monte",
    "Bank", "First", "Second", "Third", "Fourth", "Fifth",
})

# Context-based date patterns.
_DATE_PATTERNS: list[re.Pattern[str]] = [
    # Month DD, YYYY / Month DD YYYY
    re.compile(
        r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{1,2},?\s+\d{4}\b'
    ),
    # MM/DD/YYYY or MM-DD-YYYY
    re.compile(r'\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b'),
    # DD Month YYYY
    re.compile(
        r'\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{4}\b'
    ),
]

# Two-letter US state/territory abbreviations (explicit list to avoid
# false positives from generic [A-Z]{2} matching "NA", "CO", etc.).
_US_STATE_ABBREVS = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|"
    "ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|"
    "OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)

# Context-based city/state location patterns (no street address required).
# Catches references like "in Seattle, Washington" that lack a street
# address but are still identifying information in legal documents.
_CITY_STATE_PATTERNS: list[re.Pattern[str]] = [
    # Preposition + City, State [ZIP/postal code]
    re.compile(
        r'\b(?:in|at|of|from|to|near)\s+'
        r'((?:[A-Z][A-Za-z.\']+\s+){0,3}[A-Z][A-Za-z.\']+'
        r',\s*(?:' + _US_STATES + r'|' + _US_STATE_ABBREVS + r')'
        r'(?:[,\s]+' + _POSTAL_CODE + r')?)'
        r'\b',
    ),
    # City, State on its own line (signature blocks)
    re.compile(
        r'^\s*((?:[A-Z][A-Za-z.\']+\s+){0,3}[A-Z][A-Za-z.\']+'
        r',\s*(?:' + _US_STATES + r'|' + _US_STATE_ABBREVS + r')'
        r'(?:[,\s]+' + _POSTAL_CODE + r')?)'
        r'\s*$',
        re.MULTILINE,
    ),
]

# Street suffixes for standalone street address detection.
_STREET_SUFFIXES = (
    r"Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|"
    r"Way|Lane|Ln|Parkway|Pkwy|Circle|Cir|Court|Ct|Place|Pl|"
    r"Plaza|Terrace|Ter|Trail|Trl|Highway|Hwy|Pike"
)

# Standalone street address patterns (no city/state/ZIP required).
# Catches addresses like "5959 Las Colinas Boulevard" that span
# multiple lines in legal documents.
_STREET_ADDRESS_PATTERNS: list[re.Pattern[str]] = [
    # Street number + name + suffix (standalone or with suite/floor)
    re.compile(
        r'\b(\d+\s+(?:[A-Za-z\.\']+ ){1,4}(?:' + _STREET_SUFFIXES + r')\.?'
        r'(?:[,\s]+(?:Suite|Ste|Apt|Unit|Floor|Fl)\s*#?\s*\d+)?)\b',
        re.IGNORECASE,
    ),
]


# ---------------------------------------------------------------------------
# GLiNER NER backend
# ---------------------------------------------------------------------------

_GLINER_LABEL_MAP: dict[str, str] = {
    "person": "PERSON",
    "organization": "COMPANY",
    "address": "ADDRESS",
}
_GLINER_LABELS = list(_GLINER_LABEL_MAP.keys())

_GLINER_THRESHOLDS: dict[str, float] = {
    "person": 0.6,
    "organization": 0.6,
    "address": 0.5,
}

# --- GLiNER post-processing filters ---

# Pronouns, common nouns, and legal terms that GLiNER misclassifies as PERSON.
_PERSON_BLOCKLIST = frozenset({
    "you", "i", "he", "she", "we", "they", "me", "him", "her", "us", "them",
    "it", "my", "your", "his", "our", "their",
    "attorney", "arbitrator", "party", "parties", "counsel", "judge",
    "plaintiff", "defendant", "claimant", "respondent", "witness",
    "executor", "trustee", "beneficiary", "agent", "representative",
    "receiving party", "disclosing party", "prevailing party",
    "indemnifying party", "indemnified party", "injured party",
    "the parties", "the company", "the contractor", "the consultant",
    "the employee", "the employer", "the client", "the vendor",
    "single arbitrator", "independent contractor",
})

# Leading determiners/prepositions to strip from COMPANY entities.
_COMPANY_DETERMINER_RE = re.compile(
    r'^(?:the|a|an|any|any\s+other|each|no|such)\s+', re.IGNORECASE,
)

# Legal statute/act suffixes that indicate a law, not a company.
_LEGAL_STATUTE_SUFFIXES = frozenset({
    "act", "code", "rule", "statute", "law", "regulation",
    "amendment", "order", "ordinance", "convention",
})

# Known legal abbreviations often misclassified as COMPANY.
_LEGAL_ABBREVIATIONS = frozenset({
    "DTSA", "CFAA", "FCPA", "SOX", "HIPAA", "FERPA", "COPPA",
    "GDPR", "CCPA", "TCPA", "CAN-SPAM", "RICO", "ERISA", "FLSA",
    "FMLA", "ADA", "OSHA", "EPA", "SEC", "FTC", "DOJ", "FBI",
    "IRS", "EEOC", "NLRB", "FINRA", "CFTC", "OCC", "FDIC",
})

# Common corporate suffixes (lowercase) — all-caps short entities NOT in
# this set are likely legal abbreviations, not companies.
_CORPORATE_SUFFIX_WORDS = frozenset({
    "inc", "llc", "llp", "lp", "ltd", "corp", "co", "plc", "pbc",
    "gmbh", "ag", "sa", "bv", "nv", "se", "ab", "oy", "oyj",
    "sas", "srl", "spa", "pllc", "lllp", "gp", "pc", "na", "fsb",
})


def _filter_gliner_entity(text: str, entity_type: str) -> str | None:
    """Apply post-processing filters to a single GLiNER entity.

    Returns the (possibly cleaned) entity text, or None if the entity
    should be rejected entirely.
    """
    # Reject entities containing newlines (cross-line chunk artifacts).
    if "\n" in text:
        return None

    stripped = text.strip()
    if not stripped:
        return None

    if entity_type == "PERSON":
        # Reject pronouns, common nouns, and legal terms.
        if stripped.lower() in _PERSON_BLOCKLIST:
            return None

    elif entity_type == "COMPANY":
        # Strip leading determiners/prepositions.
        cleaned = _COMPANY_DETERMINER_RE.sub("", stripped).strip()
        if not cleaned:
            return None
        stripped = cleaned

        # Reject legal statutes: entities ending with Act, Code, etc.
        last_word = stripped.rsplit(None, 1)[-1].rstrip(".,;:").lower()
        if last_word in _LEGAL_STATUTE_SUFFIXES:
            return None

        # Reject known legal abbreviations.
        if stripped.rstrip(".,;:") in _LEGAL_ABBREVIATIONS:
            return None

        # Reject all-caps 2-5 letter entities that aren't corporate suffixes.
        bare = stripped.rstrip(".,;:")
        if bare.isupper() and 2 <= len(bare) <= 5 and bare.lower() not in _CORPORATE_SUFFIX_WORDS:
            return None

    return stripped


# Module-level singleton for the GLiNER model (lazy-loaded).
_gliner_model = None
_gliner_import_failed = False


def _get_gliner_model(model_name: str = "urchade/gliner_multi_pii-v1"):
    """Return a cached GLiNER model instance, or None if unavailable.

    Tries three backends in order:

    1. **Bundled ONNX model** — lightweight, no torch dependency.
       Looks for an ONNX model directory at ``CLIENTCLOAK_ONNX_MODEL_DIR``
       (env var) or ``sys._MEIPASS/models/gliner`` (PyInstaller bundle).
    2. **Full GLiNER** — requires ``pip install gliner`` (torch-backed).
    3. **None** — regex-only fallback.

    Uses a circuit breaker so that repeated calls after a failed load
    return immediately without retrying.
    """
    global _gliner_model, _gliner_import_failed

    if _gliner_import_failed:
        return None
    if _gliner_model is not None:
        return _gliner_model

    # --- Try 1: Bundled ONNX model ---
    onnx_dir = os.environ.get("CLIENTCLOAK_ONNX_MODEL_DIR")
    if not onnx_dir and getattr(sys, "frozen", False):
        onnx_dir = os.path.join(sys._MEIPASS, "models", "gliner")  # type: ignore[attr-defined]
    if onnx_dir and os.path.isdir(onnx_dir):
        try:
            from .onnx_ner import load_onnx_model  # noqa: PLC0415

            logger.info("Loading ONNX NER model from: %s", onnx_dir)
            _gliner_model = load_onnx_model(onnx_dir)
            logger.info("ONNX NER model loaded successfully.")
            return _gliner_model
        except Exception:
            logger.warning(
                "ONNX NER model failed to load from %s, trying full GLiNER.",
                onnx_dir,
                exc_info=True,
            )

    # --- Try 2: Full GLiNER (dev / open-source installs) ---
    try:
        from gliner import GLiNER  # type: ignore[import-not-found]

        logger.info("Loading GLiNER model: %s", model_name)
        _gliner_model = GLiNER.from_pretrained(model_name)
        logger.info("GLiNER model loaded successfully.")
        return _gliner_model
    except Exception:
        _gliner_import_failed = True
        logger.warning(
            "GLiNER not available (not installed or model failed to load). "
            "Falling back to regex-only detection.",
            exc_info=True,
        )
        return None


def _chunk_text(
    text: str,
    max_words: int = 350,
    overlap_words: int = 50,
) -> list[tuple[str, int]]:
    """Split *text* into overlapping chunks for GLiNER inference.

    Each chunk contains at most *max_words* words. Consecutive chunks
    overlap by *overlap_words* words so that entities straddling a
    boundary are captured in at least one chunk.

    Returns a list of ``(chunk_text, char_offset)`` tuples.
    """
    if not text or not text.strip():
        return []

    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks: list[tuple[str, int]] = []
    current_sentences: list[str] = []
    current_word_count = 0
    current_char_offset = 0

    for sentence in sentences:
        sentence_words = len(sentence.split())

        # If a single sentence exceeds max_words, split it by word count
        if sentence_words > max_words:
            # Flush current buffer first
            if current_sentences:
                chunk_text = " ".join(current_sentences)
                chunks.append((chunk_text, current_char_offset))
                current_char_offset += len(chunk_text) + 1
                current_sentences = []
                current_word_count = 0

            words = sentence.split()
            for i in range(0, len(words), max_words - overlap_words):
                word_slice = words[i:i + max_words]
                chunk_text = " ".join(word_slice)
                chunks.append((chunk_text, current_char_offset))
                current_char_offset += len(" ".join(words[i:i + max_words - overlap_words])) + 1
            continue

        if current_word_count + sentence_words > max_words and current_sentences:
            chunk_text = " ".join(current_sentences)
            chunks.append((chunk_text, current_char_offset))

            # Build overlap from the tail of the current chunk
            overlap_text_parts: list[str] = []
            overlap_count = 0
            for s in reversed(current_sentences):
                s_words = len(s.split())
                if overlap_count + s_words > overlap_words:
                    break
                overlap_text_parts.insert(0, s)
                overlap_count += s_words

            # Compute new char offset
            if overlap_text_parts:
                overlap_str = " ".join(overlap_text_parts)
                current_char_offset = current_char_offset + len(chunk_text) - len(overlap_str)
                current_sentences = overlap_text_parts
                current_word_count = overlap_count
            else:
                current_char_offset += len(chunk_text) + 1
                current_sentences = []
                current_word_count = 0

        current_sentences.append(sentence)
        current_word_count += sentence_words

    # Flush remaining sentences
    if current_sentences:
        chunk_text = " ".join(current_sentences)
        chunks.append((chunk_text, current_char_offset))

    return chunks


def _run_gliner(text: str, threshold: float = 0.5) -> list[DetectedEntity]:
    """Run GLiNER NER on *text* and return detected entities.

    Returns an empty list when GLiNER is not installed or the model
    fails to load.
    """
    model = _get_gliner_model()
    if model is None:
        return []

    chunks = _chunk_text(text)
    entities: list[DetectedEntity] = []
    type_counters: dict[str, int] = {}

    for chunk_text, _char_offset in chunks:
        predictions = model.predict_entities(
            chunk_text, _GLINER_LABELS,
            threshold=min(_GLINER_THRESHOLDS.values()),
            flat_ner=True,
        )
        for pred in predictions:
            gliner_label = pred["label"]
            label_threshold = _GLINER_THRESHOLDS.get(gliner_label, threshold)
            if pred["score"] < label_threshold:
                continue
            entity_type = _GLINER_LABEL_MAP.get(gliner_label)
            if entity_type is None:
                continue
            # Post-processing filter: reject junk, strip determiners, etc.
            filtered_text = _filter_gliner_entity(pred["text"], entity_type)
            if filtered_text is None:
                continue
            type_counters[entity_type] = type_counters.get(entity_type, 0) + 1
            entities.append(DetectedEntity(
                text=filtered_text,
                entity_type=entity_type,
                confidence=pred["score"],
                count=1,
                suggested_placeholder=generate_placeholder(
                    entity_type, type_counters[entity_type],
                ),
            ))

    entities = deduplicate_entities(entities)
    return entities


def _reassign_placeholders(entities: list[DetectedEntity]) -> list[DetectedEntity]:
    """Re-number placeholders sequentially after merge/dedup.

    Groups entities by type and assigns ``[Type-1]``, ``[Type-2]``, etc.
    within each group, preserving the existing list order.
    """
    type_counters: dict[str, int] = {}
    result: list[DetectedEntity] = []
    for entity in entities:
        type_counters[entity.entity_type] = type_counters.get(entity.entity_type, 0) + 1
        idx = type_counters[entity.entity_type]
        result.append(entity.model_copy(update={
            "suggested_placeholder": generate_placeholder(entity.entity_type, idx),
        }))
    return result


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
            name = match.group(1).strip()
            # Filter false positives: skip if any word is a known place/legal term
            name_words = name.split()
            if any(w in _PERSON_FALSE_POSITIVE_WORDS for w in name_words):
                continue
            person_counts[name] += 1
    for idx, (name, count) in enumerate(person_counts.most_common(), 1):
        entities.append(DetectedEntity(
            text=name,
            entity_type="PERSON",
            confidence=1.0,
            count=count,
            suggested_placeholder=generate_placeholder("PERSON", idx),
        ))

    # Context-based date detection
    date_counts: Counter = Counter()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            date_counts[match.group(0).strip()] += 1
    for idx, (date_text, count) in enumerate(date_counts.most_common(), 1):
        entities.append(DetectedEntity(
            text=date_text,
            entity_type="DATE",
            confidence=1.0,
            count=count,
            suggested_placeholder=generate_placeholder("DATE", idx),
        ))

    # Context-based city/state location detection (no street required).
    # Catches "in Seattle, Washington", "New York, NY" on its own line, etc.
    existing_addresses = {e.text for e in entities if e.entity_type == "ADDRESS"}
    address_idx = len(existing_addresses)
    city_state_counts: Counter = Counter()
    city_state_canonical: dict[str, str] = {}
    for pattern in _CITY_STATE_PATTERNS:
        for match in pattern.finditer(text):
            location = match.group(1).strip()
            # Skip if already detected by the full ADDRESS pattern
            if location in existing_addresses:
                continue
            # Skip if this is a substring of an already-detected address
            if any(location in addr for addr in existing_addresses):
                continue
            loc_lower = location.lower()
            if loc_lower not in city_state_canonical:
                city_state_canonical[loc_lower] = location
            city_state_counts[city_state_canonical[loc_lower]] += 1
    for idx_offset, (location, count) in enumerate(city_state_counts.most_common(), 1):
        entities.append(DetectedEntity(
            text=location,
            entity_type="ADDRESS",
            confidence=0.9,
            count=count,
            suggested_placeholder=generate_placeholder("ADDRESS", address_idx + idx_offset),
        ))
        existing_addresses.add(location)
    address_idx += len(city_state_counts)

    # Standalone street address detection (no city/state required).
    # Catches addresses like "5959 Las Colinas Boulevard" split across lines.
    street_counts: Counter = Counter()
    street_canonical: dict[str, str] = {}
    for pattern in _STREET_ADDRESS_PATTERNS:
        for match in pattern.finditer(text):
            street = match.group(1).strip()
            # Skip if already detected by full ADDRESS or city-state patterns
            if street in existing_addresses:
                continue
            if any(street in addr for addr in existing_addresses):
                continue
            if any(addr in street for addr in existing_addresses):
                continue
            street_lower = street.lower()
            if street_lower not in street_canonical:
                street_canonical[street_lower] = street
            street_counts[street_canonical[street_lower]] += 1
    for idx_offset, (street, count) in enumerate(street_counts.most_common(), 1):
        entities.append(DetectedEntity(
            text=street,
            entity_type="ADDRESS",
            confidence=0.9,
            count=count,
            suggested_placeholder=generate_placeholder("ADDRESS", address_idx + idx_offset),
        ))
        existing_addresses.add(street)

    # Context-based bare amount detection (no $ prefix required)
    existing_amounts = {e.text for e in entities if e.entity_type == "AMOUNT"}
    amount_idx = len([e for e in entities if e.entity_type == "AMOUNT"])
    for match in _BARE_AMOUNT_RE.finditer(text):
        amount_text = match.group(1).strip()
        if amount_text not in existing_amounts:
            amount_idx += 1
            entities.append(DetectedEntity(
                text=amount_text,
                entity_type="AMOUNT",
                confidence=0.9,
                count=len(re.findall(re.escape(amount_text), text)),
                suggested_placeholder=generate_placeholder("AMOUNT", amount_idx),
            ))
            existing_amounts.add(amount_text)

    # Full-document company name detection by corporate suffix.
    # Catches third-party references like "Adventura Properties, LLC" that
    # lack parenthetical defined terms and wouldn't be found by
    # detect_party_names (which only scans the preamble).
    _bare_suffix_re = re.compile(
        r',?\s*(?:' + _SUFFIX_PATTERN + r')\.?\s*$', re.IGNORECASE,
    )
    # Common determiners that can start a false-positive match
    # (e.g. "The Services", "This Agreement").
    _determiner_re = re.compile(
        r'^(?:The|This|That|These|Those|A|An)\s+', re.IGNORECASE,
    )
    company_counts: Counter = Counter()
    company_canonical: dict[str, str] = {}  # lowered name -> first-seen case form
    for match in _COMPANY_SUFFIX_RE.finditer(text):
        # Ensure the suffix isn't mid-word (e.g. "County" matching "Co")
        end_pos = match.end()
        if end_pos < len(text) and text[end_pos].isalpha():
            continue
        # Skip agreement references like "Transition Services Agreement"
        if _followed_by_agreement_term(text, end_pos):
            continue
        name = match.group(1).strip().rstrip(",").rstrip(".")
        # Skip bare suffixes (e.g. just "Services" with no real name words)
        if not _bare_suffix_re.sub('', name).strip():
            continue
        # Skip matches that start with a common determiner
        if _determiner_re.match(name):
            continue
        # Skip matches starting with "Dear " (letter salutation)
        if name.startswith("Dear "):
            continue
        # Case-insensitive dedup: merge "VENTMARKET, LLC" with "VentMarket, LLC"
        name_lower = name.lower()
        if name_lower not in company_canonical:
            company_canonical[name_lower] = name
        company_counts[company_canonical[name_lower]] += 1
    for idx, (name, count) in enumerate(company_counts.most_common(), 1):
        entities.append(DetectedEntity(
            text=name,
            entity_type="COMPANY",
            confidence=1.0,
            count=count,
            suggested_placeholder=generate_placeholder("COMPANY", idx),
        ))

    return entities


def detect_entities(
    text: str,
    party_names: list[str] | None = None,
    gliner_threshold: float = 0.5,
    use_gliner: bool = True,
    max_chars: int = 0,
) -> list[DetectedEntity]:
    """
    Detect entities in text using all available backends.

    This is the single public entry point. Callers never need to know
    which backend produced the results.

    Steps:
        1. Always run regex-based detection.
        2. Run GLiNER NER if enabled and available (graceful fallback to
           regex-only if not installed or if inference fails).
        3. Merge and deduplicate results from all backends.
        4. Filter out entities whose text matches a known party name.
        5. Re-number placeholders sequentially.
        6. Return sorted by count descending.

    Args:
        text: The full document text to scan.
        party_names: Optional list of party names to exclude from results
            (these are already handled by party config).
        gliner_threshold: Confidence threshold for GLiNER results.
        use_gliner: If True, attempt GLiNER NER detection in addition to
            regex. Set to False for regex-only mode.
        max_chars: Maximum number of characters fed to GLiNER NER.
            Text beyond this limit is not scanned by NER (regex still
            scans the full document). 0 means no limit.

    Returns:
        List of DetectedEntity instances sorted by count (descending).
    """
    # 1. Regex detection (always runs)
    entities = detect_entities_regex(text)

    # 2. GLiNER detection (skip if disabled or not installed)
    if use_gliner:
        try:
            ner_text = text
            if max_chars and len(text) > max_chars:
                logger.warning(
                    "Text length %d exceeds max_ner_chars %d; "
                    "NER will scan first %d characters only",
                    len(text), max_chars, max_chars,
                )
                ner_text = text[:max_chars]
            gliner_entities = _run_gliner(ner_text, gliner_threshold)
            entities.extend(gliner_entities)
        except Exception:
            logger.warning("GLiNER detection failed, falling back to regex-only", exc_info=True)

    # 3. Deduplicate
    entities = deduplicate_entities(entities)

    # 3b. Cross-type duplicate suppression: if the same text appears as
    # multiple entity types (e.g., an email detected as both EMAIL by regex
    # and PERSON by GLiNER), keep only the highest-confidence version.
    best_by_text: dict[str, DetectedEntity] = {}
    for e in entities:
        text_lower = e.text.lower()
        if text_lower not in best_by_text or e.confidence > best_by_text[text_lower].confidence:
            best_by_text[text_lower] = e
    # Drop entities whose text has a higher-confidence version of a different type
    entities = [
        e for e in entities
        if best_by_text[e.text.lower()].entity_type == e.entity_type
    ]

    # 4. Filter out known party names.
    # Normalize by stripping trailing periods/commas so "Acme Inc." and
    # "Acme Inc" are treated as the same name.
    if party_names:
        lower_names = {name.lower().rstrip(".,") for name in party_names if name}
        entities = [
            e for e in entities
            if e.text.lower().rstrip(".,") not in lower_names
        ]

    # 5. Sort by count descending, then by text for stability
    entities.sort(key=lambda e: (-e.count, e.text))

    # 6. Re-number placeholders sequentially after merge/filter
    entities = _reassign_placeholders(entities)

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
    r"Bank",
    r"Trust",
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
    # --- Banking / European corporate forms ---
    r"KGaA",                     # German limited partnership
    r"ASA",                      # Norwegian
    r"AG",                       # German Aktiengesellschaft
    r"AB",                       # Swedish
    r"SE",                       # European Societas Europaea
    r"NV",                       # Dutch Naamloze Vennootschap
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

# Phase 1: Find a corporate suffix anchored to one or more preceding
# capitalized words.  Allows an optional comma between the name and the
# suffix (e.g. "Acme, Inc.").  Captures the full company name + suffix.
_COMPANY_SUFFIX_RE = re.compile(
    rf'((?:[A-Z\u00C0-\u024F][A-Za-z\u00C0-\u024F&\-\']+ )*'   # zero or more capitalized words
    rf'(?:[A-Z\u00C0-\u024F][A-Za-z\u00C0-\u024F&\-\']+),?\s*'  # final name word + optional comma
    rf'(?:{_SUFFIX_PATTERN})\.?)',                                 # corporate suffix
    re.UNICODE,
)

# Phase 2: Given a suffix match position, scan forward up to 500 chars
# for a parenthetical label.  Handles optional prefixes like "hereinafter
# referred to as", "hereinafter", and "the".
# Captures the first quoted string inside the parentheses.
_LABEL_AFTER_SUFFIX_RE = re.compile(
    r'.{0,500}?'                                          # intervening text (non-greedy)
    r'\('                                                 # opening paren
    r'(?:(?:hereinafter|hereafter)\s+)?'                  # optional hereinafter
    r'(?:referred\s+to\s+as\s+)?'                         # optional "referred to as"
    r'(?:the\s+)?'                                        # optional "the"
    r'["\u201c]([^"\u201d,]+)[,"\u201d]',                   # first quoted label (stop at comma or close quote)
    re.UNICODE | re.DOTALL,
)

# Legacy single-pass pattern kept as a fast path for simple cases:
# "Company Name Inc. (the "Label")"
_DEFINED_TERM_RE = re.compile(
    rf'((?:[A-Z\u00C0-\u024F][A-Za-z\u00C0-\u024F&\-\']+ )*(?:{_SUFFIX_PATTERN}))(?:,?)\s+'
    r'(?:\((?:the\s+)?["\u201c]([^"\u201d]+)["\u201d]\))',
    re.UNICODE,
)

# Pattern: "Dear Name," — catches addressee in letter-format agreements.
_DEAR_RE = re.compile(
    rf'Dear\s+((?:[A-Z\u00C0-\u024F][A-Za-z\u00C0-\u024F&\-\']+ )*(?:{_SUFFIX_PATTERN})),',
    re.UNICODE,
)


# Default role labels assigned when the defined term is the company name itself.
_DEFAULT_ROLE_LABELS = ("Company", "Counterparty")


# Common legal document-type terms.  When one of these words immediately
# follows a corporate suffix match, the match is an agreement/document
# reference (e.g. "Transition Services Agreement") rather than a company
# name.  Real companies like "Acme Services, LLC" match on "LLC" (not
# "Services") so are unaffected by this filter.
_AGREEMENT_TYPE_TERMS = frozenset({
    "agreement", "contract", "plan", "schedule", "letter",
    "memorandum", "arrangement", "deed", "order", "addendum",
    "amendment", "supplement", "policy", "program", "handbook",
    "manual", "notice", "certificate", "indenture", "warrant",
    "lease", "sublease", "license", "note", "bond", "protocol",
})

_NEXT_WORD_RE = re.compile(r'\s+([A-Za-z]+)')


def _followed_by_agreement_term(text: str, end_pos: int) -> bool:
    """Return True if the next word after *end_pos* is a legal document type."""
    m = _NEXT_WORD_RE.match(text, end_pos)
    return bool(m and m.group(1).lower() in _AGREEMENT_TYPE_TERMS)


def _is_abbreviation(label: str, name: str) -> bool:
    """Check if label appears to be an abbreviation or acronym of name.

    Returns True for patterns like "CLS" for "Centinnial Logistics Services"
    (initials match) or short all-uppercase strings that aren't common role
    terms.
    """
    label_stripped = label.strip()
    if not label_stripped:
        return False

    # All uppercase and short → likely abbreviation (CLS, IBM, ABC)
    if label_stripped.isupper() and len(label_stripped) <= 10:
        return True

    # Check if first letters of name words form the label
    name_words = [w for w in name.split() if w and w[0].isupper()]
    initials = "".join(w[0] for w in name_words)
    if initials.upper() == label_stripped.upper():
        return True

    return False


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
    # Strip suffix to get the core name, e.g. "AiSim Inc." -> "AiSim".
    # The ,? handles comma-separated forms like "VentMarket, LLC".
    suffix_pattern = re.compile(
        r",?\s+(?:" + _SUFFIX_PATTERN + r")\s*$",
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

    1. **Two-phase defined-term detection** — first finds a corporate suffix
       (Inc., LLC, Corp., etc.) anchored to capitalized words, then scans
       forward up to 500 characters for a parenthetical label like
       ``(the "Label")``.  This handles common legal drafting where the
       company name and its defined term are separated by descriptors::

           Acme, Inc., a Delaware corporation, having its principal
           place of business at 123 Oak Ave., Berkeley, CA 95123 ("Acme")

    2. **Simple defined-term fast path** — ``Company Name Inc. (the "Label")``
       for cases where the label immediately follows the suffix.

    3. **"Dear Name," pattern** — catches addressee in letter-format
       agreements, also requiring a corporate suffix.

    If a defined-term label appears to be the company name itself (e.g.,
    ``AiSim Inc. (the "AiSim")``), it is replaced with a generic role
    label ("Company", "Counterparty") since using the name as the label
    would defeat the purpose of cloaking.

    Args:
        text: The preamble text to scan (typically first ~2000 chars).

    Returns:
        A list of dicts, each with ``"name"``, ``"label"``, and optionally
        ``"defined_term"`` keys.  When the parenthetical defined term is a
        short form of the company name (e.g., ``"Acme"`` for
        ``"Acme Wireless, Inc."``), ``defined_term`` carries that short form
        so the caller can add it as a replacement variant.
        E.g., ``[{"name": "Making Reign Inc.", "label": "Company",
        "defined_term": "Making Reign"}]``
    """
    preamble = text[:2000]
    results: list[dict[str, str]] = []
    seen_names: set[str] = set()
    role_index = 0  # tracks which default role label to assign next

    def _add(name: str, label: str) -> None:
        nonlocal role_index
        if name.lower() in seen_names:
            return
        seen_names.add(name.lower())
        entry: dict[str, str] = {"name": name, "label": label}
        if _label_resembles_name(label, name):
            # The defined term is a short form of the name — include it as
            # a replacement variant so the cloaker replaces it too, but use
            # a generic role label to avoid exposing the name.
            entry["defined_term"] = label
            label = _DEFAULT_ROLE_LABELS[min(role_index, len(_DEFAULT_ROLE_LABELS) - 1)]
            entry["label"] = label
        elif _is_abbreviation(label, name):
            # The defined term is an abbreviation/acronym (e.g. "CLS" for
            # "Centinnial Logistics Services, LLC").  Include it as a
            # defined_term so it gets replaced in the document body too.
            # Use a generic role label to avoid the abbreviation leaking
            # through the placeholder (e.g. [CLS] would reveal the acronym)
            # and to prevent double-replacement collisions.
            entry["defined_term"] = label
            label = _DEFAULT_ROLE_LABELS[min(role_index, len(_DEFAULT_ROLE_LABELS) - 1)]
            entry["label"] = label
        role_index += 1
        results.append(entry)

    _suffix_strip_re = re.compile(
        r',?\s*(?:' + _SUFFIX_PATTERN + r')\.?\s*$', re.IGNORECASE,
    )

    def _is_bare_suffix(name: str) -> bool:
        """True when the matched name is just a suffix with no real name words."""
        return not _suffix_strip_re.sub('', name).strip()

    _det_re = re.compile(
        r'^(?:The|This|That|These|Those|A|An)\s+', re.IGNORECASE,
    )

    # --- Phase 1+2: Find suffix, then scan forward for label ---
    for suffix_match in _COMPANY_SUFFIX_RE.finditer(preamble):
        # Word boundary check: "Co" in "Contract" is not a real suffix.
        end_pos = suffix_match.end()
        if end_pos < len(preamble) and preamble[end_pos].isalpha():
            continue
        name = suffix_match.group(1).strip().rstrip(",")
        if _is_bare_suffix(name):
            continue
        # Skip matches starting with a common determiner (e.g. "The Company").
        if _det_re.match(name):
            continue
        # Skip matches starting with "Dear " (letter salutation, not a company).
        if name.startswith("Dear "):
            continue
        # "Transition Services Agreement" is an agreement reference, not a
        # company.  Skip when the next word is a legal document type.
        if _followed_by_agreement_term(preamble, end_pos):
            continue
        after = preamble[suffix_match.end():]
        label_match = _LABEL_AFTER_SUFFIX_RE.match(after)
        if label_match:
            label = label_match.group(1).strip().rstrip(",")
            _add(name, label)

    # --- Fast path: simple adjacent defined terms (catches anything the
    #     two-phase approach might format-mismatch on) ---
    for match in _DEFINED_TERM_RE.finditer(preamble):
        end_pos = match.start(1) + len(match.group(1))
        if end_pos < len(preamble) and preamble[end_pos].isalpha():
            continue
        name = match.group(1).strip()
        if _is_bare_suffix(name):
            continue
        # Skip matches starting with "Dear " (letter salutation, not a company).
        if name.startswith("Dear "):
            continue
        label = match.group(2).strip()
        _add(name, label)

    # --- "Dear Name," pattern ---
    for match in _DEAR_RE.finditer(preamble):
        name = match.group(1).strip()
        if name.lower() not in seen_names:
            seen_names.add(name.lower())
            results.append({"name": name, "label": "Addressee"})

    return results
