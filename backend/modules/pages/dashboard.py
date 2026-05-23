"""
modules/pages/dashboard.py -- Dashboard view, editing, saving, and PDF export.
==============================================================================

The dashboard page has two operating modes:

  Edit / Build mode (default after analysis):
    - Shows the generated chart cards in a portrait or landscape grid.
    - Allows adding/editing KPI cards, renaming charts, adding descriptions.
    - "Save Session" persists charts + metadata to the sessions table.
    - "Export PDF" calls modules/export.py to produce a downloadable report.
    - Auto-saves progress to draft_sessions on every meaningful action.

  View / Read-only mode (?sid= URL parameter):
    - Loads a saved session's charts from the DB via get_session_charts().
    - Renders in a read-only layout (no edit controls shown).
    - Accessed via shared links or clicking a saved session card on home.py.

Session state keys managed here:
    charts          -- list of (uid, title, fig) tuples
    dashboard_title -- editable title shown at the top of the dashboard
    kpis            -- list of KPI dicts: {label, value, icon}
    layout_mode     -- "portrait" (2-col) or "landscape" (3-col)
    editing_session_id / editing_session_name -- set when editing a saved session

CONTRIBUTING -- to add a new dashboard panel or widget:
    Add a new st.expander() or column block in page_dashboard().
    Call save_draft() after any state change the user should be able to recover.
"""
import json, copy, datetime
import pandas as pd
import streamlit as st
from html import escape
import re

from modules.database import (
    log_activity,
    save_session_db, update_session_db,
    get_session_charts, get_session_meta,
    clear_draft, save_draft,
)
from modules.charts import charts_to_json, clean_insight_text, _fmt_num, apply_hover_format
from modules.export import generate_html_report
from modules.ui.css import inject_footer, render_logo
from modules.ui.chart_settings import (
    apply_chart_display_options,
    default_text_style as _shared_default_text_style,
    merge_text_style as _shared_merge_text_style,
    render_chart_settings_controls,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _dash_sync_notes() -> None:
    """
    Snapshot all live desc_{uid} note values into _notes_shadow before any
    st.rerun() that fires mid-page (e.g. KPI add/remove, chart delete from
    the dashboard).

    Dashboard renders the KPI section BEFORE the chart card loop, so any
    st.rerun() triggered by a KPI button aborts the run before the text_area
    widgets are rendered.  Streamlit then clears the widget-bound desc_ keys.
    The shadow dict is a plain non-widget dict that survives every rerun, so
    notes can be restored from it when the chart cards next render.
    """
    shadow = st.session_state.setdefault("_notes_shadow", {})
    for k, v in list(st.session_state.items()):
        if k.startswith("desc_") and isinstance(v, str):
            shadow[k[5:]] = v   # strip "desc_" prefix → uid
def _persist():
    uid = st.session_state.get("user_id")
    if not uid:
        return
    save_draft(
        user_id              = uid,
        page                 = "dashboard",
        charts_json          = charts_to_json(st.session_state.get("charts", [])),
        file_name            = st.session_state.get("file_name", ""),
        editing_session_id   = st.session_state.get("editing_session_id"),
        editing_session_name = st.session_state.get("editing_session_name"),
        dashboard_title      = st.session_state.get("dashboard_title", ""),
        kpis_json            = json.dumps(st.session_state.get("kpis", [])),
        chart_meta_json      = json.dumps(
            {k: v for k, v in st.session_state.items() if k.startswith("chart_meta_")}),
        layout_mode          = st.session_state.get("layout_mode", "portrait"),
    )


def _meta(uid):
    k = f"chart_meta_{uid}"
    if k not in st.session_state:
        st.session_state[k] = {}
    return st.session_state[k]


def _set_meta(uid, **kw):
    k = f"chart_meta_{uid}"
    if k not in st.session_state:
        st.session_state[k] = {}
    st.session_state[k].update(kw)


def _default_text_style() -> dict:
    """Return the default typography settings used by chart cards."""
    return _shared_default_text_style()


def _merge_text_style(raw: dict | None) -> dict:
    """Merge a stored text-style dict over the defaults."""
    return _shared_merge_text_style(raw)


def _apply_axes(fig, x_lbl, y_lbl, text_style: dict | None = None):
    try:
        f2 = copy.deepcopy(fig)
        style = _merge_text_style(text_style)
        axis_title_font = dict(
            size=int(style["axis_title_size"]),
            color=str(style["axis_title_color"]),
            family=str(style["family"]),
        )
        axis_tick_font = dict(
            size=int(style["axis_tick_size"]),
            color=str(style["axis_tick_color"]),
            family=str(style["family"]),
        )
        if x_lbl:
            f2.update_xaxes(title_text=x_lbl, title_font=axis_title_font)
        else:
            f2.update_xaxes(title_font=axis_title_font)
        if y_lbl:
            f2.update_yaxes(title_text=y_lbl, title_font=axis_title_font)
        else:
            f2.update_yaxes(title_font=axis_title_font)
        f2.update_xaxes(tickfont=axis_tick_font)
        f2.update_yaxes(tickfont=axis_tick_font)
        return f2
    except Exception:
        return fig


def _apply_legend_names(fig, legend_names: dict, legend_title: str = "", text_style: dict | None = None):
    """
    Rename Plotly traces using the {original_name: custom_name} mapping stored
    in chart_meta, and optionally override the legend group title.

    Works on any figure type (histogram, bar, scatter, line, etc.).
    Traces whose names are not in the mapping are left unchanged.
    Pass legend_title="" (or omit) to leave the auto-generated title as-is.
    """
    try:
        f2 = copy.deepcopy(fig)
        style = _merge_text_style(text_style)
        legend_font = dict(
            size=int(style["legend_item_size"]),
            color=str(style["legend_item_color"]),
            family=str(style["family"]),
        )
        legend_title_font = dict(
            size=int(style["legend_title_size"]),
            color=str(style["legend_title_color"]),
            family=str(style["family"]),
        )
        if legend_names:
            for trace in f2.data:
                original = getattr(trace, "name", None)
                if original is not None and str(original) in legend_names:
                    custom = legend_names[str(original)]
                    if custom:
                        trace.name = custom
        if legend_title:
            f2.update_layout(
                legend_title_text=legend_title,
                legend=dict(font=legend_font, title=dict(font=legend_title_font)),
            )
        elif legend_title == "":
            f2.update_layout(legend=dict(font=legend_font, title=dict(font=legend_title_font)))
        return f2
    except Exception:
        return fig


def _all_charts(viewing_saved):
    if viewing_saved:
        return st.session_state.get("_view_charts", [])
    out = []
    for uid, title, fig in st.session_state.get("charts", []):
        desc   = st.session_state.get(f"desc_{uid}", "")
        autos  = st.session_state.get(f"auto_insights_{uid}", [])
        ctype  = st.session_state.get(f"chart_type_{uid}", "")
        meta   = _meta(uid)
        out.append((uid, title, fig, desc, autos, ctype, meta))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# KPI Engine  (auto-calculated -- Power BI style)
# ─────────────────────────────────────────────────────────────────────────────
_KPI_TYPES = [
    "Total (Sum)", "Average (Mean)", "Median", "Count (Rows)",
    "Minimum Value", "Maximum Value",
    "% of Total (category share)", "Unique Values Count",
    "Date Range", "Top Category → Value", "Bottom Category → Value",
    "% Change (Latest Month vs Prev Month)", "% Change (Latest Year vs Prev Year)",
]
_KPI_ICONS = {
    "Total (Sum)":"📜","Average (Mean)":"📊","Median":"📐","Count (Rows)":"🔢",
    "Minimum Value":"⬇️","Maximum Value":"⬆️",
    "% of Total (category share)":"📈","Unique Values Count":"🔍",
    "Date Range":"📅","Top Category → Value":"🏆","Bottom Category → Value":"📉",
    "% Change (Latest Month vs Prev Month)":"📅","% Change (Latest Year vs Prev Year)":"📅",
}


def _calc_kpi(df, kpi_type, col=None, group_col=None, metric_col=None,
              filter_col=None, filter_val=None, label=None):
    num_c = df.select_dtypes(include="number").columns.tolist()
    icon  = _KPI_ICONS.get(kpi_type, "📊")
    val   = "--"
    lbl   = label or kpi_type
    pfx   = sfx = ""
    try:
        if kpi_type == "Total (Sum)" and col in num_c:
            v = df[col].sum()
            val = _fmt_num(v)
            lbl = label or f"Total {col}"
        elif kpi_type == "Average (Mean)" and col in num_c:
            val = _fmt_num(df[col].mean()); lbl = label or f"Avg {col}"
        elif kpi_type == "Median" and col in num_c:
            val = _fmt_num(df[col].median()); lbl = label or f"Median {col}"
        elif kpi_type == "Count (Rows)":
            val = _fmt_num(len(df)); lbl = label or "Total Records"
        elif kpi_type == "Minimum Value" and col in num_c:
            val = _fmt_num(df[col].min()); lbl = label or f"Min {col}"
        elif kpi_type == "Maximum Value" and col in num_c:
            val = _fmt_num(df[col].max()); lbl = label or f"Max {col}"
        elif kpi_type == "Unique Values Count" and col:
            val = _fmt_num(df[col].nunique()); lbl = label or f"Unique {col}"
        elif kpi_type == "Date Range" and col:
            dates = pd.to_datetime(df[col], errors="coerce").dropna()
            if len(dates):
                val = f"{dates.min().strftime('%d %b %y')} → {dates.max().strftime('%d %b %y')}"
            lbl = label or f"Range of {col}"
        elif kpi_type == "% of Total (category share)" and col in num_c and filter_col and filter_val:
            tot = df[col].sum()
            sub = df[df[filter_col].astype(str) == str(filter_val)][col].sum()
            val = f"{sub/tot*100:.1f}" if tot else "0.0"; sfx = "%"
            lbl = label or f"{filter_val} share"
        elif kpi_type == "Top Category → Value" and group_col and metric_col in num_c:
            grp = df.groupby(group_col)[metric_col].sum()
            val = f"{grp.idxmax()}: {_fmt_num(grp.max())}"; lbl = label or f"Top {group_col}"
        elif kpi_type == "Bottom Category → Value" and group_col and metric_col in num_c:
            grp = df.groupby(group_col)[metric_col].sum()
            val = f"{grp.idxmin()}: {_fmt_num(grp.min())}"; lbl = label or f"Bottom {group_col}"
        elif kpi_type == "% Change (Latest Month vs Prev Month)" and col in num_c and filter_col:
            dates = pd.to_datetime(df[filter_col], errors="coerce")
            df2 = df.copy(); df2["_dt"] = dates; df2 = df2.dropna(subset=["_dt"])
            latest_m = df2["_dt"].dt.to_period("M").max()
            prev_m   = latest_m - 1
            cur_val  = df2[df2["_dt"].dt.to_period("M") == latest_m][col].sum()
            prev_val = df2[df2["_dt"].dt.to_period("M") == prev_m][col].sum()
            pct = (cur_val - prev_val) / abs(prev_val) * 100 if prev_val else 0
            val = f"{'+' if pct >= 0 else ''}{pct:.1f}"; sfx = "%"
            lbl = label or f"MoM {col}"
            return {"icon": icon, "label": lbl, "value": val, "prefix": pfx, "suffix": sfx,
                    "change_pct": float(pct)}
        elif kpi_type == "% Change (Latest Year vs Prev Year)" and col in num_c and filter_col:
            dates = pd.to_datetime(df[filter_col], errors="coerce")
            df2 = df.copy(); df2["_dt"] = dates; df2 = df2.dropna(subset=["_dt"])
            latest_y = df2["_dt"].dt.year.max()
            cur_val  = df2[df2["_dt"].dt.year == latest_y][col].sum()
            prev_val = df2[df2["_dt"].dt.year == (latest_y - 1)][col].sum()
            pct = (cur_val - prev_val) / abs(prev_val) * 100 if prev_val else 0
            val = f"{'+' if pct >= 0 else ''}{pct:.1f}"; sfx = "%"
            lbl = label or f"YoY {col}"
            return {"icon": icon, "label": lbl, "value": val, "prefix": pfx, "suffix": sfx,
                    "change_pct": float(pct)}
    except Exception as e:
        val = f"Err: {e}"
    return {"icon":icon,"label":lbl,"value":val,"prefix":pfx,"suffix":sfx}


def _kpi_card_html(kpi):
    change_pct = kpi.get("change_pct")
    arrow_html = ""
    if change_pct is not None:
        is_pos  = change_pct >= 0
        color   = "#10b981" if is_pos else "#ef4444"
        arrow   = "▲" if is_pos else "▼"
        arrow_html = (
            f'<div style="font-size:0.78rem;font-weight:700;color:{color};margin-top:3px;">'
            f'{arrow} {abs(change_pct):.1f}% vs prior period</div>'
        )
    icon   = escape(str(kpi.get("icon", "📊")))
    value  = escape(str(kpi.get("value", "--")))
    prefix = escape(str(kpi.get("prefix", "")))
    suffix = escape(str(kpi.get("suffix", "")))
    label  = escape(str(kpi.get("label", "")))

    _UNIT_META = {
        "B": ("B", "Billions",  "#8566fc"),
        "M": ("M", "Millions",  "#6163df"),
        "K": ("K", "Thousands", "#3390c8"),
    }
    unit_html = ""
    num_part  = value
    for unit, (badge_label, title, color) in _UNIT_META.items():
        if (value.endswith(unit) and len(value) > 1
                and value[:-1].replace(".", "").replace("-", "").isdigit()):
            num_part  = value[:-1]
            unit_html = (
                f'<span title="{title}" style="' 
                f'display:inline-block;margin-left:4px;' 
                f'font-size:0.72rem;font-weight:700;' 
                f'background:{color}22;color:{color};' 
                f'border:1px solid {color}55;' 
                f'border-radius:5px;padding:1px 5px;' 
                f'vertical-align:middle;letter-spacing:0.04em;' 
                f'">{badge_label}</span>'
            )
            break

    full_val    = f"{prefix}{value}{suffix}"
    is_long     = len(full_val) > 14
    val_style   = (
        "font-size:0.95rem;font-weight:800;color:#6163df;line-height:1.25;"
        "margin-top:4px;word-break:break-word;overflow-wrap:anywhere;"
    ) if is_long else (
        "font-size:1.15rem;font-weight:800;color:#6163df;line-height:1.2;"
        "margin-top:4px;white-space:nowrap;"
    )
    val_display = f"{prefix}{num_part}{unit_html}{suffix}"

    return (
        f'<div style="background:rgba(97,99,223,0.07);border:1px solid rgba(97,99,223,0.18);' 
        f'border-radius:12px;padding:0.7rem 0.9rem;text-align:center;' 
        f'width:100%;box-shadow:0 2px 8px rgba(0,0,0,0.06);flex:1;">' 
        f'<div style="font-size:1.2rem;line-height:1">{icon}</div>' 
        f'<div style="{val_style}">{val_display}</div>' 
        f'{arrow_html}' 
        f'<div style="font-size:0.63rem;opacity:0.6;text-transform:uppercase;' 
        f'letter-spacing:.07em;margin-top:4px;font-weight:600">{label}</div>' 
        f'</div>' 
    )


def _render_kpi_section(df, readonly):
    if "kpis" not in st.session_state:
        st.session_state.kpis = []

    st.markdown("### 📌 KPI Cards")

    # ── Display existing ──────────────────────────────────────────────────────
    kpis = st.session_state.kpis
    if kpis:
        cols_per_row = 4
        rows = [kpis[i:i+cols_per_row] for i in range(0, len(kpis), cols_per_row)]
        for row in rows:
            rcols = st.columns(len(row))
            for ci, (kpi, rc) in enumerate(zip(row, rcols)):
                with rc:
                    st.markdown(_kpi_card_html(kpi), unsafe_allow_html=True)
                    if not readonly:
                        gi = kpis.index(kpi)
                        if st.button("✕", key=f"kpi_rm_{gi}", help="Remove KPI",
                                     use_container_width=True):
                            kpis.pop(gi)
                            _dash_sync_notes()
                            _persist()
                            st.rerun()
        st.markdown("<br>", unsafe_allow_html=True)

    # ── Add new KPI ───────────────────────────────────────────────────────────
    if not readonly and df is not None:
        with st.expander("➕ Add KPI from Dataset", expanded=len(kpis) == 0):
            num_c = df.select_dtypes(include="number").columns.tolist()
            cat_c = df.select_dtypes(include=["object","category"]).columns.tolist()
            all_c = df.columns.tolist()

            ka, kb = st.columns(2)
            with ka:
                ktype  = st.selectbox("KPI Type", _KPI_TYPES, key="kpi_type")
            with kb:
                klabel = st.text_input("Custom Label (leave blank for auto)",
                                       key="kpi_label",
                                       placeholder="e.g. Total Revenue")

            col = grp = met = fcol = fval = None

            simple_num = {"Total (Sum)","Average (Mean)","Median",
                          "Minimum Value","Maximum Value"}
            if ktype in simple_num:
                col = st.selectbox("Numeric column", num_c, key="kpi_col")
            elif ktype == "Unique Values Count":
                col = st.selectbox("Column", all_c, key="kpi_col2")
            elif ktype == "Date Range":
                dt_c = [c for c in all_c if any(x in c.lower() for x in
                        ("date","time","dt","year","month"))] or all_c
                col  = st.selectbox("Date column", dt_c, key="kpi_dt")
            elif ktype == "% of Total (category share)":
                p1, p2, p3 = st.columns(3)
                with p1: col  = st.selectbox("Numeric col", num_c, key="kpi_pc")
                with p2: fcol = st.selectbox("Filter col",  cat_c, key="kpi_fc") if cat_c else None
                if fcol:
                    uniq = df[fcol].dropna().unique().tolist()
                    with p3: fval = st.selectbox("Filter value", uniq, key="kpi_fv")
            elif ktype in ("Top Category → Value","Bottom Category → Value"):
                g1, g2 = st.columns(2)
                with g1: grp = st.selectbox("Category col", cat_c, key="kpi_grp") if cat_c else None
                with g2: met = st.selectbox("Metric col", num_c,   key="kpi_met") if num_c else None
            elif ktype in ("% Change (Latest Month vs Prev Month)",
                           "% Change (Latest Year vs Prev Year)"):
                dt_c = [c for c in all_c if any(x in c.lower() for x in
                        ("date","time","dt","year","month"))] or all_c
                p1, p2 = st.columns(2)
                with p1: fcol = st.selectbox("Date column",   dt_c,  key="kpi_chg_dt")
                with p2: col  = st.selectbox("Metric column", num_c, key="kpi_chg_met") if num_c else None

            if st.button("➕ Calculate & Add KPI", type="primary", key="kpi_add_btn"):
                kpi = _calc_kpi(df, ktype, col, grp, met, fcol, fval, klabel or None)
                st.session_state.kpis.append(kpi)
                _dash_sync_notes()
                _persist()
                # Also write KPIs to the sessions table immediately so they are
                # not lost if the user closes the tab before clicking Save/Update.
                eid = st.session_state.get("editing_session_id")
                if eid:
                    try:
                        from modules.database import update_session_db
                        update_session_db(
                            eid,
                            st.session_state.get("editing_session_name", "Session"),
                            charts_to_json(st.session_state.get("charts", [])),
                            st.session_state.get("selected_analyses", []),
                            st.session_state.get("user_id"),
                            dashboard_title = st.session_state.get("dashboard_title", ""),
                            kpis_json       = json.dumps(st.session_state.kpis),
                            layout_mode     = st.session_state.get("layout_mode", "portrait"),
                        )
                    except Exception:
                        pass
                st.success(f"✅ {kpi['label']}: {kpi['value']}{kpi['suffix']}")
                st.rerun()
    elif not readonly:
        st.caption("Upload a dataset and go to Analysis first to enable KPI calculation.")


# ─────────────────────────────────────────────────────────────────────────────
# Visual Grid Layout Builder
# ─────────────────────────────────────────────────────────────────────────────
def _render_layout_builder(charts):
    """
    Visual grid layout builder. Supports 2-column (default) and independent
    3-column grid mode, each slot with a full-width toggle.
    """
    if not charts:
        return []

    n = len(charts)
    uid_list   = [c[0] for c in charts]
    title_map  = {c[0]: c[1] for c in charts}
    EMPTY      = "(empty)"
    opts       = [EMPTY] + [f"[{uid}] {title_map[uid][:45]}" for uid in uid_list]
    uid_of_opt = {f"[{uid}] {title_map[uid][:45]}": uid for uid in uid_list}

    if "grid_order" not in st.session_state or \
            set(st.session_state.grid_order) != set(uid_list):
        st.session_state.grid_order     = uid_list.copy()
        st.session_state.grid_fullwidth = {}

    order      = list(st.session_state.grid_order)
    full_width = dict(st.session_state.grid_fullwidth)

    st.markdown("### 🗂️ Arrange Charts in Dashboard Grid")

    # ── Independent column-count selector ────────────────────────────────────
    grid_cols_n = st.radio(
        "Grid columns",
        [2, 3],
        index=0 if st.session_state.get("grid_cols_n", 2) == 2 else 1,
        horizontal=True,
        format_func=lambda x: f"{x}-Column Grid",
        key="grid_cols_radio",
    )
    st.session_state.grid_cols_n = grid_cols_n

    st.caption(
        f"Each row has **{grid_cols_n} slots**. "
        "Tick **Full Width** to span the first slot across the entire row.")

    st.markdown(
        '<div style="background:rgba(97,99,223,0.04);border:2px dashed rgba(97,99,223,0.25);'
        'border-radius:16px;padding:1.2rem 1.4rem;margin-bottom:1rem;">',
        unsafe_allow_html=True)

    assigned_uids = []
    seen = set()
    max_rows = n  # worst case: every chart is full-width

    for row_i in range(max_rows):
        base      = row_i * grid_cols_n
        slot_uids = [
            order[base + s] if (base + s) < len(order) else None
            for s in range(grid_cols_n)
        ]
        slot_opts = [
            (f"[{u}] {title_map.get(u,'')[:45]}" if u and u in title_map else EMPTY)
            for u in slot_uids
        ]

        is_fw = full_width.get(slot_uids[0], False) if slot_uids[0] else False

        st.markdown(
            f'<div style="font-size:0.75rem;font-weight:700;color:#64748b;'
            f'text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">'
            f'Row {row_i + 1}</div>',
            unsafe_allow_html=True)

        # columns: N slots + 1 narrow full-width toggle
        col_parts = st.columns([5] * grid_cols_n + [2])
        fw_here = col_parts[-1].checkbox(
            "Full width", value=is_fw, key=f"grid_fw_{row_i}",
            help="Span first slot's chart across the entire row")

        chosen_slots = []
        for s in range(grid_cols_n):
            if fw_here and s > 0:
                col_parts[s].markdown(
                    '<div style="height:38px;display:flex;align-items:center;'
                    'background:rgba(97,99,223,0.06);border-radius:8px;'
                    'justify-content:center;font-size:0.8rem;opacity:0.5;">'
                    '← Full width →</div>', unsafe_allow_html=True)
                chosen_slots.append(EMPTY)
            else:
                chosen = col_parts[s].selectbox(
                    f"Slot {row_i + 1}{chr(65 + s)}",
                    opts,
                    index=opts.index(slot_opts[s]) if slot_opts[s] in opts else 0,
                    key=f"grid_s{s}_{row_i}",
                    label_visibility="collapsed")
                chosen_slots.append(chosen)

        lu = uid_of_opt.get(chosen_slots[0])
        if lu and lu not in seen:
            assigned_uids.append(lu)
            seen.add(lu)
            full_width[lu] = fw_here

        if not fw_here:
            for s in range(1, grid_cols_n):
                ru = uid_of_opt.get(chosen_slots[s])
                if ru and ru not in seen:
                    assigned_uids.append(ru)
                    seen.add(ru)

        if all(uid_of_opt.get(c) is None for c in chosen_slots):
            break
        if len(seen) >= n:
            break

    st.markdown('</div>', unsafe_allow_html=True)

    for uid in uid_list:
        if uid not in seen:
            assigned_uids.append(uid)

    if st.button("✅ Apply Layout", type="primary", key="apply_layout"):
        st.session_state.grid_order     = assigned_uids
        st.session_state.grid_fullwidth = full_width
        st.session_state.grid_cols_n    = grid_cols_n
        _dash_sync_notes()
        _persist()
        st.success("✅ Layout applied!")
        st.rerun()

    return assigned_uids


# ─────────────────────────────────────────────────────────────────────────────
# Per-chart settings panel
# ─────────────────────────────────────────────────────────────────────────────
def _chart_settings(uid, title, fig, auto_insights, readonly):
    meta = _meta(uid) if not readonly else {}

    with st.expander("⚙️ Chart Settings", expanded=False):
        if readonly:
            st.caption("Read-only in view mode.")
            return

        chart_type = st.session_state.get(f"chart_type_{uid}", "")
        updates = render_chart_settings_controls(
            uid, title, fig, chart_type, meta, auto_insights,
            key_prefix="dash",
            show_text_style=True,
        )

        if st.button("💾 Save Settings", key=f"save_{uid}", type="primary"):
            _set_meta(uid, **updates)
            # Also update the stored chart title tuple so the list header matches
            charts = st.session_state.get("charts", [])
            st.session_state.charts = [
                (c[0], updates["custom_title"] if c[0] == uid else c[1], c[2])
                for c in charts
            ]
            _dash_sync_notes()
            _persist()
            st.success("Saved!")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Single chart card
# ─────────────────────────────────────────────────────────────────────────────
def _render_chart(item, idx, total, viewing_saved):
    uid, title, fig, desc, autos, ctype, saved_meta = \
        item if len(item) == 7 else (*item[:6], {})
    meta = saved_meta if viewing_saved else _meta(uid)
    note_key = f"desc_{uid}"
    if not viewing_saved:
        if note_key not in st.session_state or st.session_state[note_key] == "":
            # Shadow has priority: it holds values typed since the last DB save
            # and survives KPI-section reruns that fire before chart cards render.
            shadow_val = st.session_state.get("_notes_shadow", {}).get(uid, "")
            st.session_state[note_key] = shadow_val or desc or ""

    display = meta.get("custom_title") or title
    sub     = meta.get("subtitle","")
    xl      = meta.get("x_label","")
    yl      = meta.get("y_label","")
    text_style = _merge_text_style(meta.get("text_style", {}))

    fig_show = _apply_axes(fig, xl, yl, text_style)
    fig_show = _apply_legend_names(fig_show, meta.get("legend_names", {}), meta.get("legend_title", ""), text_style)
    fig_show = apply_chart_display_options(fig_show, meta, ctype)
    # Prepare display figure -- apply axis labels, legend names, and styling but do NOT embed
    # the title inside the Plotly figure (it is rendered as a heading above the
    # chart instead, so it only appears once).
    try:
        import copy as _copy
        fig_show = _copy.deepcopy(fig_show)

        # Apply K/M/B hover formatting and hover label styling to the display copy.
        apply_hover_format(fig_show)

        if sub:
            safe_sub = escape(str(sub))
            fig_show.update_layout(title=dict(
                text=(
                    f'<sup style="font-size:{text_style["subtitle_size"]}px;'
                    f'color:{text_style["subtitle_color"]};font-family:{text_style["family"]}">'
                    f'{safe_sub}</sup>'
                ),
                font=dict(size=int(text_style["subtitle_size"])),
            ))
        else:
            fig_show.update_layout(title_text="")

        # ── Axis readability: angle x-tick labels, expand margin
        is_horiz = any(getattr(t, "orientation", "v") == "h"
                       for t in fig_show.data if hasattr(t, "orientation"))
        tick_font = dict(
            size=int(text_style["axis_tick_size"]),
            color=str(text_style["axis_tick_color"]),
            family=str(text_style["family"]),
        )
        if is_horiz:
            fig_show.update_yaxes(tickfont=tick_font, automargin=True)
            fig_show.update_xaxes(tickfont=tick_font)
            fig_show.update_layout(margin=dict(l=120, r=20, t=28, b=20))
        else:
            fig_show.update_xaxes(tickangle=-35, tickfont=tick_font, automargin=True)
            fig_show.update_yaxes(tickfont=tick_font, automargin=True)
            fig_show.update_layout(margin=dict(l=20, r=20, t=28, b=80))
    except Exception:
        pass

    # ── Control buttons (edit mode only) ─────────────────────────────────────
    if not viewing_saved:
        btn_cols = st.columns([9, 1, 1, 1])
        with btn_cols[1]:
            if idx > 0 and st.button("⬆", key=f"up_{uid}"):
                cl = st.session_state.get("charts",[])
                i  = next((j for j,c in enumerate(cl) if c[0]==uid),-1)
                if i > 0:
                    cl[i-1],cl[i] = cl[i],cl[i-1]
                    go = st.session_state.get("grid_order",[])
                    gi = next((j for j,u in enumerate(go) if u==uid),-1)
                    if gi > 0: go[gi-1],go[gi] = go[gi],go[gi-1]
                    _dash_sync_notes(); _persist(); st.rerun()
        with btn_cols[2]:
            if idx < total-1 and st.button("⬇", key=f"dn_{uid}"):
                cl = st.session_state.get("charts",[])
                i  = next((j for j,c in enumerate(cl) if c[0]==uid),-1)
                if i >= 0 and i < len(cl)-1:
                    cl[i],cl[i+1] = cl[i+1],cl[i]
                    go = st.session_state.get("grid_order",[])
                    gi = next((j for j,u in enumerate(go) if u==uid),-1)
                    if gi >= 0 and gi < len(go)-1: go[gi],go[gi+1] = go[gi+1],go[gi]
                    _dash_sync_notes(); _persist(); st.rerun()
        with btn_cols[3]:
            if st.button("🗑", key=f"rm_{uid}"):
                st.session_state.charts = [c for c in st.session_state.get("charts",[])
                                           if c[0] != uid]
                if "grid_order" in st.session_state:
                    st.session_state.grid_order = [u for u in st.session_state.grid_order
                                                   if u != uid]
                st.session_state.get("_notes_shadow", {}).pop(uid, None)
                _dash_sync_notes(); _persist(); st.rerun()

    # ── Chart title rendered once as a heading (not inside Plotly) ───────────
    st.markdown(
        f'<div style="font-size:0.93rem;font-weight:700;color:#1e293b;margin-bottom:2px;">'
        f'{escape(str(display))}</div>'
        + (f'<div style="font-size:0.78rem;color:#64748b;margin-bottom:4px;">'
           f'{escape(str(sub))}</div>' if sub else ""),
        unsafe_allow_html=True)

    # Scrollable wrapper for large matrix tables (same logic as analysis.py)
    _db_chart_type = st.session_state.get(f"chart_type_{uid}", ctype or "")
    _db_fig_height = getattr(getattr(fig_show, "layout", None), "height", 0) or 0
    _db_is_matrix = _db_chart_type == "matrix_table"
    if _db_is_matrix:
        st.markdown(
            '<div style="max-height:540px;overflow-y:auto;overflow-x:auto;'
            'border:1px solid rgba(100,116,139,0.2);border-radius:6px;'
            'padding-bottom:4px;">',
            unsafe_allow_html=True,
        )
    st.plotly_chart(
        fig_show,
        use_container_width=not _db_is_matrix,
        # key= prevents full Plotly iframe teardown on every rerun (glitch fix).
        key=f"dash_plotly_{uid}",
        config={
            "responsive": True,
            "displayModeBar": "hover",
            # Disable Plotly's attempt to load MathJax from CDN
            "mathjax": False,
        },
    )
    if _db_is_matrix:
        st.markdown("</div>", unsafe_allow_html=True)

    # Insights
    show_ai = meta.get("show_auto_insights", True)
    hidden  = set(meta.get("hidden_insights",[]))
    if autos and show_ai:
        visible = [ins for i,ins in enumerate(autos) if i not in hidden]
        if visible:
            with st.expander("💡 Insights", expanded=False):
                for ins in visible: st.markdown(f"- {clean_insight_text(ins)}")

    # Analysis notes are independent of auto-insights and always export/save.
    live_desc = st.session_state.get(note_key, "") if not viewing_saved else (desc or "")
    if viewing_saved:
        if live_desc:
            safe_desc = escape(str(live_desc))
            st.markdown(
                f'<div style="background:rgba(133,102,252,0.07);border-left:3px solid #8566fc;'
                f'border-radius:6px;padding:.6rem .9rem;font-size:.87rem;margin-top:.3rem;">'
                f'<strong>Analysis Notes:</strong> {safe_desc}</div>', unsafe_allow_html=True)
    else:
        def _sync_note(u=uid):   # default-arg captures uid by value
            val = st.session_state.get(f"desc_{u}", "")
            st.session_state.setdefault("_notes_shadow", {})[u] = val
        st.text_area(
            "✍️ Analysis Notes",
            key=note_key,
            on_change=_sync_note,
            placeholder="Add your findings or observations here…")
        if "editing_session_id" in st.session_state:
            if st.button("💾 Update Session Notes", key=f"update_notes_{uid}",
                         use_container_width=True):
                _do_update(
                    st.session_state.get("editing_session_name", "Session"),
                    st.session_state.get("charts", []),
                    clear_editing=False)
                st.rerun()

    _chart_settings(uid, title, fig, autos, viewing_saved)


# ─────────────────────────────────────────────────────────────────────────────
# Grid renderer -- respects grid_order and grid_fullwidth
# ─────────────────────────────────────────────────────────────────────────────
def _render_grid(ordered_charts, viewing_saved):
    total    = len(ordered_charts)
    fw       = st.session_state.get("grid_fullwidth", {})
    n_cols   = st.session_state.get("grid_cols_n", 2)  # 2 or 3
    i = 0
    while i < total:
        item     = ordered_charts[i]
        uid      = item[0]
        item_meta = item[6] if viewing_saved and len(item) > 6 else _meta(uid)
        is_fw    = fw.get(uid, False) or item_meta.get("full_width", False)

        if is_fw or i == total - 1:
            # Full-width or lone last chart
            with st.container():
                _render_chart(item, i, total, viewing_saved)
            st.markdown("<br>", unsafe_allow_html=True)
            i += 1
        else:
            # Try to fill a full row of n_cols
            row_items = [item]
            for s in range(1, n_cols):
                if i + s < total:
                    ni   = ordered_charts[i + s]
                    n_fw = fw.get(ni[0], False) or (
                        ni[6] if viewing_saved and len(ni) > 6 else _meta(ni[0])
                    ).get("full_width", False)
                    if not n_fw:
                        row_items.append(ni)
                    else:
                        break
                else:
                    break

            if len(row_items) > 1:
                row_cols = st.columns(len(row_items), gap="large")
                for ci, (ri, rc) in enumerate(zip(row_items, row_cols)):
                    with rc:
                        _render_chart(ri, i + ci, total, viewing_saved)
                st.markdown("<br>", unsafe_allow_html=True)
                i += len(row_items)
            else:
                with st.container():
                    _render_chart(item, i, total, viewing_saved)
                st.markdown("<br>", unsafe_allow_html=True)
                i += 1


# ─────────────────────────────────────────────────────────────────────────────
# Main page entry
# ─────────────────────────────────────────────────────────────────────────────
def page_dashboard():
    # Token is validated in app.py on startup and kept in the URL so that
    # a browser page-refresh re-validates and restores the session.
    # If there is no authenticated session, redirect to profile.
    if "user_id" not in st.session_state:
        st.session_state.page = "profile"
        st.rerun()

    viewing_saved = "view_session_id" in st.session_state
    is_editing    = "editing_session_id" in st.session_state
    df            = st.session_state.get("df")

    # ── When editing a saved session, restore its KPIs + meta on first load ──
    if is_editing and "kpis" not in st.session_state:
        eid = st.session_state.editing_session_id
        sm  = get_session_meta(eid, st.session_state.get("user_id"))
        if sm:
            try:
                st.session_state.kpis = json.loads(sm.get("kpis_json", "[]"))
            except Exception:
                st.session_state.kpis = []
            if "layout_mode" not in st.session_state:
                st.session_state.layout_mode = sm.get("layout_mode", "portrait")
            if "dashboard_title" not in st.session_state:
                st.session_state.dashboard_title = sm.get("dashboard_title", "")

    # ── When editing, also restore per-chart notes from the saved session ─────
    if is_editing and not st.session_state.get("_edit_notes_loaded"):
        eid    = st.session_state.editing_session_id
        loaded = get_session_charts(eid, st.session_state.get("user_id"))
        for uid, title, fig, desc, auto, ctype, meta in loaded:
            note_key = f"desc_{uid}"
            # Seed note if:
            #   a) key doesn't exist yet, OR
            #   b) key is empty string (analysis.py pre-seeds it as "" before
            #      we get here, which previously blocked the saved value loading)
            # Never overwrite if user has already typed something non-empty.
            current_note = st.session_state.get(note_key, None)
            if desc and (current_note is None or current_note == ""):
                st.session_state[note_key] = desc
            # Restore chart meta (custom title, subtitle etc.) if not set
            meta_key = f"chart_meta_{uid}"
            if meta and not st.session_state.get(meta_key):
                st.session_state[meta_key] = meta
        st.session_state._edit_notes_loaded = True

    # Load saved session data once
    if viewing_saved:
        sid   = st.session_state.view_session_id
        sname = st.session_state.get("view_session_name","Saved Session")
        if "_view_charts" not in st.session_state or \
                st.session_state.get("_vsid") != sid:
            loaded = get_session_charts(sid, st.session_state.get("user_id"))
            # Restore per-chart session_state keys (previously done inside DB layer)
            for uid, title, fig, desc, auto, ctype, meta in loaded:
                st.session_state[f"desc_{uid}"]          = desc
                st.session_state[f"auto_insights_{uid}"] = auto
                st.session_state[f"chart_type_{uid}"]    = ctype
                st.session_state[f"chart_meta_{uid}"]    = meta
            st.session_state._view_charts = loaded
            st.session_state._vsid        = sid
        sm = get_session_meta(sid, st.session_state.get("user_id"))
        if sm is None:
            st.error("That saved session was not found for this account.")
            for k in ["view_session_id","_view_charts","_vsid",
                      "dashboard_title","kpis","layout_mode"]:
                st.session_state.pop(k, None)
            st.session_state.page = "home"
            st.rerun()
        st.session_state.setdefault("dashboard_title", sm["dashboard_title"])
        st.session_state.setdefault("layout_mode",     sm["layout_mode"])
        if "kpis" not in st.session_state:
            try:   st.session_state.kpis = json.loads(sm["kpis_json"])
            except Exception: st.session_state.kpis = []
        df = None  # No live df when viewing saved
    else:
        sname = f"Analysis -- {st.session_state.get('file_name','')}"

    charts = _all_charts(viewing_saved)

    render_logo()
    if st.button("← Back"):
        if viewing_saved:
            for k in ["view_session_id","_view_charts","_vsid",
                      "dashboard_title","kpis","layout_mode"]:
                st.session_state.pop(k, None)
            st.session_state.page = "home"
        else:
            st.session_state.page = "analysis"
        st.rerun()

    # ── Dashboard title ───────────────────────────────────────────────────────
    if not viewing_saved:
        ti = st.text_input("📋 Dashboard Title",
                           value=st.session_state.get("dashboard_title",""),
                           placeholder="e.g. Q1 2025 Sales Dashboard",
                           key="dbtitle")
        if ti != st.session_state.get("dashboard_title",""):
            st.session_state.dashboard_title = ti; _persist()

    display_title = st.session_state.get("dashboard_title") or sname
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    header_meta = _default_text_style()
    try:
        if charts:
            first_meta = charts[0][6] if len(charts[0]) > 6 else _meta(charts[0][0])
            header_meta = _merge_text_style(first_meta.get("text_style", {}))
    except Exception:
        pass
    st.markdown(
        f'<div style="text-align:center;margin-bottom:0.3rem;">'
        f'<span style="font-size:{header_meta["header_size"] / 16:.2f}rem;'
        f'font-weight:800;color:{header_meta["header_color"]};'
        f'font-family:{header_meta["family"]};">📊 {escape(display_title)}</span><br>'
        f'<span style="font-size:{header_meta["subtitle_size"] / 16:.2f}rem;'
        f'color:{header_meta["subtitle_color"]};font-family:{header_meta["family"]};">'
        f'Generated by Lytrize &middot; {now_str}</span>'
        f'</div>',
        unsafe_allow_html=True)

    # ── Layout mode ───────────────────────────────────────────────────────────
    if not viewing_saved:
        lo = st.radio("📐 Export Layout", ["Portrait","Landscape"],
                      index=1 if st.session_state.get("layout_mode")=="landscape" else 0,
                      horizontal=True)
        st.session_state.layout_mode = lo.lower()

    # ── Save / Update -- at the TOP so it's always visible ────────────────────
    if not viewing_saved:
        sc1, sc2, sc3 = st.columns([3, 1, 1])
        with sc1:
            def_name = st.session_state.get("editing_session_name", sname) if is_editing else sname
            sname_in = st.text_input("Session name", value=def_name, key="sname_in")
        with sc2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("💾 Save", use_container_width=True):
                _do_save(sname_in, charts, df)
        with sc3:
            if is_editing:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("🔄 Update", use_container_width=True):
                    _do_update(sname_in, charts)

    st.markdown("---")

    # ── KPIs ─────────────────────────────────────────────────────────────────
    _render_kpi_section(df, readonly=viewing_saved)
    st.markdown("---")

    if not charts:
        st.info("No charts yet. Go back to Analysis to generate some!")
        inject_footer()
        return

    # Resolve grid order — deduplicate to prevent DuplicateWidgetID errors
    # if the same uid somehow appears twice in grid_order (e.g. rapid clicks).
    uid_map   = {c[0]: c for c in charts}
    go_order  = st.session_state.get("grid_order", [c[0] for c in charts])
    seen_uids: set = set()
    ordered   = []
    for u in go_order:
        if u in uid_map and u not in seen_uids:
            ordered.append(uid_map[u])
            seen_uids.add(u)
    # Append unplaced charts not in grid_order at all
    for c in charts:
        if c[0] not in seen_uids:
            ordered.append(c)
            seen_uids.add(c[0])

    # ── Export ────────────────────────────────────────────────────────────────
    if ordered:
        _export_row(ordered, sname, viewing_saved)
        st.markdown("---")

    # ── Layout builder ────────────────────────────────────────────────────────
    if charts and not viewing_saved:
        with st.expander("🗂️ Arrange Dashboard Layout", expanded=False):
            _render_layout_builder(charts)
        st.markdown("---")

    st.markdown("### 📈 Dashboard")
    try:
        _render_grid(ordered, viewing_saved)
    except Exception as _render_err:
        err_msg = str(_render_err)
        if "DuplicateWidgetID" in err_msg or "duplicate" in err_msg.lower():
            st.warning(
                "⚠️ A chart appears more than once in the layout. "
                "Open **Arrange Dashboard Layout** above and make sure each "
                "chart is assigned to only one slot, then click **Apply Layout**.",
                icon=None)
        else:
            st.error(f"Dashboard render error: {err_msg}")
    inject_footer()


def _export_row(charts, sname, viewing_saved):
    orient     = st.session_state.get("layout_mode","portrait")
    kpis       = st.session_state.get("kpis",[])
    dash_title = st.session_state.get("dashboard_title","") or sname

    export_charts = []
    full_width = st.session_state.get("grid_fullwidth", {})
    for item in charts:
        uid  = item[0]
        meta = dict(item[6] if len(item)>6 else _meta(uid))
        if full_width.get(uid):
            meta["full_width"] = True
        style = _merge_text_style(meta.get("text_style", {}))
        fig  = _apply_axes(item[2], meta.get("x_label",""), meta.get("y_label",""), style)
        fig  = _apply_legend_names(fig, meta.get("legend_names", {}), meta.get("legend_title", ""), style)
        fig  = apply_chart_display_options(fig, meta, item[5] if len(item)>5 else "")
        # Read notes from session_state live so they're always current
        notes = st.session_state.get(f"desc_{uid}", "") or (item[3] if len(item) > 3 else "")
        export_charts.append((uid, item[1], fig, notes, item[4] if len(item)>4 else [],
                              item[5] if len(item)>5 else "", meta))

    safe_file = re.sub(r"[^A-Za-z0-9_.-]+", "_", dash_title).strip("._") or "lytrize_report"

    # ── Export colour customisation ───────────────────────────────────────────
    # Preset pattern: buttons write to _ex_pending (a plain dict key, not a
    # widget key) and rerun. On the NEXT rerun, we read _ex_pending BEFORE
    # rendering any color_picker widgets so Streamlit never sees a widget-key
    # mutation mid-run (which raises StreamlitAPIException).
    _EX_DEFAULTS = {
        "ex_bg": "#121a2e", "ex_card": "#1b2245", "ex_kpi": "#1b2245",
        "ex_accent": "#6163df", "ex_border": "#2c3564", "ex_text": "#f5f7ff",
        "ex_ins_bg": "#1a2441", "ex_ins_bd": "#6163df",
        "ex_not_bg": "#1a1732", "ex_not_bd": "#8566fc",
        "ex_density": "Comfortable", "ex_radius": 12, "ex_chart_h": 400,
        "ex_width": "Auto", "ex_meta": True, "ex_print_hint": True,
    }
    _PRESETS = {
        "🌙 Dark":    {"ex_bg":"#121a2e","ex_card":"#1b2245","ex_kpi":"#1b2245",
                       "ex_accent":"#6163df","ex_border":"#2c3564","ex_text":"#f5f7ff",
                       "ex_ins_bg":"#1a2441","ex_ins_bd":"#6163df",
                       "ex_not_bg":"#1a1732","ex_not_bd":"#8566fc"},
        "🟣 Purple":  {"ex_bg":"#121a2e","ex_card":"#1b2245","ex_kpi":"#1b2245",
                       "ex_accent":"#8566fc","ex_border":"#3b4581","ex_text":"#f5f7ff",
                       "ex_ins_bg":"#1a2441","ex_ins_bd":"#8566fc",
                       "ex_not_bg":"#1a1732","ex_not_bd":"#c084fc"},
        "🟢 Corporate":{"ex_bg":"#f0fdf4","ex_card":"#ffffff","ex_kpi":"#f0fdf4",
                        "ex_accent":"#16a34a","ex_border":"#bbf7d0","ex_text":"#14532d",
                        "ex_ins_bg":"#dcfce7","ex_ins_bd":"#16a34a",
                        "ex_not_bg":"#f7fee7","ex_not_bd":"#65a30d"},
    }

    # Apply any pending preset BEFORE widgets are rendered this rerun.
    if "_ex_pending" in st.session_state:
        for k, v in st.session_state["_ex_pending"].items():
            st.session_state[k] = v          # safe — widgets not yet created
        del st.session_state["_ex_pending"]

    with st.expander("🎨 Customise Export Colours", expanded=False):
        st.caption(
            "Set colours for the downloaded HTML dashboard. "
            "Changes are preview-only — they apply to the downloaded file."
        )
        # ── Quick preset row (rendered BEFORE pickers so clicks queue _ex_pending) ──
        st.markdown("**Quick presets:**")
        pr_cols = st.columns(len(_PRESETS))
        for col, (label, vals) in zip(pr_cols, _PRESETS.items()):
            if col.button(label, key=f"preset_{label[:3]}"):
                st.session_state["_ex_pending"] = vals   # queued; applied next rerun
                st.rerun()

        tab_colours, tab_layout = st.tabs(["Colours", "Layout"])
        with tab_colours:
            ec1, ec2, ec3 = st.columns(3)
            with ec1:
                ex_bg     = st.color_picker("Page background",     st.session_state.get("ex_bg",     _EX_DEFAULTS["ex_bg"]),     key="ex_bg")
                ex_card   = st.color_picker("Chart card fill",     st.session_state.get("ex_card",   _EX_DEFAULTS["ex_card"]),   key="ex_card")
                ex_kpi    = st.color_picker("KPI card fill",       st.session_state.get("ex_kpi",    _EX_DEFAULTS["ex_kpi"]),    key="ex_kpi")
            with ec2:
                ex_accent = st.color_picker("Accent / headings",   st.session_state.get("ex_accent", _EX_DEFAULTS["ex_accent"]), key="ex_accent")
                ex_border = st.color_picker("Card border",         st.session_state.get("ex_border", _EX_DEFAULTS["ex_border"]), key="ex_border")
                ex_text   = st.color_picker("Body text",           st.session_state.get("ex_text",   _EX_DEFAULTS["ex_text"]),   key="ex_text")
            with ec3:
                ex_ins_bg = st.color_picker("Insights background", st.session_state.get("ex_ins_bg", _EX_DEFAULTS["ex_ins_bg"]), key="ex_ins_bg")
                ex_ins_bd = st.color_picker("Insights border",     st.session_state.get("ex_ins_bd", _EX_DEFAULTS["ex_ins_bd"]), key="ex_ins_bd")
                ex_not_bg = st.color_picker("Notes background",    st.session_state.get("ex_not_bg", _EX_DEFAULTS["ex_not_bg"]), key="ex_not_bg")
                ex_not_bd = st.color_picker("Notes border",        st.session_state.get("ex_not_bd", _EX_DEFAULTS["ex_not_bd"]), key="ex_not_bd")
        with tab_layout:
            density_options = ["Compact", "Comfortable", "Spacious"]
            _density_value = st.session_state.get("ex_density", _EX_DEFAULTS["ex_density"])
            if _density_value not in density_options:
                _density_value = _EX_DEFAULTS["ex_density"]
            ex_density = st.selectbox(
                "Spacing density",
                density_options,
                index=density_options.index(_density_value),
                key="ex_density",
            )
            l1, l2, l3 = st.columns(3)
            with l1:
                ex_radius = st.slider("Card radius", 0, 24, int(st.session_state.get("ex_radius", _EX_DEFAULTS["ex_radius"])), key="ex_radius")
                ex_chart_h = st.slider("Chart height", 280, 720, int(st.session_state.get("ex_chart_h", _EX_DEFAULTS["ex_chart_h"])), 20, key="ex_chart_h")
            with l2:
                width_options = ["Auto", "Narrow", "Wide", "Full"]
                _width_value = st.session_state.get("ex_width", _EX_DEFAULTS["ex_width"])
                if _width_value not in width_options:
                    _width_value = _EX_DEFAULTS["ex_width"]
                ex_width = st.selectbox(
                    "Page width",
                    width_options,
                    index=width_options.index(_width_value),
                    key="ex_width",
                )
            with l3:
                ex_meta = st.checkbox("Show generated timestamp", value=bool(st.session_state.get("ex_meta", _EX_DEFAULTS["ex_meta"])), key="ex_meta")
                ex_print_hint = st.checkbox("Show print hint", value=bool(st.session_state.get("ex_print_hint", _EX_DEFAULTS["ex_print_hint"])), key="ex_print_hint")

    _density = {
        "Compact": {"gap": 14, "padding": 20},
        "Comfortable": {"gap": 24, "padding": 32},
        "Spacious": {"gap": 34, "padding": 44},
    }.get(st.session_state.get("ex_density", _EX_DEFAULTS["ex_density"]), {"gap": 24, "padding": 32})
    _width = {
        "Auto": "",
        "Narrow": "960px",
        "Wide": "1440px",
        "Full": "100%",
    }.get(st.session_state.get("ex_width", _EX_DEFAULTS["ex_width"]), "")

    export_theme = {
        "bg_color":       ex_bg,     "card_bg":        ex_card,
        "kpi_bg":         ex_kpi,    "accent_color":   ex_accent,
        "card_border":    ex_border, "text_color":     ex_text,
        "insight_bg":     ex_ins_bg, "insight_border": ex_ins_bd,
        "notes_bg":       ex_not_bg, "notes_border":   ex_not_bd,
        "card_radius":    st.session_state.get("ex_radius", _EX_DEFAULTS["ex_radius"]),
        "gap":            _density["gap"],
        "body_padding":   _density["padding"],
        "chart_height":   st.session_state.get("ex_chart_h", _EX_DEFAULTS["ex_chart_h"]),
        "max_width":      _width,
        "show_meta":      st.session_state.get("ex_meta", _EX_DEFAULTS["ex_meta"]),
        "show_print_hint": st.session_state.get("ex_print_hint", _EX_DEFAULTS["ex_print_hint"]),
    }

    html = generate_html_report(
        export_charts, sname,
        orientation=orient, kpis=kpis,
        dashboard_title=dash_title,
        grid_cols_n=st.session_state.get("grid_cols_n", 2),
        theme=export_theme,
    )
    c1, c2 = st.columns([2, 5])
    with c1:
        st.download_button("⬇️ Download HTML", html,
                           file_name=f"{safe_file}.html",
                           mime="text/html", use_container_width=True)
    with c2:
        st.info(
            "💡 To save as PDF: open the downloaded HTML in your browser → "
            "**Ctrl+P** → **Save as PDF** (set margins to **None** & enable "
            "**Background Graphics** for best results)"
        )


def _do_save(sname_in, charts, df):
    if "user_id" not in st.session_state:
        return
    save_session_db(
        st.session_state.user_id, sname_in,
        st.session_state.get("file_name",""),
        df.shape[0] if df is not None else 0,
        df.shape[1] if df is not None else 0,
        st.session_state.get("selected_analyses",[]),
        charts_to_json(st.session_state.get("charts",[])),
        dashboard_title = st.session_state.get("dashboard_title",""),
        kpis_json       = json.dumps(st.session_state.get("kpis",[])),
        layout_mode     = st.session_state.get("layout_mode","portrait"))
    clear_draft(st.session_state.user_id)
    st.session_state.pop("editing_session_id",    None)
    st.session_state.pop("editing_session_name",  None)
    st.session_state.pop("_edit_notes_loaded",    None)
    st.session_state.pop("_analysis_notes_loaded", None)
    st.session_state.pop("_notes_shadow",          None)
    # Navigate home so the user immediately sees the new session in the list.
    # get_user_sessions cache is already cleared by save_session_db.
    st.toast(f"✅ Saved as '{sname_in}'!", icon="✅")
    st.session_state.page = "home"
    st.rerun()


def _do_update(sname_in, charts, clear_editing=True):
    # Clear the notes-loaded flag so the next dashboard visit re-seeds desc_
    # keys from the DB values we are about to write. This ensures notes are
    # always in sync with what was actually saved.
    st.session_state.pop("_edit_notes_loaded",      None)
    st.session_state.pop("_analysis_notes_loaded",  None)
    st.session_state.pop("_notes_shadow",           None)
    eid = st.session_state.editing_session_id
    update_session_db(
        eid, sname_in,
        charts_to_json(st.session_state.get("charts",[])),
        st.session_state.get("selected_analyses",[]),
        st.session_state.user_id,
        dashboard_title = st.session_state.get("dashboard_title",""),
        kpis_json       = json.dumps(st.session_state.get("kpis",[])),
        layout_mode     = st.session_state.get("layout_mode","portrait"))
    clear_draft(st.session_state.user_id)
    st.toast(f"✅ Updated '{sname_in}'!", icon="✅")
    if clear_editing:
        st.session_state.pop("editing_session_id",   None)
        st.session_state.pop("editing_session_name", None)
        st.session_state.page = "home"
        st.rerun()
