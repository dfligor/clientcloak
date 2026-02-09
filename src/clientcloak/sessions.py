"""
Session persistence with automatic TTL cleanup for ClientCloak.

Adapted from PlaybookRedliner's app/sessions.py pattern. Each cloaking
operation gets its own session directory that holds uploaded files, mapping
files, and cloaked output. Sessions auto-expire after 24 hours to prevent
unbounded disk growth.

Session IDs are 8-character UUID prefixes -- short enough for URLs and
log messages, long enough to avoid collisions in practice.
"""

import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import get_sessions_dir

SESSION_TTL = timedelta(hours=24)
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# Session IDs must be exactly 8 lowercase hex characters.
_SESSION_ID_RE = re.compile(r"^[a-f0-9]{8}$")

# Filenames inside sessions must be simple names (no path separators or traversal).
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def create_session() -> str:
    """
    Create a new session directory and return its ID.

    The session directory is created under the platform-appropriate sessions
    root (see ``paths.get_sessions_dir``). A ``.created`` file is written
    inside the directory containing an ISO-8601 UTC timestamp that is later
    used by ``cleanup_expired_sessions`` to determine age.

    Returns:
        An 8-character hexadecimal session ID.
    """
    session_id = uuid.uuid4().hex[:8]
    session_dir = get_sessions_dir() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    created_file = session_dir / ".created"
    timestamp = datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)
    created_file.write_text(timestamp, encoding="utf-8")

    return session_id


def get_session_dir(session_id: str) -> Path:
    """
    Return the directory path for an existing session.

    Args:
        session_id: The 8-character session identifier returned by
            ``create_session``.

    Returns:
        The ``Path`` to the session directory.

    Raises:
        ValueError: If the session ID is malformed or no session directory
            exists for the given ID.
    """
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Session not found: {session_id}")
    session_dir = get_sessions_dir() / session_id
    # .resolve() follows symlinks, so a crafted session_id like
    # "../../etc" would resolve outside the sessions root.  The
    # containment check ensures the resolved path stays inside.
    sessions_root = get_sessions_dir().resolve()
    resolved = session_dir.resolve()
    if not str(resolved).startswith(str(sessions_root) + "/") and resolved != sessions_root:
        raise ValueError(f"Session not found: {session_id}")
    if not session_dir.is_dir():
        raise ValueError(f"Session not found: {session_id}")
    return session_dir


def get_session_file(session_id: str, filename: str) -> Path:
    """
    Get the path to a specific file within a session directory.

    This does **not** check whether the file itself exists -- only that the
    session directory is valid. Callers can use the returned path for both
    reading existing files and writing new ones.

    Args:
        session_id: The 8-character session identifier.
        filename: The name of the file within the session directory.

    Returns:
        The ``Path`` to the requested file inside the session directory.

    Raises:
        ValueError: If the session directory does not exist (delegated to
            ``get_session_dir``).
    """
    if not _SAFE_FILENAME_RE.match(filename):
        raise ValueError(f"Invalid session filename: {filename}")
    return get_session_dir(session_id) / filename


def cleanup_expired_sessions() -> int:
    """
    Remove session directories that are older than the TTL (24 hours).

    Iterates over all directories in the sessions root and reads each
    ``.created`` timestamp file. Sessions whose age exceeds ``SESSION_TTL``
    are removed entirely. Sessions without a readable ``.created`` file are
    treated as expired and removed as well, since their age cannot be
    verified.

    Returns:
        The number of session directories that were removed.
    """
    sessions_root = get_sessions_dir()
    now = datetime.now(timezone.utc)
    removed = 0

    for entry in sessions_root.iterdir():
        if not entry.is_dir():
            continue

        created_file = entry / ".created"
        # Fail-secure: if the timestamp is missing or unparseable, treat
        # the session as expired rather than retaining it indefinitely.
        expired = True

        if created_file.is_file():
            try:
                raw = created_file.read_text(encoding="utf-8").strip()
                created_at = datetime.strptime(raw, _TIMESTAMP_FORMAT)
                if (now - created_at) < SESSION_TTL:
                    expired = False
            except (ValueError, OSError):
                # Unparseable or unreadable -- treat as expired
                pass

        if expired:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1

    return removed
