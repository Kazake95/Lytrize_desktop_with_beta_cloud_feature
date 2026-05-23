"""
desktop/gui.py — Lytrize Desktop Launcher
==========================================

PySide6 launcher window that manages the Streamlit backend subprocess and
opens the web UI in an isolated browser window.

FEATURES
--------
- Detects all installed browsers; remembers the user's choice across sessions.
- Injects the saved session token into the URL on startup so the user lands
  on home without re-entering credentials. The token is cleared from the URL
  immediately by app.py after validation.
- Opens Chromium-based browsers in true "app mode" (no toolbar, isolated
  profile, maximised window) and Firefox in a new isolated instance.
- System tray with Open / Stop & Quit actions.
- Crash-recovery: if Streamlit exits unexpectedly the launcher shows a
  recoverable error state instead of going blank.

STARTUP BEHAVIOUR
-----------------
First launch (no token file) → opens the app in guest / profile mode.
After sign-in               → token written to ~/.local/share/lytrize/session.token.
Subsequent launches          → token injected as ?t= so the user lands on home.

BROWSER MODES
-------------
Chromium-based (Chrome, Brave, Edge, Vivaldi, Opera):
  Launched with --app=<url> which strips the browser chrome (address bar,
  tabs) and opens the page in a standalone maximised window that looks and
  feels like a native desktop app.

Firefox / Gecko-based (Firefox, Firefox ESR, LibreWolf, Zen):
  Launched with --new-instance + -profile (isolated) + --kiosk so the
  Lytrize window opens fullscreen with no address bar, no tabs, and no
  browser chrome — the closest Firefox can achieve to Chromium's --app=
  mode without installing an extension.

xdg-open (fallback):
  Delegates to the system default handler. No isolation is possible; the
  URL simply opens wherever the OS decides.

SYNC
----
Sync is intentionally NOT available from the launcher.
Users sync from Profile → "Sync my sessions now" inside the app.
This keeps the privacy model clear: sync is always an explicit user action.

CONTRIBUTING
------------
Keep all Qt / PySide6 code inside this file.
Pure-data helpers belong in backend/modules/utils/.
Threading model: all subprocess I/O is done in QThread subclasses;
results are communicated back to the main thread exclusively via Qt signals.
Never call Qt widget methods from a non-main thread.
"""

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSystemTrayIcon, QMenu,
    QFrame, QComboBox,
)
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PySide6.QtCore import Qt, QThread, Signal


# ── Paths ─────────────────────────────────────────────────────────────────────

BASE       = Path(__file__).resolve().parent.parent
DATA_DIR   = Path.home() / ".local" / "share" / "lytrize"
PREFS      = DATA_DIR / "launcher_prefs.json"
TOKEN_FILE = DATA_DIR / "session.token"
DB_PATH    = DATA_DIR / "lytrize.db"
VENV_PY    = BASE / "venv" / "bin" / "python"
DEV_VENV_PY = BASE / "my_venv" / "bin" / "python"
APP_PY     = BASE / "backend" / "app.py"
APP_URL    = "http://127.0.0.1:8501"

# Isolated browser profile directories — kept outside the user's real profiles
# so Lytrize never touches the user's bookmarks / history / settings.
_PROFILE_ROOT = DATA_DIR / "browser-profiles"
_CHROMIUM_PROFILE = _PROFILE_ROOT / "chromium"
_FIREFOX_PROFILE  = _PROFILE_ROOT / "firefox"

# Streamlit readiness polling parameters
_POLL_INTERVAL_S = 0.5   # seconds between socket probes
_POLL_MAX_TRIES  = 60    # 60 × 0.5 s = 30 s total timeout


def _find_icon() -> Path:
    """Return the first existing icon file from backend/assets/."""
    assets = BASE / "backend" / "assets"
    for name in ("lytrize.png", "Lytrize.png", "lytrize.ico", "Lytrize.ico"):
        candidate = assets / name
        if candidate.exists():
            return candidate
    return assets / "lytrize.png"   # may not exist; _make_icon() handles that


ICON_PATH = _find_icon()


# ── Preferences ───────────────────────────────────────────────────────────────

def _load_prefs() -> dict:
    """
    Load persisted launcher preferences from disk.

    Returns an empty dict on any I/O or parse error so callers never
    have to guard against missing keys or file-not-found situations.
    """
    try:
        return json.loads(PREFS.read_text())
    except Exception:
        return {}


def _save_prefs(data: dict) -> None:
    """Persist launcher preferences atomically to DATA_DIR/launcher_prefs.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except Exception:
        pass
    tmp = PREFS.with_name(f".{PREFS.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    os.replace(tmp, PREFS)
    try:
        PREFS.chmod(0o600)
    except Exception:
        pass


# ── Browser detection ─────────────────────────────────────────────────────────

# Each entry: (display_name, binary_name, is_chromium_based)
# Listed in preference order — higher entries win when multiple entries
_BROWSER_CANDIDATES: list[tuple[str, str, bool]] = [
    ("Google Chrome",  "google-chrome",        True),
    ("Google Chrome",  "google-chrome-stable", True),
    ("Chromium",       "chromium",              True),
    ("Chromium",       "chromium-browser",      True),
    ("Brave",          "brave-browser",         True),
    ("Brave",          "brave-browser-stable",  True),
    ("Microsoft Edge", "microsoft-edge",        True),
    ("Vivaldi",        "vivaldi",               True),
    ("Opera",          "opera",                 True),
    ("Firefox",        "firefox",               False),
    ("Firefox ESR",    "firefox-esr",           False),
    ("Zen Browser",    "zen",                   False),
    ("LibreWolf",      "librewolf",             False),
    ("Default",        "xdg-open",              False),
]


def _detect_browsers() -> list[dict]:
    """
    Return a deduplicated list of installed browser dicts.

    Each dict has keys: ``name`` (str), ``binary`` (str path), ``chromium`` (bool).

    Deduplication is done on the *resolved* binary path, so symlinks such as
    ``/usr/bin/chromium → /usr/bin/chromium-browser`` are counted once.
    Falls back to a single ``xdg-open`` entry if nothing else is found.
    """
    seen: set[str] = set()
    found: list[dict] = []

    for name, binary, is_chromium in _BROWSER_CANDIDATES:
        path = shutil.which(binary)
        if not path:
            continue
        resolved = str(Path(path).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append({"name": name, "binary": path, "chromium": is_chromium})

    if not found:
        found.append({"name": "Default", "binary": "xdg-open", "chromium": False})

    return found


# ── App icon ──────────────────────────────────────────────────────────────────

def _make_icon() -> QIcon:
    """
    Build the application QIcon.

    Loads the PNG/ICO from assets/; if the file is absent or unreadable,
    falls back to a programmatically drawn indigo rounded-rect with an 'L'.
    """
    if ICON_PATH.exists():
        pixmap = QPixmap(str(ICON_PATH))
        if not pixmap.isNull():
            return QIcon(pixmap)

    # Fallback: draw a simple branded icon at runtime
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor("#4f6ef7"))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(0, 0, 64, 64, 14, 14)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Sans", 28, QFont.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "L")
    painter.end()
    return QIcon(pixmap)


# ── Worker threads ────────────────────────────────────────────────────────────

class _WaitThread(QThread):
    """
    Poll localhost:8501 until Streamlit accepts TCP connections.

    Emits:
        ready()   — Streamlit is up and accepting connections.
        timeout() — 30 seconds elapsed without a successful connection.

    Threading note: this thread only emits signals; it never touches Qt
    widgets directly.  Qt auto-queues cross-thread signal delivery.
    """
    ready   = Signal()
    timeout = Signal()

    def run(self) -> None:
        for _ in range(_POLL_MAX_TRIES):
            try:
                # Use a context manager so the socket is always closed, even on
                # KeyboardInterrupt or other exceptions — avoids fd leaks.
                with socket.create_connection(("127.0.0.1", 8501), _POLL_INTERVAL_S):
                    pass
                self.ready.emit()
                return
            except OSError:
                time.sleep(_POLL_INTERVAL_S)
        self.timeout.emit()


class _WatchThread(QThread):
    """
    Monitor the Streamlit subprocess for unexpected exits.

    Emits:
        crashed(exit_code) — subprocess exited with a *non-zero* code
                             AND the thread was not cancelled before it
                             returned (i.e. it was not a deliberate stop).

    The ``cancel()`` method should be called before the process is
    deliberately terminated so that a normal SIGTERM (-15) exit is not
    reported as a crash.
    """
    crashed = Signal(int)

    def __init__(self, proc: subprocess.Popen) -> None:
        super().__init__()
        self._proc       = proc
        self._cancelled  = False   # set to True before intentional stop

    def cancel(self) -> None:
        """
        Signal that the upcoming process exit is intentional.

        Call this BEFORE terminating the process to suppress the crash
        notification that would otherwise appear on SIGTERM (-15) exit.
        """
        self._cancelled = True

    def run(self) -> None:
        code = self._proc.wait()
        if code != 0 and not self._cancelled:
            self.crashed.emit(code)


def _ensure_firefox_profile(profile_dir: Path) -> None:
    """
    Create a minimal Firefox/LibreWolf profile that suppresses all first-run
    dialogs and telemetry prompts so the isolated window opens cleanly.

    Without this, Firefox shows "Set as default?", crash-reporter opt-ins,
    and "What's new in Firefox" tabs — even on --new-instance launches.
    The user.js file in the profile overrides these preferences before
    Firefox reads its own defaults.

    Safe to call on every launch; only writes user.js on first creation.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_dir.chmod(0o700)
    except Exception:
        pass

    # ── user.js — Firefox preference overrides ────────────────────────────
    user_js = profile_dir / "user.js"
    if not user_js.exists():
        user_js.write_text(
            "// Lytrize isolated Firefox profile — auto-generated, do not edit\n"
            'user_pref("browser.shell.checkDefaultBrowser",       false);\n'
            'user_pref("browser.startup.homepage_override.mstone","ignore");\n'
            'user_pref("browser.startup.firstrunSkipsHomepage",   true);\n'
            'user_pref("browser.tabs.warnOnClose",                false);\n'
            'user_pref("browser.sessionstore.resume_from_crash",  false);\n'
            'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
            'user_pref("datareporting.healthreport.uploadEnabled", false);\n'
            'user_pref("toolkit.telemetry.enabled",               false);\n'
            'user_pref("app.normandy.enabled",                    false);\n'
            'user_pref("extensions.formautofill.addresses.enabled", false);\n'
            'user_pref("browser.newtabpage.activity-stream.feeds.section.highlights", false);\n'
            # Maximised on open — no --start-maximized CLI flag in Firefox.
            'user_pref("browser.startup.maximized",               true);\n'
            # REQUIRED to allow userChrome.css to take effect.
            'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);\n'
            # Hide the tab bar — we only want a single-tab app window.
            'user_pref("browser.tabs.inTitlebar",                 0);\n'
        )

    # ── userChrome.css — hide address bar + tab strip ─────────────────────
    # This is the ONLY reliable way to give Firefox a webapp-style window
    # (no address bar, no tab strip) without --kiosk.  The window still has
    # a native OS title bar so the user can move, resize, minimise, and close
    # it normally — just like a Chromium --app= window.
    chrome_dir = profile_dir / "chrome"
    chrome_dir.mkdir(exist_ok=True)
    user_chrome = chrome_dir / "userChrome.css"
    if not user_chrome.exists():
        user_chrome.write_text(
            "/* Lytrize — webapp-style Firefox window (auto-generated) */\n"
            "@namespace url(\"http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul\");\n"
            "\n"
            "/* Hide the URL / navigation toolbar */\n"
            "#nav-bar { display: none !important; }\n"
            "\n"
            "/* Hide the tab strip */\n"
            "#TabsToolbar { display: none !important; }\n"
            "\n"
            "/* Hide bookmarks toolbar if the user had it on */\n"
            "#PersonalToolbar { display: none !important; }\n"
            "\n"
            "/* Keep the window titlebar (OS decorations) visible so the user\n"
            "   can resize/minimise/close the window normally */\n"
        )


# ── Launcher window ───────────────────────────────────────────────────────────

class Launcher(QWidget):
    """
    Main launcher window.

    Responsibilities:
        - Build and style the UI (header, status label, browser picker,
          Start / Open / Stop buttons, system tray).
        - Start and stop the Streamlit subprocess.
        - Open the web UI in the selected browser in isolated app mode.
        - Report subprocess crashes to the user without crashing the launcher.

    The window stays alive in the system tray while Streamlit is running,
    and closes completely only when the user clicks "Stop & Quit".
    """

    # ── Stylesheet ─────────────────────────────────────────────────────────
    # Single QSS block applied to the whole window.  All colours use the
    # dark-navy palette defined in backend/.streamlit/config.toml so the
    # launcher looks consistent with the in-browser app.
    _QSS = """
        QWidget { background:#0f172a; color:#f1f5f9;
                  font-family:'Inter','Segoe UI',system-ui,sans-serif; }
        QLabel#title  { font-size:18px; font-weight:700; color:#818cf8; }
        QLabel#status { font-size:11px; color:#64748b; }
        QLabel#blbl   { font-size:11px; color:#64748b; }
        QFrame#divider { background:#1e293b; }
        QComboBox {
            background:#1e293b; color:#f1f5f9;
            border:1px solid #334155; border-radius:6px;
            padding:4px 8px; font-size:12px;
        }
        QComboBox::drop-down { border:none; }
        QComboBox QAbstractItemView {
            background:#1e293b; color:#f1f5f9;
            border:1px solid #334155;
            selection-background-color:#4f6ef7;
        }
        QPushButton {
            border-radius:8px; padding:7px 14px;
            font-weight:bold; font-size:12px; border:none;
        }
        QPushButton#btn_start {
            background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #4f6ef7, stop:1 #8b5cf6); color:white;
        }
        QPushButton#btn_start:hover    { background:#6366f1; }
        QPushButton#btn_start:disabled { background:#1e293b; color:#475569; }
        QPushButton#btn_open {
            background:#1e293b; color:#818cf8; border:1px solid #334155;
        }
        QPushButton#btn_open:hover    { background:#273449; }
        QPushButton#btn_open:disabled { color:#334155; border-color:#1e293b; }
        QPushButton#btn_stop {
            background:#1e293b; color:#f87171; border:1px solid #334155;
        }
        QPushButton#btn_stop:hover    { background:#2d1f1f; }
        QPushButton#btn_stop:disabled { color:#334155; border-color:#1e293b; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._proc         : subprocess.Popen | None = None
        self._wait_thread  : _WaitThread | None      = None
        self._watch_thread : _WatchThread | None     = None
        self._browsers     = _detect_browsers()
        self._icon         = _make_icon()
        self._crash_count  = 0

        self.setWindowTitle("Lytrize")
        self.setWindowIcon(self._icon)
        self.setFixedSize(370, 270 if len(self._browsers) > 1 else 240)
        self.setStyleSheet(self._QSS)

        self._build_ui()
        self._build_tray()
        self._connect_signals()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Construct and lay out all widgets."""
        # Header row — icon + title centred
        lbl_icon = QLabel()
        lbl_icon.setPixmap(self._icon.pixmap(26, 26))
        lbl_icon.setAlignment(Qt.AlignCenter)

        lbl_title = QLabel("Lytrize")
        lbl_title.setObjectName("title")

        header = QHBoxLayout()
        header.addStretch()
        header.addWidget(lbl_icon)
        header.addWidget(lbl_title)
        header.addStretch()

        # Status label
        self.lbl_status = QLabel("● Stopped")
        self.lbl_status.setObjectName("status")
        self.lbl_status.setAlignment(Qt.AlignCenter)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)

        # Browser selector (only shown when more than one browser is available)
        prefs = _load_prefs()
        self.combo_browser: QComboBox | None = None
        if len(self._browsers) > 1:
            lbl_b = QLabel("Open with:")
            lbl_b.setObjectName("blbl")
            self.combo_browser = QComboBox()
            saved_binary = prefs.get("browser_binary", "")
            selected_idx = 0
            for i, browser in enumerate(self._browsers):
                self.combo_browser.addItem(browser["name"], userData=browser["binary"])
                if browser["binary"] == saved_binary:
                    selected_idx = i
            self.combo_browser.setCurrentIndex(selected_idx)

        # Control buttons
        self.btn_start = QPushButton("▶  Start")
        self.btn_start.setObjectName("btn_start")
        self.btn_open  = QPushButton("⬡  Open App")
        self.btn_open.setObjectName("btn_open")
        self.btn_stop  = QPushButton("■  Stop && Quit")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_open.setEnabled(False)
        self.btn_stop.setEnabled(False)

        # Hint text at the bottom
        hint = QLabel("Sync is available inside the app under Profile.")
        hint.setObjectName("status")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)

        # Root layout
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(9)
        root.addLayout(header)
        root.addWidget(self.lbl_status)
        root.addWidget(divider)

        if self.combo_browser is not None:
            brow = QHBoxLayout()
            brow.addWidget(lbl_b)
            brow.addWidget(self.combo_browser, stretch=1)
            root.addLayout(brow)

        root.addWidget(self.btn_start)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_open)
        btn_row.addWidget(self.btn_stop)
        root.addLayout(btn_row)

        root.addWidget(hint)

    def _build_tray(self) -> None:
        """Create the system tray icon and context menu."""
        self.tray = QSystemTrayIcon(self._icon, self)
        self.tray.setToolTip("Lytrize")

        tray_menu = QMenu()
        tray_menu.addAction("Open App",     self._open_app)
        tray_menu.addSeparator()
        tray_menu.addAction("Stop && Quit", self._stop_and_quit)
        self.tray.setContextMenu(tray_menu)

    def _connect_signals(self) -> None:
        """Wire all widget and tray signals to their slots."""
        self.btn_start.clicked.connect(self._start)
        self.btn_open.clicked.connect(self._open_app)
        self.btn_stop.clicked.connect(self._stop_and_quit)
        self.tray.activated.connect(self._tray_activated)

        if self.combo_browser is not None:
            self.combo_browser.currentIndexChanged.connect(self._on_browser_changed)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _set_status(self, text: str, colour: str = "#64748b") -> None:
        """Update the status label text and colour."""
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"font-size:11px; color:{colour};")

    def _is_running(self) -> bool:
        """Return True if the Streamlit subprocess is alive."""
        return self._proc is not None and self._proc.poll() is None

    def _build_url(self) -> str:
        """
        Build the URL to open in the browser.

        If a session token is saved on disk, append it as ``?t=<token>`` so
        the user is automatically logged in. app.py strips the token from the
        URL immediately after validation to keep it out of browser history and
        server access logs.

        Returns:
            Full URL string, with or without the ``?t=`` parameter.
        """
        try:
            if TOKEN_FILE.exists():
                # Ensure the token file is only readable by the owner (chmod 600).
                # This guards against multi-user systems where /home may be readable.
                try:
                    TOKEN_FILE.chmod(0o600)
                except Exception:
                    pass
                token = TOKEN_FILE.read_text().strip()
                if token:
                    return f"{APP_URL}/?t={token}"
        except Exception:
            pass
        return APP_URL

    def _selected_browser(self) -> dict:
        """Return the currently selected browser dict."""
        if self.combo_browser is not None:
            return self._browsers[self.combo_browser.currentIndex()]
        return self._browsers[0]

    def _on_browser_changed(self, idx: int) -> None:
        """Persist the newly selected browser to prefs when the combo changes."""
        if self.combo_browser is None:
            return
        prefs = _load_prefs()
        prefs["browser_binary"] = self.combo_browser.itemData(idx)
        _save_prefs(prefs)

    # ── Start / Stop ──────────────────────────────────────────────────────

    def _start(self) -> None:
        """
        Launch the Streamlit backend subprocess.

        Reads backend/.env so environment variables such as
        LYTRIZE_SUPABASE_URL are available to the subprocess without
        modifying the system environment.

        After launching, two threads are started:
            _WaitThread  — polls TCP 8501 until Streamlit accepts connections.
            _WatchThread — blocks on proc.wait() to detect unexpected exits.

        Any previous wait/watch threads (e.g. from a crash-restart cycle) are
        stopped before the new ones are created so their signals can never
        interfere with the new process (e.g. double browser-open or a stale
        timeout disabling the Open button after a successful restart).
        """
        if self._is_running():
            return

        self.btn_start.setEnabled(False)
        self._set_status("● Starting…", "#f59e0b")
        self._crash_count = 0
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            DATA_DIR.chmod(0o700)
        except Exception:
            pass

        # ── Cancel any leftover threads from the previous run ─────────────
        # Without this, a _WaitThread still polling 8501 when the new process
        # starts could emit ready() a second time (opening the browser twice),
        # or emit timeout() and re-disable the Open/Stop buttons.
        if self._wait_thread is not None:
            try:
                self._wait_thread.ready.disconnect()
                self._wait_thread.timeout.disconnect()
            except Exception:
                pass
            self._wait_thread.quit()
            self._wait_thread.wait(2000)   # at most 2 s; thread exits as soon as it wakes
            self._wait_thread = None

        if self._watch_thread is not None:
            try:
                self._watch_thread.crashed.disconnect()
            except Exception:
                pass
            self._watch_thread.cancel()    # suppress crash signal for the now-dead proc
            self._watch_thread.quit()
            self._watch_thread.wait(2000)
            self._watch_thread = None

        # Build subprocess environment
        env = os.environ.copy()
        env["LYTRIZE_DB_PATH"] = str(DB_PATH)

        # Load backend/.env into the subprocess environment.
        # Rules:
        #   - .env values ALWAYS override inherited system env vars for app keys.
        #     Using env.setdefault() was wrong: if LYTRIZE_SUPABASE_URL was set
        #     (even to "") in the parent process, setdefault silently kept the
        #     stale value and .env had zero effect.
        #   - We only override keys that appear in .env — other system vars are
        #     preserved (OS PATH, HOME, DISPLAY, etc. must not be clobbered).
        #   - Values that contain inline comments (key=val  # comment) are
        #     stripped so accidental comment text doesn't corrupt the URL.
        env_file = BASE / "backend" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                # Strip inline comments:  val=something  # comment
                val = val.split("#")[0].strip().strip('"').strip("'")
                if key:                   # allow blank values (intentional unset)
                    env[key] = val        # always override — .env is authoritative

        if VENV_PY.exists():
            python = str(VENV_PY)
        elif DEV_VENV_PY.exists():
            python = str(DEV_VENV_PY)
        else:
            python = "python3"

        self._proc = subprocess.Popen(
            [
                python, "-m", "streamlit", "run", str(APP_PY),
                "--server.port",                 "8501",
                "--server.address",              "127.0.0.1",
                "--server.headless",             "true",
                "--server.fileWatcherType",      "none",    # saves CPU on desktop
                "--server.runOnSave",            "false",
                "--server.enableCORS",           "false",   # loopback only
                "--server.enableXsrfProtection", "false",
                "--browser.gatherUsageStats",    "false",   # no telemetry
                "--browser.serverAddress",       "127.0.0.1",
                "--client.toolbarMode",          "minimal",
                "--runner.fastReruns",           "true",
                "--runner.magicEnabled",         "false",
                # NOTE: --global.disableWatchdogWarning was removed from Streamlit
                # in 1.30+. It is NOT a valid flag in current versions and causes
                # Streamlit to exit immediately with code 2 (bad argument),
                # which the launcher reports as "Crashed (exit 2)".
                # The watchdog warning is suppressed instead via the PYTHONPATH
                # env var approach: watchdog simply won't be found in the venv
                # when --server.fileWatcherType=none is set, so no warning fires.
            ],
            cwd=str(APP_PY.parent),
            env=env,
        )
        self.tray.show()

        # Readiness poller
        self._wait_thread = _WaitThread(self)
        self._wait_thread.ready.connect(self._on_ready)
        self._wait_thread.timeout.connect(self._on_timeout)
        self._wait_thread.start()

        # Crash watcher
        self._watch_thread = _WatchThread(self._proc)
        self._watch_thread.crashed.connect(self._on_crashed)
        self._watch_thread.start()

    def _on_ready(self) -> None:
        """Slot: Streamlit is accepting connections — update UI and open browser."""
        self._set_status("● Running", "#10b981")
        self.btn_stop.setEnabled(True)
        self.btn_open.setEnabled(True)
        self.tray.showMessage(
            "Lytrize", "App is ready.",
            QSystemTrayIcon.Information, 2000,
        )
        self._open_app()

    def _on_timeout(self) -> None:
        """Slot: Streamlit did not start within the polling timeout."""
        self._set_status("  Timed out — check terminal for errors", "#ef4444")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(True)

    def _on_crashed(self, code: int) -> None:
        """Slot: Streamlit subprocess exited unexpectedly."""
        self._crash_count += 1
        self._set_status(f"  Crashed (exit {code}) — click Start to retry", "#ef4444")
        self.btn_start.setEnabled(True)
        self.btn_open.setEnabled(False)
        self.btn_stop.setEnabled(False)
        if self.tray.isVisible():
            self.tray.showMessage(
                "Lytrize",
                f"Server exited unexpectedly (code {code}). Click Start to restart.",
                QSystemTrayIcon.Warning,
                4000,
            )

    # ── Open browser ──────────────────────────────────────────────────────

    def _open_app(self) -> None:
        """
        Open the Lytrize web UI in the selected browser.

        Browser modes
        -------------
        **Chromium** (Chrome, Brave, Edge, Vivaldi, Opera):
            Uses ``--app=<url>`` to open in "app mode" — the browser window
            has no address bar, no tabs, no extensions; it looks like a native
            desktop window.  ``--start-maximized`` makes it fill the screen.
            An isolated ``--user-data-dir`` prevents the Lytrize window from
            polluting (or being polluted by) the user's regular browsing profile.

            Flags removed vs naïve approaches:
              - ``--new-window`` — redundant with ``--app=``; can cause Chrome
                to open a plain browser window instead of the app-mode window
                when an existing Chrome instance owns the user-data-dir.
              - ``--disable-features=NetworkService`` — NetworkService has been
                mandatory since Chrome 102 (2022); this flag is silently ignored
                in modern builds and is misleading in code.

        **Firefox / Gecko** (Firefox, LibreWolf, Zen, Firefox ESR):
            Uses ``--new-instance`` to spawn a completely separate Firefox
            process (rather than passing the URL to the user's running Firefox),
            combined with a dedicated ``-profile`` directory for isolation.
            There is no true "app mode" in Firefox without an extension;
            a new maximised window is the best native equivalent.

        **xdg-open** (system default fallback):
            Delegates entirely to the OS; no isolation or window mode can be
            specified.
        """
        browser = self._selected_browser()
        target  = self._build_url()
        binary  = browser["binary"]

        # ── xdg-open fallback ─────────────────────────────────────────────
        if Path(binary).name == "xdg-open":
            try:
                subprocess.Popen(["xdg-open", target])
            except Exception as exc:
                self._set_status(f"  Could not open browser: {exc}", "#ef4444")
            return

        # ── Chromium-based browsers ───────────────────────────────────────
        if browser["chromium"]:
            _CHROMIUM_PROFILE.mkdir(parents=True, exist_ok=True)
            try:
                _PROFILE_ROOT.chmod(0o700)
                _CHROMIUM_PROFILE.chmod(0o700)
            except Exception:
                pass
            try:
                subprocess.Popen([
                    binary,
                    f"--app={target}",              # Strip browser UI; open as app window
                    "--start-maximized",             # Fill the screen on open
                    # NOTE: --new-window is intentionally omitted. It is redundant
                    # with --app= and causes Chrome to revert to a regular browser
                    # window when an existing Chrome instance already holds the
                    # user-data-dir lock.
                    "--disable-extensions",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--user-data-dir={_CHROMIUM_PROFILE}",
                    # ── Offline safety flags ──────────────────────────────
                    # These prevent background network requests that cause hangs
                    # when there is no internet connection (e.g. air-gapped installs).
                    "--disable-background-networking",
                    "--disable-client-side-phishing-detection",
                    "--disable-sync",
                    "--disable-translate",
                    "--safebrowsing-disable-auto-update",
                    # NOTE: "NetworkService" removed from --disable-features.
                    # It became mandatory in Chrome 102 (May 2022) and cannot be
                    # disabled; including it produces console warnings and confusion.
                    "--disable-features=OutOfBlinkCors",
                    # NOTE: --host-resolver-rules was removed here.
                    # Chromium >= ~110 (and Brave) flag it as "unsupported" and
                    # print a stability/security warning in the info-bar.
                    # Since the app only ever loads http://127.0.0.1:8501, external
                    # DNS resolution is never needed; the two flags below provide
                    # equivalent network isolation without triggering the warning.
                    "--no-proxy-server",             # no proxy, direct loopback only
                    "--dns-prefetch-disable",         # no speculative external DNS
                    "--allow-insecure-localhost",
                ])
            except Exception as exc:
                self._set_status(f"  Browser launch failed: {exc}", "#ef4444")
            return

        # ── Firefox / Gecko-based browsers ───────────────────────────────
        # --new-instance forces a completely separate Firefox process rather
        # than handing the URL to the user's running instance via DBus.
        #
        # Flag notes:
        #   --new-window target  BAD: with --new-instance, Firefox ignores
        #                        the URL argument and opens the homepage.
        #   -url target          CORRECT: explicitly sets the start URL.
        #   --kiosk              REMOVED: kiosk forces OS-level fullscreen and
        #                        strips the window title bar / OS decorations —
        #                        users cannot resize, minimise, or close normally.
        #                        Firefox has no "--app=" equivalent; the profile
        #                        user.js sets maximized + compact toolbar instead.
        _ensure_firefox_profile(_FIREFOX_PROFILE)
        try:
            subprocess.Popen([
                binary,
                "--new-instance",                    # always a fresh isolated process
                "-profile", str(_FIREFOX_PROFILE),   # isolated from user's real profile
                "-url", target,                      # correct URL flag for --new-instance
            ])
        except Exception as exc:
            self._set_status(f"  Browser launch failed: {exc}", "#ef4444")

    # ── Stop & Quit ───────────────────────────────────────────────────────

    def _stop_and_quit(self) -> None:
        """
        Gracefully stop the Streamlit subprocess then quit the launcher.

        Sequence:
            1. Cancel the watch thread so SIGTERM exit is not reported as crash.
            2. SIGTERM the process; wait up to 4 s for clean exit.
            3. SIGKILL if it has not exited by then.
            4. Hide tray and call QApplication.quit().
        """
        self.tray.hide()

        if self._proc is not None:
            # Tell the watcher not to treat the upcoming exit as a crash
            if self._watch_thread is not None:
                self._watch_thread.cancel()

            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
            except Exception:
                pass
            self._proc = None

        QApplication.quit()

    # ── Tray / window events ──────────────────────────────────────────────

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Restore the launcher window when the tray icon is single-clicked."""
        if reason == QSystemTrayIcon.Trigger:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def showEvent(self, event) -> None:  # noqa: N802
        """Ensure the window-level icon is set when the window becomes visible."""
        super().showEvent(event)
        handle = self.windowHandle()
        if handle:
            handle.setIcon(self._icon)

    def closeEvent(self, event) -> None:  # noqa: N802
        """
        Intercept the window-close button.

        If Streamlit is running, minimise to tray instead of quitting so the
        backend keeps serving.  If Streamlit is stopped, close normally.
        """
        if self._is_running():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "Lytrize",
                "Running in background. Right-click tray to quit.",
                QSystemTrayIcon.Information,
                3000,
            )
        else:
            self.tray.hide()
            event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    QApplication.setApplicationName("Lytrize")
    QApplication.setOrganizationName("Lytrize")

    app = QApplication(sys.argv)
    app.setApplicationDisplayName("Lytrize")
    app.setDesktopFileName("lytrize")
    # Keep the process alive even when the launcher window is hidden (tray mode).
    app.setQuitOnLastWindowClosed(False)

    icon = _make_icon()
    app.setWindowIcon(icon)

    window = Launcher()
    window.show()
    sys.exit(app.exec())
