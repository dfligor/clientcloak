"""
Command-line interface for ClientCloak.

Provides subcommands for cloaking, uncloaking, scanning, and inspecting
.docx documents. This module is the entry point referenced in pyproject.toml
as ``clientcloak.cli:main``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .models import (
    CloakConfig,
    CloakResult,
    CommentMode,
    MetadataReport,
    PartyAlias,
    SecurityFinding,
    ThreatLevel,
)


# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    """Return True if stdout appears to support ANSI color codes."""
    if not hasattr(sys.stdout, "isatty"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


_COLOR_ENABLED: bool | None = None


def _color(text: str, code: str) -> str:
    """Wrap *text* in ANSI escape codes if the terminal supports it."""
    global _COLOR_ENABLED
    if _COLOR_ENABLED is None:
        _COLOR_ENABLED = _supports_color()
    if not _COLOR_ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"


def _red(text: str) -> str:
    return _color(text, "31")


def _yellow(text: str) -> str:
    return _color(text, "33")


def _blue(text: str) -> str:
    return _color(text, "34")


def _green(text: str) -> str:
    return _color(text, "32")


def _bold(text: str) -> str:
    return _color(text, "1")


def _dim(text: str) -> str:
    return _color(text, "2")


# ---------------------------------------------------------------------------
# Output formatting helpers
# ---------------------------------------------------------------------------

def _threat_color(level: ThreatLevel) -> str:
    """Return a color-coded string for a threat level."""
    label = level.value.upper()
    if level == ThreatLevel.CRITICAL:
        return _red(label)
    elif level == ThreatLevel.WARNING:
        return _yellow(label)
    else:
        return _blue(label)


def _print_header(text: str) -> None:
    """Print a section header with visual separation."""
    print()
    print(_bold(f"  {text}"))
    print(_dim(f"  {'─' * len(text)}"))


def _print_findings(findings: list[SecurityFinding]) -> None:
    """Print security findings with color-coded threat levels."""
    if not findings:
        print(f"  {_green('No security issues found.')}")
        return

    # Count by level
    counts: dict[ThreatLevel, int] = {}
    for f in findings:
        counts[f.threat_level] = counts.get(f.threat_level, 0) + 1

    summary_parts = []
    for level in (ThreatLevel.CRITICAL, ThreatLevel.WARNING, ThreatLevel.INFO):
        count = counts.get(level, 0)
        if count > 0:
            summary_parts.append(f"{_threat_color(level)}: {count}")
    print(f"  Found {len(findings)} issue(s): {', '.join(summary_parts)}")
    print()

    for i, finding in enumerate(findings, 1):
        level_str = _threat_color(finding.threat_level)
        print(f"  [{level_str}] {finding.description}")
        print(f"    Location: {finding.location}")
        if finding.content_preview:
            preview = finding.content_preview
            if len(preview) > 120:
                preview = preview[:117] + "..."
            print(f"    Preview:  {_dim(preview)}")
        print(f"    Action:   {finding.recommendation}")
        if i < len(findings):
            print()


def _print_metadata_report(report: MetadataReport) -> None:
    """Print a metadata report in a readable format."""
    fields = [
        ("Author", report.author),
        ("Last Modified By", report.last_modified_by),
        ("Company", report.company),
        ("Manager", report.manager),
        ("Created", report.created),
        ("Modified", report.modified),
        ("Last Printed", report.last_printed),
        ("Revision", report.revision),
        ("Application", report.application),
        ("App Version", report.app_version),
        ("Template", report.template),
    ]

    found_any = False
    for label, value in fields:
        if value:
            found_any = True
            print(f"  {label + ':':<20s} {value}")

    if report.comments_count > 0:
        found_any = True
        print(f"  {'Comments:':<20s} {report.comments_count}")

    if report.custom_properties:
        found_any = True
        print(f"  {'Custom Properties:':<20s} {len(report.custom_properties)}")
        for key, val in report.custom_properties.items():
            print(f"    {key}: {val}")

    if not found_any:
        print(f"  {_green('No metadata found.')}")


def _default_output_path(input_path: Path, suffix: str) -> Path:
    """Generate a default output path by inserting a suffix before the extension."""
    return input_path.parent / f"{input_path.stem}_{suffix}{input_path.suffix}"


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _parse_aliases(raw: list[str]) -> list[PartyAlias]:
    """Parse 'NAME=LABEL' strings into PartyAlias objects."""
    aliases: list[PartyAlias] = []
    for item in raw:
        if "=" not in item:
            print(f"Error: alias must be in NAME=LABEL format, got: {item!r}")
            continue
        name, label = item.split("=", 1)
        aliases.append(PartyAlias(name=name.strip(), label=label.strip()))
    return aliases


def _handle_cloak(args: argparse.Namespace) -> int:
    """Handle the 'cloak' subcommand."""
    from .cloaker import cloak_document, preview_entities

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else _default_output_path(input_path, "cloaked")
    mapping_path = Path(args.mapping) if args.mapping else _default_output_path(input_path, "mapping").with_suffix(".json")

    # Parse party labels
    labels = args.labels.split("/")
    if len(labels) != 2:
        print(f"Error: --labels must be in the format 'label_a/label_b', got: {args.labels}")
        return 1
    label_a, label_b = labels[0].strip(), labels[1].strip()

    # Parse aliases
    party_a_aliases = _parse_aliases(args.alias_a)
    party_b_aliases = _parse_aliases(args.alias_b)

    config = CloakConfig(
        party_a_name=args.party_a,
        party_a_label=label_a,
        party_b_name=args.party_b,
        party_b_label=label_b,
        party_a_aliases=party_a_aliases,
        party_b_aliases=party_b_aliases,
        comment_mode=CommentMode(args.comment_mode),
        strip_metadata=not args.no_strip_metadata,
        gliner_threshold=args.threshold,
    )

    _print_header("Cloaking Document")
    print(f"  Input:  {input_path}")
    print(f"  {config.party_a_name} -> [{config.party_a_label}]")
    for alias in config.party_a_aliases:
        print(f"    {alias.name} -> [{alias.label}]")
    print(f"  {config.party_b_name} -> [{config.party_b_label}]")
    for alias in config.party_b_aliases:
        print(f"    {alias.name} -> [{alias.label}]")
    print()

    # --- Entity detection ---
    if not args.no_detect:
        entities = preview_entities(input_path, config)
        if entities:
            _print_header("Detected Entities")
            for entity in entities:
                print(f"  {entity.text:<30s} {entity.entity_type:<8s} (x{entity.count}) -> {entity.suggested_placeholder}")
                config.additional_replacements[entity.suggested_placeholder] = entity.text
            print()
            print(f"  {_dim(f'{len(entities)} entity(ies) will be replaced.')}")
            print(f"  {_dim('Use --no-detect to skip entity detection.')}")
            print()

    result: CloakResult = cloak_document(input_path, output_path, mapping_path, config)

    # The actual output path may differ from the requested one if the
    # filename was sanitized to remove party names.
    actual_output = Path(result.output_path) if result.output_path else output_path

    # Summary
    _print_header("Cloaking Summary")
    print(f"  Replacements applied: {result.replacements_applied}")
    if result.entities_detected > 0:
        print(f"  Entities detected:    {result.entities_detected}")
    print(f"  Comment mode:         {config.comment_mode.value}")
    print(f"  Metadata stripped:    {'yes' if config.strip_metadata else 'no'}")

    # Security findings
    if result.security_findings:
        _print_header("Security Findings")
        _print_findings(result.security_findings)

    # Metadata report
    if result.metadata_report:
        _print_header("Metadata Removed")
        _print_metadata_report(result.metadata_report)

    # Output paths
    _print_header("Output Files")
    print(f"  Cloaked document: {actual_output.resolve()}")
    print(f"  Mapping file:     {mapping_path.resolve()}")
    print()

    return 0


def _handle_uncloak(args: argparse.Namespace) -> int:
    """Handle the 'uncloak' subcommand."""
    from .uncloaker import uncloak_document

    input_path = Path(args.input)
    mapping_path = Path(args.mapping)
    output_path = Path(args.output) if args.output else _default_output_path(input_path, "uncloaked")

    if not mapping_path.exists():
        print(f"Error: Mapping file not found: {mapping_path}")
        return 1

    _print_header("Uncloaking Document")
    print(f"  Input:   {input_path}")
    print(f"  Mapping: {mapping_path}")
    print()

    replacements_count: int = uncloak_document(input_path, output_path, mapping_path)

    _print_header("Uncloaking Summary")
    print(f"  Replacements restored: {replacements_count}")

    _print_header("Output Files")
    print(f"  Uncloaked document: {output_path.resolve()}")
    print()

    return 0


def _handle_scan(args: argparse.Namespace) -> int:
    """Handle the 'scan' subcommand."""
    from .docx_handler import load_document
    from .security import scan_document

    input_path = Path(args.input)

    _print_header("Security Scan")
    print(f"  Scanning: {input_path}")
    print()

    doc = load_document(input_path)
    findings = scan_document(doc)

    _print_findings(findings)

    # Summary line
    if findings:
        critical_count = sum(1 for f in findings if f.threat_level == ThreatLevel.CRITICAL)
        warning_count = sum(1 for f in findings if f.threat_level == ThreatLevel.WARNING)
        info_count = sum(1 for f in findings if f.threat_level == ThreatLevel.INFO)
        print()
        parts = []
        if critical_count:
            parts.append(f"{critical_count} critical")
        if warning_count:
            parts.append(f"{warning_count} warning")
        if info_count:
            parts.append(f"{info_count} info")
        print(f"  Total: {len(findings)} finding(s) ({', '.join(parts)})")
    print()

    return 0


def _handle_inspect(args: argparse.Namespace) -> int:
    """Handle the 'inspect' subcommand."""
    from .comments import inspect_comments
    from .metadata import inspect_metadata

    input_path = Path(args.input)

    _print_header("Document Inspection")
    print(f"  Inspecting: {input_path}")

    # Metadata
    _print_header("Metadata")
    report = inspect_metadata(input_path)
    _print_metadata_report(report)

    # Comments
    _print_header("Comments")
    comments, authors = inspect_comments(input_path)

    if not comments:
        print(f"  {_green('No comments found.')}")
    else:
        print(f"  Found {len(comments)} comment(s) from {len(authors)} author(s):")
        print()
        for author in authors:
            print(f"    {author.name} ({author.initials}): {author.comment_count} comment(s)")
        print()
        # Show first few comments as preview
        preview_count = min(5, len(comments))
        for comment in comments[:preview_count]:
            text_preview = comment.text
            if len(text_preview) > 80:
                text_preview = text_preview[:77] + "..."
            print(f"    [{comment.author}] {_dim(text_preview)}")
        if len(comments) > preview_count:
            print(f"    ... and {len(comments) - preview_count} more comment(s)")

    print()
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="clientcloak",
        description="ClientCloak: Bidirectional document sanitization for safe AI contract review.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_get_version()}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- cloak ---
    cloak_parser = subparsers.add_parser(
        "cloak",
        help="Cloak a document by replacing party names and sensitive data",
    )
    cloak_parser.add_argument(
        "input",
        help="Path to the input .docx file",
    )
    cloak_parser.add_argument(
        "--party-a",
        required=True,
        help="First party name (e.g., your client's name)",
    )
    cloak_parser.add_argument(
        "--party-b",
        required=True,
        help="Second party name (e.g., the counterparty's name)",
    )
    cloak_parser.add_argument(
        "--labels",
        default="Customer/Vendor",
        help="Party labels as 'label_a/label_b' (default: Customer/Vendor)",
    )
    cloak_parser.add_argument(
        "--output",
        default=None,
        help="Output path for the cloaked document (default: <input>_cloaked.docx)",
    )
    cloak_parser.add_argument(
        "--mapping",
        default=None,
        help="Path for the mapping JSON file (default: <input>_mapping.json)",
    )
    cloak_parser.add_argument(
        "--comment-mode",
        choices=["keep", "strip", "sanitize"],
        default="sanitize",
        help="How to handle comments: keep, strip, or sanitize (default: sanitize)",
    )
    cloak_parser.add_argument(
        "--no-strip-metadata",
        action="store_true",
        default=False,
        help="Skip metadata removal (metadata is stripped by default)",
    )
    cloak_parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="GLiNER confidence threshold, 0-1 (default: 0.5, for future use)",
    )
    cloak_parser.add_argument(
        "--no-detect",
        action="store_true",
        default=False,
        help="Skip automatic entity detection (emails, phones, SSNs, etc.)",
    )
    cloak_parser.add_argument(
        "--alias-a",
        action="append",
        default=[],
        metavar="NAME=LABEL",
        help="Additional name/label alias for party A (repeatable, e.g. 'Acme=Vendor')",
    )
    cloak_parser.add_argument(
        "--alias-b",
        action="append",
        default=[],
        metavar="NAME=LABEL",
        help="Additional name/label alias for party B (repeatable, e.g. 'BC=Customer Short')",
    )

    # --- uncloak ---
    uncloak_parser = subparsers.add_parser(
        "uncloak",
        help="Uncloak a previously cloaked document using its mapping file",
    )
    uncloak_parser.add_argument(
        "input",
        help="Path to the cloaked .docx file",
    )
    uncloak_parser.add_argument(
        "--mapping",
        required=True,
        help="Path to the mapping JSON file created during cloaking",
    )
    uncloak_parser.add_argument(
        "--output",
        default=None,
        help="Output path for the uncloaked document (default: <input>_uncloaked.docx)",
    )

    # --- scan ---
    scan_parser = subparsers.add_parser(
        "scan",
        help="Run a security scan on a document (hidden text, prompt injection, etc.)",
    )
    scan_parser.add_argument(
        "input",
        help="Path to the .docx file to scan",
    )

    # --- inspect ---
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Inspect document metadata and comments",
    )
    inspect_parser.add_argument(
        "input",
        help="Path to the .docx file to inspect",
    )

    return parser


def _get_version() -> str:
    """Return the package version string."""
    try:
        from . import __version__
        return __version__
    except ImportError:
        return "0.1.0"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """
    Main entry point for the ClientCloak CLI.

    Parses arguments, dispatches to the appropriate subcommand handler,
    and exits with the appropriate code. Referenced in pyproject.toml as
    ``clientcloak.cli:main``.

    Args:
        argv: Optional argument list for testing. Defaults to sys.argv[1:].
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Dispatch table
    handlers = {
        "cloak": _handle_cloak,
        "uncloak": _handle_uncloak,
        "scan": _handle_scan,
        "inspect": _handle_inspect,
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        exit_code = handler(args)
        sys.exit(exit_code)
    except FileNotFoundError as exc:
        print(f"\nError: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as exc:
        # Import here to avoid circular imports at module level; we only need
        # these for error handling when the handler has already been entered.
        from .docx_handler import DocumentLoadError

        if isinstance(exc, DocumentLoadError):
            print(f"\nError: {exc}")
            sys.exit(1)

        # Unexpected error: re-raise so the traceback is visible for debugging.
        raise


if __name__ == "__main__":
    main()
