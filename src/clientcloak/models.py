"""
Pydantic models for all ClientCloak data structures.

All data structures are defined here for single-source-of-truth.
Pydantic provides built-in JSON serialization, validation, and
consistency with the PlaybookRedliner patterns.
"""

from pydantic import BaseModel, Field
from datetime import datetime, timezone
from enum import Enum


# --- Enums ---

class CommentMode(str, Enum):
    KEEP = "keep"
    STRIP = "strip"
    SANITIZE = "sanitize"


class ThreatLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# --- Entity Detection ---

class DetectedEntity(BaseModel):
    """An entity detected by GLiNER or regex."""
    text: str
    entity_type: str  # "person name", "organization", "EMAIL", "PHONE", etc.
    confidence: float = Field(ge=0, le=1)
    count: int = Field(ge=0, default=1)
    suggested_placeholder: str


# --- Security ---

class SecurityFinding(BaseModel):
    """A security issue found during document scanning."""
    threat_level: ThreatLevel
    finding_type: str  # "hidden_text", "prompt_injection", "invisible_chars", "metadata"
    description: str
    location: str
    content_preview: str = Field(default="", max_length=200)
    recommendation: str


# --- Metadata ---

class MetadataReport(BaseModel):
    """Report of metadata found in (and optionally removed from) a document."""
    author: str | None = None
    last_modified_by: str | None = None
    company: str | None = None
    manager: str | None = None
    created: str | None = None
    modified: str | None = None
    last_printed: str | None = None
    revision: str | None = None
    application: str | None = None
    app_version: str | None = None
    template: str | None = None
    comments_count: int = 0
    custom_properties: dict[str, str] = {}


# --- Comments ---

class CommentInfo(BaseModel):
    """A single comment extracted from a document."""
    id: str
    author: str
    author_initials: str
    date: str
    text: str
    paragraph_index: int = 0


class CommentAuthor(BaseModel):
    """A unique comment author found in a document."""
    name: str
    initials: str
    comment_count: int = 0
    suggested_label: str  # e.g., "Reviewer A"


class PartyAlias(BaseModel):
    """An additional name/label pair for a party (e.g., short name, abbreviation)."""
    name: str
    label: str


# --- Mapping ---

class MappingFile(BaseModel):
    """
    The mapping file that records all substitutions for round-trip fidelity.

    Stored as JSON. Mappings are placeholder -> original value:
    - Used directly for uncloaking (placeholder -> original)
    - Inverted at cloak time for lookup (original -> placeholder)
    """
    version: str = "1.0"
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    original_file: str = ""
    party_labels: dict[str, str] = {}  # e.g., {"party_a": "Customer", "party_b": "Vendor"}
    mappings: dict[str, str] = {}  # placeholder -> original value
    comment_authors: dict[str, str] = {}  # anonymous label -> original author


# --- Cloaking ---

class CloakConfig(BaseModel):
    """Configuration for a cloaking operation."""
    party_a_name: str
    party_a_label: str = "Customer"
    party_b_name: str
    party_b_label: str = "Vendor"
    party_a_aliases: list[PartyAlias] = []
    party_b_aliases: list[PartyAlias] = []
    additional_replacements: dict[str, str] = {}  # user-confirmed entity replacements
    comment_mode: CommentMode = CommentMode.SANITIZE
    strip_metadata: bool = True
    gliner_threshold: float = Field(default=0.5, ge=0, le=1)


class CloakResult(BaseModel):
    """Result returned after cloaking a document."""
    mapping: MappingFile
    security_findings: list[SecurityFinding] = []
    metadata_report: MetadataReport | None = None
    entities_detected: int = 0
    replacements_applied: int = 0
    output_path: str | None = None  # Actual output path (may differ from requested if filename was sanitized)
