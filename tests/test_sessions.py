"""
Tests for clientcloak.sessions: session creation, directories, files, TTL cleanup.
"""

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from clientcloak.sessions import (
    SESSION_TTL,
    _TIMESTAMP_FORMAT,
    cleanup_expired_sessions,
    create_session,
    get_session_dir,
    get_session_file,
)


# ===================================================================
# Helpers
# ===================================================================

@pytest.fixture
def mock_sessions_dir(tmp_path):
    """Patch get_sessions_dir to use a tmp_path-based directory."""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    with patch("clientcloak.sessions.get_sessions_dir", return_value=sessions_root):
        yield sessions_root


# ===================================================================
# create_session
# ===================================================================

class TestCreateSession:
    """Tests for create_session()."""

    def test_returns_8char_hex_id(self, mock_sessions_dir):
        sid = create_session()
        assert len(sid) == 8
        assert all(c in "0123456789abcdef" for c in sid)

    def test_creates_directory(self, mock_sessions_dir):
        sid = create_session()
        session_dir = mock_sessions_dir / sid
        assert session_dir.is_dir()

    def test_creates_timestamp_file(self, mock_sessions_dir):
        sid = create_session()
        created_file = mock_sessions_dir / sid / ".created"
        assert created_file.exists()
        ts = created_file.read_text(encoding="utf-8").strip()
        # Should be parseable with the timestamp format
        parsed = datetime.strptime(ts, _TIMESTAMP_FORMAT)
        assert parsed.tzinfo is not None  # timezone-aware

    def test_unique_ids(self, mock_sessions_dir):
        ids = {create_session() for _ in range(20)}
        assert len(ids) == 20  # all unique


# ===================================================================
# get_session_dir
# ===================================================================

class TestGetSessionDir:
    """Tests for get_session_dir()."""

    def test_returns_existing_session_dir(self, mock_sessions_dir):
        sid = create_session()
        d = get_session_dir(sid)
        assert d == mock_sessions_dir / sid

    def test_raises_for_nonexistent_session(self, mock_sessions_dir):
        with pytest.raises(ValueError, match="Session not found"):
            get_session_dir("deadbeef")


# ===================================================================
# get_session_file
# ===================================================================

class TestGetSessionFile:
    """Tests for get_session_file()."""

    def test_returns_file_path(self, mock_sessions_dir):
        sid = create_session()
        fp = get_session_file(sid, "mapping.json")
        assert fp == mock_sessions_dir / sid / "mapping.json"

    def test_file_need_not_exist(self, mock_sessions_dir):
        sid = create_session()
        fp = get_session_file(sid, "nonexistent.txt")
        assert not fp.exists()  # just returns the path, doesn't require existence

    def test_raises_for_invalid_session(self, mock_sessions_dir):
        with pytest.raises(ValueError, match="Session not found"):
            get_session_file("bad_id_0", "file.txt")


# ===================================================================
# cleanup_expired_sessions
# ===================================================================

class TestCleanupExpiredSessions:
    """Tests for cleanup_expired_sessions()."""

    def test_removes_expired_session(self, mock_sessions_dir):
        sid = create_session()
        # Backdate the .created file to 25 hours ago
        created_file = mock_sessions_dir / sid / ".created"
        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        created_file.write_text(old_time.strftime(_TIMESTAMP_FORMAT), encoding="utf-8")

        removed = cleanup_expired_sessions()
        assert removed == 1
        assert not (mock_sessions_dir / sid).exists()

    def test_keeps_fresh_session(self, mock_sessions_dir):
        sid = create_session()
        removed = cleanup_expired_sessions()
        assert removed == 0
        assert (mock_sessions_dir / sid).is_dir()

    def test_removes_session_without_timestamp(self, mock_sessions_dir):
        # A directory without .created should be treated as expired
        orphan = mock_sessions_dir / "orphan00"
        orphan.mkdir()
        removed = cleanup_expired_sessions()
        assert removed == 1
        assert not orphan.exists()

    def test_mixed_sessions(self, mock_sessions_dir):
        """Mix of fresh, expired, and orphan sessions."""
        fresh = create_session()
        expired = create_session()
        orphan_dir = mock_sessions_dir / "orphanxx"
        orphan_dir.mkdir()

        # Backdate the expired session
        cf = mock_sessions_dir / expired / ".created"
        old_time = datetime.now(timezone.utc) - timedelta(hours=48)
        cf.write_text(old_time.strftime(_TIMESTAMP_FORMAT), encoding="utf-8")

        removed = cleanup_expired_sessions()
        assert removed == 2  # expired + orphan
        assert (mock_sessions_dir / fresh).is_dir()
        assert not (mock_sessions_dir / expired).exists()
        assert not orphan_dir.exists()

    def test_no_sessions_returns_zero(self, mock_sessions_dir):
        removed = cleanup_expired_sessions()
        assert removed == 0
