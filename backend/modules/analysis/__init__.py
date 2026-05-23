"""
modules/analysis/__init__.py -- Analysis registry & configuration layer.

Owns three things:
  1. ANALYSIS_OPTIONS  -- analysis cards shown on the analysis page (order = render order).
  2. _RUNNERS          -- maps each analysis ID → its runner function.
  3. render_config_panel / _collect_kwargs -- config UI: draws widgets, reads them back.

"""

import uuid
import streamlit as st

# ── Individual runner imports ─────────────────────────────────────────────────
# Each module lives in modules/analysis/<name>.py
from modules.analysis.descriptive  import run_descriptive
from modules.analysis.statistical  import run_statistical
from modules.analysis.distribution import run_distribution
from modules.analysis.correlation  import run_correlation
from modules.analysis.categorical  import run_categorical
from modules.analysis.pie_chart    import run_pie_chart
from modules.analysis.time_series  import run_time_series
from modules.analysis.outlier       import run_outlier, OUTLIER_HELP
from modules.analysis.scatter_plot  import run_scatter_plot
from modules.analysis.matrix_table  import run_matrix_table
from modules.analysis.map_plot      import run_map_plot
from modules.analysis.insights      import generate_insights  
# run_data_quality is imported directly in pages/upload.py

# Column-list helpers -- read from session_state (set during upload/classify)
from modules.charts import PALETTES, num_cols as _num_cols, cat_cols as _cat_cols, dt_cols as _dt_cols


# ─────────────────────────────────────────────────────────────────────────────
# Analysis registry
# ─────────────────────────────────────────────────────────────────────────────

# ANALYSIS_OPTIONS drives the card grid on the analysis page.
# To add a new analysis: append a dict here AND add an entry to _RUNNERS below.
ANALYSIS_OPTIONS = [
    {"id": "descriptive",  "icon": "🗂️", "name": "Descriptive",      "desc": "Summary statistics"},
    {"id": "statistical",  "icon": "📐", "name": "Statistical",       "desc": "Mean, std, min, max"},
    {"id": "distribution", "icon": "📊", "name": "Distribution",      "desc": "Histograms & boxplots"},
    {"id": "correlation",  "icon": "🔗", "name": "Correlation",       "desc": "Correlation matrix"},
    {"id": "categorical",  "icon": "🏷️", "name": "Categorical Bar",   "desc": "Bar charts"},
    {"id": "pie_chart",    "icon": "🍩", "name": "Pie & Donut",       "desc": "Share of total"},
    {"id": "time_series",  "icon": "⏱️", "name": "Time Series",       "desc": "Trends over time"},
    {"id": "scatter_plot", "icon": "📉",  "name": "Scatter Plot",      "desc": "Variable relationships"},
    {"id": "matrix_table", "icon": "🔲", "name": "Matrix / Heatmap",  "desc": "Cross-tab heatmap"},
    {"id": "map_plot",     "icon": "🗺️", "name": "Map Plot",          "desc": "Geographic scatter"},
]
_RUNNERS = {
    "descriptive":  run_descriptive,
    "statistical":  run_statistical,
    "distribution": run_distribution,
    "correlation":  run_correlation,
    "categorical":  run_categorical,
    "pie_chart":    run_pie_chart,
    "time_series":  run_time_series,
    "outlier":      run_outlier,
    "scatter_plot": run_scatter_plot,
    "matrix_table": run_matrix_table,
    "map_plot":     run_map_plot,
}

# Analyses that need axis/column selection via the config panel.
_NEEDS_AXES = {"statistical", "distribution", "correlation", "categorical",
               "pie_chart", "time_series", "scatter_plot", "matrix_table", "map_plot"}

# Reserved for future analyses that must bypass the standard st.form() wrapper.
_NO_FORM = set()

# ── Aggregation function labels → pandas method strings ───────────────────────
_AGG_FUNCS = {
    "Avg": "mean",
    "Sum":        "sum",
    "Median":     "median",
    "Count":      "count",
    "Min":        "min",
    "Max":        "max",
}

# ── Date-part grouping labels → pandas period/alias strings ───────────────────
# None means "use the raw date column without any grouping".
_DATE_PARTS = {
    "None":           None,
    "Year":           "Y",
    "Quarter":        "Q",
    "Month (number)": "M",
    "Month Name":     "month_name",    # special-cased in time_series.py
    "Weekday Name":   "weekday_name",  # special-cased in time_series.py
    "Day":            "D",
    "Hour":           "H",
}


# ─────────────────────────────────────────────────────────────────────────────
# Session-state helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sk(aid: str, key: str) -> str:
    """
    Build a namespaced session_state key for a widget inside analysis `aid`.

    Using a consistent naming scheme prevents key collisions between different
    analysis types sharing widget names like "x" or "palette".

    Example: _sk("categorical", "top_n")  →  "_cfg_categorical_top_n"
    """
    return f"_cfg_{aid}_{key}"


def _g(aid: str, key: str, default=None):
    """
    Read a widget value from session_state, falling back to `default`.

    Args:
        aid:     Analysis ID (e.g. "categorical").
        key:     Widget key suffix (e.g. "top_n").
        default: Value returned when the key is not yet in session_state.
    """
    return st.session_state.get(_sk(aid, key), default)


# ─────────────────────────────────────────────────────────────────────────────
# Scoped helpers -- per-chart-uid key prefix so multiple panels never collide
# ─────────────────────────────────────────────────────────────────────────────

def _sk_uid(uid: str, aid: str, key: str) -> str:
    """Namespaced key scoped to a specific chart uid for the regenerate panel."""
    return f"_edit_{uid}_{aid}_{key}"


def _g_uid(uid: str, aid: str, key: str, default=None):
    return st.session_state.get(_sk_uid(uid, aid, key), default)


def render_config_panel_scoped(uid: str, aid: str, df) -> None:
    """
    Same as render_config_panel() but every widget key is scoped to `uid` so
    multiple charts can have independent regeneration panels without collision.
    """
    num, cat, dt, all_cols = _num_cols(), _cat_cols(), _dt_cols(), df.columns.tolist()
    NONE = "None"
    sk = lambda key: _sk_uid(uid, aid, key)   # noqa: E731

    # Colour Palette only applies to bar/scatter/line charts.
    # For matrix_table: heatmap uses its own colorscale; table header uses first palette colour.
    if aid != "matrix_table":
        st.selectbox("🎨 Colour Palette", list(PALETTES.keys()), key=sk("palette"))
    else:
        st.info(
            "🎨 **Heatmap:** Colorscale is set in Chart Settings → Colour scale. "
            "**Table:** accent palette controls the header colour.",
            icon=None,
        )
        st.selectbox("🎨 Table Header Accent", list(PALETTES.keys()), key=sk("palette"))
    st.markdown("---")

    if aid == "statistical":
        c1, c2, c3 = st.columns(3)
        with c1: st.multiselect("Group by (optional)", cat, max_selections=1, key=sk("x"))
        with c2: st.multiselect("Metrics", num, default=num[:4], key=sk("y"))
        with c3: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=sk("agg"))

    elif aid == "distribution":
        c1, c2 = st.columns(2)
        with c1: st.multiselect("Numeric columns", num, default=num[:4], key=sk("x"))
        with c2: st.multiselect("Colour by (optional)", cat, max_selections=1, key=sk("color"))

    elif aid == "correlation":
        c1, c2 = st.columns(2)
        with c1: st.multiselect("Columns", num, default=num, key=sk("x"))
        with c2: st.multiselect("Additional (optional)", num, key=sk("y"))

    elif aid in ("categorical", "pie_chart"):
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.multiselect("Dimension columns", cat, default=cat[:2], key=sk("x"))
        with c2: st.multiselect("Metric columns (optional)", num, key=sk("y"))
        with c3: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=sk("agg"))
        with c4: st.selectbox("Sort", ["Value ↓","Value ↑","Category A→Z","Category Z→A"], key=sk("sort"))
        st.markdown("---")
        if aid == "categorical":
            st.selectbox("📊 Chart Direction",
                         ["Vertical (Column chart)", "Horizontal (Bar chart)"],
                         key=sk("direction"))
        st.markdown("**🔝 Top N Categories**")
        st.caption("0 = show all categories")
        st.number_input("Top N (0 = show all)", min_value=0, max_value=200,
                        step=1, value=0, key=sk("top_n"))
        if aid == "categorical":
            st.markdown("---")
            st.markdown("**📊 Dual Y-Axis (Secondary metric as line overlay)**")
            dual_opts = [NONE] + list(num)
            st.selectbox("Secondary Y-Axis metric", dual_opts, key=sk("dual_y"))
            d2a, _ = st.columns([1, 2])
            with d2a:
                st.selectbox("Secondary metric aggregation",
                             list(_AGG_FUNCS.keys()), key=sk("dual_y_agg"),
                             help="Independent aggregation for the secondary Y-axis metric.")

    elif aid == "time_series":
        dt_candidates = dt if dt else all_cols
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.multiselect("Date / Time column", dt_candidates,
                           default=dt_candidates[:1] if dt_candidates else [],
                           max_selections=1, key=sk("x"))
        with c2: st.multiselect("Primary metric(s)", num, default=num[:2], key=sk("y"))
        with c3: st.selectbox("Date grouping", list(_DATE_PARTS.keys()), key=sk("date_part"))
        with c4: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=sk("agg"))
        st.markdown("---")
        dual_opts_ts = [NONE] + list(num)
        ts_d1, ts_d2, _ = st.columns([2, 1, 1])
        with ts_d1:
            st.selectbox("Secondary Y-Axis metric", dual_opts_ts, key=sk("dual_y_ts"))
        with ts_d2:
            st.selectbox("Secondary metric aggregation",
                         list(_AGG_FUNCS.keys()), key=sk("dual_y_agg"),
                         help="Independent aggregation for the secondary Y-axis metric.")

    elif aid == "scatter_plot":
        sp1, sp2 = st.columns(2)
        with sp1: st.selectbox("X Axis (numeric)", [NONE] + num, key=sk("x_col"))
        with sp2: st.selectbox("Y Axis (numeric)", [NONE] + num, key=sk("y_col"))
        sp3, sp4 = st.columns(2)
        with sp3: st.selectbox("Colour by (optional)", [NONE] + cat, key=sk("color_col"))
        with sp4: st.selectbox("Size by (optional)", [NONE] + num, key=sk("size_col"))
        st.selectbox("Trendline", ["None", "ols", "lowess"], key=sk("trendline"))

    elif aid == "matrix_table":
        mt1, mt2, mt3 = st.columns(3)
        with mt1: st.selectbox("Row (Index column)", [NONE] + cat, key=sk("index_col"))
        with mt2: st.selectbox("Column dimension", [NONE] + cat, key=sk("columns_col"))
        with mt3: st.selectbox("Value column", [NONE] + num, key=sk("values_col"))
        mt4, mt5, mt6, mt7 = st.columns(4)
        with mt4: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=sk("agg"))
        with mt5: st.selectbox("View type", ["Heatmap", "Table"], key=sk("view_type"))
        with mt6: st.selectbox("Sort rows by", ["Value ↓", "Value ↑", "Category A→Z", "Category Z→A"],
                               key=sk("sort_rows"),
                               help="Sort index rows by their aggregated value or alphabetically")
        with mt7: st.number_input("Top N rows (0 = all)", min_value=0, max_value=500,
                                  step=5, value=0, key=sk("top_n_rows"),
                                  help="Limit to the top N rows after sorting. 0 shows all rows.")

    elif aid == "map_plot":
        from modules.analysis.map_plot import _CHOROPLETH_SCALES, _PROJECTIONS, _SCOPES, detect_geo_column
        # ── Mode selector ─────────────────────────────────────────────────
        _map_mode_opts = ["Scatter (Lat/Lon)", "Choropleth (Location Names)"]
        # Cache detect_geo_column per df (identified by shape+columns hash) so it
        # doesn't re-scan pycountry lookups on every widget interaction rerun.
        _df_sig = f"{df.shape}_{list(df.columns)}"
        _geo_cache_key = "_detected_geo_col"
        _geo_sig_key   = "_detected_geo_sig"
        if (st.session_state.get(_geo_sig_key) != _df_sig
                or _geo_cache_key not in st.session_state):
            st.session_state[_geo_cache_key] = detect_geo_column(df)
            st.session_state[_geo_sig_key]   = _df_sig
        _detected_geo  = st.session_state[_geo_cache_key]
        _default_mode  = 1 if _detected_geo else 0
        st.selectbox("Map mode", _map_mode_opts, index=_default_mode, key=sk("map_mode"),
                     help="Scatter = numeric lat/lon columns. Choropleth = country/state name column.")
        _mode = st.session_state.get(sk("map_mode"), _map_mode_opts[_default_mode])
        if _mode == "Scatter (Lat/Lon)":
            mp1, mp2 = st.columns(2)
            with mp1: st.selectbox("Latitude column", [NONE] + num, key=sk("lat_col"))
            with mp2: st.selectbox("Longitude column", [NONE] + num, key=sk("lon_col"))
            mp3, mp4 = st.columns(2)
            with mp3: st.selectbox("Location label (hover name)", [NONE] + cat, key=sk("location_col"))
            with mp4: st.selectbox("Colour by", [NONE] + cat + num, key=sk("color_col"),
                                   help="Categorical → discrete colours  ·  Numeric → palette gradient")
            mp5, mp6 = st.columns(2)
            with mp5: st.selectbox("Size by (optional)", [NONE] + num, key=sk("size_col"))
            with mp6: st.selectbox("Value column (aggregated in hover)", [NONE] + num, key=sk("value_col"))
            mp7, mp8 = st.columns(2)
            with mp7: st.selectbox("Map style", ["carto-positron", "open-street-map", "carto-darkmatter"],
                                   key=sk("map_style"))
            with mp8: st.selectbox("Aggregation (when location+value set)", list(_AGG_FUNCS.keys()), key=sk("agg_func"))
            mp9, mp10 = st.columns(2)
            with mp9:  st.slider("Marker opacity", 0.3, 1.0, 0.82, 0.05, key=sk("marker_opacity"))
            with mp10: st.checkbox("🔄 Invert colour scale", key=sk("invert_colorscale"))
        else:
            _all_cols_str = [c for c in df.columns if df[c].dtype == object]
            _geo_default_idx = (_all_cols_str.index(_detected_geo)
                                if _detected_geo and _detected_geo in _all_cols_str else 0)
            st.selectbox("Location name column (countries / US states)",
                         _all_cols_str if _all_cols_str else df.columns.tolist(),
                         index=_geo_default_idx, key=sk("geo_col"),
                         help="Column containing country names, ISO-2/3 codes, or US state names/abbrevs")
            cg1, cg2 = st.columns(2)
            with cg1: st.selectbox("Value column (fill colour)", [NONE] + num, key=sk("value_col"))
            with cg2: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=sk("agg_func"))
            cg3, cg4 = st.columns(2)
            with cg3: st.selectbox("Colour scale", _CHOROPLETH_SCALES, key=sk("choropleth_colorscale"))
            with cg4: st.selectbox("Projection", _PROJECTIONS, key=sk("choropleth_projection"))
            cg5, cg6 = st.columns(2)
            with cg5: st.selectbox("Scope", _SCOPES, key=sk("choropleth_scope"))
            with cg6: st.checkbox("Show country borders", value=True, key=sk("choropleth_show_borders"))
            st.checkbox("🔄 Invert colour scale", key=sk("invert_colorscale"))


def _collect_kwargs_scoped(uid: str, aid: str, df) -> dict:
    """Same as _collect_kwargs() but reads from uid-scoped widget keys."""
    num, cat, dt, all_cols = _num_cols(), _cat_cols(), _dt_cols(), df.columns.tolist()
    NONE = "None"
    g = lambda key, default=None: _g_uid(uid, aid, key, default)   # noqa: E731
    _sort_map = {
        "Value ↓": "Value (Desc)", "Value ↑": "Value (Asc)",
        "Category A→Z": "Category (A-Z)", "Category Z→A": "Category (Z-A)",
    }

    pal_label = g("palette", list(PALETTES.keys())[0])
    kwargs = {"palette": PALETTES.get(pal_label, list(PALETTES.values())[0])}

    if aid == "statistical":
        kwargs.update(x_cols=g("x",[]) or None, y_cols=g("y",num[:4]) or num,
                      agg=_AGG_FUNCS.get(g("agg","Mean (Avg)"), "mean"))
    elif aid == "distribution":
        color = g("color",[])
        kwargs.update(x_cols=g("x",num[:4]) or num[:4], y_cols=color or None)
    elif aid == "correlation":
        kwargs.update(x_cols=g("x",num) or num, y_cols=g("y",[]) or None)
    elif aid in ("categorical","pie_chart"):
        x = g("x",cat[:2]) or cat[:2]
        y = g("y",[]) or None
        agg = _AGG_FUNCS.get(g("agg","Mean (Avg)"), "mean")
        top_n_v = int(g("top_n",0) or 0)
        top_n = top_n_v if top_n_v > 0 else None
        sort_by = _sort_map.get(g("sort","Value ↓"), "Value (Desc)")
        kwargs.update(x_cols=x, y_cols=y, agg=agg, sort_by=sort_by, top_n=top_n)
        if aid == "categorical":
            direction = g("direction","Vertical (Column chart)")
            raw_dual = g("dual_y", NONE)
            dual_y = None if (not raw_dual or raw_dual == NONE) else raw_dual
            if dual_y and y and dual_y in (y if isinstance(y,list) else [y]):
                dual_y = None
            dual_y_agg = _AGG_FUNCS.get(g("dual_y_agg", "Mean (Avg)"), "mean") if dual_y else None
            kwargs.update(direction=direction, dual_y_col=dual_y, dual_y_agg=dual_y_agg)
    elif aid == "time_series":
        x = g("x",[])
        y = g("y",num[:2]) or num[:2]
        agg = _AGG_FUNCS.get(g("agg","Mean (Avg)"), "mean")
        date_part = _DATE_PARTS.get(g("date_part","None"))
        raw_dual = g("dual_y_ts", NONE)
        dual_y = None if (not raw_dual or raw_dual == NONE) else raw_dual
        if dual_y and dual_y in (y if isinstance(y,list) else [y]):
            dual_y = None
        dual_y_agg = _AGG_FUNCS.get(g("dual_y_agg", "Mean (Avg)"), "mean") if dual_y else None
        kwargs.update(x_cols=x or None, y_cols=y, agg=agg,
                      date_part=date_part, dual_y_col=dual_y, dual_y_agg=dual_y_agg)

    elif aid == "scatter_plot":
        def _sp_r(key):
            v = g(key, NONE)
            return None if v in (NONE, None, "") else v
        kwargs.update(
            x_col=_sp_r("x_col"), y_col=_sp_r("y_col"),
            color_col=_sp_r("color_col"), size_col=_sp_r("size_col"),
            trendline=g("trendline", "None"),
        )

    elif aid == "matrix_table":
        def _mt_r(key):
            v = g(key, NONE)
            return None if v in (NONE, None, "") else v
        _mt_sort_map = {
            "Value ↓": "value_desc", "Value ↑": "value_asc",
            "Category A→Z": "cat_asc", "Category Z→A": "cat_desc",
        }
        _mt_top_n_v = int(g("top_n_rows", 0) or 0)
        kwargs.update(
            index_col=_mt_r("index_col"), columns_col=_mt_r("columns_col"),
            values_col=_mt_r("values_col"),
            agg=_AGG_FUNCS.get(g("agg", "Mean (Avg)"), "mean"),
            view_type=g("view_type", "Heatmap"),
            sort_rows=_mt_sort_map.get(g("sort_rows", "Value ↓"), "value_desc"),
            top_n_rows=_mt_top_n_v if _mt_top_n_v > 0 else None,
        )

    elif aid == "map_plot":
        def _mp_r(key):
            v = g(key, NONE)
            return None if v in (NONE, None, "") else v
        _mp_mode_s = g("map_mode", "Scatter (Lat/Lon)")
        if _mp_mode_s == "Choropleth (Location Names)":
            kwargs.update(
                geo_col=_mp_r("geo_col"),
                value_col=_mp_r("value_col"),
                agg_func=_AGG_FUNCS.get(g("agg_func", "Sum"), "sum"),
                invert_colorscale=bool(g("invert_colorscale", False)),
                choropleth_colorscale=g("choropleth_colorscale", "Blues"),
                choropleth_projection=g("choropleth_projection", "natural earth"),
                choropleth_scope=g("choropleth_scope", "world"),
                choropleth_show_borders=bool(g("choropleth_show_borders", True)),
            )
        else:
            kwargs.update(
                lat_col=_mp_r("lat_col"), lon_col=_mp_r("lon_col"),
                location_col=_mp_r("location_col"), color_col=_mp_r("color_col"),
                size_col=_mp_r("size_col"), value_col=_mp_r("value_col"),
                agg_func=_AGG_FUNCS.get(g("agg_func", "Mean (Avg)"), "mean"),
                invert_colorscale=bool(g("invert_colorscale", False)),
                map_style=g("map_style", "carto-positron"),
                marker_opacity=float(g("marker_opacity", 0.82)),
            )

    return kwargs


# ─────────────────────────────────────────────────────────────────────────────
# Configuration panel -- widget rendering
# ─────────────────────────────────────────────────────────────────────────────

def render_config_panel(aid: str, df) -> None:
    """
    Render configuration widgets for the analysis identified by `aid`.

    Design rules:
      - ALL widgets are ALWAYS visible -- no show/hide conditionals that react
        to other widget values during the same rerun. This avoids the Streamlit
        "missing key" crash caused by widgets appearing/disappearing mid-rerun.
      - Every widget uses _sk(aid, key) as its Streamlit key so values persist
        across reruns and are accessible via _collect_kwargs().
      - This function returns nothing; the caller reads config via _collect_kwargs().

    Args:
        aid: Analysis ID string (e.g. "categorical", "time_series").
        df:  The working DataFrame from st.session_state.df.
    """
    num, cat, dt, all_cols = _num_cols(), _cat_cols(), _dt_cols(), df.columns.tolist()
    NONE = "None"  # Sentinel string used in "no secondary column" selectboxes.

    # Colour palette selector — shown for all analyses except matrix_table heatmap
    # (heatmap uses its own independent colorscale set in Chart Settings).
    if aid != "matrix_table":
        st.selectbox("🎨 Colour Palette", list(PALETTES.keys()), key=_sk(aid, "palette"))
    else:
        st.info(
            "🎨 **Heatmap:** colorscale is set in Chart Settings → Colour scale. "
            "**Table:** accent palette controls the header colour.",
            icon=None,
        )
        st.selectbox("🎨 Table Header Accent", list(PALETTES.keys()), key=_sk(aid, "palette"))
    st.markdown("---")

    # ── Descriptive ───────────────────────────────────────────────────────────
    # No configuration needed -- the runner outputs a full pandas describe() table.
    if aid == "descriptive":
        st.info("No configuration needed -- outputs a full stats table.")

    # ── Statistical ───────────────────────────────────────────────────────────
    # Aggregates numeric metrics, optionally grouped by one categorical column.
    elif aid == "statistical":
        c1, c2, c3 = st.columns(3)
        with c1: st.multiselect("Group by (optional)", cat, max_selections=1, key=_sk(aid, "x"))
        with c2: st.multiselect("Metrics", num, default=num[:4], key=_sk(aid, "y"))
        with c3: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=_sk(aid, "agg"))

    # ── Distribution ──────────────────────────────────────────────────────────
    # Histograms with box-plot marginals for each selected numeric column.
    elif aid == "distribution":
        c1, c2 = st.columns(2)
        with c1: st.multiselect("Numeric columns", num, default=num[:4], key=_sk(aid, "x"))
        with c2: st.multiselect("Colour by (optional)", cat, max_selections=1, key=_sk(aid, "color"))

    # ── Correlation ───────────────────────────────────────────────────────────
    # Pearson correlation heatmap. Requires at least 2 numeric columns.
    elif aid == "correlation":
        c1, c2 = st.columns(2)
        with c1: st.multiselect("Columns", num, default=num, key=_sk(aid, "x"))
        with c2: st.multiselect("Additional (optional)", num, key=_sk(aid, "y"))

    # ── Categorical Bar & Pie / Donut ─────────────────────────────────────────
    # Both share dimension / metric / aggregation / sort selectors.
    # Categorical adds direction (vertical/horizontal) and dual Y-axis.
    # Pie adds "Other" grouping for categories beyond Top N.
    elif aid in ("categorical", "pie_chart"):
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.multiselect("Dimension columns", cat, default=cat[:2], key=_sk(aid, "x"))
        with c2: st.multiselect("Metric columns (optional)", num, key=_sk(aid, "y"))
        with c3: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=_sk(aid, "agg"))
        with c4: st.selectbox(
            "Sort", ["Value ↓", "Value ↑", "Category A→Z", "Category Z→A"],
            key=_sk(aid, "sort"))
        st.markdown("---")

        # Categorical-only: chart orientation toggle.
        if aid == "categorical":
            st.selectbox(
                "📊 Chart Direction",
                ["Vertical (Column chart)", "Horizontal (Bar chart)"],
                key=_sk(aid, "direction"),
                help="Vertical = column chart. Horizontal = bar chart with values outside tips.")

        # Top-N -- always a number_input (0 = show all).
        # Using number_input instead of a conditional checkbox avoids key-missing crashes.
        st.markdown("**🔝 Top N Categories**")
        st.caption("Enter how many top categories to show. Set to 0 to show all categories.")
        st.number_input(
            "Top N (0 = show all)", min_value=0, max_value=200, step=1, value=0,
            key=_sk(aid, "top_n"),
            help="0 = no limit. e.g. 10 = show only the 10 highest-value categories.")

        # Dual Y-axis -- categorical only; selectbox with "None" sentinel.
        if aid == "categorical":
            st.markdown("---")
            st.markdown("**📊 Dual Y-Axis (Secondary metric as line overlay)**")
            st.caption("Choose a secondary metric to overlay as a line on a second Y-axis. Select 'None' to disable.")
            dual_opts = [NONE] + list(num)
            cat_d1, cat_d2, _ = st.columns([2, 1, 1])
            with cat_d1:
                st.selectbox(
                    "Secondary Y-Axis metric", dual_opts, key=_sk(aid, "dual_y"),
                    help="Primary metric → bars. Secondary metric → line on the right Y-axis.")
            with cat_d2:
                st.selectbox(
                    "Secondary metric aggregation", list(_AGG_FUNCS.keys()),
                    key=_sk(aid, "dual_y_agg"),
                    help="Independent aggregation applied only to the secondary Y-axis metric.")



    # ── Scatter Plot ──────────────────────────────────────────────────────────
    elif aid == "scatter_plot":
        sp1, sp2 = st.columns(2)
        with sp1: st.selectbox("X Axis (numeric)", [NONE] + num, key=_sk(aid, "x_col"))
        with sp2: st.selectbox("Y Axis (numeric)", [NONE] + num, key=_sk(aid, "y_col"))
        sp3, sp4 = st.columns(2)
        with sp3: st.selectbox("Colour by (optional)", [NONE] + cat, key=_sk(aid, "color_col"))
        with sp4: st.selectbox("Size by (optional)", [NONE] + num, key=_sk(aid, "size_col"))
        st.selectbox("Trendline", ["None", "ols", "lowess"], key=_sk(aid, "trendline"),
                     help="ols = linear regression line  ·  lowess = smoothed curve")

    # ── Matrix Heatmap / Pivot Table ──────────────────────────────────────────
    elif aid == "matrix_table":
        mt1, mt2, mt3 = st.columns(3)
        with mt1: st.selectbox("Row (Index column)", [NONE] + cat, key=_sk(aid, "index_col"))
        with mt2: st.selectbox("Column dimension", [NONE] + cat, key=_sk(aid, "columns_col"))
        with mt3: st.selectbox("Value column", [NONE] + num, key=_sk(aid, "values_col"))
        mt4, mt5, mt6, mt7 = st.columns(4)
        with mt4: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=_sk(aid, "agg"))
        with mt5: st.selectbox("View type", ["Heatmap", "Table"], key=_sk(aid, "view_type"))
        with mt6: st.selectbox("Sort rows by", ["Value ↓", "Value ↑", "Category A→Z", "Category Z→A"],
                               key=_sk(aid, "sort_rows"),
                               help="Sort index rows by their aggregated value or alphabetically")
        with mt7: st.number_input("Top N rows (0 = all)", min_value=0, max_value=500,
                                  step=5, value=0, key=_sk(aid, "top_n_rows"),
                                  help="Limit to the top N rows after sorting. 0 shows all rows.")

    # ── Map Plot ──────────────────────────────────────────────────────────────
    elif aid == "map_plot":
        from modules.analysis.map_plot import _CHOROPLETH_SCALES, _PROJECTIONS, _SCOPES, detect_geo_column
        _map_mode_opts = ["Scatter (Lat/Lon)", "Choropleth (Location Names)"]
        # Reuse cached result to avoid re-running pycountry scan on every widget rerun.
        _df_sig_s = f"{df.shape}_{list(df.columns)}"
        if st.session_state.get("_detected_geo_sig") != _df_sig_s or "_detected_geo_col" not in st.session_state:
            st.session_state["_detected_geo_col"] = detect_geo_column(df)
            st.session_state["_detected_geo_sig"] = _df_sig_s
        _detected_geo  = st.session_state["_detected_geo_col"]
        _default_mode  = 1 if _detected_geo else 0
        st.selectbox("Map mode", _map_mode_opts, index=_default_mode, key=_sk(aid, "map_mode"),
                     help="Scatter = numeric lat/lon columns. Choropleth = country/state name column.")
        _mode = st.session_state.get(_sk(aid, "map_mode"), _map_mode_opts[_default_mode])
        if _mode == "Scatter (Lat/Lon)":
            mp1, mp2 = st.columns(2)
            with mp1: st.selectbox("Latitude column", [NONE] + num, key=_sk(aid, "lat_col"))
            with mp2: st.selectbox("Longitude column", [NONE] + num, key=_sk(aid, "lon_col"))
            mp3, mp4 = st.columns(2)
            with mp3: st.selectbox("Location label (hover name)", [NONE] + cat, key=_sk(aid, "location_col"),
                                   help="Categorical column shown as hover name per marker")
            with mp4: st.selectbox("Colour by", [NONE] + cat + num, key=_sk(aid, "color_col"),
                                   help="Categorical → discrete colours  ·  Numeric → continuous gradient")
            mp5, mp6 = st.columns(2)
            with mp5: st.selectbox("Size by (optional)", [NONE] + num, key=_sk(aid, "size_col"))
            with mp6: st.selectbox("Value column (aggregated in hover)", [NONE] + num, key=_sk(aid, "value_col"))
            mp7, mp8 = st.columns(2)
            with mp7: st.selectbox("Map style", ["carto-positron", "open-street-map", "carto-darkmatter"],
                                   key=_sk(aid, "map_style"))
            with mp8: st.selectbox("Aggregation (when location+value set)", list(_AGG_FUNCS.keys()), key=_sk(aid, "agg_func"))
            mp9, mp10 = st.columns(2)
            with mp9:  st.slider("Marker opacity", 0.3, 1.0, 0.82, 0.05, key=_sk(aid, "marker_opacity"))
            with mp10: st.checkbox("🔄 Invert colour scale", key=_sk(aid, "invert_colorscale"),
                                   help="Flip gradient: e.g. make high values lighter instead of darker.")
        else:
            _all_cols_str = [c for c in df.columns if df[c].dtype == object]
            _geo_default_idx = (_all_cols_str.index(_detected_geo)
                                if _detected_geo and _detected_geo in _all_cols_str else 0)
            st.selectbox("Location name column (countries / US states)",
                         _all_cols_str if _all_cols_str else df.columns.tolist(),
                         index=_geo_default_idx, key=_sk(aid, "geo_col"),
                         help=(
                             "Column with country names, ISO-2/3 codes, or US state names/abbrevs. "
                             "Lytrize auto-resolves them — no manual mapping needed."
                         ))
            cg1, cg2 = st.columns(2)
            with cg1: st.selectbox("Value column (fill colour)", [NONE] + num, key=_sk(aid, "value_col"))
            with cg2: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=_sk(aid, "agg_func"))
            cg3, cg4 = st.columns(2)
            with cg3: st.selectbox("Colour scale", _CHOROPLETH_SCALES, key=_sk(aid, "choropleth_colorscale"))
            with cg4: st.selectbox("Projection", _PROJECTIONS, key=_sk(aid, "choropleth_projection"))
            cg5, cg6 = st.columns(2)
            with cg5: st.selectbox("Scope", _SCOPES, key=_sk(aid, "choropleth_scope"))
            with cg6: st.checkbox("Show borders", value=True, key=_sk(aid, "choropleth_show_borders"))
            st.checkbox("🔄 Invert colour scale", key=_sk(aid, "invert_colorscale"))

    # ── Time Series ───────────────────────────────────────────────────────────
    # Line charts over time with optional date-part grouping and dual Y-axis.
    elif aid == "time_series":
        dt_candidates = dt if dt else all_cols
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.multiselect(
                "Date / Time column", dt_candidates,
                default=dt_candidates[:1] if dt_candidates else [],
                max_selections=1, key=_sk(aid, "x"))
        with c2: st.multiselect("Primary metric(s)", num, default=num[:2], key=_sk(aid, "y"))
        with c3: st.selectbox("Date grouping", list(_DATE_PARTS.keys()), key=_sk(aid, "date_part"))
        with c4: st.selectbox("Aggregation", list(_AGG_FUNCS.keys()), key=_sk(aid, "agg"))
        st.markdown("---")

        # Dual Y -- always-visible selectbox with "None" sentinel (see note above).
        st.markdown("**📊 Dual Y-Axis (Secondary metric as dashed line)**")
        st.caption("Choose a secondary metric on the right Y-axis. Select 'None' to disable.")
        dual_opts_ts = [NONE] + list(num)
        ts_d1, ts_d2, _ = st.columns([2, 1, 1])
        with ts_d1:
            st.selectbox(
                "Secondary Y-Axis metric", dual_opts_ts, key=_sk(aid, "dual_y_ts"),
                help="Adds a second line on the right axis.")
        with ts_d2:
            st.selectbox(
                "Secondary metric aggregation", list(_AGG_FUNCS.keys()),
                key=_sk(aid, "dual_y_agg"),
                help="Independent aggregation applied only to the secondary Y-axis metric.")



# ─────────────────────────────────────────────────────────────────────────────
# Configuration collection -- reading widget values → kwargs dict
# ─────────────────────────────────────────────────────────────────────────────

def _collect_kwargs(aid: str, df) -> dict:
    """
    Read widget values from session_state and return a kwargs dict for the runner.

    Called immediately before _run() to translate the user's configuration
    choices into typed Python arguments understood by each analysis runner.

    Args:
        aid: Analysis ID (e.g. "categorical").
        df:  The working DataFrame (used to infer defaults when selection is empty).

    Returns:
        dict of keyword arguments passed to the runner via **kwargs.
    """
    num, cat, dt, all_cols = _num_cols(), _cat_cols(), _dt_cols(), df.columns.tolist()
    NONE = "None"

    # Resolve palette -- always present because the selectbox is shown for all analyses.
    pal_label = _g(aid, "palette", list(PALETTES.keys())[0])
    palette   = PALETTES.get(pal_label, list(PALETTES.values())[0])
    kwargs    = {"palette": palette}

    # Sort label → internal sort key used by categorical / pie runners.
    _sort_map = {
        "Value ↓":       "Value (Desc)",
        "Value ↑":       "Value (Asc)",
        "Category A→Z":  "Category (A-Z)",
        "Category Z→A":  "Category (Z-A)",
    }

    # ── Statistical ───────────────────────────────────────────────────────────
    if aid == "statistical":
        x   = _g(aid, "x", [])
        y   = _g(aid, "y", num[:4]) or num
        agg = _AGG_FUNCS.get(_g(aid, "agg", "Mean (Avg)"), "mean")
        kwargs.update(x_cols=x or None, y_cols=y, agg=agg)

    # ── Distribution ──────────────────────────────────────────────────────────
    elif aid == "distribution":
        x     = _g(aid, "x", num[:4]) or num[:4]
        color = _g(aid, "color", [])
        kwargs.update(x_cols=x, y_cols=color or None)

    # ── Correlation ───────────────────────────────────────────────────────────
    elif aid == "correlation":
        x = _g(aid, "x", num) or num
        y = _g(aid, "y", [])
        kwargs.update(x_cols=x, y_cols=y or None)

    # ── Categorical & Pie ─────────────────────────────────────────────────────
    elif aid in ("categorical", "pie_chart"):
        x        = _g(aid, "x", cat[:2]) or cat[:2]
        y        = _g(aid, "y", []) or None
        agg      = _AGG_FUNCS.get(_g(aid, "agg", "Mean (Avg)"), "mean")
        raw_sort = _g(aid, "sort", "Value ↓")
        sort_by  = _sort_map.get(raw_sort, "Value (Desc)")
        top_n_v  = int(_g(aid, "top_n", 0) or 0)
        top_n    = top_n_v if top_n_v > 0 else None  # 0 → None means "show all"
        kwargs.update(x_cols=x, y_cols=y, agg=agg, sort_by=sort_by, top_n=top_n)

        if aid == "categorical":
            direction = _g(aid, "direction", "Vertical (Column chart)")
            raw_dual  = _g(aid, "dual_y", NONE)
            dual_y    = None if (not raw_dual or raw_dual == NONE) else raw_dual
            # Prevent the secondary metric from being the same as the primary.
            if dual_y and y and dual_y in (y if isinstance(y, list) else [y]):
                dual_y = None
            dual_y_agg = _AGG_FUNCS.get(_g(aid, "dual_y_agg", "Mean (Avg)"), "mean") if dual_y else None
            kwargs.update(direction=direction, dual_y_col=dual_y, dual_y_agg=dual_y_agg)

    # ── Scatter Plot ──────────────────────────────────────────────────────────
    elif aid == "scatter_plot":
        def _sp_resolve(key):
            v = _g(aid, key, NONE)
            return None if v in (NONE, None, "") else v
        kwargs.update(
            x_col=_sp_resolve("x_col"),
            y_col=_sp_resolve("y_col"),
            color_col=_sp_resolve("color_col"),
            size_col=_sp_resolve("size_col"),
            trendline=_g(aid, "trendline", "None"),
        )

    # ── Matrix Heatmap / Pivot Table ──────────────────────────────────────────
    elif aid == "matrix_table":
        def _mt_resolve(key):
            v = _g(aid, key, NONE)
            return None if v in (NONE, None, "") else v
        _mt_sort_map2 = {
            "Value ↓": "value_desc", "Value ↑": "value_asc",
            "Category A→Z": "cat_asc", "Category Z→A": "cat_desc",
        }
        _mt_top_n_v2 = int(_g(aid, "top_n_rows", 0) or 0)
        kwargs.update(
            index_col=_mt_resolve("index_col"),
            columns_col=_mt_resolve("columns_col"),
            values_col=_mt_resolve("values_col"),
            agg=_AGG_FUNCS.get(_g(aid, "agg", "Mean (Avg)"), "mean"),
            view_type=_g(aid, "view_type", "Heatmap"),
            sort_rows=_mt_sort_map2.get(_g(aid, "sort_rows", "Value ↓"), "value_desc"),
            top_n_rows=_mt_top_n_v2 if _mt_top_n_v2 > 0 else None,
        )

    # ── Map Plot ──────────────────────────────────────────────────────────────
    elif aid == "map_plot":
        def _mp_resolve(key):
            v = _g(aid, key, NONE)
            return None if v in (NONE, None, "") else v
        _mp_mode = _g(aid, "map_mode", "Scatter (Lat/Lon)")
        if _mp_mode == "Choropleth (Location Names)":
            kwargs.update(
                geo_col=_mp_resolve("geo_col"),
                value_col=_mp_resolve("value_col"),
                agg_func=_AGG_FUNCS.get(_g(aid, "agg_func", "Sum"), "sum"),
                invert_colorscale=bool(_g(aid, "invert_colorscale", False)),
                choropleth_colorscale=_g(aid, "choropleth_colorscale", "Blues"),
                choropleth_projection=_g(aid, "choropleth_projection", "natural earth"),
                choropleth_scope=_g(aid, "choropleth_scope", "world"),
                choropleth_show_borders=bool(_g(aid, "choropleth_show_borders", True)),
            )
        else:
            kwargs.update(
                lat_col=_mp_resolve("lat_col"),
                lon_col=_mp_resolve("lon_col"),
                location_col=_mp_resolve("location_col"),
                value_col=_mp_resolve("value_col"),
                size_col=_mp_resolve("size_col"),
                color_col=_mp_resolve("color_col"),
                agg_func=_AGG_FUNCS.get(_g(aid, "agg_func", "Mean (Avg)"), "mean"),
                invert_colorscale=bool(_g(aid, "invert_colorscale", False)),
                map_style=_g(aid, "map_style", "carto-positron"),
                marker_opacity=float(_g(aid, "marker_opacity", 0.82)),
            )

    # ── Time Series ───────────────────────────────────────────────────────────
    elif aid == "time_series":
        x         = _g(aid, "x", [])
        y         = _g(aid, "y", num[:2]) or num[:2]
        agg       = _AGG_FUNCS.get(_g(aid, "agg", "Mean (Avg)"), "mean")
        date_part = _DATE_PARTS.get(_g(aid, "date_part", "None"))
        raw_dual  = _g(aid, "dual_y_ts", NONE)
        dual_y    = None if (not raw_dual or raw_dual == NONE) else raw_dual
        # Prevent secondary from being the same column as any primary metric.
        if dual_y and dual_y in (y if isinstance(y, list) else [y]):
            dual_y = None
        dual_y_agg = _AGG_FUNCS.get(_g(aid, "dual_y_agg", "Mean (Avg)"), "mean") if dual_y else None
        kwargs.update(x_cols=x or None, y_cols=y, agg=agg,
                      date_part=date_part, dual_y_col=dual_y, dual_y_agg=dual_y_agg)

    return kwargs


# ─────────────────────────────────────────────────────────────────────────────
# Runner dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def _run(aid: str, df, **kwargs):
    """
    Dispatch to the correct runner and return a list of (uid, title, fig) tuples.

    Sequence per chart produced by the runner:
        1. Assign a fresh 8-character UID.
        2. Call generate_insights() to populate st.session_state[f"auto_insights_{uid}"].
        3. Append (uid, title, fig) to the result list.

    Insight generation is wrapped in its own try/except inside generate_insights()
    so a bad dataset (e.g. all-NaN column) can never prevent the chart from rendering.

    Args:
        aid:      Analysis ID (must be a key in _RUNNERS, e.g. "correlation").
        df:       The full working DataFrame from session_state.
        **kwargs: Collected from _collect_kwargs() / _collect_kwargs_scoped();
                  forwarded unchanged to both the runner and the insight generator.

    Returns:
        list of (uid: str, title: str, fig: Figure) — ready to append to charts.
        Returns None if the runner itself raises an error (caller shows st.error).
        Returns []  if aid is not registered.

    Notes:
        - descriptive and data_quality runners only accept df (no kwargs).
          All others receive **kwargs.
        - UIDs are 8-character hex strings; short enough to be readable in
          session_state keys but unique enough for typical session sizes (~100 charts).
        - generate_insights() uses the same **kwargs as the runner, so it has access
          to x_col, y_cols, agg, top_n, date_part, etc. without any extra plumbing.
    """
    fn = _RUNNERS.get(aid)
    if not fn:
        return []

    try:
        # ── Step 1: Run the analysis / chart builder ───────────────────────────
        # descriptive and data_quality render inline (no Plotly figures returned).
        # All other runners receive the full kwargs dict.
        raw = fn(df) if aid in ("descriptive", "data_quality") else fn(df, **kwargs)

        # ── Step 2: Assign UIDs + generate insights per chart ─────────────────
        results = []
        for title, fig in raw:
            uid = str(uuid.uuid4())[:8]

            # Populate st.session_state[f"auto_insights_{uid}"] with plain-English
            # insights derived from df and the runner's configuration kwargs.
            # generate_insights() swallows all exceptions internally — it will
            # never break the chart pipeline.
            generate_insights(aid, df, uid, **kwargs)

            results.append((uid, title, fig))

        return results

    except Exception as e:
        st.error(f"Analysis error ({aid}): {e}")
        return None
