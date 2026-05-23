"""
modules/analysis/categorical.py -- Categorical bar chart runner.
===============================================================

Produces vertical column charts or horizontal bar charts for categorical
dimensions aggregated over numeric metrics.

Features:
  - Vertical (column) and horizontal (bar) orientation via `direction` kwarg.
  - Top-N filtering with optional "Other" roll-up (handled in __init__.py).
  - Dual Y-axis: primary metric as bars, secondary metric as a line overlay.
  - Value labels rendered outside each bar for easy reading.
  - Sort order respected by both pandas AND Plotly (Plotly reorders internally
    unless explicitly overridden -- see _apply_plotly_sort()).

Returns:
    list of (title: str, fig: Figure) tuples -- one tuple per (dimension × metric)
    combination. If no metrics are selected, one chart per dimension (value counts).

CONTRIBUTING -- to add a new chart type within this module:
    Add a branch inside run_categorical() and append (title, fig) to `charts`.
"""

import plotly.graph_objects as go
from plotly.subplots import make_subplots
from modules.charts import chart_layout, COLORS, cat_cols as _cat_cols


# ─────────────────────────────────────────────────────────────────────────────
# Colour helpers
# ─────────────────────────────────────────────────────────────────────────────

def _luminance(hex_color: str) -> float:
    """Perceived brightness of a hex colour via ITU-R BT.601 coefficients (0-255)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 128.0
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def _pick_contrast_color(pal: list, exclude_idx: int = 0) -> str:
    """
    Return the palette colour most visually distinct from pal[exclude_idx].

    Prevents dual-axis bar+line from using near-identical colours such as
    COLORS[0]='#6163df' (mid-blue) and COLORS[1]='#8566fc' (also mid-purple).
    Falls back to amber accent if no palette entry is sufficiently different.
    """
    if not pal:
        return "#f59e0b"
    base_lum   = _luminance(pal[exclude_idx])
    best_color = pal[0]
    best_dist  = -1.0
    for i, c in enumerate(pal):
        if i == exclude_idx:
            continue
        dist = abs(_luminance(c) - base_lum)
        if dist > best_dist:
            best_dist  = dist
            best_color = c
    return best_color if best_dist > 5 else "#f59e0b"


# ─────────────────────────────────────────────────────────────────────────────
# Sorting helpers
# ─────────────────────────────────────────────────────────────────────────────

# Maps the sort-by string (from __init__.py _sort_map) to a pandas sort call.
_SORT = {
    "Value (Desc)":   lambda d, vc: d.sort_values(vc, ascending=False),
    "Value (Asc)":    lambda d, vc: d.sort_values(vc, ascending=True),
    "Category (A-Z)": lambda d, vc: d.sort_values(d.columns[0], ascending=True),
    "Category (Z-A)": lambda d, vc: d.sort_values(d.columns[0], ascending=False),
}


def _sort(df, val_col: str, sort_by: str):
    """Apply the requested sort to an aggregated DataFrame."""
    fn = _SORT.get(sort_by)
    return fn(df, val_col) if fn else df


def _apply_plotly_sort(fig, cats: list, is_horiz: bool, sort_by: str):
    """
    Force Plotly to respect the DataFrame's pre-sorted category order.

    Plotly independently decides how to order categories on an axis unless
    categoryorder="array" and categoryarray are set explicitly.

    For horizontal bars Plotly renders items bottom→top, so we reverse the
    category list so that the "highest" sorted item appears at the top.

    Args:
        fig:      The Plotly figure to mutate in-place.
        cats:     Ordered list of category labels from the sorted DataFrame.
        is_horiz: True for horizontal bar charts.
        sort_by:  The sort key string (used only to decide whether to reverse).
    """
    if is_horiz:
        # Reverse so the top-sorted item appears at the top of the Y-axis.
        fig.update_yaxes(categoryorder="array", categoryarray=list(reversed(cats)))
    else:
        fig.update_xaxes(categoryorder="array", categoryarray=cats)
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run_categorical(df, x_cols=None, y_cols=None, agg="mean", sort_by=None,
                    palette=None, top_n=None, dual_y_col=None, dual_y_agg=None,
                    direction="Vertical (Column chart)", **_):
    """
    Generate categorical bar / column charts.

    Args:
        df:         Working DataFrame.
        x_cols:     Categorical dimension columns to chart. Defaults to first 4 cat cols.
        y_cols:     Numeric metric columns. If None, value counts are used instead.
        agg:        Aggregation string: "mean", "sum", "median", "count", "min", "max".
        sort_by:    Sort key: "Value (Desc)", "Value (Asc)", "Category (A-Z)", "Category (Z-A)".
        palette:    List of hex colour strings.
        top_n:      Keep only the N highest-value categories. None = show all.
        dual_y_col: A second numeric column overlaid as a line on a secondary Y-axis.
                    Only used when `y_cols` has exactly one metric.
        direction:  "Vertical (Column chart)" or "Horizontal (Bar chart)".
        **_:        Extra kwargs silently ignored (forward-compatibility).

    Returns:
        list of (title: str, fig: Figure) tuples.
    """
    charts   = []
    dims     = x_cols or _cat_cols()[:4]
    metrics  = y_cols
    agg_lbl  = agg.title()
    pal      = palette or COLORS
    is_horiz = "Horizontal" in str(direction)
    sec_agg     = dual_y_agg or agg          # aggregation for secondary metric
    sec_agg_lbl = sec_agg.title()

    for col in dims:

        # ── Case A: metric column(s) provided → aggregate ─────────────────────
        if metrics:
            for metric in metrics:
                # Aggregate the metric by the dimension column.
                agg_df = df.groupby(col)[metric].agg(agg).reset_index()
                agg_df.columns = [col, "val"]
                agg_df = _sort(agg_df, "val", sort_by)
                if top_n and top_n > 0:
                    agg_df = agg_df.nlargest(top_n, "val")
                agg_df   = agg_df.reset_index(drop=True)
                top_sfx  = f" (Top {top_n})" if top_n else ""

                # ── Dual Y-axis variant ────────────────────────────────────────
                # When a secondary metric is requested, create a subplot with two
                # Y-axes: bars for the primary, a line for the secondary.
                dual = dual_y_col
                if dual and dual in df.columns and dual != metric:
                    d2 = df.groupby(col)[dual].agg(sec_agg).reset_index()
                    d2.columns = [col, "val2"]
                    merged = agg_df.merge(d2, on=col, how="left")
                    cats   = merged[col].tolist()
                    v1     = merged["val"].tolist()
                    v2     = merged["val2"].tolist()

                    fig = make_subplots(specs=[[{"secondary_y": True}]])
                    bar_x, bar_y = (v1, cats) if is_horiz else (cats, v1)
                    fig.add_trace(go.Bar(
                        x=bar_x, y=bar_y,
                        orientation="h" if is_horiz else "v",
                        name=f"{agg_lbl} {metric}",
                        marker_color=pal[0],
                        text=[f"{v:,.1f}" for v in v1],
                        textposition="outside",
                        cliponaxis=False,
                    ), secondary_y=False)
                    # Use a colour with maximum luminance contrast vs the bar colour
                    # so bar and line are always visually distinct.
                    line_color = _pick_contrast_color(pal, 0)
                    fig.add_trace(go.Scatter(
                        x=cats, y=v2,
                        name=f"{sec_agg_lbl} {dual}",
                        # mode must include "text" for value labels to render;
                        # apply_chart_display_options can then toggle them on/off.
                        mode="lines+markers+text",
                        line=dict(color=line_color, width=2),
                        marker=dict(size=8, color=line_color),
                        text=[f"{v:,.1f}" for v in v2],
                        textposition="top center",
                        # Labels hidden by default — user enables via "Value labels" toggle.
                        textfont=dict(color=line_color),
                        texttemplate=None,  # suppressed until show_value_labels is enabled
                    ), secondary_y=True)
                    # Hide line text by default (mode kept as lines+markers; +text injected
                    # by apply_chart_display_options when show_value_labels is True).
                    fig.data[-1].mode = "lines+markers"
                    fig.update_layout(
                        title=f"{agg_lbl} {metric} & {sec_agg_lbl} {dual} by {col}{top_sfx}",
                        **chart_layout())
                    fig.update_yaxes(title_text=f"{agg_lbl} {metric}", secondary_y=False)
                    fig.update_yaxes(title_text=f"{sec_agg_lbl} {dual}",   secondary_y=True)
                    if is_horiz:
                        fig.update_layout(margin=dict(l=20, r=100, t=56, b=20))
                    _apply_plotly_sort(fig, cats, is_horiz, sort_by)

                else:
                    # ── Single Y-axis variant ─────────────────────────────────
                    cats   = agg_df[col].tolist()
                    vals   = agg_df["val"].tolist()
                    texts  = [f"{v:,.1f}" for v in vals]
                    bar_x, bar_y = (vals, cats) if is_horiz else (cats, vals)
                    colors = [pal[i % len(pal)] for i in range(len(cats))]

                    fig = go.Figure(go.Bar(
                        x=bar_x, y=bar_y,
                        orientation="h" if is_horiz else "v",
                        marker_color=colors,
                        text=texts,
                        textposition="outside",
                        cliponaxis=False,
                    ))
                    d_lbl = "Bar" if is_horiz else "Column"
                    fig.update_layout(
                        title=f"{d_lbl}: {agg_lbl} {metric} by {col}{top_sfx}",
                        showlegend=False,
                        **chart_layout())
                    if is_horiz:
                        fig.update_layout(margin=dict(l=20, r=100, t=56, b=20))
                    else:
                        fig.update_layout(margin=dict(l=20, r=20, t=56, b=60))
                    _apply_plotly_sort(fig, cats, is_horiz, sort_by)

                charts.append((f"{agg_lbl} {metric} by {col}", fig))

        # ── Case B: no metric → value counts ──────────────────────────────────
        else:
            vc = df[col].value_counts().reset_index()
            vc.columns = [col, "Count"]
            vc = _sort(vc, "Count", sort_by)
            if top_n and top_n > 0:
                vc = vc.nlargest(top_n, "Count")
            vc      = vc.reset_index(drop=True)
            top_sfx = f" (Top {top_n})" if top_n else ""

            cats   = vc[col].tolist()
            vals   = vc["Count"].tolist()
            texts  = [str(v) for v in vals]
            bar_x, bar_y = (vals, cats) if is_horiz else (cats, vals)
            colors = [pal[i % len(pal)] for i in range(len(cats))]
            d_lbl  = "Bar" if is_horiz else "Column"

            fig = go.Figure(go.Bar(
                x=bar_x, y=bar_y,
                orientation="h" if is_horiz else "v",
                marker_color=colors,
                text=texts,
                textposition="outside",
                cliponaxis=False,
            ))
            fig.update_layout(
                title=f"{d_lbl} Counts: {col}{top_sfx}",
                showlegend=False,
                **chart_layout())
            if is_horiz:
                fig.update_layout(margin=dict(l=20, r=100, t=56, b=20))
            _apply_plotly_sort(fig, cats, is_horiz, sort_by)

            charts.append((f"Counts: {col}", fig))

    return charts
