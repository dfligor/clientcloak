"""
Cross-platform path utilities for ClientCloak.

Adapted from PlaybookRedliner's app/paths.py. Handles the difference between:
- Development mode: resources live in the source tree
- Bundled mode (PyInstaller): resources are frozen in sys._MEIPASS

Also provides platform-appropriate user data directories for:
- Session files (temp, cleaned up automatically)
- User preferences (persistent)
"""

import os
import sys
from pathlib import Path


def get_bundle_path() -> Path:
    """
    Read-only path to bundled resources.

    Frozen (PyInstaller): sys._MEIPASS
    Development: project root (two levels up from this file)
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    # src/clientcloak/paths.py -> project root
    return Path(__file__).parent.parent.parent


def get_user_data_dir() -> Path:
    """
    Writable user data directory (platform-appropriate).

    macOS:   ~/Library/Application Support/ClientCloak/
    Windows: %APPDATA%/ClientCloak/
    Linux:   ~/.local/share/ClientCloak/
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    else:
        base = Path.home() / ".local" / "share"

    data_dir = base / "ClientCloak"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_sessions_dir() -> Path:
    """Directory for session files (temp storage during cloaking operations)."""
    sessions = get_user_data_dir() / "sessions"
    sessions.mkdir(exist_ok=True)
    return sessions
