"""
Tests for clientcloak.mapping: create, save, load, and invert mappings.
"""

import json
import pytest
from pathlib import Path

from clientcloak.mapping import (
    create_mapping,
    get_cloak_replacements,
    load_mapping,
    save_mapping,
)
from clientcloak.models import MappingFile


# ===================================================================
# create_mapping
# ===================================================================

class TestCreateMapping:
    """Tests for create_mapping()."""

    def test_basic_creation(self):
        m = create_mapping(
            original_file="test.docx",
            mappings={"Licensor": "Acme Corp", "Licensee": "BigCo LLC"},
        )
        assert isinstance(m, MappingFile)
        assert m.original_file == "test.docx"
        assert m.mappings == {"Licensor": "Acme Corp", "Licensee": "BigCo LLC"}
        assert m.version == "1.0"

    def test_with_party_labels(self):
        m = create_mapping(
            original_file="test.docx",
            mappings={"A": "B"},
            party_labels={"party_a": "Customer", "party_b": "Vendor"},
        )
        assert m.party_labels == {"party_a": "Customer", "party_b": "Vendor"}

    def test_with_comment_authors(self):
        m = create_mapping(
            original_file="test.docx",
            mappings={"A": "B"},
            comment_authors={"Reviewer A": "Jane Smith"},
        )
        assert m.comment_authors == {"Reviewer A": "Jane Smith"}

    def test_defaults_empty_dicts(self):
        m = create_mapping(original_file="f.docx", mappings={})
        assert m.party_labels == {}
        assert m.comment_authors == {}


# ===================================================================
# save_mapping / load_mapping
# ===================================================================

class TestSaveLoadMapping:
    """Tests for save_mapping() and load_mapping() round-trip."""

    def test_round_trip(self, tmp_path):
        original = create_mapping(
            original_file="contract.docx",
            mappings={"Licensor": "Acme Corp", "Licensee": "BigCo LLC"},
            party_labels={"party_a": "Licensor", "party_b": "Licensee"},
        )
        path = tmp_path / "mapping.json"
        save_mapping(original, path)

        loaded = load_mapping(path)
        assert loaded.original_file == original.original_file
        assert loaded.mappings == original.mappings
        assert loaded.party_labels == original.party_labels

    def test_save_creates_json_file(self, tmp_path):
        m = create_mapping("f.docx", {"A": "B"})
        path = tmp_path / "out.json"
        save_mapping(m, path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert "mappings" in data

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_mapping(tmp_path / "nope.json")

    def test_load_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json at all", encoding="utf-8")
        with pytest.raises(Exception):  # pydantic.ValidationError or JSONDecodeError
            load_mapping(bad)

    def test_load_existing_fixture(self):
        """Load the pre-existing sample mapping fixture."""
        path = Path(__file__).parent / "fixtures" / "sample_contract_mapping.json"
        if not path.exists():
            pytest.skip("Fixture not available")
        m = load_mapping(path)
        assert m.original_file == "sample_contract.docx"
        assert "Licensor" in m.mappings
        assert "Licensee" in m.mappings


# ===================================================================
# get_cloak_replacements
# ===================================================================

class TestGetCloakReplacements:
    """Tests for get_cloak_replacements() (mapping inversion)."""

    def test_basic_inversion(self):
        m = create_mapping(
            "f.docx",
            mappings={"Licensor": "Acme Corp", "Licensee": "BigCo LLC"},
        )
        inverted = get_cloak_replacements(m)
        assert inverted == {"Acme Corp": "Licensor", "BigCo LLC": "Licensee"}

    def test_empty_mappings(self):
        m = create_mapping("f.docx", mappings={})
        assert get_cloak_replacements(m) == {}

    def test_inversion_is_bijective(self):
        """Verify that inverting twice returns the original mapping."""
        original_mappings = {"[PARTY_A]": "Acme", "[PARTY_B]": "BigCo"}
        m = create_mapping("f.docx", mappings=original_mappings)
        inverted = get_cloak_replacements(m)
        # Invert back
        double_inverted = {v: k for k, v in inverted.items()}
        assert double_inverted == original_mappings
