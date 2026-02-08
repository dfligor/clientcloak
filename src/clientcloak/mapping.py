"""
Mapping file operations for ClientCloak.

Handles creation, persistence, and lookup of the mapping file that records
all substitutions made during cloaking. The mapping file is the key to
round-trip fidelity: it allows uncloaking to perfectly restore the original
text.

Mappings are stored as placeholder -> original (the uncloaking direction).
At cloak time, the mappings are inverted to original -> placeholder for
efficient lookup during text replacement.
"""

from pathlib import Path

from .models import MappingFile


def create_mapping(
    original_file: str,
    mappings: dict[str, str],
    party_labels: dict[str, str] | None = None,
    comment_authors: dict[str, str] | None = None,
) -> MappingFile:
    """
    Create a new mapping file from cloaking results.

    Args:
        original_file: Name of the original document that was cloaked.
        mappings: Dictionary of placeholder -> original value. For example:
            {"[PARTY_A]": "Acme Corp", "[EMAIL_1]": "jane@acme.com"}.
        party_labels: Optional dictionary of party role labels. For example:
            {"party_a": "Customer", "party_b": "Vendor"}.
        comment_authors: Optional dictionary of anonymous label -> original
            author name. For example: {"Reviewer A": "Jane Smith"}.

    Returns:
        A fully populated MappingFile instance ready for serialization.
    """
    return MappingFile(
        original_file=original_file,
        mappings=mappings,
        party_labels=party_labels or {},
        comment_authors=comment_authors or {},
    )


def save_mapping(mapping: MappingFile, file_path: str | Path) -> None:
    """
    Save a mapping file to disk as formatted JSON.

    Args:
        mapping: The MappingFile instance to persist.
        file_path: Destination path for the JSON file. Parent directories
            must already exist.

    Raises:
        OSError: If the file cannot be written (permissions, disk full, etc.).
    """
    path = Path(file_path)
    path.write_text(mapping.model_dump_json(indent=2), encoding="utf-8")


def load_mapping(file_path: str | Path) -> MappingFile:
    """
    Load a mapping file from a JSON file on disk.

    Args:
        file_path: Path to the JSON mapping file.

    Returns:
        A validated MappingFile instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        pydantic.ValidationError: If the JSON does not match the MappingFile
            schema.
    """
    path = Path(file_path)
    raw = path.read_text(encoding="utf-8")
    return MappingFile.model_validate_json(raw)


def get_cloak_replacements(mapping: MappingFile) -> dict[str, str]:
    """
    Invert a mapping for use during cloaking.

    The mapping file stores placeholder -> original (the uncloaking direction).
    During cloaking, we need original -> placeholder so that we can find
    original text and replace it with its placeholder.

    Args:
        mapping: The MappingFile whose mappings should be inverted.

    Returns:
        A dictionary of original value -> placeholder. For example, if the
        mapping contains {"[PARTY_A]": "Acme Corp"}, this returns
        {"Acme Corp": "[PARTY_A]"}.
    """
    return {original: placeholder for placeholder, original in mapping.mappings.items()}
