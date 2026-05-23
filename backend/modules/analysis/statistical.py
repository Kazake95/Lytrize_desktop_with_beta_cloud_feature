"""
modules/analysis/statistical.py -- Statistical aggregation chart runner.
=======================================================================

Produces aggregated bar charts summarising numeric columns using a chosen
aggregation function (mean, sum, median, count, min, max).

Two operating modes depending on whether a group-by column is selected:

  Grouped mode:   One bar chart per metric, bars split by the categorical
                  group-by column. Useful for comparing averages across
                  departments, regions, product lines, etc.

  Overview mode:  A single bar chart showing the aggregated value for every
                  numeric column side by side, plus a companion standard
                  deviation chart. Useful for a quick "how do the numbers
                  compare?" across all metrics.
"""

import plotly.express as px
from modules.charts import chart_layout, COLORS, num_cols as _num_cols


def run_statistical(df, x_cols=None, y_cols=None, agg="mean", palette=None, **kwargs):
    """
    Generate statistical aggregation bar charts.

    Args:
        df:      Working DataFrame.
        x_cols:  List containing one categorical column to group by (optional).
                 If empty or None, overview mode is used.
        y_cols:  Numeric columns to aggregate. Defaults to all numeric cols.
        agg:     Aggregation function: "mean", "sum", "median", "count", "min", "max".
        palette: List of hex colour strings.
        **kwargs: Extra kwargs silently ignored.

    Returns:
        list of (title: str, fig: Figure) tuples.
        Grouped mode   → one chart per metric in y_cols.
        Overview mode  → two charts: aggregated overview + std dev.
    """
    charts    = []
    num       = y_cols or _num_cols()
    grp       = x_cols[0] if x_cols else None
    agg_label = agg.title()
    pal       = palette or COLORS

    if grp and grp in df.columns:
        # ── Grouped mode -- one chart per metric ───────────────────────────────
        for metric in num:
            agg_vals = df.groupby(grp)[metric].agg(agg).reset_index()
            agg_vals.columns = [grp, f"{agg_label} {metric}"]
            fig = px.bar(
                agg_vals, x=grp, y=f"{agg_label} {metric}",
                title=f"{agg_label} of {metric} by {grp}",
                color=grp, color_discrete_sequence=pal, text_auto=".2f")
            fig.update_layout(**chart_layout())
            charts.append((f"{agg_label} by {grp}", fig))

    else:
        # ── Overview mode -- all metrics in one chart ───────────────────────────
        summary = df[num].agg(agg).reset_index()
        summary.columns = ["Column", agg_label]
        fig = px.bar(
            summary, x="Column", y=agg_label,
            title=f"{agg_label} Overview",
            color="Column", color_discrete_sequence=pal, text_auto=".2f")
        fig.update_layout(**chart_layout())
        charts.append((f"{agg_label} Values", fig))

        # Companion standard deviation chart -- helps spot which columns vary most.
        stds = df[num].std().reset_index()
        stds.columns = ["Column", "Std Dev"]
        fig2 = px.bar(
            stds, x="Column", y="Std Dev",
            title="Standard Deviation",
            color="Column", color_discrete_sequence=pal, text_auto=".2f")
        fig2.update_layout(**chart_layout())
        charts.append(("Standard Deviation", fig2))

    return charts
