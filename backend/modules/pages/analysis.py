"""
modules/pages/analysis.py -- Analysis selection and chart generation page.
==========================================================================

Orchestrates the user flow between selecting analyses, configuring them,
generating charts, and navigating to the dashboard.

Flow on each rerun:
    1. Render the analysis card grid (ANALYSIS_OPTIONS from __init__.py).
    2. When the user clicks a card, it is added to selected_analyses.
    3. For each selected analysis, render its config panel via render_config_panel().
    4. "Generate Charts" button calls _run() for each selected analysis and
       appends results to st.session_state.charts.
    5. "Go to Dashboard" button navigates to the dashboard page.

Special handling:
    - descriptive renders inline via st.dataframe() and returns no charts.
    - Outlier Detection moved to the upload page (Data Quality section).
    - Auto-insights are generated via generate_chart_insights() after each chart.

CONTRIBUTING -- after adding a new analysis type in __init__.py:
    No changes needed here unless your analysis requires special page-level
    handling. The card grid, config panel, and chart generation all read from
    ANALYSIS_OPTIONS and _RUNNERS automatically.

Two-step "Configure → Generate" flow -- no st.form.
All config widgets are reactive; options like Top N and Dual Y show/hide instantly.
"""

import uuid, json
import streamlit as st
import streamlit.components.v1 as _comp
from modules.database import log_activity, save_draft, update_session_db, get_session_meta
from modules.analysis import (
    ANALYSIS_OPTIONS, _NEEDS_AXES, _NO_FORM,
    render_config_panel, _collect_kwargs, _run,
    render_config_panel_scoped, _collect_kwargs_scoped,
)
from modules.analysis.runners   import run_descriptive
from modules.analysis.data_quality import run_data_quality
from modules.charts import (
    charts_to_json,
    clean_insight_text,
    generate_chart_insights,
    apply_hover_format,  # ★ Added for consistent hover tooltips
)
from modules.ui.css import inject_footer, render_logo
from modules.ui.chart_settings import (
    apply_chart_display_options,
    render_chart_settings_controls,
)


def _shadow_notes_sync() -> None:
    """
    Copy all live desc_{uid} widget values into st.session_state._notes_shadow.

    _notes_shadow is a plain dict (not widget-keyed) so it survives st.rerun()
    regardless of whether the text_area widgets are rendered in the current run.

    Call this BEFORE any st.rerun() in an action handler that fires before
    _render_chart_list is reached — which is every handler in the config panel,
    the regen panel, and the buttons at the top of each chart card.
    """
    shadow = st.session_state.setdefault("_notes_shadow", {})
    for k, v in list(st.session_state.items()):
        if k.startswith("desc_") and k not in ("desc_add", "desc_close"):
            shadow[k[5:]] = v   # strip "desc_" prefix → uid


def _sync_one_note(uid: str) -> None:
    """on_change callback for a single notes text_area.  Writes the new value
    into the shadow dict immediately so it is never lost to a subsequent rerun."""
    val = st.session_state.get(f"desc_{uid}", "")
    st.session_state.setdefault("_notes_shadow", {})[uid] = val


def _autosave() -> None:  # Two-level write: draft_sessions (always) + sessions table (if editing).
    """
    Persist the current chart/notes state to the database on every meaningful
    user action (chart add, delete, regen, settings save).

    Two-level write:
      1. draft_sessions — always written; survives browser refresh.
      2. sessions table — written when editing_session_id is set, so the saved
         session is updated in-place and notes are never lost even if the user
         closes the tab without reaching the dashboard Save button.

    KPI preservation: the analysis page never loads or manages KPIs, so
    st.session_state.kpis is absent here.  We read the current kpis_json from
    the DB rather than overwriting it with "[]", which would silently wipe
    any KPIs the user added on the dashboard.
    """
    _shadow_notes_sync()
    _persist_draft()
    eid  = st.session_state.get("editing_session_id")
    uid  = st.session_state.get("user_id")
    name = st.session_state.get("editing_session_name", "Session")
    if eid and uid:
        try:
            # Preserve KPIs: analysis page never sets st.session_state.kpis, so
            # if it's absent we must read the saved value rather than write "[]".
            if "kpis" in st.session_state:
                kpis_json = json.dumps(st.session_state["kpis"])
            else:
                try:
                    sm = get_session_meta(eid, uid)
                    kpis_json = sm.get("kpis_json", "[]") if sm else "[]"
                except Exception:
                    kpis_json = "[]"

            update_session_db(
                eid, name,
                charts_to_json(st.session_state.get("charts", [])),
                st.session_state.get("selected_analyses", []),
                uid,
                dashboard_title = st.session_state.get("dashboard_title", ""),
                kpis_json       = kpis_json,
                layout_mode     = st.session_state.get("layout_mode", "portrait"),
            )
            try:
                st.toast("✅ Auto-saved", icon="✅")
            except Exception:
                pass  # toast unavailable in older Streamlit builds
        except Exception:
            pass  # DB errors must never block the UI


def _restore_edit_notes() -> None:
    """
    Re-seed desc_{uid} keys for all charts in the current editing session.

    Checks _notes_shadow first (catches notes typed after the last DB save),
    then falls back to the sessions table for the initial load.

    Guard: skipped when _analysis_notes_loaded is already True so the DB is
    hit at most once per edit session.  The flag is cleared by home.py on Edit
    click, and by _autosave/_do_update after every save.
    """
    if st.session_state.get("_analysis_notes_loaded"):
        # Shadow dict is always kept current, so re-seed from it on every entry.
        # This handles the case where the user typed a note, a rerun wiped the
        # widget key, and they land back on the page — shadow still has the value.
        shadow = st.session_state.get("_notes_shadow", {})
        for uid, note in shadow.items():
            key = f"desc_{uid}"
            if note and not st.session_state.get(key):
                st.session_state[key] = note
        return

    eid = st.session_state.get("editing_session_id")
    uid = st.session_state.get("user_id")
    if not eid or not uid:
        st.session_state["_analysis_notes_loaded"] = True
        return
    from modules.database import get_session_charts
    try:
        saved = get_session_charts(eid, uid)
        for chart_uid, _title, _fig, desc, _auto, _ctype, _meta in saved:
            note_key = f"desc_{chart_uid}"
            # Prefer shadow (contains notes typed since last DB save).
            shadow_val = st.session_state.get("_notes_shadow", {}).get(chart_uid, "")
            restore_val = shadow_val or desc
            if restore_val and not st.session_state.get(note_key):
                st.session_state[note_key] = restore_val
                st.session_state.setdefault("_notes_shadow", {})[chart_uid] = restore_val
    except Exception:
        pass  # DB errors must never break the analysis page.
    st.session_state["_analysis_notes_loaded"] = True


def _persist_draft(page="analysis"):
    uid = st.session_state.get("user_id")
    if not uid:
        return
    # Snapshot the DataFrame to the per-user parquet cache so it survives a
    # browser tab change (new WebSocket session → empty server session_state).
    try:
        from modules.utils.session_cache import save_df_snapshot
        save_df_snapshot(uid)
    except Exception:
        pass
    save_draft(
        user_id              = uid,
        page                 = page,
        charts_json          = charts_to_json(st.session_state.get("charts", [])),
        file_name            = st.session_state.get("file_name", ""),
        editing_session_id   = st.session_state.get("editing_session_id"),
        editing_session_name = st.session_state.get("editing_session_name"),
        dashboard_title      = st.session_state.get("dashboard_title", ""),
        kpis_json            = json.dumps(st.session_state.get("kpis", [])),
        chart_meta_json      = json.dumps({
            k: v for k, v in st.session_state.items()
            if k.startswith("chart_meta_")
        }),
        layout_mode          = st.session_state.get("layout_mode", "portrait"),
    )


def _add_charts(new_charts, active):
    col_descs = st.session_state.get("col_descriptions", {})
    for uid, title, fig in new_charts:
        st.session_state[f"chart_type_{uid}"]    = active
        st.session_state[f"auto_insights_{uid}"] = generate_chart_insights(
            active, title, fig, col_descs)
    st.session_state.charts.extend(new_charts)
    st.session_state._last_analysis_type = active
    if active not in st.session_state.selected_analyses:
        st.session_state.selected_analyses.append(active)
    log_activity(st.session_state.get("user_id", 0), "analysis_run",
                 f"type={active} charts_added={len(new_charts)}")
    _persist_draft()


def page_analysis():
    # Token is validated in app.py on startup and kept in the URL so that
    # a browser page-refresh re-validates and restores the session.
    # If no authenticated session exists, redirect to profile.
    if "user_id" not in st.session_state:
        st.session_state.page = "profile"
        st.rerun()

    df         = st.session_state.get("df")
    is_editing = "editing_session_id" in st.session_state

    render_logo()

    # Show a brief loading indicator on the very first render of this page
    # (before charts and config panels are painted) to eliminate the grey-out
    # that users saw when navigating here from the upload page.
    if not st.session_state.get("_analysis_page_ready"):
        st.session_state["_analysis_page_ready"] = True
        with st.spinner("Loading analysis workspace…"):
            import time as _t; _t.sleep(0.05)   # yields control so spinner paints

    # ── Edit mode without df ──────────────────────────────────────────────────
    if df is None and is_editing:
        _restore_edit_notes()   # Re-seed notes cleared by Streamlit widget cleanup
        sname  = st.session_state.get("editing_session_name", "Session")
        fname  = st.session_state.get("editing_file_name",    "the original file")
        charts = st.session_state.get("charts", [])

        if st.button("← Home"):
            for k in ["editing_session_id","editing_session_name","editing_file_name"]:
                st.session_state.pop(k, None)
            st.session_state.page = "home"; st.rerun()

        st.markdown(f"## ✏️ Editing: **{sname}**")
        st.info(
            f"📂 **Re-upload needed to run new analyses.** "
            f"Upload **{fname}** to add more charts, or go to Dashboard to save what you have.")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("📂 Upload Dataset to Add Charts", use_container_width=True):
                # Sync shadow + autosave to DB before leaving so notes are safe
                # even while the upload page is rendered (Streamlit wipes desc_ keys then).
                _autosave()
                # Clear the notes-loaded flag so _restore_edit_notes() re-seeds
                # desc_{uid} keys from shadow + DB when we return.
                st.session_state.pop("_analysis_notes_loaded", None)
                st.session_state.page = "upload"; st.rerun()
        with c2:
            if st.button("📊 Go to Dashboard →", use_container_width=True):
                _autosave()
                st.session_state.page = "dashboard"; st.rerun()

        _render_chart_list(charts, edit_mode=True)
        inject_footer()
        return

    if df is None:
        st.session_state.page = "upload"; st.rerun()

    if "charts"            not in st.session_state: st.session_state.charts            = []
    if "selected_analyses" not in st.session_state: st.session_state.selected_analyses = []

    _nav_c1, _nav_c2, _nav_spacer = st.columns([1, 1, 6])
    with _nav_c1:
        if st.button("← Home", use_container_width=True):
            st.session_state.pop("_analysis_page_ready", None)
            st.session_state.page = "home"; st.rerun()
    with _nav_c2:
        if st.button("📂 Upload", use_container_width=True,
                     help="Go back to Upload & Transformation to re-upload or clean data"):
            _shadow_notes_sync()
            st.session_state.pop("_analysis_notes_loaded", None)
            st.session_state.pop("_analysis_page_ready", None)
            st.session_state.page = "upload"; st.rerun()

    if is_editing:
        _restore_edit_notes()   # Re-seed notes that Streamlit cleared during upload navigation
        sname = st.session_state.get("editing_session_name", "Session")
        st.info(f"✏️ Edit mode -- adding charts to **{sname}**. "
                f"Click **Proceed to Dashboard** when done.")

    # ── Chart Regeneration Panel ─────────────────────────────────────────────
    # Triggered when user clicks "🔄 Edit Chart" on an existing chart.
    # Shows the full config panel for that chart's analysis type, scoped to
    # its uid so widget keys never collide with the main analysis panel.
    regen_uid  = st.session_state.get("_regen_uid")
    regen_type = st.session_state.get("_regen_type", "")
    if regen_uid and regen_type and df is not None:
        chart_entry = next(
            (c for c in st.session_state.get("charts", []) if c[0] == regen_uid), None)
        if chart_entry:
            regen_title = chart_entry[1]
            type_label  = next(
                (o["name"] for o in ANALYSIS_OPTIONS if o["id"] == regen_type),
                regen_type)
            st.markdown(f"### 🔄 Regenerate Chart — *{regen_title}* ({type_label})")
            st.caption("Adjust options below then click **Apply Changes** to replace the chart.")

            render_config_panel_scoped(regen_uid, regen_type, df)

            ra, rb, _ = st.columns([1, 1, 5])
            with ra:
                if st.button("✅ Apply Changes", key="regen_apply", type="primary",
                             use_container_width=True):
                    kwargs = _collect_kwargs_scoped(regen_uid, regen_type, df)
                    new_charts = _run(regen_type, df, **kwargs)
                    if new_charts:
                        # Replace the existing chart in-place (keep uid + position)
                        new_fig   = new_charts[0][2]  # take first generated chart
                        new_title = new_charts[0][1]
                        st.session_state.charts = [
                            (c[0], new_title if c[0] == regen_uid else c[1],
                             new_fig  if c[0] == regen_uid else c[2])
                            for c in st.session_state.get("charts", [])
                        ]
                        # Refresh auto-insights for the replaced chart
                        st.session_state.pop(f"auto_insights_{regen_uid}", None)
                        st.session_state[f"chart_type_{regen_uid}"] = regen_type
                    st.session_state.pop("_regen_uid",  None)
                    st.session_state.pop("_regen_type", None)
                    _autosave()
                    st.rerun()
            with rb:
                if st.button("✕ Cancel", key="regen_cancel", use_container_width=True):
                    st.session_state.pop("_regen_uid",  None)
                    st.session_state.pop("_regen_type", None)
                    _shadow_notes_sync()
                    st.rerun()

            st.markdown("---")

    # ── Dataset Preview ───────────────────────────────────────────────────────
    # Shows the user what data they're working with before they pick an analysis.
    # Three view modes: Top 10, Bottom 10, Random Sample.
    # State key "_preview_mode" persists across reruns so mode survives chart gen.
    _fname   = st.session_state.get("file_name", "dataset")
    _n_rows  = len(df)
    _n_cols  = len(df.columns)

    with st.expander(
        f"📋 Data Preview — {_fname}  ({_n_rows:,} rows × {_n_cols} columns)",
        expanded=st.session_state.get("_preview_expanded", True),
    ):
        # ── Mode buttons ─────────────────────────────────────────────────────
        _pb1, _pb2, _pb3, _pb4 = st.columns([1, 1, 1, 4])
        with _pb1:
            if st.button("⬆ Top 10",    key="prev_top",    use_container_width=True):
                st.session_state["_preview_mode"] = "top"
        with _pb2:
            if st.button("⬇ Bottom 10", key="prev_bot",    use_container_width=True):
                st.session_state["_preview_mode"] = "bottom"
        with _pb3:
            if st.button("🎲 Random",   key="prev_rand",   use_container_width=True):
                st.session_state["_preview_mode"] = "random"
        with _pb4:
            st.caption(
                f"Showing a sample of your loaded dataset. "
                f"Columns: **{', '.join(str(c) for c in df.columns[:8])}"
                f"{'…' if _n_cols > 8 else ''}**"
            )

        # ── Render sample ─────────────────────────────────────────────────────
        _mode = st.session_state.get("_preview_mode", "top")
        try:
            if _mode == "bottom":
                _prev_df = df.tail(10)
                _label   = "Bottom 10 rows"
            elif _mode == "random":
                _prev_df = df.sample(min(10, _n_rows), random_state=None)
                _label   = "10 random rows"
            else:
                _prev_df = df.head(10)
                _label   = "Top 10 rows"
        except Exception:
            _prev_df = df.head(10)
            _label   = "Top 10 rows"

        st.caption(f"*{_label}*")
        st.dataframe(
            _prev_df,
            use_container_width=True,
            height=min(380, 38 + len(_prev_df) * 35),
        )

        # ── Column summary ────────────────────────────────────────────────────
        _num_count = len(df.select_dtypes("number").columns)
        _cat_count = len(df.select_dtypes("object").columns)
        _dt_count  = len(df.select_dtypes("datetime").columns)
        _null_pct  = round(df.isnull().sum().sum() / max(df.size, 1) * 100, 1)
        st.caption(
            f"🔢 {_num_count} numeric  ·  🔤 {_cat_count} text  ·  "
            f"📅 {_dt_count} datetime  ·  ⚠️ {_null_pct}% missing values"
        )

    st.markdown("## 🔬 Select Analysis Type")

    active = st.session_state.get("_active_analysis")

    # Compact grouped card layout.
    # Using smaller dynamic columns prevents large empty gaps when
    # future analysis cards are added or removed.
    cols   = st.columns([1,1,1,1,1], gap="small")
    for i, opt in enumerate(ANALYSIS_OPTIONS):
        with cols[i % 5]:
            selected = opt["id"] == active
            st.markdown(
                f'<div class="ag-card" style="{"border-color:#6163df;box-shadow:0 0 0 3px rgba(97,99,223,0.18);" if selected else ""}">'
                f'<div class="ag-icon">{opt["icon"]}</div>'
                f'<div class="ag-name">{opt["name"]}</div>'
                f'<div class="ag-desc">{opt["desc"]}</div></div>',
                unsafe_allow_html=True)
            if st.button("▶ Select", key=f"btn_{opt['id']}"):
                if st.session_state.get("_active_analysis") == opt["id"]:
                    st.session_state["_active_analysis"] = None
                else:
                    st.session_state["_active_analysis"] = opt["id"]
                _shadow_notes_sync()
                st.rerun()

    # ── Active analysis config panel ──────────────────────────────────────────
    if active:
        analysis_name = next(o["name"] for o in ANALYSIS_OPTIONS if o["id"] == active)
        st.markdown("---")
        # Auto-scroll to Configure section for better UX
        _comp.html("""<script>
        setTimeout(function(){
            var els = window.parent.document.querySelectorAll('h3');
            for(var el of els){
                if(el.textContent && el.textContent.includes('Configure')){
                    el.scrollIntoView({behavior:'smooth',block:'start'});
                    break;
                }
            }
        }, 150);
        </script>""", height=0)

        # ── Descriptive -- no chart output ─────────────────────────────────────
        if active == "descriptive":
            st.markdown("### 🗂️ Descriptive Statistics")
            run_descriptive(df)
            # "Keep in Analysis" was non-functional (descriptive stats produce no
            # chart object to add to the chart list) — removed per user feedback.
            if st.button("✕ Close", key="desc_close"):
                st.session_state["_active_analysis"] = None
                _shadow_notes_sync()
                st.rerun()

        # ── All other analysis types -- two-step: configure then generate ──────
        else:
            st.markdown(f"### ⚙️ Configure -- {analysis_name}")
            st.caption("Adjust options below. All selections are live -- no submit needed until Generate.")

            # Render config widgets (fully reactive -- no form)
            render_config_panel(active, df)

            st.markdown("<br>", unsafe_allow_html=True)
            g1, g2, _ = st.columns([1, 1, 5])
            with g1:
                generate_clicked = st.button(
                    "▶ Generate Charts", key=f"gen_{active}",
                    type="primary", use_container_width=True)
            with g2:
                close_clicked = st.button(
                    "✕ Close", key=f"close_{active}",
                    use_container_width=True)

            if close_clicked:
                st.session_state["_active_analysis"] = None
                _shadow_notes_sync()
                st.rerun()

            if generate_clicked:
                kwargs = _collect_kwargs(active, df)
                new_charts = _run(active, df, **kwargs)
                if new_charts is not None:
                    if new_charts:
                        _add_charts(new_charts, active)
                    st.session_state["_active_analysis"] = None
                    _autosave()
                    st.rerun()

    # ── Generated charts ──────────────────────────────────────────────────────
    if st.session_state.charts:
        st.markdown("---")
        h1, h2 = st.columns([5, 1])
        with h1:
            st.markdown(f"## 📈 Generated Charts ({len(st.session_state.charts)})")
        with h2:
            if st.button("🗑️ Clear All", key="clear_all_charts"):
                log_activity(st.session_state.get("user_id",0),"charts_cleared_all",
                             f"count={len(st.session_state.charts)}")
                st.session_state.charts = []
                st.session_state.selected_analyses = []
                st.session_state.pop("_notes_shadow", None)   # charts gone, clear shadow too
                _autosave()
                st.rerun()

        _render_chart_list(st.session_state.charts, edit_mode=is_editing)

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🎯 Proceed to Dashboard →", type="primary"):
            log_activity(st.session_state.get("user_id",0),"proceed_to_dashboard",
                         f"charts={len(st.session_state.charts)}")
            with st.spinner("Saving your work and preparing the dashboard…"):
                _autosave()
            st.session_state.page = "dashboard"; st.rerun()

    inject_footer()


def _chart_meta(uid) -> dict:
    """Read chart meta from session_state (same structure as dashboard._meta)."""
    k = f"chart_meta_{uid}"
    if k not in st.session_state:
        st.session_state[k] = {}
    return st.session_state[k]


def _set_chart_meta(uid, **kw) -> None:
    """Write chart meta to session_state."""
    k = f"chart_meta_{uid}"
    if k not in st.session_state:
        st.session_state[k] = {}
    st.session_state[k].update(kw)


def _render_chart_list(charts, edit_mode=False):
    """Render chart cards with full settings, insights, and notes in edit mode."""
    col_descs = st.session_state.get("col_descriptions", {})
    for uid, title, fig in charts:
        meta = _chart_meta(uid)

        # ── Header row: display title + action buttons ────────────────────────
        display_title = meta.get("custom_title") or title
        df_available  = st.session_state.get("df") is not None
        # Read text_style so header/subtitle size+colour changes preview live
        # (without needing a full dashboard build to confirm).
        _ts          = meta.get("text_style") or {}
        _hdr_size    = int(_ts.get("header_size",    28))
        _hdr_color   = str(_ts.get("header_color",   "#6163df"))
        _sub_size    = int(_ts.get("subtitle_size",  11))
        _sub_color   = str(_ts.get("subtitle_color", "#64748b"))
        ctrl = st.columns([9, 2, 1])
        with ctrl[0]:
            st.markdown(
                f'<div style="font-size:{_hdr_size}px;font-weight:700;'
                f'color:{_hdr_color};margin-bottom:0.2rem;">'
                f'{display_title}</div>',
                unsafe_allow_html=True)
            if meta.get("subtitle"):
                st.markdown(
                    f'<div style="font-size:{_sub_size}px;color:{_sub_color};'
                    f'margin-top:-4px;margin-bottom:4px;">{meta["subtitle"]}</div>',
                    unsafe_allow_html=True)
        with ctrl[1]:
            chart_type = st.session_state.get(f"chart_type_{uid}", "")
            if chart_type and chart_type not in ("descriptive", "data_quality"):
                if df_available:
                    if st.button("🔄 Edit Chart", key=f"regen_btn_{uid}",
                                 use_container_width=True,
                                 help="Re-run this chart with new columns / settings"):
                        st.session_state._regen_uid  = uid
                        st.session_state._regen_type = chart_type
                        # Clear old scoped widget keys so panel starts fresh
                        for k in list(st.session_state.keys()):
                            if k.startswith(f"_edit_{uid}_"):
                                del st.session_state[k]
                        _shadow_notes_sync()   # protect notes before rerun skips text_areas
                        st.rerun()
                else:
                    st.button("🔄 Edit Chart", key=f"regen_btn_{uid}",
                              use_container_width=True, disabled=True,
                              help="Upload the original dataset first to regenerate this chart")
        with ctrl[2]:
            if st.button("✕", key=f"del_{uid}", help="Remove this chart"):
                log_activity(st.session_state.get("user_id",0),"chart_deleted",f"title='{title}'")
                st.session_state.charts = [c for c in st.session_state.charts if c[0] != uid]
                st.session_state.pop("_regen_uid", None)
                st.session_state.get("_notes_shadow", {}).pop(uid, None)  # clean up shadow entry
                _autosave()
                st.rerun()

        # ── Chart plot ────────────────────────────────────────────────────────
        import copy as _acopy
        fig_show = _acopy.deepcopy(fig)
        xl = meta.get("x_label", "")
        yl = meta.get("y_label", "")
        _ctype_apply = st.session_state.get(f"chart_type_{uid}", "")
        if _ctype_apply == "matrix_table":
            # For matrix tables: x_label renames the index column header in go.Table traces,
            # y_label renames the coloraxis / heatmap yaxis title.
            if xl or yl:
                for _tr in fig_show.data:
                    if hasattr(_tr, "header") and hasattr(_tr, "cells"):
                        # go.Table: rename first column header
                        if xl and hasattr(_tr.header, "values") and _tr.header.values:
                            vals = list(_tr.header.values)
                            if vals:
                                vals[0] = xl
                                _tr.header.values = vals
            if yl:
                fig_show.update_yaxes(title_text=yl)
                fig_show.update_coloraxes(
                    colorbar=dict(title=dict(text=yl))
                )
        else:
            if xl: fig_show.update_xaxes(title_text=xl)
            if yl: fig_show.update_yaxes(title_text=yl)
        fig_show.update_layout(title_text="")
        # Apply stored legend trace renames (from Chart Settings → Legend Labels)
        # New format: legend_names = {original_name: custom_name}
        # Legacy fallback: trace_names = {str(index): custom_name}
        stored_legend = meta.get("legend_names", {})
        stored_trace_idx = meta.get("trace_names", {})
        for _ti, _trace in enumerate(fig_show.data):
            _orig = getattr(_trace, "name", None)
            if _orig is None:
                continue
            renamed = stored_legend.get(str(_orig)) or stored_trace_idx.get(str(_ti))
            if renamed:
                _trace.name = renamed
        _leg_title = meta.get("legend_title", "")
        if _leg_title:
            fig_show.update_layout(legend_title_text=_leg_title)
        fig_show = apply_chart_display_options(fig_show, meta, _ctype_apply)
        is_horiz = any(getattr(t, "orientation", "v") == "h"
                       for t in fig_show.data if hasattr(t, "orientation"))
        if is_horiz:
            fig_show.update_yaxes(tickfont=dict(size=10), automargin=True)
            fig_show.update_xaxes(tickfont=dict(size=10))
            fig_show.update_layout(margin=dict(l=120, r=20, t=28, b=20))
        else:
            fig_show.update_xaxes(tickangle=-35, tickfont=dict(size=10), automargin=True)
            fig_show.update_yaxes(tickfont=dict(size=10), automargin=True)
            fig_show.update_layout(margin=dict(l=20, r=20, t=28, b=80))

        # ★ Apply consistent K/M/B hover tooltips (same as Dashboard)
        apply_hover_format(fig_show)

        # ── Apply saved colorbar range (heatmap / map colour scale) ──────────
        _saved_zmin = meta.get("colorbar_zmin", "")
        _saved_zmax = meta.get("colorbar_zmax", "")
        try:
            _zmin = float(_saved_zmin) if str(_saved_zmin).strip() else None
            _zmax = float(_saved_zmax) if str(_saved_zmax).strip() else None
            if _zmin is not None or _zmax is not None:
                for _tr in fig_show.data:
                    if hasattr(_tr, "zmin"):
                        if _zmin is not None:
                            _tr.zmin = _zmin
                        if _zmax is not None:
                            _tr.zmax = _zmax
                # Also update the shared coloraxis if present
                if _zmin is not None or _zmax is not None:
                    fig_show.update_coloraxes(
                        cmin=_zmin if _zmin is not None else None,
                        cmax=_zmax if _zmax is not None else None,
                    )
        except Exception:
            pass

        # ── Scrollable container for Table view (many rows) ─────────────
        # go.Table figures have a fixed pixel height set by matrix_table.py.
        # When that height > the viewport the chart clips at the bottom and
        # the user sees only the top rows with no way to scroll. Wrapping in
        # a CSS overflow-y:auto div gives native browser scrolling.
        _chart_type_now = st.session_state.get(f"chart_type_{uid}", "")
        _fig_height     = getattr(fig_show, "layout", None)
        _fig_height_px  = (
            getattr(_fig_height, "height", 0) or 0
            if _fig_height else 0
        )
        _is_matrix = _chart_type_now == "matrix_table"
        # Wrap matrix tables in a scrollable container:
        #   overflow-y: vertical scroll when many rows exceed viewport height
        #   overflow-x: horizontal scroll when many columns exceed container width
        # Both axes are needed — previously only overflow-y was set, so wide
        # tables with many category columns had no side-scroll at all.
        if _is_matrix:
            st.markdown(
                '<div style="max-height:540px;overflow-y:auto;overflow-x:auto;'
                'border:1px solid rgba(100,116,139,0.2);border-radius:6px;'
                'padding-bottom:4px;">',
                unsafe_allow_html=True,
            )
        st.plotly_chart(
            fig_show,
            use_container_width=not _is_matrix,
            # key= is mandatory: without it Streamlit tears down and rebuilds
            # the Plotly iframe on every rerun, causing the visible chart flicker.
            key=f"plotly_{uid}",
            config={
                "responsive": True,
                "displayModeBar": "hover",
                "mathjax": False,
            },
        )
        if _is_matrix:
            st.markdown("</div>", unsafe_allow_html=True)

        # ── Chart Settings expander ───────────────────────────────────────────
        # Use a persistent boolean so the expander stays open after "Apply Settings"
        # and across any widget interaction reruns that occur inside it.
        # The flag is set True by "Apply Settings" and cleared on page navigation.
        _settings_key  = f"_settings_open_{uid}"
        _settings_open = st.session_state.get(_settings_key, False)
        with st.expander("⚙️ Chart Settings", expanded=_settings_open):
            chart_type    = st.session_state.get(f"chart_type_{uid}", "")
            auto_insights = st.session_state.get(f"auto_insights_{uid}")
            if auto_insights is None:
                auto_insights = generate_chart_insights(chart_type, title, fig, col_descs)
                st.session_state[f"auto_insights_{uid}"] = auto_insights
            updates = render_chart_settings_controls(
                uid, title, fig, chart_type, meta, auto_insights,
                key_prefix="analysis",
                show_text_style=True,
            )

            if st.button("💾 Apply Settings", key=f"asave_{uid}", type="primary"):
                _set_chart_meta(uid, **updates)
                # Keep title in charts list in sync
                st.session_state.charts = [
                    (c[0], updates["custom_title"] if c[0] == uid else c[1], c[2])
                    for c in st.session_state.get("charts", [])
                ]
                _autosave()
                # Keep this chart's settings expander open after the rerun
                # (persistent flag — not popped — so it survives further widget reruns).
                st.session_state[f"_settings_open_{uid}"] = True
                st.toast("✅ Settings applied — chart updated above.", icon="✅")
                st.rerun()

        # ── Insights (read view below chart) ──────────────────────────────────
        if auto_insights:
            with st.expander("💡 Auto-Insights", expanded=False):
                for ins in auto_insights:
                    st.markdown(f"- {clean_insight_text(ins)}")

        # ── Analysis Notes ────────────────────────────────────────────────────
        # Seed from shadow first (survives any rerun), then empty.
        # Shadow is populated by on_change below and by _autosave()/_restore_edit_notes().
        if f"desc_{uid}" not in st.session_state:
            st.session_state[f"desc_{uid}"] = (
                st.session_state.get("_notes_shadow", {}).get(uid, ""))
        st.text_area(
            "✍️ Analysis Notes (auto-saved to Dashboard)",
            key=f"desc_{uid}",
            on_change=_sync_one_note,
            args=(uid,),
            placeholder="Add your findings or observations here…")

        st.markdown("---")
