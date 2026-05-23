"""
modules/analysis/time_series.py -- Time series line chart runner.
================================================================

Produces line charts showing numeric metrics over time, with optional:
  - Date-part grouping (Year / Quarter / Month / Weekday / Day / Hour)
  - Dual Y-axis: primary metric as solid line, secondary as dashed line

Date handling:
    The runner accepts a user-selected datetime column. If none is selected,
    it auto-detects the first parseable datetime column in the DataFrame.
    Grouping is performed after parsing so even string-encoded dates work.

Month Name / Weekday Name grouping:
    These two date parts produce categorical X-axes and require explicit
    sort-order maps (_MONTH, _WEEKDAY) so months/days appear in calendar
    order rather than alphabetical order.

Dual Y-axis:
    When `dual_y_col` is provided (and differs from the primary metric),
    a Plotly subplot with secondary_y=True is used. The secondary line is
    dashed to visually separate the two axes' data.
"""

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from modules.charts import chart_layout, COLORS


# ─────────────────────────────────────────────────────────────────────────────
# Calendar order maps for named date-part groupings
# ─────────────────────────────────────────────────────────────────────────────

_MONTH = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}

_WEEKDAY = {d: i for i, d in enumerate(
    ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])}


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run_time_series(df, x_cols=None, y_cols=None, agg="mean", date_part=None,
                    palette=None, dual_y_col=None, dual_y_agg=None, **_):
    """
    Generate time-series line charts for selected numeric metrics.

    Args:
        df:          Working DataFrame.
        x_cols:      List containing one datetime column (optional).
                     If None, auto-detects the first parseable datetime column.
        y_cols:      Numeric metric columns to plot as separate lines.
        agg:         Aggregation to apply when grouping by date_part.
        date_part:   pandas period alias or special string:
                       "Y" | "Q" | "M" | "D" | "H" -- use pandas to_period()
                       "month_name"  -- group by calendar month name
                       "weekday_name" -- group by day-of-week name
                       None -- use the raw datetime column (no grouping)
        palette:     List of hex colour strings.
        dual_y_col:  A second numeric column to plot on a secondary Y-axis.
                     Shown as a dashed line. Ignored if equal to the primary metric.
        dual_y_agg:  Aggregation for the secondary metric. Defaults to `agg` when None.
        **_:         Extra kwargs silently ignored.

    Returns:
        list of (title: str, fig: Figure) -- one entry per metric in y_cols.
    """
    charts       = []
    df           = df.copy()
    dt_col       = (x_cols or [None])[0]
    agg_lbl      = agg.title()
    pal          = palette or COLORS
    dual_agg     = dual_y_agg or agg
    dual_agg_lbl = dual_agg.title()

    # ── Auto-detect datetime column if not explicitly selected ─────────────────
    if not dt_col:
        for c in df.columns:
            try:
                df[c] = pd.to_datetime(df[c])
                dt_col = c
                break
            except Exception:
                pass

    # ── Ensure the selected column is actually datetime dtype ──────────────────
    if dt_col and not pd.api.types.is_datetime64_any_dtype(df[dt_col]):
        df[dt_col] = pd.to_datetime(df[dt_col], errors="coerce")

    num       = y_cols or []
    plot_x    = dt_col
    x_label   = dt_col or "Index"
    order_map = None

    # ── Apply date-part grouping ───────────────────────────────────────────────
    # Compute a "_p" (period) column and update plot_x accordingly.
    if dt_col and date_part:
        tmp = pd.to_datetime(df[dt_col].astype(str), errors="coerce")

        if date_part == "month_name":
            df["_p"] = tmp.dt.month_name()
            order_map = _MONTH
            plot_x, x_label = "_p", "Month"

        elif date_part == "weekday_name":
            df["_p"] = tmp.dt.day_name()
            order_map = _WEEKDAY
            plot_x, x_label = "_p", "Weekday"

        else:
            # Standard pandas period alias (Y / Q / M / D / H).
            try:
                df["_p"] = tmp.dt.to_period(date_part).astype(str)
                plot_x, x_label = "_p", f"{dt_col} ({date_part})"
            except Exception:
                pass

    # ── Build one chart per primary metric ────────────────────────────────────
    for i, col in enumerate(num):
        c_pri = pal[i % len(pal)]
        c_sec = pal[(i + 1) % len(pal)]

        # Aggregate primary metric values.
        if dt_col and plot_x == "_p":
            # Grouped by date part → aggregate and sort by the period column.
            g = df.groupby("_p")[col].agg(agg).reset_index()
            g.columns = ["_p", col]
            if order_map:
                g["_s"] = g["_p"].map(order_map)
                g = g.sort_values("_s").drop(columns="_s")
            else:
                g = g.sort_values("_p")
            x_vals = g["_p"].tolist()
            y_vals = g[col].tolist()

        elif dt_col:
            # Raw datetime column -- sort chronologically.
            sd = df.sort_values(dt_col)
            x_vals = sd[dt_col].tolist()
            y_vals = sd[col].tolist()

        else:
            # No datetime column found -- use DataFrame index as X.
            r = df.reset_index()
            x_vals = r["index"].tolist()
            y_vals = r[col].tolist()

        # ── Aggregate secondary metric values (dual Y) ─────────────────────────
        dual        = dual_y_col
        dual_valid  = (dual and dual != col and dual in df.columns)
        y2_vals     = None

        if dual_valid:
            if dt_col and plot_x == "_p":
                g2 = df.groupby("_p")[dual].agg(dual_agg).reset_index()
                g2.columns = ["_p", dual]
                if order_map:
                    g2["_s"] = g2["_p"].map(order_map)
                    g2 = g2.sort_values("_s").drop(columns="_s")
                else:
                    g2 = g2.sort_values("_p")
                y2_vals = g2[dual].tolist()
            elif dt_col:
                y2_vals = df.sort_values(dt_col)[dual].tolist()
            else:
                y2_vals = df.reset_index()[dual].tolist()

        # ── Build the Plotly figure ────────────────────────────────────────────
        if dual_valid and y2_vals is not None:
            # Two-axis subplot for dual-Y mode.
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            fig.add_trace(go.Scatter(
                x=x_vals, y=y_vals, mode="lines+markers",
                name=f"{agg_lbl} {col}",
                line=dict(color=c_pri, width=2), marker=dict(size=5),
            ), secondary_y=False)
            fig.add_trace(go.Scatter(
                x=x_vals, y=y2_vals, mode="lines+markers",
                name=f"{dual_agg_lbl} {dual}",
                line=dict(color=c_sec, width=2, dash="dash"),
                marker=dict(size=5),
                text=[f"{v:,.1f}" for v in y2_vals],      # ★ add static labels
                textposition="top center",                 # ★ position them above the points
            ), secondary_y=True)
            fig.update_layout(
                title=f"{agg_lbl} {col} & {dual_agg_lbl} {dual} over {x_label}",
                **chart_layout())
            if plot_x == "_p":
                # Categorical X-axis -- tell Plotly to preserve the sorted order.
                fig.update_xaxes(type="category", title_text=x_label)
            fig.update_yaxes(title_text=f"{agg_lbl} {col}", secondary_y=False)
            fig.update_yaxes(title_text=f"{dual_agg_lbl} {dual}", secondary_y=True)

        else:
            # Single-axis line chart.
            if dt_col and plot_x == "_p":
                fig = px.line(
                    x=x_vals, y=y_vals,
                    title=f"{agg_lbl} {col} by {x_label}",
                    color_discrete_sequence=[c_pri], markers=True)
                fig.update_xaxes(type="category", title_text=x_label)
            elif dt_col:
                fig = px.line(
                    x=x_vals, y=y_vals,
                    title=f"Time Series: {col}",
                    color_discrete_sequence=[c_pri])
            else:
                fig = px.line(
                    x=x_vals, y=y_vals,
                    title=f"Trend: {col}",
                    color_discrete_sequence=[c_pri])
            fig.update_layout(**chart_layout())

        charts.append((f"TS: {col}", fig))

    return charts
