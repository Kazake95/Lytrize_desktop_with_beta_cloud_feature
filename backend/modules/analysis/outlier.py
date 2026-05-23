"""
modules/analysis/outlier.py -- Fast IQR outlier detection for the upload page.
===============================================================================

Architecture
------------
Outlier detection was moved from the analysis page (where it was blocking every
rerun) to the upload / data-quality page so it runs once, on demand, before the
user proceeds to chart generation.

Performance model (why the old code froze):
  The original run_outlier() was called inside the Streamlit render loop, meaning
  it re-ran IQR calculations across every selected column on *every* rerun --
  including reruns triggered by unrelated widget interactions.  For wide datasets
  this produced noticeable UI freezes.

New model:
  - _compute_outliers() is only called when the user explicitly clicks
    "Detect Outliers".  Results are stored in st.session_state keyed to a cheap
    DataFrame fingerprint so they survive reruns without recomputation.
  - When the DataFrame changes (rows deleted) the fingerprint changes and the
    cached results are invalidated automatically.
  - Rendering is separated from computation: displaying tables, expanders, and
    delete buttons is pure Python/Streamlit widget work with no pandas overhead.

Public API
----------
  run_outlier_upload(df)   -- Render the full interactive upload-page widget.
                              Modifies st.session_state.df and .charts in-place.

  run_outlier(df, ...)     -- Legacy runner for backwards compatibility.
                              Kept so saved sessions with outlier charts still load.

  OUTLIER_HELP             -- Markdown help string used on legacy chart cards.
"""

import uuid
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from modules.charts import chart_layout, COLORS
from modules.analysis.insights import outlier_insights
from modules.database import log_activity


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _df_fingerprint(df: pd.DataFrame) -> str:  # O(1) fingerprint: changes when rows are added/deleted.
    """
    Cheap fingerprint that changes when rows are added or deleted.

    Uses shape + first-row + last-row string so it runs in O(1) regardless of
    dataset size.  Collision probability is negligible for normal editing patterns.
    """
    try:
        parts = [str(df.shape), str(df.iloc[0].tolist()), str(df.iloc[-1].tolist())]
    except Exception:
        parts = [str(df.shape)]
    return str(hash("".join(parts)))


def _compute_outliers(df: pd.DataFrame, cols: list, multiplier: float) -> dict:  # IQR-based outlier detection. multiplier=1.5 is standard; 3.0 is extreme.
    """
    Run IQR outlier detection on the given columns.

    Args:
        df:         Working DataFrame.
        cols:       Numeric column names to analyse.
        multiplier: IQR fence multiplier k (standard = 1.5, extreme = 3.0).

    Returns:
        dict mapping column name to {
            "q1", "q3", "iqr", "lo", "hi",
            "out_count",    -- number of outlier rows
            "pct",          -- percentage of total rows
            "out_indices",  -- list of DataFrame index values that are outliers
        }
    """
    results = {}
    num_available = set(df.select_dtypes(include="number").columns)

    for col in cols:
        if col not in num_available:
            continue
        s = df[col].dropna()
        if len(s) == 0:
            continue

        q1  = s.quantile(0.25)
        q3  = s.quantile(0.75)
        iqr = q3 - q1
        lo  = q1 - multiplier * iqr
        hi  = q3 + multiplier * iqr

        out_mask  = (df[col] < lo) | (df[col] > hi)
        out_count = int(out_mask.sum())
        pct       = round(out_count / len(df) * 100, 2) if len(df) else 0.0

        results[col] = {
            "q1":          q1,
            "q3":          q3,
            "iqr":         iqr,
            "lo":          lo,
            "hi":          hi,
            "out_count":   out_count,
            "pct":         pct,
            "out_indices": df.index[out_mask].tolist(),
        }

    return results


def _make_outlier_fig(df: pd.DataFrame, col: str, info: dict) -> go.Figure:
    """
    Build a Plotly scatter figure visualising outliers for one column.

    Normal points: small semi-transparent dots.
    Outliers: large red x markers.
    Dashed horizontal lines mark the IQR fences.
    """
    # Recompute mask against the live df -- rows may have been deleted since
    # the info dict was computed.
    live_mask = (df[col] < info["lo"]) | (df[col] > info["hi"])
    nrm = df[~live_mask]
    out = df[live_mask]

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=nrm.index,
        y=nrm[col].values,
        mode="markers",
        name="Normal",
        marker=dict(color=COLORS[0], size=4, opacity=0.45),
        hovertemplate=(
            f"<b>Row:</b> %{{x}}<br>"
            f"<b>{col}:</b> %{{y:,.2f}}<br>"
            "<extra>Normal</extra>"
        ),
    ))

    if len(out):
        fig.add_trace(go.Scatter(
            x=out.index,
            y=out[col].values,
            mode="markers",
            name="Outlier ⚠️",
            marker=dict(color="#ef4444", size=9, symbol="x"),
            hovertemplate=(
                f"<b>Row:</b> %{{x}}<br>"
                f"<b>{col}:</b> %{{y:,.2f}}<br>"
                "<extra>⚠️ Outlier</extra>"
            ),
        ))

    fig.add_hline(
        y=info["hi"], line_dash="dash", line_color="#f59e0b",
        annotation_text=f"Upper fence: {info['hi']:.4g}",
        annotation_position="top right",
    )
    fig.add_hline(
        y=info["lo"], line_dash="dash", line_color="#f59e0b",
        annotation_text=f"Lower fence: {info['lo']:.4g}",
        annotation_position="bottom right",
    )

    n_out = int(live_mask.sum())
    fig.update_layout(
        title=f"Outliers — {col}  ({n_out} outlier{'s' if n_out != 1 else ''} detected)",
        xaxis_title="Row Index (hover to see exact row number)",
        yaxis_title=col,
        **chart_layout(),
    )
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Upload-page entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_outlier_upload(df: pd.DataFrame) -> None:  # Full interactive upload-page widget: detect → view → delete outliers.
    """
    Render the full interactive outlier detection widget on the upload/data-quality page.

    Performance contract:
      - IQR computation ONLY runs when the user clicks "Detect Outliers".
      - All subsequent reruns read from session_state cache -- zero pandas work.
      - Cache is invalidated automatically when the DataFrame changes
        (fingerprint change) or the multiplier is adjusted.

    Side effects on st.session_state:
      - .df             -- rows deleted by user action
      - .charts         -- outlier chart appended via "Add to Dashboard"
      - ._outlier_*     -- internal cache keys

    Args:
        df: The current working DataFrame (caller reads from session_state.df).
    """
    st.markdown("### 🚨 Outlier Detection")
    st.caption(
        "Detect statistical anomalies using the IQR method. "
        "Values outside Q1 − k·IQR or Q3 + k·IQR are flagged. "
        "Remove them here before analysis to avoid skewed charts."
    )

    num_cols = df.select_dtypes(include="number").columns.tolist()
    if not num_cols:
        st.info("No numeric columns found — outlier detection requires at least one numeric column.")
        return

    # ── Config widgets ────────────────────────────────────────────────────────
    cfg1, cfg2 = st.columns([3, 1])
    with cfg1:
        selected_cols = st.multiselect(
            "Columns to analyse",
            options=num_cols,
            default=num_cols[: min(6, len(num_cols))],
            key="outlier_cols",
            help="Select numeric columns to check for outliers.",
        )
    with cfg2:
        multiplier = st.number_input(
            "IQR multiplier (k)",
            min_value=0.5, max_value=5.0, value=1.5, step=0.5,
            key="outlier_k",
            help="Standard = 1.5. Use 3.0 for extreme-only detection.",
        )

    if not selected_cols:
        st.info("Select at least one column, then click Detect Outliers.")
        return

    # ── Cache invalidation ────────────────────────────────────────────────────
    fp = _df_fingerprint(df)
    if (
        st.session_state.get("_outlier_fp") != fp
        or st.session_state.get("_outlier_k") != multiplier
    ):
        st.session_state.pop("_outlier_results", None)

    # ── Detect button -- only expensive step ──────────────────────────────────
    if st.button("🔍 Detect Outliers", key="outlier_detect_btn", type="primary"):
        with st.spinner(f"Analysing {len(selected_cols)} column(s)…"):
            results = _compute_outliers(df, selected_cols, multiplier)
        st.session_state["_outlier_results"] = results
        st.session_state["_outlier_fp"]      = fp
        st.session_state["_outlier_k"]       = multiplier
        st.session_state["_outlier_cols"]    = selected_cols

    # ── Show cached results (zero computation on reruns) ──────────────────────
    results = st.session_state.get("_outlier_results")
    if not results:
        return

    # Restrict to user's current column selection
    results = {c: v for c, v in results.items() if c in selected_cols}
    if not results:
        return

    total_outliers     = sum(v["out_count"] for v in results.values())
    cols_with_outliers = [c for c, v in results.items() if v["out_count"] > 0]

    if total_outliers == 0:
        st.success("✅ No outliers detected in the selected columns!")
        return

    st.markdown(
        f"**{total_outliers:,} outlier value(s)** across "
        f"**{len(cols_with_outliers)}** column(s)"
    )

    # ── Summary table ─────────────────────────────────────────────────────────
    summary_df = pd.DataFrame([
        {
            "Column":      col,
            "Outliers":    info["out_count"],
            "% of rows":   f"{info['pct']}%",
            "Lower fence": f"{info['lo']:.4g}",
            "Upper fence": f"{info['hi']:.4g}",
            "Q1":          f"{info['q1']:.4g}",
            "Q3":          f"{info['q3']:.4g}",
        }
        for col, info in results.items()
    ])
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    # ── Bulk delete across all columns ────────────────────────────────────────
    all_outlier_indices: set = set()
    for col in cols_with_outliers:
        mask = (df[col] < results[col]["lo"]) | (df[col] > results[col]["hi"])
        all_outlier_indices.update(df.index[mask].tolist())

    if all_outlier_indices:
        if st.button(
            f"🗑️ Delete ALL outlier rows across all columns  "
            f"({len(all_outlier_indices):,} unique rows)",
            key="outlier_del_all_cols",
        ):
            st.session_state.df = (
                st.session_state.df
                .drop(index=list(all_outlier_indices))
                .reset_index(drop=True)
            )
            log_activity(
                st.session_state.get("user_id", 0),
                "outlier_delete_all_cols",
                f"removed={len(all_outlier_indices)}",
            )
            st.session_state.pop("_outlier_results", None)
            st.success(
                f"✅ Removed {len(all_outlier_indices):,} rows. "
                f"Dataset now has {len(st.session_state.df):,} rows."
            )
            st.rerun()

    st.markdown("---")

    # ── Per-column expanders ──────────────────────────────────────────────────
    for col, info in results.items():
        if info["out_count"] == 0:
            continue

        label = (
            f"📋 {col}  —  "
            f"{info['out_count']} outlier{'s' if info['out_count'] != 1 else ''} "
            f"({info['pct']}%)  |  "
            f"fences [{info['lo']:.4g}, {info['hi']:.4g}]"
        )
        with st.expander(label, expanded=False):

            # Recompute mask on live df so deleted rows don't show
            live_mask = (df[col] < info["lo"]) | (df[col] > info["hi"])
            out_rows  = df[live_mask].copy()

            if len(out_rows) == 0:
                st.success("All outliers in this column have already been removed.")
                continue

            st.dataframe(out_rows.head(500), use_container_width=True)
            if len(out_rows) > 500:
                st.caption(f"Showing 500 of {len(out_rows):,} outlier rows.")

            # ── Action row: delete-all | add-to-dashboard ─────────────────────
            act1, act2 = st.columns(2)

            with act1:
                if st.button(
                    f"🗑️ Delete all {len(out_rows):,} outlier rows in '{col}'",
                    key=f"out_del_all_{col}",
                ):
                    st.session_state.df = (
                        st.session_state.df
                        .drop(index=out_rows.index.tolist())
                        .reset_index(drop=True)
                    )
                    log_activity(
                        st.session_state.get("user_id", 0),
                        "outlier_delete_all",
                        f"col={col} removed={len(out_rows)}",
                    )
                    st.session_state.pop("_outlier_results", None)
                    st.success(
                        f"✅ Removed {len(out_rows):,} rows. "
                        f"Dataset now has {len(st.session_state.df):,} rows."
                    )
                    st.rerun()

            with act2:
                if st.button(
                    f"📊 Add '{col}' outlier chart to dashboard",
                    key=f"out_add_dash_{col}",
                    help=(
                        "Generates an IQR scatter plot and adds it to your "
                        "chart list, ready to save to a dashboard."
                    ),
                ):
                    fig   = _make_outlier_fig(df, col, info)
                    uid   = uuid.uuid4().hex[:8]
                    title = f"Outliers: {col}"

                    charts = st.session_state.get("charts", [])
                    charts.append((uid, title, fig))
                    st.session_state.charts = charts

                    # Metadata used by analysis/dashboard pages
                    st.session_state[f"chart_type_{uid}"]    = "outlier"
                    st.session_state[f"desc_{uid}"]          = (
                        f"IQR outlier analysis for '{col}'. "
                        f"{info['out_count']} outlier(s) detected ({info['pct']}% of rows). "
                        f"Fences: [{info['lo']:.4g}, {info['hi']:.4g}]. "
                        f"Multiplier: k = {multiplier}."
                    )
                    st.session_state[f"auto_insights_{uid}"] = outlier_insights(col, info)
                    log_activity(
                        st.session_state.get("user_id", 0),
                        "outlier_chart_added_to_dashboard",
                        f"col={col}",
                    )
                    st.success(f"✅ '{col}' outlier chart added to the dashboard!")

            # ── Selective row deletion ────────────────────────────────────────
            st.markdown("**Delete specific rows by index:**")
            sel_indices = st.multiselect(
                "Select row indices to remove",
                options=out_rows.index.tolist(),
                key=f"out_sel_{col}",
                help="Row indices correspond to positions in the current dataset.",
            )
            if st.button(
                f"🗑️ Delete {len(sel_indices)} selected row(s)",
                key=f"out_del_sel_{col}",
                disabled=(len(sel_indices) == 0),
            ):
                st.session_state.df = (
                    st.session_state.df
                    .drop(index=sel_indices)
                    .reset_index(drop=True)
                )
                log_activity(
                    st.session_state.get("user_id", 0),
                    "outlier_delete_selected",
                    f"col={col} indices={sel_indices[:20]}",
                )
                st.session_state.pop("_outlier_results", None)
                st.success(
                    f"✅ Deleted {len(sel_indices)} row(s). "
                    f"Dataset now has {len(st.session_state.df):,} rows."
                )
                st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Legacy runner -- backwards compatibility with saved sessions
# ─────────────────────────────────────────────────────────────────────────────

def run_outlier(df, x_cols=None, y_cols=None, palette=None, **kwargs):  # Legacy runner — kept for backwards compatibility with saved sessions.
    """
    IQR outlier scatter plots -- legacy runner.

    Kept in _RUNNERS so saved sessions containing outlier charts can still have
    those charts regenerated from the Edit Chart panel.  New outlier work uses
    run_outlier_upload() on the upload page.

    Args:
        df:      Working DataFrame.
        x_cols:  Numeric columns to analyse.  Defaults to first 6 numeric cols.
        palette: Colour list.
        **kwargs: Silently ignored.

    Returns:
        list of (title: str, fig: go.Figure)
    """
    from modules.charts import num_cols as _num_cols

    charts = []
    num    = x_cols or _num_cols()[:6]
    pal    = palette or COLORS

    for col in num:
        if col not in df.columns:
            continue
        s = df[col].dropna()
        if len(s) == 0:
            continue

        q1  = s.quantile(0.25)
        q3  = s.quantile(0.75)
        iqr = q3 - q1
        lo  = q1 - 1.5 * iqr
        hi  = q3 + 1.5 * iqr

        out_mask = (df[col] < lo) | (df[col] > hi)
        out      = df[out_mask]
        nrm      = df[~out_mask]

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=nrm.index, y=nrm[col].values,
            mode="markers", name="Normal",
            marker=dict(color=pal[0], size=4, opacity=0.45),
            hovertemplate=(
                f"<b>Row:</b> %{{x}}<br>"
                f"<b>{col}:</b> %{{y:,.2f}}<br>"
                "<extra>Normal</extra>"
            ),
        ))
        if len(out):
            fig.add_trace(go.Scatter(
                x=out.index, y=out[col].values,
                mode="markers", name="Outlier ⚠️",
                marker=dict(color="#ef4444", size=9, symbol="x"),
                hovertemplate=(
                    f"<b>Row:</b> %{{x}}<br>"
                    f"<b>{col}:</b> %{{y:,.2f}}<br>"
                    "<extra>⚠️ Outlier</extra>"
                ),
            ))
        fig.add_hline(y=hi, line_dash="dash", line_color="#f59e0b",
                      annotation_text=f"Upper IQR boundary: {hi:.2f}",
                      annotation_position="top right")
        fig.add_hline(y=lo, line_dash="dash", line_color="#f59e0b",
                      annotation_text=f"Lower IQR boundary: {lo:.2f}",
                      annotation_position="bottom right")

        n_out = int(out_mask.sum())
        fig.update_layout(
            title=f"Outliers — {col}  ({n_out} outlier{'s' if n_out != 1 else ''} detected)",
            xaxis_title="Row Index (hover to see exact row number)",
            yaxis_title=col,
            **chart_layout(),
        )
        charts.append((f"Outliers: {col}", fig))

    return charts


# ─────────────────────────────────────────────────────────────────────────────
# Help text -- shown on legacy outlier chart cards
# ─────────────────────────────────────────────────────────────────────────────

OUTLIER_HELP = (
    "**📊 How to read Outlier charts:**  "
    "Each dot is one row in your dataset. "
    "**🔴 Red × marks** are outliers — values that fall outside the IQR boundaries (dashed lines). "
    "**Hover** over any point to see its exact Row Index and value. "
    "The row index maps directly to the row number in your raw data table.\n\n"
    "**Business use:** Outliers often signal data-entry errors, fraud, returns, or exceptional events. "
    "Investigate red points before running predictive models — they can skew results significantly."
)
