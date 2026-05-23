"""
app.py -- Lytrize Desktop application entry point.

PAGE ROUTING
  On a brand-new install (no ~/.local/share/lytrize/.initialized sentinel):
    → Profile page — lets the user sign in, register, or continue as guest.

  On all subsequent launches (sentinel exists):
    → Home page  (signed-in or returning guest)
    → analysis/dashboard  (if _restore_draft finds an in-progress session)

  The sentinel is written exactly once, when the user first establishes a
  session: sign in, register, or "Continue as Guest". After that, the Profile
  page is only accessible via the Profile button in the navbar.

TOKEN VALIDATION (desktop mode)
  gui.py reads ~/.local/share/lytrize/session.token and injects it as ?t=
  each time the browser is opened. app.py validates the token and restores
  the session. The token is kept in the URL so that a browser page-refresh
  within the same browser tab re-validates and restores the session without
  requiring the user to re-open the app from the launcher.

  Within a single Streamlit session the flag _token_validated prevents
  double-validation on widget reruns. On a true page-refresh Streamlit
  creates a new session, the flag is absent, and validation runs again.
"""

import warnings
import json
import os

warnings.filterwarnings("ignore")

# ── Plotly offline configuration ─────────────────────────────────────────────
# Must happen BEFORE any Plotly figure is created or Streamlit imports plotly.
# These settings prevent Plotly from reaching out to CDN for MathJax or any
# other external resource during chart rendering.
try:
    import plotly.io as _pio
    _pio.renderers.default = "browser"
    try:
        _pio.config.mathjax = None          # plotly >= 5.13
    except AttributeError:
        pass
    try:
        import plotly.offline as _poff
        _poff._DEFAULT_INCLUDE_PLOTLYJS = "inline"
    except Exception:
        pass
except Exception:
    pass

import streamlit as st

st.set_page_config(
    page_title="Lytrize",
    page_icon="assets/lytrize.ico",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Environment loading ───────────────────────────────────────────────────────
# Load backend/.env first (primary — shipped with the app).
# Then load ~/.local/share/lytrize/sync.env as a secondary override so users
# can set LYTRIZE_SUPABASE_URL without touching the install directory
# (important when the app is installed system-wide in /opt/lytrize).
# override=False means the primary .env always wins over the secondary.
try:
    from dotenv import load_dotenv as _ld
    import pathlib as _pl
    # override=True: .env is authoritative over inherited system environment.
    # Without this, if LYTRIZE_SUPABASE_URL was ever set at system level (even
    # empty), load_dotenv silently kept the stale value.
    # CWD is backend/ (set by gui.py), so _ld() with no path finds backend/.env.
    _ld(override=True)   # backend/.env
    _user_sync_env = (
        _pl.Path.home() / ".local" / "share" / "lytrize" / "sync.env"
    )
    if _user_sync_env.exists():
        # User override: sync.env can override backend/.env (useful for custom
        # Supabase instances without editing the install directory).
        _ld(dotenv_path=str(_user_sync_env), override=True)
except ImportError:
    pass

from modules.database              import init_db, validate_token, get_draft, get_or_create_guest_user, cleanup_expired_tokens
from modules.ui.css                import inject_css
from modules.pages.auth            import page_auth, page_profile, is_initialized, mark_initialized
from modules.pages.home            import page_home
from modules.pages.upload          import page_upload
from modules.pages.analysis        import page_analysis
from modules.pages.dashboard       import page_dashboard
from modules.utils.session_cache   import save_df_snapshot, load_df_snapshot


@st.cache_resource(show_spinner=False)
def _init_db_once():
    """Initialise the local SQLite database. Runs exactly once per process."""
    init_db()
    cleanup_expired_tokens()


def _restore_draft(user_id: int) -> None:
    """
    Reload an in-progress analysis session from the local DB into session_state.
    Called after token validation so the user continues exactly where they left off,
    regardless of which browser window or tab they use.

    Restores:
      - Charts (Plotly figures from JSON)
      - Per-chart metadata (type, insights, notes, settings)
      - Dashboard title, KPIs, layout mode
      - The dataframe (from a parquet snapshot on disk)
      - The last active page (analysis / dashboard), so the user lands
        in the same view they were working in
    """
    import plotly.io as pio

    draft = get_draft(user_id)
    if not draft:
        return

    st.session_state.file_name       = draft.get("file_name", "")
    st.session_state.dashboard_title = draft.get("dashboard_title", "")
    st.session_state.layout_mode     = draft.get("layout_mode", "portrait")

    try:
        st.session_state.kpis = json.loads(draft.get("kpis_json", "[]"))
    except Exception:
        st.session_state.kpis = []

    if draft.get("editing_session_id"):
        st.session_state.editing_session_id   = draft["editing_session_id"]
        st.session_state.editing_session_name = draft.get("editing_session_name", "")

    try:
        charts_raw = json.loads(draft.get("charts_json", "[]"))
        charts = []
        for item in charts_raw:
            uid      = item.get("uid", "")
            title    = item.get("title", "")
            fig_json = item.get("fig_json", "")
            try:
                fig = pio.from_json(fig_json)
                charts.append((uid, title, fig))
                st.session_state[f"desc_{uid}"]          = item.get("desc", "")
                st.session_state[f"auto_insights_{uid}"] = item.get("auto_insights", [])
                st.session_state[f"chart_type_{uid}"]    = item.get("chart_type", "")
                st.session_state[f"chart_meta_{uid}"]    = item.get("meta", {})
            except Exception:
                pass
        if charts:
            st.session_state.charts = charts
    except Exception:
        pass

    try:
        meta_map = json.loads(draft.get("chart_meta_json", "{}"))
        for k, v in meta_map.items():
            if k not in st.session_state:
                st.session_state[k] = v
    except Exception:
        pass

    # ── Restore the dataframe from disk snapshot ──────────────────────────
    # The df is not stored in the DB (too large); it lives in a per-user
    # parquet file in /tmp.  If the snapshot exists and charts were restored,
    # re-inject df so the analysis page can immediately add more charts.
    if st.session_state.get("charts"):
        df = load_df_snapshot(user_id)
        if df is not None:
            st.session_state.df = df

    # ── Restore the last active page ─────────────────────────────────────
    # Restore analysis/dashboard only if BOTH charts AND df are available.
    # df lives in a /tmp parquet snapshot which is wiped on reboot — if it's
    # gone, sending the user to dashboard/analysis would show a broken page
    # (charts with no data, Save button that crashes). Go to home instead
    # and let them re-upload. Their charts/session are still safely saved.
    saved_page = draft.get("page", "")
    df_available = st.session_state.get("df") is not None
    if saved_page in ("analysis", "dashboard") and st.session_state.get("charts") and df_available:
        st.session_state._restore_to_page = saved_page
    elif saved_page in ("analysis", "dashboard") and st.session_state.get("charts") and not df_available:
        # Charts restored but df lost (reboot). Stay at home — saved sessions
        # list will show the saved session so user can view/edit it.
        pass


def main() -> None:
    _init_db_once()
    inject_css()

    url_token      = st.query_params.get("t", "")
    url_page       = st.query_params.get("p", "")
    url_session_id = st.query_params.get("sid", "")
    url_nav        = st.query_params.get("nav", "")

    # ── Token validation ──────────────────────────────────────────────────────
    # gui.py injects the saved token as ?t= each time the browser opens.
    # We validate it here and restore the session.
    #
    # The token is intentionally LEFT in the URL so that a browser page-refresh
    # re-validates and restores the session without requiring the user to
    # relaunch the app. Within the same Streamlit session (i.e. on widget reruns
    # but not on a true page-refresh) the _token_validated flag prevents the DB
    # lookup from running more than once per session.
    if url_token and "user_id" not in st.session_state and not st.session_state.get("_token_validated"):
        restored = validate_token(url_token)
        if restored:
            st.session_state.user_id         = restored[0]
            st.session_state.username        = restored[1]
            st.session_state.is_guest        = False
            st.session_state._token_validated = True
            mark_initialized()   # returning signed-in user — sentinel must exist
            _restore_draft(restored[0])

            # Returning signed-in user → go to home (they've been onboarded).
            # If _restore_draft found an active analysis/dashboard, restore that.
            restore_page = st.session_state.pop("_restore_to_page", None)
            st.session_state.page = restore_page if restore_page else "home"

            # Honor an explicit deep-link (e.g. ?p=dashboard&sid=5).
            if url_page and url_page not in ("auth", "upload"):
                st.session_state.page = url_page

            if url_session_id:
                try:
                    st.session_state.view_session_id = int(url_session_id)
                    st.session_state.pop("_view_charts",            None)
                    st.session_state.pop("_view_session_id_loaded", None)
                except Exception:
                    pass
        else:
            # Token invalid or expired — clear it from the URL and state.
            st.query_params.clear()

    # ── Guest bootstrap ───────────────────────────────────────────────────────
    if "user_id" not in st.session_state:
        guest = get_or_create_guest_user()
        if guest["id"] is None:
            # DB is not yet ready (race condition on first launch or corrupt DB).
            # Show a friendly spinner and rerun — init_db will fix itself next cycle.
            st.info("⏳ Setting up your workspace… please wait.", icon="🔧")
            import time as _time
            _time.sleep(0.5)
            st.rerun()
        st.session_state.user_id  = guest["id"]
        st.session_state.username = guest["username"]
        st.session_state.is_guest = True
        # Restore any in-progress draft so the guest's work survives page refresh.
        _restore_draft(guest["id"])
    elif "is_guest" not in st.session_state:
        st.session_state.is_guest = False

    # ── Default page ──────────────────────────────────────────────────────────
    # Profile-first is a ONE-TIME onboarding experience shown only on the
    # very first app launch. Once the user has established a session — whether
    # by signing in, registering, or clicking "Continue as Guest" — a sentinel
    # file is written to ~/.local/share/lytrize/.initialized.
    #
    # On all subsequent launches:
    #   - Signed-in users  → home  (their workspace)
    #   - Returning guests → home  (their saved local sessions)
    #   - Mid-analysis     → analysis/dashboard (restored by _restore_draft)
    #
    # Only a brand-new install (no sentinel) gets the profile page first.
    if "page" not in st.session_state:
        restore_page = st.session_state.pop("_restore_to_page", None)
        if restore_page:
            st.session_state.page = restore_page
        elif is_initialized():
            st.session_state.page = "home"
        else:
            st.session_state.page = "profile"

    # ── ?nav=home: clean navigation to home ───────────────────────────────────
    if "user_id" in st.session_state and url_nav == "home":
        for k in [
            "view_session_id", "_view_charts", "_vsid",
            "_view_session_id_loaded", "dashboard_title", "kpis", "layout_mode",
        ]:
            st.session_state.pop(k, None)
        st.session_state.page = "home"
        st.query_params.pop("nav", None)

    # ── Sync URL params to current page ──────────────────────────────────────
    st.query_params["p"] = st.session_state.page
    if st.session_state.get("view_session_id"):
        st.query_params["sid"] = st.session_state.view_session_id
    else:
        st.query_params.pop("sid", None)

    # ── Route ─────────────────────────────────────────────────────────────────
    p = st.session_state.page
    if   p == "auth":      page_auth()
    elif p == "home":      page_home()
    elif p == "upload":    page_upload()
    elif p == "analysis":  page_analysis()
    elif p == "dashboard": page_dashboard()
    elif p == "profile":   page_profile()
    else:
        # Unknown page key: fall back to profile. Profile is the correct
        # starting point for both guests and signed-in users, and it always
        # offers a clear path forward (continue as guest / home navigation).
        st.session_state.page = "profile"
        st.rerun()


if __name__ == "__main__":
    main()
