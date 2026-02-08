"""
Tests for clientcloak.detector: regex-based entity detection.

Covers each regex pattern (match and reject), deduplication, party name
filtering, placeholder generation, and the unified detect_entities() entry point.
"""

import pytest

from clientcloak.detector import (
    detect_entities,
    detect_entities_regex,
    deduplicate_entities,
    generate_placeholder,
)
from clientcloak.models import DetectedEntity


# ===================================================================
# Placeholder generation
# ===================================================================

class TestGeneratePlaceholder:

    def test_email_placeholder(self):
        assert generate_placeholder("EMAIL", 1) == "[Email-1]"

    def test_phone_placeholder(self):
        assert generate_placeholder("PHONE", 2) == "[Phone-2]"

    def test_ssn_placeholder(self):
        assert generate_placeholder("SSN", 1) == "[Ssn-1]"

    def test_amount_placeholder(self):
        assert generate_placeholder("AMOUNT", 3) == "[Amount-3]"

    def test_ein_placeholder(self):
        assert generate_placeholder("EIN", 1) == "[Ein-1]"


# ===================================================================
# Regex pattern matching
# ===================================================================

class TestEmailPattern:

    def test_matches_standard_email(self):
        entities = detect_entities_regex("Contact us at john@example.com for info.")
        emails = [e for e in entities if e.entity_type == "EMAIL"]
        assert len(emails) == 1
        assert emails[0].text == "john@example.com"

    def test_matches_complex_email(self):
        entities = detect_entities_regex("Send to jane.doe+tag@sub.example.co.uk please.")
        emails = [e for e in entities if e.entity_type == "EMAIL"]
        assert len(emails) == 1
        assert emails[0].text == "jane.doe+tag@sub.example.co.uk"

    def test_rejects_non_email(self):
        entities = detect_entities_regex("This is not an email: hello@")
        emails = [e for e in entities if e.entity_type == "EMAIL"]
        assert len(emails) == 0


class TestPhonePattern:

    def test_matches_standard_phone(self):
        entities = detect_entities_regex("Call 555-123-4567 for details.")
        phones = [e for e in entities if e.entity_type == "PHONE"]
        assert len(phones) == 1
        assert phones[0].text == "555-123-4567"

    def test_matches_parenthetical_phone(self):
        entities = detect_entities_regex("Phone: (555) 123-4567")
        phones = [e for e in entities if e.entity_type == "PHONE"]
        assert len(phones) == 1
        assert "555" in phones[0].text
        assert "123-4567" in phones[0].text

    def test_matches_dotted_phone(self):
        entities = detect_entities_regex("Phone: 555.123.4567")
        phones = [e for e in entities if e.entity_type == "PHONE"]
        assert len(phones) == 1
        assert phones[0].text == "555.123.4567"

    def test_matches_with_country_code(self):
        entities = detect_entities_regex("Call 1-555-123-4567 now.")
        phones = [e for e in entities if e.entity_type == "PHONE"]
        assert len(phones) == 1
        assert "1-555-123-4567" in phones[0].text

    def test_rejects_short_number(self):
        entities = detect_entities_regex("Reference: 555-12")
        phones = [e for e in entities if e.entity_type == "PHONE"]
        assert len(phones) == 0


class TestSsnPattern:

    def test_matches_ssn(self):
        entities = detect_entities_regex("SSN: 123-45-6789")
        ssns = [e for e in entities if e.entity_type == "SSN"]
        assert len(ssns) == 1
        assert ssns[0].text == "123-45-6789"

    def test_rejects_non_ssn(self):
        entities = detect_entities_regex("ID: 12-345-6789")
        ssns = [e for e in entities if e.entity_type == "SSN"]
        assert len(ssns) == 0


class TestEinPattern:

    def test_matches_ein(self):
        entities = detect_entities_regex("EIN: 12-3456789")
        eins = [e for e in entities if e.entity_type == "EIN"]
        assert len(eins) == 1
        assert eins[0].text == "12-3456789"

    def test_rejects_non_ein(self):
        entities = detect_entities_regex("Code: 123-456789")
        eins = [e for e in entities if e.entity_type == "EIN"]
        assert len(eins) == 0


class TestAmountPattern:

    def test_matches_simple_amount(self):
        entities = detect_entities_regex("The fee is $500 per month.")
        amounts = [e for e in entities if e.entity_type == "AMOUNT"]
        assert len(amounts) == 1
        assert amounts[0].text == "$500"

    def test_matches_amount_with_cents(self):
        entities = detect_entities_regex("Total: $1,234.56 due on receipt.")
        amounts = [e for e in entities if e.entity_type == "AMOUNT"]
        assert len(amounts) == 1
        assert amounts[0].text == "$1,234.56"

    def test_matches_large_amount(self):
        entities = detect_entities_regex("Cap of $10,000,000.00 applies.")
        amounts = [e for e in entities if e.entity_type == "AMOUNT"]
        assert len(amounts) == 1
        assert amounts[0].text == "$10,000,000.00"

    def test_rejects_no_dollar_sign(self):
        entities = detect_entities_regex("The amount is 500 dollars.")
        amounts = [e for e in entities if e.entity_type == "AMOUNT"]
        assert len(amounts) == 0


# ===================================================================
# Deduplication
# ===================================================================

class TestDeduplication:

    def test_merges_identical_entities(self):
        entities = [
            DetectedEntity(text="john@example.com", entity_type="EMAIL", confidence=1.0, count=1, suggested_placeholder="[Email-1]"),
            DetectedEntity(text="john@example.com", entity_type="EMAIL", confidence=0.9, count=2, suggested_placeholder="[Email-1]"),
        ]
        result = deduplicate_entities(entities)
        assert len(result) == 1
        assert result[0].count == 3
        assert result[0].confidence == 1.0

    def test_keeps_different_texts_separate(self):
        entities = [
            DetectedEntity(text="john@example.com", entity_type="EMAIL", confidence=1.0, count=1, suggested_placeholder="[Email-1]"),
            DetectedEntity(text="jane@example.com", entity_type="EMAIL", confidence=1.0, count=1, suggested_placeholder="[Email-2]"),
        ]
        result = deduplicate_entities(entities)
        assert len(result) == 2

    def test_empty_input(self):
        assert deduplicate_entities([]) == []


# ===================================================================
# Party name filtering
# ===================================================================

class TestPartyNameFiltering:

    def test_filters_party_names(self):
        text = "Contact Acme Corporation at info@acme.com or 555-123-4567."
        result = detect_entities(text, party_names=["info@acme.com"])
        texts = [e.text for e in result]
        assert "info@acme.com" not in texts

    def test_filter_is_case_insensitive(self):
        text = "Email: John@Example.Com for details."
        result = detect_entities(text, party_names=["john@example.com"])
        emails = [e for e in result if e.entity_type == "EMAIL"]
        assert len(emails) == 0

    def test_no_filter_when_none(self):
        text = "Contact john@example.com"
        result = detect_entities(text, party_names=None)
        emails = [e for e in result if e.entity_type == "EMAIL"]
        assert len(emails) == 1


# ===================================================================
# Unified detect_entities()
# ===================================================================

class TestDetectEntities:

    def test_returns_regex_results_without_gliner(self):
        text = "Send $500 to john@example.com, SSN 123-45-6789"
        result = detect_entities(text)
        types = {e.entity_type for e in result}
        assert "EMAIL" in types
        assert "AMOUNT" in types
        assert "SSN" in types

    def test_empty_text_returns_empty(self):
        assert detect_entities("") == []

    def test_no_matches_returns_empty(self):
        assert detect_entities("This is a plain sentence with no PII.") == []

    def test_sorted_by_count_descending(self):
        text = "Call 555-123-4567 or 555-123-4567. Email john@example.com."
        result = detect_entities(text)
        if len(result) >= 2:
            assert result[0].count >= result[1].count

    def test_counts_multiple_occurrences(self):
        text = "Pay $100 now. Then pay $100 later. Also $200."
        result = detect_entities(text)
        amounts = [e for e in result if e.entity_type == "AMOUNT"]
        amount_100 = [e for e in amounts if e.text == "$100"]
        assert len(amount_100) == 1
        assert amount_100[0].count == 2
