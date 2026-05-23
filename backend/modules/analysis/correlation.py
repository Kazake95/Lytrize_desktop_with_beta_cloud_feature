"""
modules/analysis/correlation.py -- Pearson correlation heatmap runner.
=====================================================================

Produces a single annotated heatmap showing pairwise Pearson correlation
coefficients between all selected numeric columns.

Colour scale: uses the active palette; values range from -1 (inverse) to +1 (direct).
Cell annotations: rounded to 2 decimal places.

Requirements: at least 2 numeric columns must be selected. If fewer are provided
the runner returns [] and the caller is expected to show no charts.
"""

import plotly.express as px
from modules.charts import chart_layout, COLORS, num_cols as _num_cols


def run_correlation(df, x_cols=None, y_cols=None, palette=None, **kwargs):
    """
    Generate a Pearson correlation heatmap.

    Args:
        df:      Working DataFrame.
        x_cols:  Primary list of numeric columns to include.
        y_cols:  Additional numeric columns merged with x_cols (optional).
        palette: List of hex colour strings used as the colour scale.
        **kwargs: Extra kwargs silently ignored.

    Returns:
        list of (title: str, fig: Figure) -- always zero or one entry.
    """
    charts = []

    # Merge x_cols and y_cols, deduplicate while preserving order.
    num = list(dict.fromkeys((x_cols or []) + (y_cols or []) or _num_cols()))
    if len(num) < 2:
        return charts

    pal  = palette or COLORS
    corr = df[num].corr()

    fig = px.imshow(
        corr,
        text_auto=".2f",
        title="Correlation Heatmap",
        color_continuous_scale=pal,
        color_continuous_midpoint=0,   # centre scale at 0 so white = no correlation
        aspect="auto",
        zmin=-1, zmax=1)
    fig.update_layout(**chart_layout())
    charts.append(("Correlation", fig))

    return charts
