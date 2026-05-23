"""
desktop/launcher.py — CLI entry point called by /usr/local/bin/lytrize
=======================================================================

Thin wrapper that resolves the correct Python interpreter and delegates
immediately to gui.py.

Why a separate file?
    /usr/local/bin/lytrize (the installed shell stub) invokes
    ``python3 /opt/lytrize/desktop/launcher.py``.  Keeping the
    subprocess call here means the shell stub stays a one-liner and
    gui.py stays importable / testable on its own.

Process lifecycle:
    launcher.py spawns gui.py as a *child* process and waits for it.
    All subprocess management of the Streamlit backend is handled inside
    gui.py itself; this file has no visibility into it.

Interpreter resolution:
    1. Use the venv Python at /opt/lytrize/venv/bin/python if it exists.
    2. Fall back to the system python3 otherwise (development / CI use).
"""

import subprocess
import sys
from pathlib import Path

# Absolute paths rooted at the package install location.
# Adjust INSTALL_BASE if the package is installed to a non-standard prefix.
INSTALL_BASE = Path("/opt/lytrize")
VENV_PY      = INSTALL_BASE / "venv" / "bin" / "python"
GUI          = INSTALL_BASE / "desktop" / "gui.py"


def _resolve_python() -> str:
    """
    Return the path to the Python interpreter to use.

    Prefers the venv interpreter so that PySide6 and all other dependencies
    are guaranteed to be available.  Falls back to ``python3`` on the PATH
    for development environments where no venv has been created.
    """
    if VENV_PY.exists():
        return str(VENV_PY)
    return "python3"


if __name__ == "__main__":
    python = _resolve_python()
    result = subprocess.run([python, str(GUI)] + sys.argv[1:])
    sys.exit(result.returncode)
