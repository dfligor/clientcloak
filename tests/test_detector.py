"""
Tests for clientcloak.detector: regex-based entity detection.

Covers each regex pattern (match and reject), deduplication, party name
filtering, placeholder generation, the unified detect_entities() entry point,
and party name detection from legal preambles.
"""

import pytest
from unittest.mock import patch, MagicMock

from clientcloak.detector import (
    detect_entities,
    detect_entities_regex,
    detect_party_names,
    deduplicate_entities,
    generate_placeholder,
    _chunk_text,
    _run_gliner,
    _reassign_placeholders,
    _GLINER_LABEL_MAP,
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

    def test_filters_allcaps_party_variant(self):
        """ALL-CAPS variants of party names should be filtered by case-insensitive match."""
        text = (
            "VentMarket, LLC agrees to the terms.\n"
            "VENTMARKET, LLC\nBy: _______________"
        )
        result = detect_entities(text, party_names=["VentMarket, LLC"])
        company_texts = [e.text for e in result if e.entity_type == "COMPANY"]
        assert "VENTMARKET, LLC" not in company_texts
        assert "VentMarket, LLC" not in company_texts


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
# COMPANY entity case-insensitive dedup
# ===================================================================

class TestCompanyCaseDedup:

    def test_allcaps_and_mixedcase_merge(self):
        """ALL-CAPS company name should merge with mixed-case form."""
        text = (
            "VentMarket, LLC provides HVAC services.\n"
            "VENTMARKET, LLC\nBy: _______________"
        )
        entities = detect_entities_regex(text)
        company_entities = [e for e in entities if e.entity_type == "COMPANY"]
        company_texts = [e.text for e in company_entities]
        # Should have only one entry (the first-seen form)
        assert company_texts.count("VentMarket, LLC") == 1
        assert "VENTMARKET, LLC" not in company_texts

    def test_allcaps_only_uses_first_seen(self):
        """When ALL-CAPS appears first, that becomes the canonical form."""
        text = (
            "NOVATECH, LLC SHALL INDEMNIFY THE OTHER PARTY.\n"
            "NovaTech, LLC agrees to the terms."
        )
        entities = detect_entities_regex(text)
        company_entities = [e for e in entities if e.entity_type == "COMPANY"]
        company_texts = [e.text for e in company_entities]
        # Only one entry, and the first-seen form wins
        ventmarket_entries = [t for t in company_texts if "novatech" in t.lower()]
        assert len(ventmarket_entries) == 1

    def test_counts_are_merged(self):
        """Merged case variants should have combined counts."""
        text = (
            "VentMarket, LLC provides services. "
            "VentMarket, LLC is based in Texas. "
            "VENTMARKET, LLC\nBy: _______________"
        )
        entities = detect_entities_regex(text)
        company_entities = [e for e in entities if e.entity_type == "COMPANY"]
        vm = [e for e in company_entities if e.text == "VentMarket, LLC"]
        assert len(vm) == 1
        assert vm[0].count == 3  # 2 mixed-case + 1 all-caps


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

    def test_agreement_reference_not_detected_as_party(self):
        """'Transition Services Agreement' should not yield a party named 'Transition Services'."""
        text = (
            'This Transition Services Agreement dated January 1, 2026 '
            '(the "Prior Agreement") is entered into by Acme Corp. '
            '(the "Company") and Beta LLC (the "Vendor").'
        )
        result = detect_party_names(text)
        names = {r["name"] for r in result}
        assert "Transition Services" not in names, (
            f"Agreement reference mistakenly detected: {names}"
        )
        assert "Acme Corp." in names
        assert "Beta LLC" in names


# ===================================================================
# GLiNER integration: text chunking
# ===================================================================

class TestChunkText:

    def test_short_text_single_chunk(self):
        text = "This is a short sentence with only a few words."
        chunks = _chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0][0] == text
        assert chunks[0][1] == 0  # char offset

    def test_long_text_splits(self):
        # 400 words, well above the 350-word default
        words = ["word"] * 400
        text = " ".join(words)
        chunks = _chunk_text(text)
        assert len(chunks) >= 2

    def test_sentence_boundaries_respected(self):
        # Build text with clear sentence boundaries and enough words
        sentences = ["This is sentence number %d." % i for i in range(80)]
        text = " ".join(sentences)
        chunks = _chunk_text(text)
        # Each chunk (except possibly the last) should end at a sentence boundary
        for chunk_text, _ in chunks[:-1]:
            assert chunk_text.rstrip().endswith(".")

    def test_overlap_between_chunks(self):
        words = ["word%d" % i for i in range(500)]
        text = " ".join(words)
        chunks = _chunk_text(text, max_words=100, overlap_words=20)
        if len(chunks) >= 2:
            first_words = set(chunks[0][0].split())
            second_words = set(chunks[1][0].split())
            overlap = first_words & second_words
            assert len(overlap) > 0

    def test_empty_text(self):
        assert _chunk_text("") == []


# ===================================================================
# GLiNER label mapping
# ===================================================================

class TestGlinerLabelMapping:

    def test_all_labels_mapped(self):
        expected_labels = {"person", "organization", "address", "date", "money"}
        assert set(_GLINER_LABEL_MAP.keys()) == expected_labels

    def test_mapped_types_are_valid(self):
        expected_types = {"PERSON", "COMPANY", "ADDRESS", "DATE", "AMOUNT"}
        assert set(_GLINER_LABEL_MAP.values()) == expected_types


# ===================================================================
# GLiNER inference (mocked model)
# ===================================================================

class TestRunGliner:

    @patch("clientcloak.detector._get_gliner_model")
    def test_returns_entities(self, mock_get_model):
        mock_model = MagicMock()
        mock_model.predict_entities.return_value = [
            {"text": "John Smith", "label": "person", "score": 0.95},
            {"text": "Acme Corp", "label": "organization", "score": 0.88},
        ]
        mock_get_model.return_value = mock_model

        result = _run_gliner("John Smith works at Acme Corp.")
        assert len(result) == 2
        types = {e.entity_type for e in result}
        assert "PERSON" in types
        assert "COMPANY" in types
        texts = {e.text for e in result}
        assert "John Smith" in texts
        assert "Acme Corp" in texts

    @patch("clientcloak.detector._get_gliner_model")
    def test_uses_minimum_threshold_for_predict(self, mock_get_model):
        """predict_entities receives min of per-label thresholds (currently 0.3)."""
        mock_model = MagicMock()
        mock_model.predict_entities.return_value = []
        mock_get_model.return_value = mock_model

        from clientcloak.detector import _GLINER_THRESHOLDS
        expected_min = min(_GLINER_THRESHOLDS.values())

        _run_gliner("Some text", threshold=0.7)
        call_args = mock_model.predict_entities.call_args
        assert call_args is not None
        # threshold is passed as keyword arg
        if call_args.kwargs.get("threshold") is not None:
            assert call_args.kwargs["threshold"] == expected_min
        else:
            assert call_args[0][2] == expected_min

    @patch("clientcloak.detector._get_gliner_model")
    def test_deduplicates_cross_chunk(self, mock_get_model):
        mock_model = MagicMock()
        # Same entity returned from overlapping chunks
        mock_model.predict_entities.return_value = [
            {"text": "John Smith", "label": "person", "score": 0.95},
        ]
        mock_get_model.return_value = mock_model

        # Use a long text that forces multiple chunks
        words = ["word"] * 400
        words[10] = "John"
        words[11] = "Smith"
        text = " ".join(words)
        result = _run_gliner(text)
        # Even if predict_entities is called multiple times (once per chunk),
        # "John Smith" should appear only once in the results
        john_entities = [e for e in result if e.text == "John Smith"]
        assert len(john_entities) == 1

    @patch("clientcloak.detector._get_gliner_model")
    def test_returns_empty_when_unavailable(self, mock_get_model):
        mock_get_model.return_value = None
        result = _run_gliner("John Smith works at Acme Corp.")
        assert result == []


# ===================================================================
# Placeholder reassignment
# ===================================================================

class TestReassignPlaceholders:

    def test_sequential_numbering(self):
        entities = [
            DetectedEntity(text="john@a.com", entity_type="EMAIL", confidence=1.0, count=1, suggested_placeholder="[Email-99]"),
            DetectedEntity(text="jane@b.com", entity_type="EMAIL", confidence=1.0, count=1, suggested_placeholder="[Email-100]"),
        ]
        result = _reassign_placeholders(entities)
        placeholders = [e.suggested_placeholder for e in result]
        assert "[Email-1]" in placeholders
        assert "[Email-2]" in placeholders

    def test_independent_per_type(self):
        entities = [
            DetectedEntity(text="john@a.com", entity_type="EMAIL", confidence=1.0, count=1, suggested_placeholder="[Email-5]"),
            DetectedEntity(text="555-123-4567", entity_type="PHONE", confidence=1.0, count=1, suggested_placeholder="[Phone-5]"),
            DetectedEntity(text="jane@b.com", entity_type="EMAIL", confidence=1.0, count=1, suggested_placeholder="[Email-6]"),
        ]
        result = _reassign_placeholders(entities)
        emails = [e for e in result if e.entity_type == "EMAIL"]
        phones = [e for e in result if e.entity_type == "PHONE"]
        email_placeholders = sorted([e.suggested_placeholder for e in emails])
        assert email_placeholders == ["[Email-1]", "[Email-2]"]
        assert phones[0].suggested_placeholder == "[Phone-1]"


# ===================================================================
# detect_entities() with GLiNER integration
# ===================================================================

class TestDetectEntitiesWithGliner:

    @patch("clientcloak.detector._run_gliner")
    def test_gliner_merged_with_regex(self, mock_run_gliner):
        mock_run_gliner.return_value = [
            DetectedEntity(
                text="John Smith",
                entity_type="PERSON",
                confidence=0.92,
                count=1,
                suggested_placeholder="[Person-1]",
            ),
        ]
        text = "John Smith can be reached at john@example.com."
        result = detect_entities(text, use_gliner=True)
        types = {e.entity_type for e in result}
        assert "EMAIL" in types   # from regex
        assert "PERSON" in types  # from GLiNER

    @patch("clientcloak.detector._run_gliner")
    def test_use_gliner_false_skips_it(self, mock_run_gliner):
        text = "Contact john@example.com for details."
        result = detect_entities(text, use_gliner=False)
        mock_run_gliner.assert_not_called()
        types = {e.entity_type for e in result}
        assert "EMAIL" in types

    @patch("clientcloak.detector._run_gliner")
    def test_gliner_failure_falls_back_to_regex(self, mock_run_gliner):
        mock_run_gliner.side_effect = RuntimeError("Model failed")
        text = "Send $500 to john@example.com."
        result = detect_entities(text, use_gliner=True)
        # Regex results should still be returned
        types = {e.entity_type for e in result}
        assert "EMAIL" in types
        assert "AMOUNT" in types

    @patch("clientcloak.detector._run_gliner")
    def test_date_entity_type(self, mock_run_gliner):
        mock_run_gliner.return_value = [
            DetectedEntity(
                text="January 15, 2026",
                entity_type="DATE",
                confidence=0.89,
                count=1,
                suggested_placeholder="[Date-1]",
            ),
        ]
        text = "The contract was signed on January 15, 2026."
        result = detect_entities(text, use_gliner=True)
        date_entities = [e for e in result if e.entity_type == "DATE"]
        assert len(date_entities) == 1
        assert date_entities[0].text == "January 15, 2026"


# ===================================================================
# DATE regex detection
# ===================================================================

class TestDatePatterns:
    """Tests for DATE regex detection in detect_entities_regex()."""

    def test_matches_month_dd_comma_yyyy(self):
        entities = detect_entities_regex("Effective as of June 30, 2025.")
        dates = [e for e in entities if e.entity_type == "DATE"]
        assert len(dates) >= 1
        assert any("June 30, 2025" in d.text for d in dates)

    def test_matches_month_dd_yyyy_no_comma(self):
        entities = detect_entities_regex("Filed on November 6 2024 with the SEC.")
        dates = [e for e in entities if e.entity_type == "DATE"]
        assert len(dates) >= 1
        assert any("November 6 2024" in d.text for d in dates)

    def test_matches_mm_slash_dd_slash_yyyy(self):
        entities = detect_entities_regex("Date: 01/15/2025")
        dates = [e for e in entities if e.entity_type == "DATE"]
        assert len(dates) >= 1
        assert any("01/15/2025" in d.text for d in dates)

    def test_matches_mm_dash_dd_dash_yyyy(self):
        entities = detect_entities_regex("Signed 12-31-2024.")
        dates = [e for e in entities if e.entity_type == "DATE"]
        assert len(dates) >= 1
        assert any("12-31-2024" in d.text for d in dates)

    def test_matches_dd_month_yyyy(self):
        entities = detect_entities_regex("On the 6 November 2024, the parties agreed.")
        dates = [e for e in entities if e.entity_type == "DATE"]
        assert len(dates) >= 1
        assert any("6 November 2024" in d.text for d in dates)

    def test_no_false_positive_on_year_alone(self):
        entities = detect_entities_regex("In the year 2025, the company grew.")
        dates = [e for e in entities if e.entity_type == "DATE"]
        # A bare "2025" should NOT be detected as a date
        assert not any(d.text.strip() == "2025" for d in dates)

    def test_counts_multiple_dates(self):
        text = "From June 30, 2025 to December 31, 2025."
        entities = detect_entities_regex(text)
        dates = [e for e in entities if e.entity_type == "DATE"]
        assert len(dates) >= 2


# ===================================================================
# Expanded PERSON regex patterns
# ===================================================================

class TestExpandedPersonPatterns:
    """Tests for expanded PERSON regex patterns."""

    def test_signature_block_underscores(self):
        text = "_______________\nJohn Smith"
        entities = detect_entities_regex(text)
        persons = [e for e in entities if e.entity_type == "PERSON"]
        assert any("John Smith" in p.text for p in persons)

    def test_signature_block_s_slash(self):
        text = "/s/ Jane Doe"
        entities = detect_entities_regex(text)
        persons = [e for e in entities if e.entity_type == "PERSON"]
        assert any("Jane Doe" in p.text for p in persons)

    def test_between_pattern(self):
        text = "This agreement is between Hugh Johnston, a resident of New York"
        entities = detect_entities_regex(text)
        persons = [e for e in entities if e.entity_type == "PERSON"]
        assert any("Hugh Johnston" in p.text for p in persons)

    def test_allcaps_name_after_label(self):
        text = "Name: JOHN SMITH"
        entities = detect_entities_regex(text)
        persons = [e for e in entities if e.entity_type == "PERSON"]
        assert any("JOHN SMITH" in p.text for p in persons)

    def test_name_with_middle_initial(self):
        text = "By: Hugh F. Johnston"
        entities = detect_entities_regex(text)
        persons = [e for e in entities if e.entity_type == "PERSON"]
        assert any("Hugh F. Johnston" in p.text for p in persons)

    def test_role_keyword_as_pattern(self):
        """'and Charles H. Noski as Beneficiaries' should match."""
        text = "as Trustee therein and Charles H. Noski as Beneficiaries' Representative"
        entities = detect_entities_regex(text)
        persons = [e for e in entities if e.entity_type == "PERSON"]
        assert any("Charles H. Noski" in p.text for p in persons)

    def test_false_positive_new_york_filtered(self):
        """Place names like 'New York' should not be detected as persons."""
        text = "between New York, a city in the state"
        entities = detect_entities_regex(text)
        persons = [e for e in entities if e.entity_type == "PERSON"]
        assert not any("New York" in p.text for p in persons)

    def test_false_positive_stock_market_filtered(self):
        """'Stock Market' should not be detected as a person."""
        text = "listed on the between Stock Market, the premier exchange"
        entities = detect_entities_regex(text)
        persons = [e for e in entities if e.entity_type == "PERSON"]
        assert not any("Stock Market" in p.text for p in persons)


# ===================================================================
# GLiNER per-label threshold filtering
# ===================================================================

class TestGlinerThresholds:
    """Tests for per-label GLiNER threshold filtering."""

    @patch("clientcloak.detector._get_gliner_model")
    def test_person_at_low_score_passes_with_lower_threshold(self, mock_get_model):
        """Person at 0.35 should pass since person threshold is 0.3."""
        mock_model = MagicMock()
        mock_model.predict_entities.return_value = [
            {"text": "John Smith", "label": "person", "score": 0.35},
        ]
        mock_get_model.return_value = mock_model
        result = _run_gliner("John Smith works here.")
        persons = [e for e in result if e.entity_type == "PERSON"]
        assert len(persons) == 1

    @patch("clientcloak.detector._get_gliner_model")
    def test_person_below_threshold_filtered(self, mock_get_model):
        """Person at 0.2 should be filtered since person threshold is 0.3."""
        mock_model = MagicMock()
        mock_model.predict_entities.return_value = [
            {"text": "John Smith", "label": "person", "score": 0.2},
        ]
        mock_get_model.return_value = mock_model
        result = _run_gliner("John Smith works here.")
        persons = [e for e in result if e.entity_type == "PERSON"]
        assert len(persons) == 0

    @patch("clientcloak.detector._get_gliner_model")
    def test_money_needs_higher_threshold(self, mock_get_model):
        """Money at 0.4 should be filtered since money threshold is 0.5."""
        mock_model = MagicMock()
        mock_model.predict_entities.return_value = [
            {"text": "$500", "label": "money", "score": 0.4},
        ]
        mock_get_model.return_value = mock_model
        result = _run_gliner("Pay $500 now.")
        amounts = [e for e in result if e.entity_type == "AMOUNT"]
        assert len(amounts) == 0


# ===================================================================
# ADDRESS regex with full state names
# ===================================================================

class TestAddressFullStateNames:
    """Tests for ADDRESS regex matching full state names."""

    def test_matches_full_state_name(self):
        text = "Located at 601 Travis Street, Houston, Texas 77002"
        entities = detect_entities_regex(text)
        addresses = [e for e in entities if e.entity_type == "ADDRESS"]
        assert len(addresses) >= 1
        assert any("601 Travis Street" in a.text for a in addresses)

    def test_still_matches_abbreviation(self):
        """Existing 2-letter state abbreviations should still work."""
        text = "Office at 123 Main Street, Springfield, IL 62704"
        entities = detect_entities_regex(text)
        addresses = [e for e in entities if e.entity_type == "ADDRESS"]
        assert len(addresses) >= 1

    def test_matches_multi_word_state(self):
        text = "Offices at 456 Oak Avenue, Anytown, North Carolina 27654"
        entities = detect_entities_regex(text)
        addresses = [e for e in entities if e.entity_type == "ADDRESS"]
        assert len(addresses) >= 1


# ===================================================================
# Context-aware bare amount detection (no $ prefix)
# ===================================================================

class TestBareAmountPattern:
    """Tests for context-aware bare amount detection (no $ prefix)."""

    def test_matches_exceed_shares(self):
        text = "shall not exceed 11,200,000 shares"
        entities = detect_entities_regex(text)
        amounts = [e for e in entities if e.entity_type == "AMOUNT"]
        assert any("11,200,000" in a.text for a in amounts)

    def test_matches_up_to_dollars(self):
        text = "up to 5,000,000 dollars in total"
        entities = detect_entities_regex(text)
        amounts = [e for e in entities if e.entity_type == "AMOUNT"]
        assert any("5,000,000" in a.text for a in amounts)

    def test_rejects_plain_number_no_context(self):
        """A number without financial context keywords should not match."""
        text = "Section 11,200,000 of the document"
        entities = detect_entities_regex(text)
        amounts = [e for e in entities if e.entity_type == "AMOUNT"]
        assert not any("11,200,000" in a.text for a in amounts)


# ===================================================================
# NER text length limit (max_chars)
# ===================================================================

class TestMaxNerChars:
    """Tests for the max_chars parameter in detect_entities()."""

    @patch("clientcloak.detector._run_gliner")
    def test_truncates_text_for_gliner(self, mock_run_gliner):
        """GLiNER should only receive the first max_chars characters."""
        mock_run_gliner.return_value = []

        text = "A" * 500
        detect_entities(text, use_gliner=True, max_chars=100)

        # _run_gliner should have been called with truncated text
        call_args = mock_run_gliner.call_args
        assert len(call_args[0][0]) == 100

    @patch("clientcloak.detector._run_gliner")
    def test_zero_means_unlimited(self, mock_run_gliner):
        """max_chars=0 should pass the full text to GLiNER."""
        mock_run_gliner.return_value = []

        text = "A" * 500
        detect_entities(text, use_gliner=True, max_chars=0)

        call_args = mock_run_gliner.call_args
        assert len(call_args[0][0]) == 500

    @patch("clientcloak.detector._run_gliner")
    def test_regex_still_scans_full_text(self, mock_run_gliner):
        """Regex detection should scan the entire document regardless of max_chars."""
        mock_run_gliner.return_value = []

        # Put an email past the max_chars boundary
        text = "A" * 200 + " john@example.com"
        result = detect_entities(text, use_gliner=True, max_chars=100)

        emails = [e for e in result if e.entity_type == "EMAIL"]
        assert len(emails) == 1
        assert emails[0].text == "john@example.com"

    @patch("clientcloak.detector._run_gliner")
    def test_entity_before_limit_detected(self, mock_run_gliner):
        """An entity in the first max_chars characters should be detected by GLiNER."""
        mock_run_gliner.return_value = [
            DetectedEntity(
                text="John Smith",
                entity_type="PERSON",
                confidence=0.95,
                count=1,
                suggested_placeholder="[Person-1]",
            ),
        ]

        text = "John Smith works here. " + "A" * 500
        result = detect_entities(text, use_gliner=True, max_chars=100)

        persons = [e for e in result if e.entity_type == "PERSON"]
        assert len(persons) == 1

    @patch("clientcloak.detector._run_gliner")
    def test_no_truncation_when_text_shorter_than_limit(self, mock_run_gliner):
        """When text is shorter than max_chars, the full text is passed."""
        mock_run_gliner.return_value = []

        text = "Short text."
        detect_entities(text, use_gliner=True, max_chars=200_000)

        call_args = mock_run_gliner.call_args
        assert call_args[0][0] == text
