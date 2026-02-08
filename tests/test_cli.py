"""
Tests for clientcloak.cli: command-line interface subcommands.

Tests use SystemExit assertions since the CLI calls sys.exit().
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from clientcloak.cli import main, _build_parser, _default_output_path
from tests.conftest import make_simple_docx, make_docx_with_comments


# ===================================================================
# Parser construction
# ===================================================================

class TestBuildParser:
    """Tests for the argument parser structure."""

    def test_parser_has_subcommands(self):
        parser = _build_parser()
        # Parse each subcommand with minimal args to verify they exist
        args = parser.parse_args(["scan", "file.docx"])
        assert args.command == "scan"

    def test_cloak_requires_parties(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["cloak", "file.docx"])  # missing --party-a, --party-b

    def test_uncloak_requires_mapping(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["uncloak", "file.docx"])  # missing --mapping

    def test_no_subcommand_prints_help(self):
        """Calling main() with no arguments prints help and exits 0."""
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 0


# ===================================================================
# _default_output_path
# ===================================================================

class TestDefaultOutputPath:
    """Tests for the default output path generator."""

    def test_inserts_suffix(self):
        result = _default_output_path(Path("/tmp/contract.docx"), "cloaked")
        assert result == Path("/tmp/contract_cloaked.docx")

    def test_preserves_extension(self):
        result = _default_output_path(Path("my_file.docx"), "uncloaked")
        assert result.suffix == ".docx"
        assert "uncloaked" in result.stem


# ===================================================================
# Cloak subcommand
# ===================================================================

class TestCloakSubcommand:
    """Tests for the 'cloak' subcommand via main()."""

    def test_cloak_creates_output_files(self, tmp_path):
        input_path = make_simple_docx(
            tmp_path / "input.docx",
            ["Agreement between Acme Corp and BigCo LLC."],
        )
        output_path = tmp_path / "cloaked.docx"
        mapping_path = tmp_path / "mapping.json"

        with pytest.raises(SystemExit) as exc_info:
            main([
                "cloak",
                str(input_path),
                "--party-a", "Acme Corp",
                "--party-b", "BigCo LLC",
                "--output", str(output_path),
                "--mapping", str(mapping_path),
            ])
        assert exc_info.value.code == 0
        assert output_path.exists()
        assert mapping_path.exists()

    def test_cloak_with_custom_labels(self, tmp_path):
        input_path = make_simple_docx(
            tmp_path / "input.docx",
            ["Agreement between Acme Corp and BigCo LLC."],
        )
        output_path = tmp_path / "cloaked.docx"
        mapping_path = tmp_path / "mapping.json"

        with pytest.raises(SystemExit) as exc_info:
            main([
                "cloak",
                str(input_path),
                "--party-a", "Acme Corp",
                "--party-b", "BigCo LLC",
                "--labels", "Vendor/Client",
                "--output", str(output_path),
                "--mapping", str(mapping_path),
            ])
        assert exc_info.value.code == 0

    def test_cloak_nonexistent_file_exits_1(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main([
                "cloak",
                str(tmp_path / "nonexistent.docx"),
                "--party-a", "A",
                "--party-b", "B",
            ])
        assert exc_info.value.code == 1


# ===================================================================
# Uncloak subcommand
# ===================================================================

class TestUncloakSubcommand:
    """Tests for the 'uncloak' subcommand via main()."""

    def test_uncloak_roundtrip(self, tmp_path):
        # First, cloak a document
        input_path = make_simple_docx(
            tmp_path / "input.docx",
            ["Acme Corp provides services to BigCo LLC."],
        )
        cloaked_path = tmp_path / "cloaked.docx"
        mapping_path = tmp_path / "mapping.json"
        uncloaked_path = tmp_path / "uncloaked.docx"

        with pytest.raises(SystemExit) as exc_info:
            main([
                "cloak",
                str(input_path),
                "--party-a", "Acme Corp",
                "--party-b", "BigCo LLC",
                "--output", str(cloaked_path),
                "--mapping", str(mapping_path),
            ])
        assert exc_info.value.code == 0

        # Then uncloak
        with pytest.raises(SystemExit) as exc_info:
            main([
                "uncloak",
                str(cloaked_path),
                "--mapping", str(mapping_path),
                "--output", str(uncloaked_path),
            ])
        assert exc_info.value.code == 0
        assert uncloaked_path.exists()

    def test_uncloak_missing_mapping_exits_1(self, tmp_path):
        input_path = make_simple_docx(tmp_path / "input.docx", ["Text"])
        with pytest.raises(SystemExit) as exc_info:
            main([
                "uncloak",
                str(input_path),
                "--mapping", str(tmp_path / "nope.json"),
            ])
        assert exc_info.value.code == 1


# ===================================================================
# Scan subcommand
# ===================================================================

class TestScanSubcommand:
    """Tests for the 'scan' subcommand via main()."""

    def test_scan_clean_document(self, tmp_path):
        input_path = make_simple_docx(tmp_path / "clean.docx", ["Normal text."])
        with pytest.raises(SystemExit) as exc_info:
            main(["scan", str(input_path)])
        assert exc_info.value.code == 0

    def test_scan_nonexistent_file_exits_1(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(["scan", str(tmp_path / "nope.docx")])
        assert exc_info.value.code == 1


# ===================================================================
# Inspect subcommand
# ===================================================================

class TestInspectSubcommand:
    """Tests for the 'inspect' subcommand via main()."""

    def test_inspect_document(self, tmp_path):
        input_path = make_simple_docx(tmp_path / "doc.docx", ["Some content."])
        with pytest.raises(SystemExit) as exc_info:
            main(["inspect", str(input_path)])
        assert exc_info.value.code == 0

    def test_inspect_nonexistent_raises(self, tmp_path):
        """The inspect handler calls inspect_metadata -> Document() which raises
        PackageNotFoundError for nonexistent files. This propagates as an
        uncaught exception (not FileNotFoundError, so the CLI re-raises it)."""
        from docx.opc.exceptions import PackageNotFoundError
        with pytest.raises((SystemExit, PackageNotFoundError)):
            main(["inspect", str(tmp_path / "nope.docx")])
