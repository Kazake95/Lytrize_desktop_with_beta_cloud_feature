"""
modules/charts.py -- Shared chart utilities, palettes, and the auto-insight engine.
===================================================================================

This module is the central import point for all analysis runner modules.
Import from here -- never reach into individual analysis files for these helpers.

Contents
────────
  COLORS / DANGER          -- default colour lists
  PALETTES                 -- named palette dict shown in the UI
  chart_layout()           -- common Plotly layout dict (transparent bg, margins)
  num_cols() / cat_cols() / dt_cols() -- column-type lists from session_state
  clean_insight_text()     -- strip Markdown from auto-generated insight strings
  clean_insights()         -- clean a list of insight strings
  _fmt_num()               -- human-readable number formatter (1.2K, 3.4M, etc.)
  _fmt_pct()               -- percentage string with sign
  _plural()                -- singular/plural helper
  _fmt_label()             -- smart date/string label formatter
  _as_number_series()      -- coerce any value list to a numeric pd.Series
  _as_list()               -- safely convert any value to a plain list
  charts_to_json()         -- serialise the chart list to a JSON string for the DB
  generate_chart_insights() -- auto-insight engine: reads a Plotly figure and
                              produces plain-English observations

"""

import json
import re
import streamlit as st
import pandas as pd
import plotly.io as pio


# ─────────────────────────────────────────────────────────────────────────────
# Colour constants
# ─────────────────────────────────────────────────────────────────────────────

# Default 8-colour palette -- used as a fallback when no palette is selected.
COLORS = ["#6163df", "#8566fc", "#3390c8", "#f59e0b",
          "#ef4444", "#10b981", "#ec4899", "#f97316"]

# Red-to-green gradient used for "danger" colour scales (e.g. missing % charts).
DANGER = ["#bbf7d0", "#fbbf24", "#ef4444"]

# Named palettes shown in the colour-palette selectbox on the analysis page.
# Keys are display labels; values are ordered lists of 8 hex colours.
PALETTES = {
    "🔵 Default Blue-Purple": ["#6163df", "#8566fc", "#3390c8", "#f59e0b",
                                "#ef4444", "#10b981", "#ec4899", "#f97316"],
    "🌈 Vibrant":             ["#e63946", "#f4a261", "#2a9d8f", "#457b9d",
                                "#e9c46a", "#264653", "#a8dadc", "#f1faee"],
    "🍃 Nature Green":        ["#2d6a4f", "#40916c", "#52b788", "#74c69d",
                                "#95d5b2", "#b7e4c7", "#d8f3dc", "#1b4332"],
    "🌅 Warm Sunset":         ["#e76f51", "#f4a261", "#e9c46a", "#264653",
                                "#2a9d8f", "#e63946", "#f1faee", "#457b9d"],
    "🩷 Pink & Coral":        ["#ff6b6b", "#feca57", "#48dbfb", "#ff9ff3",
                                "#54a0ff", "#5f27cd", "#01abc6", "#ff9f43"],
    "🌊 Ocean Blues":         ["#03045e", "#0077b6", "#00b4d8", "#90e0ef",
                                "#caf0f8", "#023e8a", "#0096c7", "#ade8f4"],
    "🟣 Monochrome Purple":   ["#3c096c", "#5a189a", "#7b2fbe", "#9d4edd",
                                "#c77dff", "#e0aaff", "#240046", "#10002b"],
    "🔆 Pastel Light":        ["#ffadad", "#ffd6a5", "#fdffb6", "#caffbf",
                                "#9bf6ff", "#a0c4ff", "#bdb2ff", "#ffc6ff"],
}


# ─────────────────────────────────────────────────────────────────────────────
# Chart layout defaults
# ─────────────────────────────────────────────────────────────────────────────

def chart_layout(height: int | None = None) -> dict:
    """
    Return a dict of Plotly layout kwargs used by every chart in Lytrize.

    Apply with:  fig.update_layout(**chart_layout())
               fig.update_layout(**chart_layout(height=520))

    Args:
        height: Optional fixed pixel height to include in the layout dict.
                When None (default) no height key is set, allowing Plotly's
                responsive sizing to fill the container width naturally.
                Pass an int (e.g. 520) for charts that need a fixed height,
                such as heatmaps, maps, and pivot tables.

    Transparent backgrounds let charts blend with the glassmorphism UI.
    Margins are kept tight; individual runners override them if needed
    (e.g. horizontal bar charts need extra right margin for value labels).
    """
    layout = dict(
        paper_bgcolor="rgba(0,0,0,0)",  # Transparent outer background.
        plot_bgcolor="rgba(0,0,0,0)",   # Transparent inner plot area.
        margin=dict(l=20, r=20, t=48, b=20),
        bargap=0.28,
        bargroupgap=0.1,
        # ── Hover tooltip styling ──────────────────────────────────────────────
        # Dark semi-opaque background so text is legible on any chart colour.
        # namelength=-1 shows the full trace name instead of truncating it.
        #
        # IMPORTANT:
        # Plotly renders the <extra> hover section as a separate tooltip badge.
        # In dual-axis charts (especially bar + scatter combinations), Plotly
        # applies its own default white background + coloured text styling to
        # that extra section, bypassing our global hoverlabel theme.
        #
        # To keep ALL chart types visually consistent, every auto-generated
        # hovertemplate below explicitly uses:
        #
        #     <extra></extra>
        #
        # instead of:
        #
        #     <extra></extra>
        #
        # This makes the hover render as a single themed tooltip everywhere,
        # including future dual-axis charts and newly added trace combinations.
        hovermode="closest",
        hoverlabel=dict(
            bgcolor="rgba(18, 26, 45, 0.96)",
            bordercolor="rgba(133, 102, 252, 0.30)",
            font=dict(size=13, color="#f5f7ff", family="Inter, system-ui, sans-serif"),
            namelength=-1,
        ),
    )
    if height is not None:
        layout["height"] = int(height)
    return layout


def apply_hover_format(fig) -> None:
    """
    Apply K/M/B-formatted hovertemplates to every trace in a Plotly figure.

    Called once in dashboard._render_chart() so it covers every chart type
    without requiring each runner to set its own template.

    Applies per trace type: bar, scatter, histogram, pie/sunburst, heatmap.
    Traces with a customdata-driven template are skipped (preserved as-is).
    Box/violin/other traces keep Plotly defaults.
    """
    for trace in fig.data:
        existing = getattr(trace, "hovertemplate", None) or ""
        # Preserve templates that reference customdata columns.
        if "customdata" in existing:
            continue

        ttype = type(trace).__name__.lower()

        if ttype == "bar":
            orient = getattr(trace, "orientation", "v") or "v"
            if orient == "h":
                # Horizontal: x = value, y = category label
                trace.hovertemplate = (
                    "<b>%{y}</b><br>"
                    "Value: <b>%{x:.3~s}</b>"
                    "<extra></extra>"
                )
            else:
                # Vertical: x = category label, y = value
                trace.hovertemplate = (
                    "<b>%{x}</b><br>"
                    "Value: <b>%{y:.3~s}</b>"
                    "<extra></extra>"
                )

        elif ttype == "scatter":
            trace.hovertemplate = (
                "<b>%{x}</b><br>"
                "Value: <b>%{y:.3~s}</b>"
                "<extra></extra>"
            )

        elif ttype == "histogram":
            trace.hovertemplate = (
                "Range: <b>%{x}</b><br>"
                "Count: <b>%{y:.3~s}</b>"
                "<extra></extra>"
            )

        elif ttype in ("pie", "sunburst"):
            trace.hovertemplate = (
                "<b>%{label}</b><br>"
                "Value: <b>%{value:.3~s}</b><br>"
                "Share: %{percent}"
                "<extra></extra>"
            )

        elif ttype == "heatmap":
            # Correlation matrices: keep full decimal precision, no SI units.
            trace.hovertemplate = (
                "%{x} × %{y}<br>"
                "r = <b>%{z:.3f}</b>"
                "<extra></extra>"
            )
        # box / violin / candlestick / choropleth / other: leave Plotly defaults


# ─────────────────────────────────────────────────────────────────────────────
# Column-type accessors
# ─────────────────────────────────────────────────────────────────────────────
# These read from session_state which is populated by the column classifier
# on the upload page (modules/ui/column_tools.py :: show_column_classifier()).
# Use these helpers in runner modules -- never read session_state directly.

def num_cols() -> list:
    """Return the list of numeric column names confirmed by the user."""
    return st.session_state.get("num_cols", [])

def cat_cols() -> list:
    """Return the list of categorical column names confirmed by the user."""
    return st.session_state.get("cat_cols", [])

def dt_cols() -> list:
    """Return the list of date/time column names confirmed by the user."""
    return st.session_state.get("dt_cols", [])


# ─────────────────────────────────────────────────────────────────────────────
# Insight text utilities
# ─────────────────────────────────────────────────────────────────────────────

def clean_insight_text(text) -> str:
    """
    Strip Markdown formatting from an auto-generated insight string.

    Removes **bold**, __underline__, and normalises separator spacing.
    Used to produce plain text safe for both display and PDF export.
    """
    s = str(text or "")
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)  # **bold** → bold
    s = s.replace("__", "")
    s = s.replace("  ·  ", " · ")
    return s.strip()


def clean_insights(insights) -> list:
    """Clean and filter a list of raw insight strings. Removes empty entries."""
    return [s for s in (clean_insight_text(i) for i in (insights or [])) if s]


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers  (private -- used only within this module)
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_num(value) -> str:
    """
    Format a number as a compact, human-readable string.

    Examples:
        1234567  → "1.2M"
        98765    → "98.8K"
        42.5     → "42.50"
        42.0     → "42"
    """
    try:
        v = float(value)
    except Exception:
        return str(value)
    if pd.isna(v):
        return "n/a"
    sign = "-" if v < 0 else ""
    av = abs(v)
    if av >= 1_000_000_000: return f"{sign}{av / 1_000_000_000:.1f}B"
    if av >= 1_000_000:     return f"{sign}{av / 1_000_000:.1f}M"
    if av >= 1_000:         return f"{sign}{av / 1_000:.1f}K"
    if av == int(av):       return f"{int(v):,}"
    return f"{v:,.2f}"


def _fmt_pct(value) -> str:
    """Format a float as a percentage string with sign: 0.123 → '+12.3%'."""
    try:
        return f"{float(value):+.1f}%"
    except Exception:
        return "n/a"


def _plural(count, singular: str, plural: str = None) -> str:
    """Return singular or plural noun based on count."""
    return singular if int(count) == 1 else (plural or f"{singular}s")


def _fmt_label(value) -> str:
    """
    Format a value as a readable label, auto-detecting datetime strings.

    Dates with time components → "15 Jan 2024 14:30"
    Dates without               → "15 Jan 2024"
    Everything else             → str(value)
    """
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.notna(ts):
            if ts.hour or ts.minute or ts.second:
                return ts.strftime("%d %b %Y %H:%M")
            return ts.strftime("%d %b %Y")
    except Exception:
        pass
    return str(value)


def _as_number_series(values) -> pd.Series:
    """Coerce any iterable of values to a numeric pd.Series, dropping non-numeric."""
    return pd.to_numeric(pd.Series(values), errors="coerce").dropna()


def _as_list(values) -> list:
    """Safely convert any value to a plain Python list. Returns [] on failure."""
    if values is None:
        return []
    try:
        return list(values)
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Chart serialisation
# ─────────────────────────────────────────────────────────────────────────────

def charts_to_json(charts: list) -> str:
    """
    Serialise the active chart list to a JSON string for database storage.

    Each entry in `charts` must be a tuple of (uid, title, fig).
    Additional per-chart metadata is read from session_state using the uid.

    Session state keys read per chart:
        desc_{uid}           -- user's free-text description
        auto_insights_{uid}  -- list of auto-generated insight strings
        chart_type_{uid}     -- analysis type string (e.g. "categorical")
        chart_meta_{uid}     -- dict of arbitrary metadata

    Args:
        charts: List of (uid: str, title: str, fig: Figure) tuples.

    Returns:
        JSON string -- stored in the sessions and draft_sessions tables.
    """
    out = []
    for chart in charts:
        uid, title, fig = chart[:3]
        desc          = st.session_state.get(f"desc_{uid}", "")
        auto_insights = clean_insights(st.session_state.get(f"auto_insights_{uid}", []))
        chart_type    = st.session_state.get(f"chart_type_{uid}", "")
        meta          = st.session_state.get(f"chart_meta_{uid}", {})
        try:
            out.append({
                "uid":           uid,
                "title":         title,
                "fig_json":      pio.to_json(fig),   # Full Plotly JSON spec.
                "desc":          desc,
                "auto_insights": auto_insights,
                "chart_type":    chart_type,
                "meta":          meta,
            })
        except Exception:
            pass  # Skip serialisation failures -- damaged figures silently omitted.
    return json.dumps(out)


# ─────────────────────────────────────────────────────────────────────────────
# Auto-insight engine
# ─────────────────────────────────────────────────────────────────────────────

def generate_chart_insights(chart_type: str, title: str, fig,
                             col_descriptions: dict = None) -> list:
    """
    Produce plain-English observations from a Plotly figure.

    Called automatically after chart generation (pages/analysis.py).
    Insights are stored in session_state[f"auto_insights_{uid}"] and
    displayed below each chart card.

    When the user has filled in column descriptions on the upload page,
    those descriptions are woven directly into the insight text so each
    observation reads like a business-analyst comment rather than a
    generic statistical note.

    Args:
        chart_type:       Analysis type string (e.g. "distribution", "outlier").
        title:            Chart title string used as a fallback match signal.
        fig:              Plotly Figure object.
        col_descriptions: Optional dict {column_name: description}.

    Returns:
        list of plain-text insight strings (Markdown stripped).
    """
    insights = []
    tl = title.lower()
    col_desc = col_descriptions or {}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _named(col: str) -> str:
        """Return 'col (description)' when a description exists, else 'col'."""
        desc = col_desc.get(col, "").strip()
        if desc:
            short = desc[:55] + "…" if len(desc) > 55 else desc
            return f"{col} ({short})"
        return col

    def _primary_col_from_title() -> str:
        """
        Extract the primary column name from known title prefixes.
        e.g. "Dist: Revenue"  → "Revenue"
             "TS: Sales"      → "Sales"
             "Outliers: Price"→ "Price"
        """
        for prefix in ("Dist: ", "TS: ", "Outliers: ", "Trend: ",
                       "Counts: ", "Time Series: "):
            if title.startswith(prefix):
                return title[len(prefix):]
        return ""

    def _cols_in_title() -> list:
        """Return all col_description keys whose name appears in the chart title."""
        return [c for c in col_desc if c and c.lower() in tl and col_desc[c].strip()]

    def _append_desc_context():
        """
        Append a short 'What these columns mean' footnote for any columns
        referenced in the chart that have user-provided descriptions.
        Only fires when descriptions haven't already been woven into the text.
        """
        relevant = _cols_in_title()
        for col in relevant:
            desc = col_desc[col].strip()
            if desc and col not in " ".join(insights):
                insights.append(f"Column context — {col}: {desc}")

    # ── Distribution ──────────────────────────────────────────────────────────
    if chart_type == "distribution" or "dist:" in tl:
        try:
            arr = _as_number_series(fig.data[0].x)
            if arr.empty:
                return []
            col = _primary_col_from_title() or "this column"
            mean, median, std = arr.mean(), arr.median(), arr.std()
            skew = float(arr.skew())

            # Lead with the column's business meaning if available
            insights.append(
                f"{_named(col)} centres around {_fmt_num(median)} "
                f"(median). The average is {_fmt_num(mean)}, "
                f"with a typical spread of ±{_fmt_num(std)}."
            )

            # Plain-language skew interpretation
            if abs(skew) > 1.5:
                if skew > 0:
                    insights.append(
                        "A small number of unusually high values are pulling the average "
                        "above the typical case — the median is the more reliable benchmark here."
                    )
                else:
                    insights.append(
                        "A few very low values are dragging the average down — "
                        "the median gives a fairer picture of the typical record."
                    )
            elif abs(skew) > 0.5:
                direction = "higher" if skew > 0 else "lower"
                insights.append(
                    f"The distribution leans slightly {direction}, "
                    "so averages and medians tell a similar but not identical story."
                )
            else:
                insights.append(
                    "Values are symmetrically distributed — the average and median "
                    "are close, making either a reliable summary."
                )

            # Outlier count
            q1, q3 = arr.quantile(0.25), arr.quantile(0.75)
            iqr = q3 - q1
            n_out = int(((arr < q1 - 1.5 * iqr) | (arr > q3 + 1.5 * iqr)).sum())
            if n_out > 0:
                pct_out = n_out / len(arr) * 100
                insights.append(
                    f"{n_out:,} {_plural(n_out, 'value')} ({pct_out:.1f}%) "
                    f"{'sits' if n_out == 1 else 'sit'} outside the normal range — "
                    "check these before using totals or averages in reports."
                )

            # Percentile range for context
            p10, p90 = arr.quantile(0.10), arr.quantile(0.90)
            insights.append(
                f"The middle 80% of records fall between "
                f"{_fmt_num(p10)} and {_fmt_num(p90)}."
            )
        except Exception:
            pass

    # ── Correlation ───────────────────────────────────────────────────────────
    elif chart_type == "correlation" or "correlation" in tl:
        try:
            z        = fig.data[0].z
            x_labels = _as_list(getattr(fig.data[0], "x", None))
            y_labels = _as_list(getattr(fig.data[0], "y", None)) or x_labels
            if z is not None:
                best = None
                for r, row in enumerate(z):
                    for c, val in enumerate(row):
                        if r == c or val is None:
                            continue
                        try:
                            fv = float(val)
                        except Exception:
                            continue
                        if abs(fv) >= 1:
                            continue
                        if best is None or abs(fv) > abs(best[0]):
                            left  = str(y_labels[r]) if r < len(y_labels) else f"Column {r+1}"
                            right = str(x_labels[c]) if c < len(x_labels) else f"Column {c+1}"
                            best  = (fv, left, right)
                if best:
                    strength  = ("strong" if abs(best[0]) >= 0.7
                                 else "moderate" if abs(best[0]) >= 0.4 else "weak")
                    direction = ("tend to rise together"   if best[0] > 0
                                 else "move in opposite directions")
                    insights.append(
                        f"{_named(best[1])} and {_named(best[2])} show the "
                        f"strongest link: {strength} ({best[0]:+.2f}) — they {direction}."
                    )
                    if abs(best[0]) >= 0.7:
                        insights.append(
                            "A correlation above 0.7 is worth investigating for a "
                            "cause-and-effect relationship, though correlation alone "
                            "does not prove causation."
                        )
                else:
                    insights.append(
                        "No clear relationship stands out. "
                        "The selected columns appear largely independent of each other."
                    )

            # Count weak vs strong pairs for a summary
            try:
                strong_pairs, total_pairs = 0, 0
                for r, row in enumerate(z):
                    for c_idx, val in enumerate(row):
                        if r >= c_idx or val is None:
                            continue
                        try:
                            fv = float(val)
                            total_pairs += 1
                            if abs(fv) >= 0.6:
                                strong_pairs += 1
                        except Exception:
                            pass
                if total_pairs > 1:
                    insights.append(
                        f"{strong_pairs} of {total_pairs} column pairs "
                        f"{'has' if strong_pairs == 1 else 'have'} a correlation "
                        f"above 0.6 — scan the darkest cells for the most actionable links."
                    )
            except Exception:
                pass

            insights.append(
                "Use this as a lead for deeper investigation, not as proof "
                "that one column directly causes changes in another."
            )
        except Exception:
            pass

    # ── Outlier detection ─────────────────────────────────────────────────────
    elif chart_type == "outlier" or "outlier" in tl:
        try:
            col = _primary_col_from_title() or "this column"
            outlier_trace = next(
                (t for t in fig.data
                 if "outlier" in str(getattr(t, "name", "")).lower()), None)
            normal_trace = next(
                (t for t in fig.data
                 if "normal" in str(getattr(t, "name", "")).lower()), None)

            total_pts = sum(
                len(getattr(t, "y", None) or []) for t in fig.data
                if getattr(t, "y", None) is not None
            )

            if outlier_trace and len(getattr(outlier_trace, "y", []) or []) > 0:
                n    = len(outlier_trace.y)
                vals = _as_number_series(outlier_trace.y)
                pct  = n / total_pts * 100 if total_pts > 0 else 0
                if not vals.empty:
                    insights.append(
                        f"{_named(col)} has {n:,} {_plural(n, 'outlier')} "
                        f"({pct:.1f}% of records), ranging from "
                        f"{_fmt_num(vals.min())} to {_fmt_num(vals.max())}."
                    )
                else:
                    insights.append(
                        f"{_named(col)}: {n:,} {_plural(n, 'outlier')} detected "
                        f"({pct:.1f}% of records)."
                    )
                if pct > 10:
                    insights.append(
                        "Over 10% of records are flagged — this may indicate a "
                        "measurement scale issue, data-entry errors, or a genuine "
                        "multi-modal distribution. Review before computing averages."
                    )
                elif n > 5:
                    insights.append(
                        "Check these rows individually — they could be data-entry "
                        "mistakes or legitimately exceptional events worth noting."
                    )
                else:
                    insights.append(
                        "A small number of outliers. Inspect each one; a single "
                        "extreme value can shift averages and totals significantly."
                    )
            else:
                insights.append(
                    f"No outliers detected in {_named(col)} using the standard "
                    "IQR (1.5× interquartile range) threshold. The data looks clean."
                )
        except Exception:
            pass

    # ── Time series ───────────────────────────────────────────────────────────
    elif chart_type == "time_series" or "ts:" in tl or "trend" in tl:
        try:
            col = _primary_col_from_title() or "the metric"
            y = _as_number_series(fig.data[0].y)
            x_vals = _as_list(getattr(fig.data[0], "x", None))
            if len(y) >= 2:
                trend = ("increased" if y.iloc[-1] > y.iloc[0]
                         else "decreased" if y.iloc[-1] < y.iloc[0] else "stayed flat")
                pct = ((y.iloc[-1] - y.iloc[0]) / abs(y.iloc[0]) * 100
                       if y.iloc[0] != 0 else 0)
                insights.append(
                    f"{_named(col)} {trend} overall — "
                    f"from {_fmt_num(y.iloc[0])} to {_fmt_num(y.iloc[-1])} "
                    f"({_fmt_pct(pct)} change from first to last period)."
                )

                # Peak and trough with labels
                peak_i = int(y.reset_index(drop=True).idxmax())
                low_i  = int(y.reset_index(drop=True).idxmin())
                peak_x = f" at {_fmt_label(x_vals[peak_i])}" if peak_i < len(x_vals) else ""
                low_x  = f" at {_fmt_label(x_vals[low_i])}"  if low_i  < len(x_vals) else ""
                insights.append(
                    f"Peak: {_fmt_num(y.max())}{peak_x}. "
                    f"Lowest: {_fmt_num(y.min())}{low_x}. "
                    f"The range spans {_fmt_num(y.max() - y.min())}."
                )

                # Volatility / consistency signal
                cv = y.std() / abs(y.mean()) if y.mean() != 0 else 0
                if cv > 0.5:
                    insights.append(
                        "High variability across periods — look for recurring "
                        "seasonal patterns or one-off spikes before using this trend "
                        "for forecasting."
                    )
                elif cv < 0.1:
                    insights.append(
                        "Very consistent across periods — a reliable baseline "
                        "for benchmarking or target-setting."
                    )
                else:
                    insights.append(
                        "Moderate variability. Look for repeating peaks or dips; "
                        "they often point to seasonality or cyclical business patterns."
                    )
        except Exception:
            insights.append(
                "Look for repeating peaks or dips; those often point to "
                "seasonality or operating patterns."
            )

    # ── Categorical & pie ─────────────────────────────────────────────────────
    elif (chart_type in ("categorical", "pie_chart")
          or any(k in tl for k in ("count", "bar", "pie", "donut"))):
        try:
            data = fig.data[0]
            is_horiz = getattr(data, "orientation", "v") == "h"
            if is_horiz:
                vals = [v for v in _as_list(getattr(data, "x", None)) if v is not None]
                xs   = _as_list(getattr(data, "y", None))
            elif (hasattr(data, "y") and data.y is not None
                  and not isinstance(data.y[0] if len(data.y) else 0, str)):
                vals = [v for v in _as_list(data.y) if v is not None]
                xs   = _as_list(getattr(data, "x", None))
            elif hasattr(data, "values") and data.values is not None:
                vals = _as_list(data.values)
                xs   = _as_list(getattr(data, "labels", None))
            else:
                vals = [v for v in _as_list(getattr(data, "x", None))
                        if isinstance(v, (int, float))]
                xs   = _as_list(getattr(data, "y", None))

            if vals:
                vals    = [float(v) for v in vals]
                total   = sum(v for v in vals if v)
                top_i   = vals.index(max(vals))
                bot_i   = vals.index(min(vals))
                top_cat = xs[top_i] if xs and top_i < len(xs) else str(top_i)
                bot_cat = xs[bot_i] if xs and bot_i < len(xs) else str(bot_i)
                top_pct = (max(vals) / total * 100) if total else 0

                # Top category with context if the column is described
                cat_col = next((c for c in col_desc if c.lower() in tl), "")
                cat_ctx = f" ({col_desc[cat_col].strip()[:50]})" if cat_col and col_desc.get(cat_col) else ""
                insights.append(
                    f"{top_cat}{cat_ctx} leads at {_fmt_num(max(vals))}, "
                    f"representing {top_pct:.1f}% of the total."
                )

                n_cats = len(vals)
                if n_cats > 1:
                    sorted_vals = sorted(vals, reverse=True)
                    # Gap between #1 and #2
                    if len(sorted_vals) > 1 and sorted_vals[1]:
                        ratio = sorted_vals[0] / sorted_vals[1]
                        if ratio >= 2:
                            insights.append(
                                f"The top category is {ratio:.1f}× the second — "
                                "a clear leader with a significant gap."
                            )
                        elif ratio >= 1.1:
                            insights.append(
                                f"The leader is {ratio:.1f}× the next category — "
                                "a meaningful but not extreme gap."
                            )

                    # Concentration / balance
                    even_pct      = 100 / n_cats
                    concentration = max(vals) / total * 100
                    if concentration > 2.5 * even_pct:
                        insights.append(
                            f"Highly concentrated — a single category holds "
                            f"{top_pct:.0f}% of the total across {n_cats} options. "
                            "This creates dependency risk."
                        )
                    elif concentration < 1.5 * even_pct:
                        insights.append(
                            f"Values are evenly spread across {n_cats} categories "
                            "— no single category dominates."
                        )

                    # Bottom performer
                    bot_pct = (min(vals) / total * 100) if total else 0
                    insights.append(
                        f"Lowest: {bot_cat} at {_fmt_num(min(vals))} ({bot_pct:.1f}%). "
                        "If this is a revenue or performance metric, it may be worth reviewing."
                    )
        except Exception:
            pass

    # ── Scatter plot ──────────────────────────────────────────────────────────
    elif chart_type in ("scatter", "scatter_plot") or "scatter:" in tl:
        try:
            # Column names from title: "Scatter: X vs Y"
            cols_match = re.search(r"Scatter:\s*(.+?)\s+vs\s+(.+?)(\s|$|·|—)", title)
            x_col = cols_match.group(1).strip() if cols_match else "X"
            y_col = cols_match.group(2).strip() if cols_match else "Y"

            # ── Detect trendline type ─────────────────────────────────────────
            # Plotly trendline traces are "scatter" mode="lines" with a name
            # that contains "OLS" or the trendline equation.  LOWESS traces
            # have no equation name.  Both have no marker attribute.
            has_ols    = False
            has_lowess = False
            ols_slope  = None
            for _t in fig.data:
                _mode = str(getattr(_t, "mode", ""))
                _name = str(getattr(_t, "name", "")).lower()
                if "lines" in _mode and "markers" not in _mode:
                    if "ols" in _name or "=" in _name or "trendline" in _name:
                        has_ols = True
                        # Try to extract slope from name like "y = 0.42x + 5.1"
                        _slope_m = re.search(r"y\s*=\s*([+-]?[\d.]+)x", _name)
                        if _slope_m:
                            try: ols_slope = float(_slope_m.group(1))
                            except: pass
                    else:
                        has_lowess = True

            # ── Pearson r (from title annotation) ────────────────────────────
            r_match = re.search(r"r\s*=\s*([+-]?\d+\.\d+)", title)
            r_val   = float(r_match.group(1)) if r_match else None

            # ── Scatter trace (for point count) ──────────────────────────────
            scatter_trace = next(
                (t for t in fig.data if "markers" in str(getattr(t, "mode", ""))), None)

            # ── Correlation insight ───────────────────────────────────────────
            if r_val is not None:
                strength  = "strong" if abs(r_val) >= 0.7 else "moderate" if abs(r_val) >= 0.4 else "weak"
                direction = "positive" if r_val > 0 else "negative"
                insights.append(
                    f"{_named(x_col)} and {_named(y_col)} show a {strength} {direction} "
                    f"linear correlation (r = {r_val:+.3f})."
                )
                if abs(r_val) >= 0.7:
                    insights.append(
                        "A strong correlation suggests a predictable relationship — "
                        "consider a regression model, but verify causality before acting."
                    )
                elif abs(r_val) >= 0.4:
                    insights.append(
                        "A moderate correlation exists. The relationship may be real "
                        "but is likely influenced by other variables — check for confounders."
                    )
                else:
                    insights.append(
                        "The weak linear correlation suggests these variables move largely "
                        "independently. A non-linear pattern may still exist — the LOWESS "
                        "trendline option can reveal curved relationships."
                    )

            # ── OLS trendline explanation ─────────────────────────────────────
            if has_ols:
                if ols_slope is not None:
                    direction_word = "increases" if ols_slope > 0 else "decreases"
                    insights.append(
                        f"OLS trendline: for every 1-unit increase in {_named(x_col)}, "
                        f"{_named(y_col)} {direction_word} by {abs(ols_slope):.3g} on average. "
                        "OLS assumes a straight-line relationship — verify this holds across the range."
                    )
                else:
                    insights.append(
                        "OLS (Ordinary Least Squares) trendline fitted. It models the best-fit "
                        "straight line through the data — suitable when the relationship is roughly linear."
                    )
                insights.append(
                    "Points far from the OLS line are outliers or high-leverage observations "
                    "that may disproportionately influence the model."
                )

            # ── LOWESS trendline explanation ──────────────────────────────────
            if has_lowess:
                insights.append(
                    "LOWESS (Locally Weighted Smoothing) trendline fitted. Unlike OLS, LOWESS "
                    "follows the local shape of the data — bends and curves in the line indicate "
                    "non-linear relationships that a straight-line model would miss."
                )
                insights.append(
                    "Where the LOWESS curve flattens, {_named(y_col)} stops responding to changes "
                    "in {_named(x_col)} — a potential saturation or threshold effect."
                    .format(_named=_named, _x=x_col, _y=y_col)
                    .replace("{_named(x_col)}", _named(x_col))
                    .replace("{_named(y_col)}", _named(y_col))
                )

            # ── No trendline ──────────────────────────────────────────────────
            if not has_ols and not has_lowess and r_val is None:
                insights.append(
                    f"Explore the pattern between {_named(x_col)} and {_named(y_col)}. "
                    "Add an OLS trendline to quantify a linear relationship, or LOWESS "
                    "to reveal curved or non-linear patterns."
                )

            # ── Point count ───────────────────────────────────────────────────
            if scatter_trace:
                n_pts = len(getattr(scatter_trace, "x", []) or [])
                if n_pts:
                    insights.append(f"Chart shows {n_pts:,} data points.")
                    if n_pts >= 7_000:
                        insights.append(
                            "Large sample — dense overplotting may hide structure. "
                            "Try colouring by a category column to separate groups."
                        )

        except Exception:
            pass

        if not insights:
            insights.append(
                f"Scatter plot generated. Explore the relationship between the "
                "X and Y axes — add a trendline to quantify the pattern."
            )

    # ── Map plot ───────────────────────────────────────────────────────────────
    elif chart_type == "map_plot" or "map:" in tl:
        try:
            map_trace = fig.data[0] if fig.data else None
            if map_trace:
                lats = _as_number_series(getattr(map_trace, "lat", []) or [])
                lons = _as_number_series(getattr(map_trace, "lon", []) or [])
                n_pts = len(lats)

                if n_pts:
                    insights.append(f"Map shows {n_pts:,} location point(s).")

                if len(lats) >= 2:
                    lat_spread = float(lats.max() - lats.min())
                    lon_spread = float(lons.max() - lons.min())
                    if lat_spread < 2 and lon_spread < 2:
                        insights.append(
                            "Points are geographically concentrated — zoom in "
                            "for detailed cluster analysis."
                        )
                    elif lat_spread > 50 or lon_spread > 50:
                        insights.append(
                            "Data spans a wide geographic area — "
                            "consider filtering by region for more focused analysis."
                        )

                # Hover for colour column context
                color_col = next(
                    (c for c in col_desc if c.lower() in tl.lower()), None)
                if color_col and col_desc.get(color_col):
                    insights.append(
                        f"Colour column context — {color_col}: "
                        f"{col_desc[color_col].strip()[:80]}"
                    )
        except Exception:
            pass

    # ── Matrix Heatmap ─────────────────────────────────────────────────────────
    elif chart_type in ("matrix_heatmap", "matrix_table") or "matrix" in tl:
        try:
            heat_trace = next(
                (t for t in fig.data
                 if type(t).__name__.lower() == "heatmap"), None)
            tbl_trace  = next(
                (t for t in fig.data
                 if type(t).__name__.lower() == "table"), None)

            if heat_trace:
                z = heat_trace.z
                if z is not None:
                    flat = [v for row in z for v in (row or [])
                            if v is not None and not (isinstance(v, float) and v != v)]
                    flat_nums = _as_number_series(flat)
                    if not flat_nums.empty:
                        vmax = float(flat_nums.max())
                        vmin = float(flat_nums.min())
                        insights.append(
                            f"Values range from {_fmt_num(vmin)} to {_fmt_num(vmax)}. "
                            "The darkest cells show the highest aggregated values."
                        )
                        # Find the hottest cell coordinates
                        x_labels = _as_list(getattr(heat_trace, "x", None))
                        y_labels = _as_list(getattr(heat_trace, "y", None))
                        best_r, best_c, best_v = None, None, None
                        for ri, row in enumerate(z):
                            for ci, v in enumerate(row or []):
                                if v is not None and not (isinstance(v, float) and v != v):
                                    if best_v is None or float(v) > best_v:
                                        best_r, best_c, best_v = ri, ci, float(v)
                        if best_r is not None and x_labels and y_labels:
                            row_lbl = str(y_labels[best_r]) if best_r < len(y_labels) else f"Row {best_r}"
                            col_lbl = str(x_labels[best_c]) if best_c < len(x_labels) else f"Col {best_c}"
                            insights.append(
                                f"Hotspot: {row_lbl} × {col_lbl} = {_fmt_num(best_v)} — "
                                "highest aggregated value in the matrix."
                            )
                        # Blank / sparse warning
                        n_total = sum(len(row or []) for row in z)
                        n_blank = sum(
                            1 for row in z for v in (row or [])
                            if v is None or (isinstance(v, float) and v != v)
                        )
                        if n_total and n_blank / n_total > 0.25:
                            insights.append(
                                f"{n_blank / n_total:.0%} of cells are empty — "
                                "sparse combinations might need more data or broader groupings."
                            )

            elif tbl_trace:
                n_rows_t = len((tbl_trace.cells.values[0] or []) if tbl_trace.cells else [])
                insights.append(
                    f"Pivot table shows {n_rows_t} row(s). "
                    "Sort by column average to find the highest-performing categories."
                )
        except Exception:
            pass

    # ── Statistical ───────────────────────────────────────────────────────────
    elif (chart_type == "statistical"
          or any(k in tl for k in ("mean", "std", "min", "max"))):
        try:
            data   = fig.data[0]
            vals   = _as_number_series(getattr(data, "y", []))
            labels = _as_list(getattr(data, "x", None))
            if not vals.empty:
                top_i     = int(vals.reset_index(drop=True).idxmax())
                bot_i     = int(vals.reset_index(drop=True).idxmin())
                top_label = labels[top_i] if top_i < len(labels) else "The highest item"
                bot_label = labels[bot_i] if bot_i < len(labels) else "The lowest item"
                insights.append(
                    f"{_named(top_label)} is the highest at {_fmt_num(vals.max())}; "
                    f"{_named(bot_label)} is the lowest at {_fmt_num(vals.min())}."
                )
                val_range = vals.max() - vals.min()
                if val_range > 0:
                    insights.append(
                        f"The gap between top and bottom is {_fmt_num(val_range)} — "
                        f"a {val_range / vals.min() * 100:.0f}% difference from the lowest."
                        if vals.min() != 0 else
                        f"The gap between top and bottom is {_fmt_num(val_range)}."
                    )
        except Exception:
            pass
        if not insights:
            insights.append(
                "Compare the largest and smallest values first — "
                "they usually explain the main story."
            )

    # ── Data quality ──────────────────────────────────────────────────────────
    elif (chart_type == "data_quality"
          or any(k in tl for k in ("missing", "duplicate", "quality"))):
        try:
            data = fig.data[0]
            if hasattr(data, "labels") and hasattr(data, "values"):
                labels = list(data.labels)
                vals   = [float(v) for v in data.values]
                total  = sum(vals)
                details = [
                    f"{label}: {_fmt_num(val)} ({val/total*100:.1f}%)"
                    for label, val in zip(labels, vals)
                ]
                if details:
                    insights.append("Data quality split — " + "; ".join(details) + ".")
        except Exception:
            pass
        insights.append(
            "Resolve missing or duplicate rows before using these charts for decisions."
        )

    # ── Column description footnotes (for any chart type) ─────────────────────
    # Only appended when the description hasn't already been woven into the text above.
    if col_desc:
        mentioned = " ".join(insights).lower()
        for col, desc in col_desc.items():
            if col and desc.strip() and col.lower() in tl and desc.strip().lower() not in mentioned:
                insights.append(f"Column context — {col}: {desc.strip()}")

    return clean_insights(insights)
