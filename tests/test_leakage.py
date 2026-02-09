"""
Leakage detection tests for the cloaking pipeline.

Creates synthetic .docx documents covering common legal patterns and verifies
that no original party names survive cloaking.  Also tests round-trip fidelity
(cloak → uncloak) and filename sanitization.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from docx import Document

from clientcloak.cloaker import cloak_document, sanitize_filename, build_cloak_replacements
from clientcloak.detector import detect_party_names, detect_entities
from clientcloak.docx_handler import extract_all_text, load_document
from clientcloak.models import CloakConfig, CommentMode
from clientcloak.uncloaker import uncloak_document


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_docx(tmp_path: Path, name: str, paragraphs: list[str]) -> Path:
    """Create a minimal .docx with the given paragraphs."""
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    path = tmp_path / name
    doc.save(str(path))
    return path


def _extract_text(path: Path) -> str:
    """Extract all paragraph text from a .docx file."""
    doc = load_document(path)
    fragments = extract_all_text(doc)
    return "\n".join(text for text, _source in fragments)


def _assert_no_leaks(cloaked_text: str, forbidden: list[str], context: str = "") -> None:
    """Assert that none of the forbidden strings appear in cloaked_text (case-insensitive).

    Checks both the document body and inside placeholder brackets, since a
    placeholder like ``[IBM]`` would still reveal the party's identity.
    """
    lower_text = cloaked_text.lower()
    for term in forbidden:
        # Skip very short terms (≤2 chars) that would produce false positives
        if len(term) <= 2:
            continue
        assert term.lower() not in lower_text, (
            f"Leaked '{term}' in cloaked output{' (' + context + ')' if context else ''}"
        )


# ---------------------------------------------------------------------------
# Test corpus: synthetic documents
# ---------------------------------------------------------------------------

# Each entry: (test_id, paragraphs, party_a, party_b, extra_forbidden)
# extra_forbidden = additional strings that must not appear in the cloaked output
# beyond party_a and party_b.

CORPUS = [
    pytest.param(
        "comma_llc",
        [
            'This Agreement is by and between VentMarket, LLC, a Texas LLC '
            '("VentMarket"), and Centinnial Logistics Services, LLC ("CLS").',
            "WHEREAS, VentMarket is in the business of HVAC distribution.",
            "WHEREAS, CLS provides warehouse labor services to VentMarket.",
            "VentMarket shall pay CLS monthly fees.",
            "VentMarket's obligations include timely payment.",
        ],
        "VentMarket, LLC",
        "Centinnial Logistics Services, LLC",
        ["VentMarket", "Centinnial", "CLS"],
        id="comma_llc",
    ),
    pytest.param(
        "standard_inc",
        [
            'This NDA is between Acme Corp., a Delaware corporation '
            '("Company"), and BigTech Solutions Inc. ("Vendor").',
            "Company shall disclose information to Vendor.",
            "Acme Corp. retains all intellectual property rights.",
            "BigTech Solutions Inc. shall maintain confidentiality.",
            "BigTech Solutions shall not disclose to third parties.",
        ],
        "Acme Corp.",
        "BigTech Solutions Inc.",
        ["Acme", "BigTech"],
        id="standard_inc",
    ),
    pytest.param(
        "abbreviation_defined_term",
        [
            'Agreement between International Business Machines Corporation '
            '("IBM") and Advanced Micro Devices, Inc. ("AMD").',
            "IBM shall license technology to AMD.",
            "AMD shall pay royalties to IBM quarterly.",
            "IBM's patents cover the licensed technology.",
        ],
        "International Business Machines Corporation",
        "Advanced Micro Devices, Inc.",
        ["IBM", "AMD", "International Business Machines", "Advanced Micro Devices"],
        id="abbreviation_defined_term",
    ),
    pytest.param(
        "possessives",
        [
            'Service Agreement between MakeRight Holdings, LLC '
            '("MakeRight") and Stellar Group PBC ("Stellar").',
            "MakeRight's employees shall access Stellar's facilities.",
            "Stellar's equipment remains Stellar's property.",
            "MakeRight's obligations survive termination.",
        ],
        "MakeRight Holdings, LLC",
        "Stellar Group PBC",
        ["MakeRight", "Stellar"],
        id="possessives",
    ),
    pytest.param(
        "all_caps_sections",
        [
            'Agreement between NovaTech, LLC ("NovaTech") and '
            'Pinnacle Systems Inc. ("Pinnacle").',
            "NOVATECH SHALL INDEMNIFY PINNACLE AGAINST ALL CLAIMS.",
            "PINNACLE'S LIABILITY SHALL NOT EXCEED THE CONTRACT VALUE.",
            "NovaTech and Pinnacle agree to binding arbitration.",
        ],
        "NovaTech, LLC",
        "Pinnacle Systems Inc.",
        ["NovaTech", "Pinnacle", "NOVATECH", "PINNACLE"],
        id="all_caps_sections",
    ),
    pytest.param(
        "signature_block",
        [
            'Agreement between AlphaWave Corp. ("AlphaWave") and '
            'BetaForge Ltd. ("BetaForge").',
            "AlphaWave shall deliver services to BetaForge.",
            "IN WITNESS WHEREOF, the Parties have executed this Agreement.",
            "ALPHAWAVE CORP.",
            "By: _______________",
            "BETAFORGE LTD.",
            "By: _______________",
        ],
        "AlphaWave Corp.",
        "BetaForge Ltd.",
        ["AlphaWave", "BetaForge", "ALPHAWAVE", "BETAFORGE"],
        id="signature_block",
    ),
    pytest.param(
        "third_party_company",
        [
            'Lease Agreement between TrueNorth Properties, LLC '
            '("Landlord") and Bright Horizon Services Inc. ("Tenant").',
            "WHEREAS, Landlord owns the premises at 123 Main St.",
            "WHEREAS, Tenant leases space from Adventura Holdings, LLC for its offices.",
            "TrueNorth Properties shall maintain the building.",
            "Bright Horizon Services shall pay rent monthly.",
        ],
        "TrueNorth Properties, LLC",
        "Bright Horizon Services Inc.",
        ["TrueNorth", "Bright Horizon"],
        id="third_party_company",
    ),
]


# ---------------------------------------------------------------------------
# Test: no party name leakage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "test_id, paragraphs, party_a, party_b, extra_forbidden",
    CORPUS,
)
def test_no_leakage(
    tmp_path, test_id, paragraphs, party_a, party_b, extra_forbidden,
):
    """Cloak a document and verify no original party names survive."""
    docx_path = _make_docx(tmp_path, f"{test_id}.docx", paragraphs)
    output_path = tmp_path / f"{test_id}_cloaked.docx"
    mapping_path = tmp_path / f"{test_id}_mapping.json"

    # Detect party names from preamble to get short forms
    preamble = "\n".join(paragraphs[:3])
    detected = detect_party_names(preamble)

    # Build short forms from detected defined terms
    a_short = []
    b_short = []
    for party in detected:
        dt = party.get("defined_term")
        if not dt:
            continue
        if party["name"].lower() == party_a.lower():
            a_short.append(dt)
        elif party["name"].lower() == party_b.lower():
            b_short.append(dt)

    config = CloakConfig(
        party_a_name=party_a,
        party_b_name=party_b,
        party_a_label=detected[0]["label"] if detected else "Company",
        party_b_label=detected[1]["label"] if len(detected) > 1 else "Counterparty",
        party_a_short_forms=a_short,
        party_b_short_forms=b_short,
        comment_mode=CommentMode.STRIP,
        strip_metadata=True,
    )

    result = cloak_document(
        input_path=docx_path,
        output_path=output_path,
        mapping_path=mapping_path,
        config=config,
    )

    assert result.replacements_applied > 0, "No replacements were applied"

    # Extract text from cloaked document
    cloaked_text = _extract_text(Path(result.output_path))

    # Build forbidden list: party names + all extra forbidden terms
    forbidden = [party_a, party_b] + extra_forbidden
    _assert_no_leaks(cloaked_text, forbidden, context=test_id)


# ---------------------------------------------------------------------------
# Test: round-trip fidelity (cloak → uncloak)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "test_id, paragraphs, party_a, party_b, extra_forbidden",
    CORPUS,
)
def test_roundtrip_fidelity(
    tmp_path, test_id, paragraphs, party_a, party_b, extra_forbidden,
):
    """Cloak then uncloak, and verify party names are restored."""
    docx_path = _make_docx(tmp_path, f"{test_id}.docx", paragraphs)
    output_path = tmp_path / f"{test_id}_cloaked.docx"
    mapping_path = tmp_path / f"{test_id}_mapping.json"

    preamble = "\n".join(paragraphs[:3])
    detected = detect_party_names(preamble)

    a_short = []
    b_short = []
    for party in detected:
        dt = party.get("defined_term")
        if not dt:
            continue
        if party["name"].lower() == party_a.lower():
            a_short.append(dt)
        elif party["name"].lower() == party_b.lower():
            b_short.append(dt)

    config = CloakConfig(
        party_a_name=party_a,
        party_b_name=party_b,
        party_a_label=detected[0]["label"] if detected else "Company",
        party_b_label=detected[1]["label"] if len(detected) > 1 else "Counterparty",
        party_a_short_forms=a_short,
        party_b_short_forms=b_short,
        comment_mode=CommentMode.STRIP,
        strip_metadata=True,
    )

    result = cloak_document(
        input_path=docx_path,
        output_path=output_path,
        mapping_path=mapping_path,
        config=config,
    )

    # Uncloak
    uncloaked_path = tmp_path / f"{test_id}_uncloaked.docx"
    restored = uncloak_document(
        input_path=Path(result.output_path),
        output_path=uncloaked_path,
        mapping_path=mapping_path,
    )
    assert restored > 0, "No replacements were restored"

    # Verify the primary party names appear in the uncloaked text
    uncloaked_text = _extract_text(uncloaked_path)
    assert party_a.lower() in uncloaked_text.lower(), (
        f"Party A '{party_a}' not found in uncloaked output"
    )
    assert party_b.lower() in uncloaked_text.lower(), (
        f"Party B '{party_b}' not found in uncloaked output"
    )


# ---------------------------------------------------------------------------
# Test: filename sanitization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, party_a, party_b, a_short, b_short, forbidden",
    [
        pytest.param(
            "VentMarket - CLS Agreement.docx",
            "VentMarket, LLC", "Centinnial Logistics Services, LLC",
            ["VentMarket"], ["CLS"],
            ["VentMarket", "CLS"],
            id="filename_both_parties",
        ),
        pytest.param(
            "Acme_BigTech_NDA.docx",
            "Acme Corp.", "BigTech Solutions Inc.",
            [], [],
            ["Acme", "BigTech"],
            id="filename_underscore_separated",
        ),
        pytest.param(
            "AlphaWave-BetaForge-Services.docx",
            "AlphaWave Corp.", "BetaForge Ltd.",
            [], [],
            ["AlphaWave", "BetaForge"],
            id="filename_hyphen_separated",
        ),
    ],
)
def test_filename_sanitization(filename, party_a, party_b, a_short, b_short, forbidden):
    """Verify party names are replaced in output filenames."""
    config = CloakConfig(
        party_a_name=party_a,
        party_b_name=party_b,
        party_a_label="Company",
        party_b_label="Vendor",
        party_a_short_forms=a_short,
        party_b_short_forms=b_short,
        comment_mode=CommentMode.STRIP,
    )
    replacements = build_cloak_replacements(config)
    sanitized = sanitize_filename(filename, replacements)
    for term in forbidden:
        assert term.lower() not in sanitized.lower(), (
            f"Leaked '{term}' in sanitized filename: {sanitized}"
        )


# ---------------------------------------------------------------------------
# Test: comma-separated suffix stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected_core",
    [
        ("VentMarket, LLC", "VentMarket"),
        ("Acme, Inc.", "Acme"),
        ("BigOrg Group, Ltd.", "BigOrg Group"),
        ("Centinnial Logistics Services, LLC", "Centinnial Logistics Services"),
        # Non-comma forms still work
        ("Acme Corp.", "Acme"),
        ("BigTech Solutions Inc.", "BigTech Solutions"),
        ("Partners", "Partners"),  # bare suffix — no stripping
    ],
    ids=["comma_llc", "comma_inc", "comma_ltd", "comma_multi_word",
         "no_comma_corp", "no_comma_inc", "bare_suffix"],
)
def test_strip_corporate_suffix(name, expected_core):
    """Verify comma-separated corporate suffixes are stripped cleanly."""
    from clientcloak.cloaker import _strip_corporate_suffix
    result = _strip_corporate_suffix(name)
    # If expected_core equals the input, stripping should be a no-op
    if expected_core == name:
        assert result == name
    else:
        assert result == expected_core, f"Expected '{expected_core}', got '{result}'"


# ---------------------------------------------------------------------------
# Test: abbreviation detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, name, expected",
    [
        ("CLS", "Centinnial Logistics Services, LLC", True),
        ("IBM", "International Business Machines Corporation", True),
        ("AMD", "Advanced Micro Devices, Inc.", True),
        ("Licensee", "Acme Corp.", False),
        ("Vendor", "BigTech Solutions Inc.", False),
        ("Company", "MakeRight Holdings, LLC", False),
    ],
    ids=["acronym_cls", "acronym_ibm", "acronym_amd",
         "role_licensee", "role_vendor", "role_company"],
)
def test_is_abbreviation(label, name, expected):
    """Verify abbreviation/acronym detection heuristic."""
    from clientcloak.detector import _is_abbreviation
    assert _is_abbreviation(label, name) == expected


# ---------------------------------------------------------------------------
# Test: defined term detection covers abbreviations
# ---------------------------------------------------------------------------


def test_detect_abbreviation_defined_term():
    """Verify that abbreviation defined terms get defined_term set and generic label."""
    preamble = (
        'Agreement between Centinnial Logistics Services, LLC, a Texas LLC '
        '("CLS"), and VentMarket, LLC, a Texas LLC ("VentMarket").'
    )
    parties = detect_party_names(preamble)
    # Find the party whose defined_term is "CLS"
    cls_party = next((p for p in parties if p.get("defined_term") == "CLS"), None)
    assert cls_party is not None, f"CLS not detected in: {parties}"
    assert cls_party["defined_term"] == "CLS"
    # Label should be generic (not "CLS") to avoid leaking the acronym
    assert cls_party["label"] in ("Company", "Counterparty"), (
        f"Expected generic label, got '{cls_party['label']}'"
    )


def test_detect_name_resembling_defined_term():
    """Verify that name-resembling defined terms get defined_term set."""
    preamble = (
        'Agreement between VentMarket, LLC, a Texas limited liability company '
        '("VentMarket"), and Acme Corp., a Delaware corporation ("Acme").'
    )
    parties = detect_party_names(preamble)
    vm = next((p for p in parties if "VentMarket" in p["name"]), None)
    acme = next((p for p in parties if "Acme" in p["name"]), None)
    assert vm is not None, f"VentMarket not detected in: {parties}"
    assert acme is not None, f"Acme not detected in: {parties}"
    assert "defined_term" in vm, f"defined_term not set for VentMarket: {vm}"
    assert vm["defined_term"] == "VentMarket"
    assert "defined_term" in acme, f"defined_term not set for Acme: {acme}"
    assert acme["defined_term"] == "Acme"


# ---------------------------------------------------------------------------
# Test: full-document company name detection
# ---------------------------------------------------------------------------


def test_company_detection_catches_third_parties(tmp_path):
    """Verify that company names are detected throughout the full document."""
    paragraphs = [
        'Agreement between TrueNorth Properties, LLC ("Landlord") '
        'and Bright Horizon Services Inc. ("Tenant").',
        "Landlord owns the building located at 123 Main Street.",
        "WHEREAS, Tenant subleases space from Adventura Holdings, LLC.",
        "Phoenix Capital Group Inc. provided the financing.",
    ]
    docx_path = _make_docx(tmp_path, "test.docx", paragraphs)
    doc = load_document(docx_path)
    full_text = "\n".join(text for text, _source in extract_all_text(doc))

    entities = detect_entities(
        full_text,
        party_names=["TrueNorth Properties, LLC", "Bright Horizon Services Inc."],
    )
    company_entities = [e for e in entities if e.entity_type == "COMPANY"]
    company_names = {e.text for e in company_entities}

    assert "Adventura Holdings, LLC" in company_names, (
        f"Adventura Holdings not detected. Found: {company_names}"
    )
    assert "Phoenix Capital Group Inc." in company_names or \
           "Phoenix Capital Group Inc" in company_names, (
        f"Phoenix Capital Group not detected. Found: {company_names}"
    )
    # Primary parties should be filtered out
    for name in company_names:
        assert "TrueNorth" not in name, f"Primary party leaked: {name}"
        assert "Bright Horizon" not in name, f"Primary party leaked: {name}"
