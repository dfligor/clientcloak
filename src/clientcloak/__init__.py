"""
ClientCloak: Bidirectional document sanitization for safe AI contract review.

All processing runs locally. No data leaves your machine.
"""

__version__ = "0.1.0"

from .models import (
    CloakConfig,
    CloakResult,
    CommentMode,
    DetectedEntity,
    MappingFile,
    MetadataReport,
    SecurityFinding,
    ThreatLevel,
)

__all__ = [
    "CloakConfig",
    "CloakResult",
    "CommentMode",
    "DetectedEntity",
    "MappingFile",
    "MetadataReport",
    "SecurityFinding",
    "ThreatLevel",
]
