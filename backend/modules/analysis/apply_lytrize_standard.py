
"""
modules/analysis/apply_lytrize_standard.py -- Universal chart standardiser.
============================================================================

Centralises layout, hover, metadata, and editor-compatibility setup for all
Lytrize chart types. Call once per figure after the runner has built it.

Usage:
    fig = apply_lytrize_standard(
        fig,
        title="Revenue by Region",
        subtitle="2024 YTD",         
        xaxis="Region",
        yaxis="Revenue ($)",
        legend="Segment",
        height=480,
        analysis_type="categorical",
    )

The returned fig has:
  - chart_layout() applied (transparent bg, themed hover labels)
  - fig._lytrize_meta dict set (read by dashboard, insight engine, editor)
  - Axis titles applied via update_xaxes / update_yaxes
  - Legend title text set
  - Hover <extra>%{fullData.name}</extra> stripped to <extra></extra>

NOTE: title subtitle requires Plotly >= 5.21. Earlier versions receive the
subtitle appended to the main title string as a fallback.
"""

import importlib.metadata

from modules.charts import chart_layout


def _plotly_version() -> tuple:
    """Return (major, minor) for the installed Plotly version."""
    try:
        ver = importlib.metadata.version("plotly")
        parts = ver.split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return (5, 0)


_PLOTLY_SUPPORTS_SUBTITLE = _plotly_version() >= (5, 21)


def apply_lytrize_standard(
    fig,
    *,
    title,
    subtitle=None,
    xaxis=None,
    yaxis=None,
    legend=None,
    height=None,
    analysis_type="generic",
    notes_supported=True,
    insights_supported=True,
    axis_editing=True,
    legend_editing=True,
):
    """Universal Lytrize chart standardiser.

    Centralises:
    - layout application
    - hover consistency
    - editor compatibility
    - metadata contracts
    - legend styling
    - axis naming support
    - insight engine compatibility
    """

    layout = chart_layout(height=height)

    # Build title dict — subtitle key requires Plotly >= 5.21
    if _PLOTLY_SUPPORTS_SUBTITLE and subtitle:
        title_dict = dict(text=title, subtitle=dict(text=subtitle))
    elif subtitle:
        # Fallback: append subtitle to title separated by a newline
        title_dict = dict(text=f"{title}<br><sup>{subtitle}</sup>")
    else:
        title_dict = dict(text=title)

    fig.update_layout(
        **layout,
        title=title_dict,
        legend=dict(
            title=dict(text=legend or ""),
            bgcolor="rgba(15,23,42,0.35)",
            orientation="v"
        )
    )

    if xaxis:
        fig.update_xaxes(title=xaxis)

    if yaxis:
        fig.update_yaxes(title=yaxis)

    # Universal hover consistency
    for trace in fig.data:
        try:
            ht = getattr(trace, "hovertemplate", None)
            if ht and "%{fullData.name}" in ht:
                trace.hovertemplate = ht.replace(
                    "<extra>%{fullData.name}</extra>",
                    "<extra></extra>"
                )
        except Exception:
            pass

    fig._lytrize_meta = {
        "analysis_type": analysis_type,
        "x_axis": xaxis,
        "y_axis": yaxis,
        "legend": legend,
        "supports_auto_insights": insights_supported,
        "supports_notes": notes_supported,
        "supports_axis_editing": axis_editing,
        "supports_legend_editing": legend_editing,
    }

    return fig
