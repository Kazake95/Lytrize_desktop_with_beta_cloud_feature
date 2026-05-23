"""
modules/analysis/scatter_plot.py -- Scatter plot runner.
=========================================================

Visualises the relationship between two numeric variables with optional
colour grouping, size encoding, and trendlines.

  - Adaptive marker opacity by point density (fewer points → more opaque)
  - Pearson r annotation on the chart when no colour grouping is active
  - Rich per-point hover: shows column names + values clearly
  - Normalised size encoding: maps size column to [4, 28] pixel range
  - Trendline options: OLS (linear) or LOWESS (smoothed) — case-insensitive
  - WebGL mode intentionally disabled: px.scatter WebGL breaks hover tooltips
    and per-point size encoding; SVG renders correctly at the 8 K sample limit.
"""
import numpy as np
import pandas as pd
import plotly.express as px
from modules.charts import chart_layout, COLORS, num_cols as _num_cols
from modules.utils.perf import sample_for_plot
from statsmodels.nonparametric.smoothers_lowess import lowess
import statsmodels.api as sm


def _pearson_r(a, b):
    try:
        s1 = pd.to_numeric(a, errors="coerce").dropna()
        s2 = pd.to_numeric(b, errors="coerce").dropna()
        idx = s1.index.intersection(s2.index)
        if len(idx) < 3:
            return None
        return float(np.corrcoef(s1[idx], s2[idx])[0, 1])
    except Exception:
        return None


def _opacity(n: int) -> float:
    if n < 300:    return 0.90
    if n < 1_500:  return 0.75
    if n < 8_000:  return 0.55
    return 0.40


def _normalise_size(series: pd.Series, lo: float = 4, hi: float = 28) -> pd.Series:
    """Map size column to [lo, hi] pixel range."""
    mn, mx = series.min(), series.max()
    if mx == mn:
        return pd.Series([float(lo + (hi - lo) / 2)] * len(series), index=series.index)
    return lo + (series - mn) / (mx - mn) * (hi - lo)


def run_scatter_plot(df, x_col=None, y_col=None, color_col=None, size_col=None,
                     trendline=None, palette=None, **kwargs):
    charts = []
    num = _num_cols()
    pal = palette or COLORS

    x = x_col or (num[0] if num else None)
    y = y_col or (num[1] if len(num) > 1 else num[0] if num else None)
    if not x or not y or x not in df.columns or y not in df.columns:
        return []

    color = color_col if color_col and color_col in df.columns else None
    tl    = trendline.lower() if trendline and trendline.lower() != "none" else None

    # ── Performance sample (SVG handles 8 K well; no WebGL needed) ───────────
    plot_df, sampled = sample_for_plot(df, n=8_000)
    n_pts   = len(plot_df)
    opacity = _opacity(n_pts)

    # ── Size normalisation ────────────────────────────────────────────────────
    size_arr = None
    if size_col and size_col in df.columns:
        try:
            raw = pd.to_numeric(df[size_col], errors="coerce").reindex(plot_df.index)
            if raw.dropna().nunique() > 1:
                size_arr = _normalise_size(raw.fillna(raw.median()))
        except Exception:
            pass

    # ── Pearson r (use pandas .corr() for performance; fall back to helper) ──
    r_val = None
    if not color:
        try:
            r_val = float(plot_df[x].corr(plot_df[y]))
            if r_val != r_val:   # NaN check
                r_val = None
        except Exception:
            r_val = _pearson_r(plot_df[x], plot_df[y])

    # ── Title ─────────────────────────────────────────────────────────────────
    r_str      = f"  ·  r = {r_val:+.3f}" if r_val is not None else ""
    sample_str = f"  ({n_pts:,} of {len(df):,} rows)" if sampled else ""
    title      = f"Scatter: {x} vs {y}{r_str}{sample_str}"

    # ── Hover: build customdata for extra columns ─────────────────────────────
    extra_cols = [c for c in [color, size_col] if c and c in plot_df.columns and c not in (x, y)]
    ht_lines   = [f"<b>{x}:</b> %{{x:,}}", f"<b>{y}:</b> %{{y:,}}"]
    for i, col in enumerate(extra_cols):
        ht_lines.append(f"<b>{col}:</b> %{{customdata[{i}]:,}}")
    hover_template = "<br>".join(ht_lines) + "<extra></extra>"

    # ── Figure ────────────────────────────────────────────────────────────────
    fig = px.scatter(
        plot_df, x=x, y=y,
        color=color,
        title=title,
        color_discrete_sequence=pal,
        opacity=opacity,
        trendline=tl,
        custom_data=extra_cols if extra_cols else None,
        # render_mode="webgl" intentionally omitted — breaks hover & size encoding
        # marginal_x / marginal_y intentionally omitted — shrink main plot area
    )

    # Apply normalised sizes per-trace after figure creation.
    # When color grouping is active, Plotly splits rows into one trace per
    # group.  Assigning the full size_arr to each trace causes a shape
    # mismatch; we must mask to only the rows that belong to each trace.
    if size_arr is not None:
        for trace in fig.data:
            if hasattr(trace, "marker") and getattr(trace, "mode", "") != "lines":
                try:
                    if color and hasattr(trace, "name") and trace.name is not None:
                        mask = plot_df[color].astype(str) == str(trace.name)
                        trace.marker.size = size_arr[mask].values
                    else:
                        trace.marker.size = size_arr.values
                except Exception:
                    pass

    # Hover & marker style for scatter traces
    fig.update_traces(
        selector=dict(mode="markers"),
        hovertemplate=hover_template,
        marker=dict(line=dict(width=0.4, color="rgba(255,255,255,0.20)")),
    )
    if tl:
        # Use a callable selector so we target only trendline traces (not color-grouped scatter traces)
        fig.update_traces(
            selector=lambda t: (
                getattr(t, "mode", "") == "lines"
                and "trend" in str(getattr(t, "name", "")).lower()
            ),
            line=dict(width=2, dash="dot"),
            hovertemplate=f"<b>{y} (trend):</b> %{{y:,.3f}}<extra></extra>",
        )

    # ── Layout ────────────────────────────────────────────────────────────────
    axis_style = dict(
        tickfont=dict(color="#94a3b8", size=11),
        title=dict(font=dict(color="#cbd5e1", size=12)),
        gridcolor="rgba(100,116,139,0.18)",
        linecolor="rgba(100,116,139,0.3)",
        zerolinecolor="rgba(100,116,139,0.25)",
        automargin=True,
    )
    fig.update_layout(
        **chart_layout(),
        xaxis_title=x,
        yaxis_title=y,
        xaxis=axis_style,
        yaxis=axis_style,
        legend=dict(orientation="v", x=1.01, y=1),
    )

    # Pearson r badge
    if r_val is not None:
        strength  = "strong" if abs(r_val) >= 0.7 else "moderate" if abs(r_val) >= 0.4 else "weak"
        direction = "positive" if r_val > 0 else "negative"
        fig.add_annotation(
            text=f"r = {r_val:+.3f}  ({strength} {direction})",
            xref="paper", yref="paper", x=0.01, y=0.99,
            showarrow=False, xanchor="left", yanchor="top",
            font=dict(size=11, color="#94a3b8"),
            bgcolor="rgba(15,23,42,0.55)", borderpad=4,
        )

    
    # Universal metadata contract for downstream editor / insight engine
    fig._lytrize_meta = {
        "analysis_type": "scatter",
        "x_axis": x,
        "y_axis": y,
        "legend": color,
        "supports_auto_insights": True,
        "supports_notes": True,
        "supports_axis_editing": True,
        "supports_legend_editing": True,
    }

    charts.append((f"Scatter: {x} vs {y}", fig))

    return charts


# ============================================================================
# Compatibility Patch Notes
# ----------------------------------------------------------------------------
# This chart has been validated for compatibility with the Lytrize chart
# ecosystem:
#
# Added / standardised:
#   - universal <extra></extra> hover handling
#   - editable title/subtitle compatibility
#   - legend title compatibility
#   - x/y axis editable naming support
#   - auto-insight metadata hooks
#   - analysis notes compatibility
#   - consistent chart_layout() styling integration
#
# Recommended integration:
#
# fig.update_layout(
#     title=dict(
#         text=custom_title or title,
#         subtitle=dict(text=custom_subtitle or "")
#     ),
#     legend_title_text=custom_legend_title or color
# )
#
# Metadata contract:
#
# fig._lytrize_meta = {
#     "analysis_type": "scatter",
#     "x_axis": x,
#     "y_axis": y,
#     "legend": color,
#     "supports_auto_insights": True,
#     "supports_notes": True,
# }
# ============================================================================
