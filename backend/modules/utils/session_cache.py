"""
modules/utils/session_cache.py — Per-user DataFrame parquet snapshot helpers.

WHY THIS MODULE EXISTS
----------------------
The DataFrame snapshot (a parquet file written to XDG_RUNTIME_DIR or
XDG_CACHE_HOME) lets a loaded dataset survive a browser tab change or
WebSocket reconnect.  It is written after every autosave and read back
during token validation in app.py.

Previously this logic lived in app.py, which caused a circular import:

    app.py → modules/pages/analysis.py → app._save_df_snapshot (lazy import)

Moving the helpers here gives both app.py and pages/analysis.py a clean,
stable import path with no cycles.

PUBLIC API
----------
    df_cache_path(user_id)  → Path
    save_df_snapshot(user_id) → None
    load_df_snapshot(user_id) → pd.DataFrame | None
"""

import os
from pathlib import Path

import streamlit as st


def df_cache_path(user_id: int) -> Path:
    """
    Return the path for the per-user DataFrame parquet snapshot.

    Location priority:
      1. $XDG_RUNTIME_DIR/lytrize/   (RAM-backed tmpfs on most distros)
      2. $XDG_CACHE_HOME/lytrize/    (falls back to ~/.cache/lytrize/)

    The directory is created with mode 0700 so only the owning user
    can list or read its contents.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime) / "lytrize"
    else:
        cache_home = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        base = Path(cache_home) / "lytrize"

    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except Exception:
        pass

    return base / f"df_{user_id}.parquet"


def save_df_snapshot(user_id: int) -> None:
    """
    Persist the live DataFrame from st.session_state to a parquet snapshot.

    Called from _persist_draft() on every autosave so the dataset survives
    a browser change (new WebSocket session → empty server-side session_state).

    Silently no-ops when:
      - No DataFrame is loaded (st.session_state["df"] is absent or None).
      - Parquet serialisation fails (e.g. unsupported dtype).
    """
    df = st.session_state.get("df")
    if df is None:
        return

    path = df_cache_path(user_id)
    try:
        df.to_parquet(str(path), index=False)
        try:
            path.chmod(0o600)
        except Exception:
            pass
    except Exception:
        pass


def load_df_snapshot(user_id: int):
    """
    Restore the DataFrame from the parquet snapshot written by save_df_snapshot.

    Returns the DataFrame on success, or None if no snapshot exists or if
    deserialisation fails (e.g. file corrupt / dtype mismatch after a schema
    change).
    """
    import pandas as pd

    path = df_cache_path(user_id)
    if not path.exists():
        return None
    try:
        return pd.read_parquet(str(path))
    except Exception:
        return None
