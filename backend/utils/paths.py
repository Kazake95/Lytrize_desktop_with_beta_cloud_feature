"""
backend/utils/paths.py — Canonical filesystem paths for Lytrize.

Import these helpers instead of hard-coding paths in individual modules:

    from utils.paths import get_user_dir, get_db_path

All three functions return pathlib.Path objects.
"""

from pathlib import Path

APP_NAME = "lytrize"


def get_install_dir() -> Path:
    """Return the backend installation directory."""
    return Path("/opt/lytrize/backend")


def get_user_dir() -> Path:
    """Return (and create if absent) the per-user data directory."""
    path = Path.home() / ".local" / "share" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path() -> Path:
    """Return the path to the local SQLite database."""
    return get_user_dir() / "lytrize.db"
