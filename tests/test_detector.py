"""
Tests for clientcloak.detector: regex-based entity detection.

Covers each regex pattern (match and reject), deduplication, party name
filtering, placeholder generation, the unified detect_entities() entry point,
and party name detection from legal preambles.
"""

import pytest

from clientcloak.detector import (
    detect_entities,
    detect_entities_regex,
    detect_party_names,
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
        assert phones[0].text == "(555) 123-4567"

    def test_matches_dotted_phone(self):
        entities = detect_entities_regex("Phone: 555.123.4567")
        phones = [e for e in entities if e.entity_type == "PHONE"]
        assert len(phones) == 1
        assert phones[0].text == "555.123.4567"

    def test_matches_with_country_code(self):
        entities = detect_entities_regex("Call 1-555-123-4567 now.")
        phones = [e for e in entities if e.entity_type == "PHONE"]
        assert len(phones) == 1
        assert phones[0].text == "1-555-123-4567"

    def test_rejects_short_number(self):
        entities = detect_entities_regex("Reference: 555-12")
        phones = [e for e in entities if e.entity_type == "PHONE"]
        assert len(phones) == 0

    def test_no_false_positive_from_alphanumeric_prefix(self):
        """Phone regex should not match when preceded by alphanumeric chars."""
        entities = detect_entities_regex("ID: ABC1234567890")
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


class TestAddressPattern:

    def test_matches_simple_address(self):
        entities = detect_entities_regex(
            "Located at 123 Main Street, Springfield, IL 62704"
        )
        addresses = [e for e in entities if e.entity_type == "ADDRESS"]
        assert len(addresses) == 1
        assert "123 Main Street" in addresses[0].text

    def test_matches_address_with_suite(self):
        entities = detect_entities_regex(
            "Office: 456 Oak Ave, Suite 200, Portland, OR 97201"
        )
        addresses = [e for e in entities if e.entity_type == "ADDRESS"]
        assert len(addresses) == 1
        assert "Suite 200" in addresses[0].text

    def test_matches_address_with_zip_plus_four(self):
        entities = detect_entities_regex(
            "Send to 789 Elm Boulevard, Austin, TX 73301-1234"
        )
        addresses = [e for e in entities if e.entity_type == "ADDRESS"]
        assert len(addresses) == 1
        assert "73301-1234" in addresses[0].text

    def test_rejects_non_address(self):
        entities = detect_entities_regex("This is not an address at all.")
        addresses = [e for e in entities if e.entity_type == "ADDRESS"]
        assert len(addresses) == 0


class TestUrlPattern:

    def test_matches_https_url(self):
        entities = detect_entities_regex("Visit https://www.example.com for details.")
        urls = [e for e in entities if e.entity_type == "URL"]
        assert len(urls) == 1
        assert "example.com" in urls[0].text

    def test_matches_bare_domain(self):
        entities = detect_entities_regex("Check out softwareexperts.io today.")
        urls = [e for e in entities if e.entity_type == "URL"]
        assert len(urls) == 1
        assert urls[0].text == "softwareexperts.io"

    def test_matches_url_with_path(self):
        entities = detect_entities_regex("See https://docs.example.com/api/v2/guide for info.")
        urls = [e for e in entities if e.entity_type == "URL"]
        assert len(urls) == 1
        assert "/api/v2/guide" in urls[0].text

    def test_url_dedup_filters_email_domains(self):
        """URL matches that are substrings of detected emails should be filtered."""
        entities = detect_entities_regex(
            "Contact michael@softwareexperts.io for help."
        )
        urls = [e for e in entities if e.entity_type == "URL"]
        emails = [e for e in entities if e.entity_type == "EMAIL"]
        assert len(emails) == 1
        # softwareexperts.io should NOT appear as a separate URL
        url_texts = [u.text for u in urls]
        assert "softwareexperts.io" not in url_texts

    def test_rejects_non_url(self):
        entities = detect_entities_regex("This is just text, not a URL.")
        urls = [e for e in entities if e.entity_type == "URL"]
        assert len(urls) == 0


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


# ===================================================================
# Party name detection from legal preambles
# ===================================================================

class TestDetectPartyNames:

    def test_defined_term_with_straight_quotes(self):
        text = 'This Agreement is entered into by Making Reign Inc. (the "Company") and BigCo LLC (the "Client").'
        result = detect_party_names(text)
        assert len(result) == 2
        assert result[0]["name"] == "Making Reign Inc."
        assert result[0]["label"] == "Company"
        assert result[1]["name"] == "BigCo LLC"
        assert result[1]["label"] == "Client"

    def test_defined_term_with_curly_quotes(self):
        text = "This Agreement is entered into by Acme Corporation (\u201cLicensor\u201d)."
        result = detect_party_names(text)
        assert len(result) == 1
        assert result[0]["name"] == "Acme Corporation"
        assert result[0]["label"] == "Licensor"

    def test_defined_term_with_the_prefix(self):
        text = 'Software Experts LLC (the "Vendor") agrees to provide services.'
        result = detect_party_names(text)
        assert len(result) == 1
        assert result[0]["name"] == "Software Experts LLC"
        assert result[0]["label"] == "Vendor"

    def test_dear_pattern(self):
        text = "Dear Acme Corp.,\n\nWe are writing to confirm the terms of our agreement."
        result = detect_party_names(text)
        assert len(result) == 1
        assert result[0]["name"] == "Acme Corp."
        assert result[0]["label"] == "Addressee"

    def test_no_false_positives_on_plain_text(self):
        """Plain text without corporate suffixes should not match."""
        text = "This is a simple paragraph about ordinary things with no companies."
        result = detect_party_names(text)
        assert len(result) == 0

    def test_deduplication_across_patterns(self):
        """Same company found by multiple patterns should appear once."""
        text = 'Dear Acme Corp.,\nThis Agreement is entered into by Acme Corp. (the "Vendor").'
        result = detect_party_names(text)
        names = [r["name"] for r in result]
        assert names.count("Acme Corp.") == 1

    def test_multiple_corporate_suffixes(self):
        text = 'Beta LLP (the "Firm") and Gamma Ltd. (the "Supplier")'
        result = detect_party_names(text)
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert "Beta LLP" in names
        assert "Gamma Ltd." in names

    def test_only_scans_first_2000_chars(self):
        """Party names after the first 2000 characters should be ignored."""
        preamble = 'Acme Inc. (the "Vendor") agrees.'
        padding = "x" * 2000
        text = padding + ' Later Corp. (the "Client")'
        result = detect_party_names(text)
        assert len(result) == 0  # Acme is at beginning of preamble but padding pushes it out

    def test_comma_in_company_name_with_intervening_text(self):
        """Company name with comma before suffix and long intervening text before label."""
        text = (
            'Acme, Inc., a Delaware corporation, having its principal '
            'place of business at 123 Oak Ave., Berkeley, California '
            '95123 ("Acme")'
        )
        result = detect_party_names(text)
        assert len(result) >= 1
        names = {r["name"] for r in result}
        assert "Acme, Inc." in names

    def test_complex_parenthetical_with_multiple_terms(self):
        """Parenthetical containing multiple defined terms; only first label extracted."""
        text = (
            'Smith Corporation, a Delaware corporation, having its principal '
            'place of business at 100 Anystreet Way, Suite 100, Anytown, '
            'North Carolina 27654 ("\u201cSmith,\u201d and together with '
            'Acme, the \u201cParties,\u201d and each, a \u201cParty\u201d)'
        )
        result = detect_party_names(text)
        assert len(result) >= 1
        names = {r["name"] for r in result}
        assert "Smith Corporation" in names

    def test_two_parties_with_intervening_text(self):
        """Both parties detected when separated by long descriptors."""
        text = (
            'This Agreement is entered into by and between '
            'Acme, Inc., a Delaware corporation, having its principal '
            'place of business at 123 Oak Ave., Berkeley, California '
            '95123 ("Acme") and Smith Corporation, a Delaware corporation, '
            'having its principal place of business at 100 Anystreet Way, '
            'Suite 100, Anytown, North Carolina 27654 (the "Contractor").'
        )
        result = detect_party_names(text)
        names = {r["name"] for r in result}
        assert "Acme, Inc." in names
        assert "Smith Corporation" in names
        assert len(result) == 2

    def test_curly_quotes_correct_label_assignment(self):
        """Curly-quoted labels are matched correctly (not skipped to next party)."""
        text = (
            'Acme Wireless, Inc., a Delaware corporation, having its principal '
            'place of business at 123 Oak Ave., Berkeley, California '
            '95123 (\u201cAcme\u201d) and Beta Systems, Inc., a Delaware '
            'corporation (\u201cBeta,\u201d and together with Acme, '
            'the \u201cParties\u201d).'
        )
        result = detect_party_names(text)
        assert len(result) == 2
        by_name = {r["name"]: r for r in result}
        assert "Acme Wireless, Inc." in by_name
        assert "Beta Systems, Inc." in by_name
        # Acme should NOT get Beta's label
        assert by_name["Acme Wireless, Inc."]["label"] != "Beta"

    def test_defined_term_returned_for_short_form(self):
        """When the defined term is a short form of the name, it is returned."""
        text = 'Acme Wireless, Inc. (the \u201cAcme\u201d) agrees.'
        result = detect_party_names(text)
        assert len(result) == 1
        assert result[0]["name"] == "Acme Wireless, Inc."
        assert result[0]["defined_term"] == "Acme"
        # Label should be a generic role, not the company name
        assert result[0]["label"] == "Company"

    def test_no_defined_term_for_role_labels(self):
        """When the defined term is a role (not a name), no defined_term is returned."""
        text = 'Acme Wireless, Inc. (the \u201cVendor\u201d) agrees.'
        result = detect_party_names(text)
        assert len(result) == 1
        assert result[0]["label"] == "Vendor"
        assert "defined_term" not in result[0]
