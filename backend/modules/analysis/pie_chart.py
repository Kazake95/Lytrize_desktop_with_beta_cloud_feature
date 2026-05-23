"""
modules/analysis/pie_chart.py -- Pie / Donut chart runner.
=========================================================

Produces donut charts (pie charts with a centre hole) showing the
proportional split of a categorical dimension across a numeric metric,
or across simple row counts when no metric is selected.

Features:
  - Top-N filtering with automatic "Other" roll-up for remaining slices.
  - Sort order applied before filtering so "Other" always contains the
    lowest-value categories.
  - hole=0.45 gives a donut appearance (set to 0 for a solid pie).

One chart is produced per (dimension × metric) combination.
"""

import plotly.express as px
import pandas as pd
from modules.charts import chart_layout, COLORS, cat_cols as _cat_cols


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sort_df(df_target, col_x: str, col_y: str, sort_by: str):
    """Apply the requested sort order to an aggregated DataFrame."""
    if sort_by == "Value (Desc)":   return df_target.sort_values(col_y, ascending=False)
    if sort_by == "Value (Asc)":    return df_target.sort_values(col_y, ascending=True)
    if sort_by == "Category (A-Z)": return df_target.sort_values(col_x, ascending=True)
    if sort_by == "Category (Z-A)": return df_target.sort_values(col_x, ascending=False)
    return df_target


def _apply_top_n(df_agg, name_col: str, val_col: str, top_n, group_others: bool = True):
    """
    Keep the top N rows by value; bundle remaining rows into an "Other" slice.

    Args:
        df_agg:       Aggregated DataFrame (name_col, val_col).
        name_col:     Column holding category labels.
        val_col:      Column holding numeric values.
        top_n:        Number of top categories to keep. None or 0 = keep all.
        group_others: If True, remaining categories are summed into an "Other" row.

    Returns:
        Filtered (and optionally extended with "Other") DataFrame.
    """
    if not top_n or top_n <= 0 or len(df_agg) <= top_n:
        return df_agg  # No filtering needed.

    top  = df_agg.nlargest(top_n, val_col)
    rest = df_agg[~df_agg[name_col].isin(top[name_col])]

    if group_others and len(rest) > 0:
        # Collapse all remaining categories into one "Other" slice.
        other_row = pd.DataFrame({name_col: ["Other"], val_col: [rest[val_col].sum()]})
        top = pd.concat([top, other_row], ignore_index=True)

    return top


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run_pie_chart(df, x_cols=None, y_cols=None, agg="mean", sort_by=None,
                  palette=None, top_n=None, **kwargs):
    """
    Generate donut charts for categorical dimensions.

    Args:
        df:      Working DataFrame.
        x_cols:  Categorical dimension columns. Defaults to first 2 cat cols.
        y_cols:  Numeric metric columns (optional). If None, value counts are used.
        agg:     Aggregation function string.
        sort_by: Sort key string.
        palette: List of hex colour strings.
        top_n:   Show only top N slices; remaining are grouped into "Other".
        **kwargs: Extra kwargs silently ignored.

    Returns:
        list of (title: str, fig: Figure) tuples.
    """
    charts    = []
    dims      = x_cols or _cat_cols()[:2]
    metrics   = y_cols
    agg_label = agg.title()
    pal       = palette or COLORS

    for col in dims:

        # ── Case A: metric column(s) → aggregate metric by dimension ──────────
        if metrics:
            for metric in metrics:
                agg_vals = df.groupby(col)[metric].agg(agg).reset_index()
                agg_vals.columns = [col, "Value"]
                agg_vals = _sort_df(agg_vals, col, "Value", sort_by)
                agg_vals = _apply_top_n(agg_vals, col, "Value", top_n)

                title_suffix = (
                    f" (Top {top_n})" if top_n and len(df[col].unique()) > top_n else "")
                fig = px.pie(
                    agg_vals, names=col, values="Value",
                    title=f"{agg_label} {metric} Split by {col}{title_suffix}",
                    color_discrete_sequence=pal,
                    hole=0.45)  # 0.45 = donut. Change to 0 for solid pie.
                fig.update_layout(**chart_layout())
                charts.append((f"Pie: {col}", fig))

        # ── Case B: no metric → value counts ──────────────────────────────────
        else:
            vc = df[col].value_counts().reset_index()
            vc.columns = [col, "Count"]
            vc = _sort_df(vc, col, "Count", sort_by)
            vc = _apply_top_n(vc, col, "Count", top_n)

            title_suffix = (
                f" (Top {top_n})" if top_n and len(df[col].unique()) > top_n else "")
            fig = px.pie(
                vc, names=col, values="Count",
                title=f"Distribution of {col}{title_suffix}",
                color_discrete_sequence=pal,
                hole=0.45)
            fig.update_layout(**chart_layout())
            charts.append((f"Pie Counts: {col}", fig))

    return charts
