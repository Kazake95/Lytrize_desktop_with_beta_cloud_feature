"""
modules/pages/home.py -- Home page.
"""
import streamlit as st
from html import escape
from pathlib import Path
from modules.database import (
    revoke_token, log_activity,
    get_user_sessions, get_session_charts, get_session_meta,
    rename_session_db, delete_session_db,
)
import plotly.io as pio
from modules.ui.css import inject_footer, render_logo
from modules.analysis import ANALYSIS_OPTIONS

# -------------------------------------------------------------------------
# Image for the right column
# -------------------------------------------------------------------------
RIGHT_IMAGE_PATH = Path(__file__).resolve().parents[2] / "assets" / "welcome-banner-removebg.png"
USE_RIGHT_IMAGE = True

def page_home():
    render_logo() 
    is_guest = st.session_state.get("is_guest", False)

    # -------------------------------------------------------------------------
    # TWO-COLUMN LAYOUT: left = banner + KPIs + button; right = image
    # -------------------------------------------------------------------------
    if USE_RIGHT_IMAGE:
        left_col, right_col = st.columns([1, 1], gap="small")
    else:
        left_col, right_col = st.columns([2, 1])
        right_col.empty()

    with left_col:
        # ── Welcome banner ────────────────────────────────────────────────
        display_name = "Guest" if is_guest else escape(str(st.session_state.username))
        back_txt = " back" if not is_guest else ""

        st.markdown(
            f'<div class="welcome-banner">'
            f'<div style="font-size:0.7rem;font-weight:700;letter-spacing:0.12em;'
            f'text-transform:uppercase;opacity:0.75;margin-bottom:0.35rem;">DASHBOARD OVERVIEW</div>'
            f'<div style="font-size:2rem;font-weight:800;font-family:\'Sora\',sans-serif;'
            f'letter-spacing:-0.03em;margin-bottom:0.35rem;">'
            f'Welcome{back_txt}, {display_name} 👋</div>'
            f'<div style="font-size:0.9rem;opacity:0.88;line-height:1.55;text-align:center;">'
            f'Your data workspace is ready. '
            f'Upload a dataset or pick up where you left off ✌️</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── KPI cards ─────────────────────────────────────────────────────
        sessions = get_user_sessions(st.session_state["user_id"])
        unique_files = len(set(s[2] for s in sessions)) if sessions else 0

        m1, m2, m3 = st.columns(3, gap="medium")
        for col, icon, val, lbl in [
            (m1, "📁", len(sessions), "Saved Sessions"),
            (m2, "🗂️", unique_files, "Datasets Analysed"),
            (m3, "🔬", len(ANALYSIS_OPTIONS), "Available Analyses"),
        ]:
            with col:
                st.markdown(
                    f'<div class="kpi-card">'
                    f'  <div class="kpi-icon">{icon}</div>'
                    f'  <div class="kpi-body">'
                    f'    <div class="kpi-val">{val}</div>'
                    f'    <div class="kpi-lbl">{lbl}</div>'
                    f'  </div></div>',
                    unsafe_allow_html=True,
                )

        st.markdown("<div style='margin-top:0.85rem'></div>", unsafe_allow_html=True)

        # ── Start New Analysis button ────────────────────────────────────
        if st.button("🚀 Start New Analysis", type="primary"):
            if not is_guest:
                log_activity(st.session_state["user_id"], "new_analysis_started")
            for k in ["editing_session_id", "editing_session_name", "editing_file_name",
                      "df", "charts", "selected_analyses", "dashboard_title", "kpis",
                      "layout_mode", "_view_charts", "view_session_id"]:
                st.session_state.pop(k, None)
            st.session_state.page = "upload"
            st.rerun()

    if USE_RIGHT_IMAGE:
        with right_col:
            if RIGHT_IMAGE_PATH.exists():
                st.image(str(RIGHT_IMAGE_PATH), use_container_width=True)

    # -------------------------------------------------------------------------
    # Previous Sessions section
    # -------------------------------------------------------------------------
    st.markdown("---")
    st.markdown('<div class="sec-label">📁 Previous Sessions</div>', unsafe_allow_html=True)

    # ── First-sign-in cloud sessions nudge ────────────────────────────────────
    # After sign-in the home page shows local SQLite sessions only.
    # Cloud sessions are NOT pulled automatically (sync is always manual).
    # Show a dismissible banner so users know to sync if they expect cloud data.
    if not is_guest and not st.session_state.get("_cloud_nudge_dismissed"):
        try:
            from modules.sync import get_sync_status as _gss
            if _gss()["configured"]:
                ca, cb = st.columns([10, 1])
                with ca:
                    st.info(
                        "☁️ **Have sessions on another device?** "
                        "Go to **Profile → Sync** to pull your cloud sessions here. "
                        "Only local sessions are shown until you sync.",
                        icon=None,
                    )
                with cb:
                    if st.button("✕", key="_dismiss_cloud_nudge", help="Dismiss"):
                        st.session_state["_cloud_nudge_dismissed"] = True
                        st.rerun()
        except Exception:
            pass

    if is_guest:
        st.info("📝 You're in guest mode — sessions are saved locally on this device.")
        # Fall through to show local sessions below — do NOT return early.
        # Sessions are saved to lytrize.db under the guest user ID and are
        # fully queryable; the early return here was hiding them from the UI.

    if not sessions:
        st.info("No saved sessions yet. Start your first analysis above!")
    else:
        for s in sessions[:20]:
            sid, sname, fname, rows, cols, atypes, created = s
            sa, sb, sc, sd, se = st.columns([3, 1, 1, 1, 1])  # name | View | Edit | Rename | Delete
            with sa:
                st.markdown(
                    f'<div class="sess-card"><b>{escape(str(sname))}</b><br>'
                    f'<span style="opacity:.65;font-size:.8rem;">'
                    f'{escape(str(fname or ""))} &nbsp;· &nbsp; {rows}×{cols}</span></div>',
                    unsafe_allow_html=True,
                )
            with sb:
                if st.button("View", key=f"v_{sid}"):
                    st.session_state.view_session_id = sid
                    st.session_state.page = "dashboard"
                    st.rerun()
            with sc:
                if st.button("✏️ Edit", key=f"e_{sid}", help="Add/modify charts"):
                    # Load the saved charts + metadata into live session state
                    # so the user can add/modify charts exactly like a new analysis.
                    sm = get_session_meta(sid, st.session_state.get("user_id"))
                    loaded = get_session_charts(sid, st.session_state.get("user_id"))
                    charts = []
                    for uid, title, fig, desc, auto, ctype, meta in loaded:
                        st.session_state[f"desc_{uid}"]          = desc
                        st.session_state[f"auto_insights_{uid}"] = auto
                        st.session_state[f"chart_type_{uid}"]    = ctype
                        st.session_state[f"chart_meta_{uid}"]    = meta
                        charts.append((uid, title, fig))
                    st.session_state.charts           = charts
                    st.session_state.editing_session_id   = sid
                    st.session_state.editing_session_name = sname
                    st.session_state.editing_file_name    = fname
                    st.session_state.file_name            = fname
                    if sm:
                        st.session_state.dashboard_title = sm.get("dashboard_title", "")
                        st.session_state.layout_mode     = sm.get("layout_mode", "portrait")
                        try:
                            import json
                            st.session_state.kpis = json.loads(sm.get("kpis_json", "[]"))
                        except Exception:
                            st.session_state.kpis = []
                    # MUST clear df: without this, if a df is still loaded from a
                    # previous analysis, the upload page shows the stale-dataset
                    # "continue with current dataset" banner and hides the
                    # _edit_needs_reupload re-upload warning entirely.
                    st.session_state.pop("df", None)
                    st.session_state.pop("file_signature", None)
                    st.session_state._edit_needs_reupload = True
                    st.session_state.page = "upload"
                    st.rerun()
            with sd:
                if st.button("✏ Rename", key=f"rn_{sid}", help="Rename this session"):
                    st.session_state[f"_renaming_{sid}"] = True
                    st.rerun()
            with se:
                if st.button("🗑️", key=f"d_{sid}", help="Delete session"):
                    st.session_state[f"_confirm_del_{sid}"] = True
                    st.rerun()
            if st.session_state.get(f"_renaming_{sid}"):
                new_name = st.text_input(
                    "New session name", value=sname, key=f"rn_input_{sid}"
                )
                rc1, rc2 = st.columns(2)
                with rc1:
                    if st.button("Save name", key=f"rn_save_{sid}", type="primary"):
                        if new_name.strip():
                            rename_session_db(sid, new_name.strip(),
                                              st.session_state.get("user_id"))
                        st.session_state.pop(f"_renaming_{sid}", None)
                        st.rerun()
                with rc2:
                    if st.button("Cancel", key=f"rn_cancel_{sid}"):
                        st.session_state.pop(f"_renaming_{sid}", None)
                        st.rerun()
            # BUG FIX: use get() not pop() here.
            # pop() removes the key on the rerun that SHOWS the dialog, so by the
            # time the user clicks "Confirm delete" the key is already gone and the
            # entire block evaluates to False — the delete never fires.
            # Now we pop() explicitly inside each action branch instead.
            if st.session_state.get(f"_confirm_del_{sid}", False):
                ca, cb = st.columns(2)
                with ca:
                    if st.button("✅ Confirm delete", key=f"cd_{sid}"):
                        st.session_state.pop(f"_confirm_del_{sid}", None)
                        deleted = delete_session_db(sid, st.session_state.get("user_id"))
                        if not deleted:
                            st.error("Could not delete this session. It may already be gone or the backup import left a stale row mapping.")
                        for key in (
                            "editing_session_id",
                            "editing_session_name",
                            "editing_file_name",
                            "view_session_id",
                            "_view_charts",
                            "_view_session_id_loaded",
                        ):
                            st.session_state.pop(key, None)
                        st.rerun()
                with cb:
                    if st.button("Cancel", key=f"cx_{sid}"):
                        st.session_state.pop(f"_confirm_del_{sid}", None)
                        st.rerun()

    inject_footer()
