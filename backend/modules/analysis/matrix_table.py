"""
modules/analysis/matrix_table.py -- Pivot table & heatmap runner.
=================================================================

Redesigned to behave like an Excel pivot table / Power BI matrix:
  - Table view: alternating rows, sticky-style header, row totals, column
    totals, grand total row, conditional colour gradient per column,
    configurable via chart_settings (header colour, cell size, footer toggle).
  - Heatmap: uses its own independent colorscale — NOT the palette selector
    (palette is irrelevant for heatmaps). Granular controls in chart_settings:
    colorscale picker, annotation toggle, precision, font size, colour mode,
    scale min/max, colorbar title.
  - Both views: hover shows row-index label, column label, and formatted value.
  - Both views: capped at sensible row/col limits with truncation note.
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from modules.charts import chart_layout, COLORS, num_cols as _num_cols, cat_cols as _cat_cols
from modules.utils.perf import cached_pivot

_DIVERGING_AGGS   = {"mean", "median", "std"}
_MAX_CATS_HEATMAP = 40
_MAX_CATS_TABLE   = 300
_HEX_GRADIENT     = [
    "#0f172a", "#111827", "#172554", "#1e3a5f",
    "#1e40af", "#2563eb", "#3b82f6", "#60a5fa",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trim_pivot(pivot: pd.DataFrame, max_cats: int) -> pd.DataFrame:
    if pivot.shape[0] > max_cats:
        pivot = pivot.loc[pivot.abs().sum(axis=1).nlargest(max_cats).index]
    if pivot.shape[1] > max_cats:
        pivot = pivot[pivot.abs().sum(axis=0).nlargest(max_cats).index]
    return pivot


def _sort_pivot(pivot: pd.DataFrame) -> pd.DataFrame:
    try:
        row_order = pivot.mean(axis=1).sort_values(ascending=False).index
        col_order = pivot.mean(axis=0).sort_values(ascending=False).index
        return pivot.loc[row_order, col_order]
    except Exception:
        return pivot


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    try:
        if abs(v) >= 1_000_000:
            return f"{v / 1_000_000:,.1f}M"
        if abs(v) >= 1_000:
            return f"{v:,.0f}"
        return f"{v:,.2f}"
    except Exception:
        return str(v)


def _fmt_total(v) -> str:
    """Bold-formatted value for totals row/column."""
    raw = _fmt(v)
    return f"<b>{raw}</b>" if raw != "—" else "—"


def _cell_bg_gradient(val, vmin, vmax, base_dark="#0f172a", accent="#2563eb") -> str:
    """Return a hex colour interpolated between dark base and accent based on val position."""
    if vmin == vmax or val is None or (isinstance(val, float) and np.isnan(val)):
        return base_dark
    ratio = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
    # Parse hex
    def _hex_to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    def _rgb_to_hex(r, g, b):
        return "#{:02x}{:02x}{:02x}".format(int(r), int(g), int(b))
    r1, g1, b1 = _hex_to_rgb(base_dark)
    r2, g2, b2 = _hex_to_rgb(accent)
    return _rgb_to_hex(r1 + ratio * (r2 - r1), g1 + ratio * (g2 - g1), b1 + ratio * (b2 - b1))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_matrix_table(df, index_col=None, columns_col=None, values_col=None,
                     agg="mean", view_type="Heatmap", palette=None,
                     sort_rows="value_desc", top_n_rows=None, **kwargs):
    charts = []
    cats   = _cat_cols()
    num    = _num_cols()
    pal    = palette or COLORS

    idx  = index_col  or (cats[0] if cats else df.columns[0])
    cols = columns_col or (cats[1] if len(cats) > 1 else df.columns[1])
    num_fallback = list(df.select_dtypes("number").columns)
    vals = values_col or (num[0] if num else (num_fallback[0] if num_fallback else None))

    if not vals or idx not in df.columns or cols not in df.columns or vals not in df.columns:
        return []

    # ── Build pivot ──────────────────────────────────────────────────────────
    try:
        pivot = cached_pivot(df, index=idx, columns=cols, values=vals, aggfunc=agg)
        if isinstance(pivot.columns, pd.MultiIndex):
            pivot.columns = [" · ".join(str(c) for c in col).strip() for col in pivot.columns]
        if isinstance(pivot.index, pd.MultiIndex):
            pivot.index = [" · ".join(str(c) for c in row).strip() for row in pivot.index]
    except Exception:
        return []

    _row_cap = _MAX_CATS_TABLE if view_type == "Table" else _MAX_CATS_HEATMAP
    pivot = _trim_pivot(pivot, _row_cap)

    # ── Sort ─────────────────────────────────────────────────────────────────
    try:
        if sort_rows in ("value_desc", "value_asc"):
            row_means = pivot.mean(axis=1)
            pivot = pivot.loc[row_means.sort_values(ascending=(sort_rows == "value_asc")).index]
            col_order = pivot.mean(axis=0).sort_values(ascending=False).index
            pivot = pivot[col_order]
        elif sort_rows == "cat_asc":
            pivot = pivot.sort_index(ascending=True)
        elif sort_rows == "cat_desc":
            pivot = pivot.sort_index(ascending=False)
        else:
            pivot = _sort_pivot(pivot)
    except Exception:
        pivot = _sort_pivot(pivot)

    if top_n_rows and top_n_rows > 0:
        pivot = pivot.iloc[:top_n_rows]

    n_rows, n_cols_p = pivot.shape
    if n_rows == 0 or n_cols_p == 0:
        return []

    agg_label  = agg.upper()
    tr = df[idx].nunique()
    tc = df[cols].nunique()
    trunc_note = f"  (top {n_rows}×{n_cols_p} of {tr}×{tc})" if (tr > _row_cap or tc > _row_cap) else ""
    base_title = f"Matrix ({agg_label}): {vals}  ·  {idx} × {cols}{trunc_note}"

    z_values = pivot.values.tolist()
    x_labels = [str(c) for c in pivot.columns]
    y_labels  = [str(r) for r in pivot.index]
    flat = [v for row in z_values for v in row
            if v is not None and not (isinstance(v, float) and np.isnan(v))]

    # ── Heatmap view ──────────────────────────────────────────────────────────
    if view_type == "Heatmap":
        height = max(420, min(n_rows * 30 + 160, 800))

        use_diverging = agg in _DIVERGING_AGGS
        # NOTE: heatmap colorscale is completely independent of the palette.
        # The palette is for bar/line/scatter charts only. Heatmap uses its own
        # scale set in chart_settings → "Colour scale" (heatmap_colorscale).
        colorscale = "RdBu" if use_diverging else "Blues"
        zmid  = float(np.mean(flat)) if use_diverging and flat else None
        zmin  = float(min(flat)) if (not use_diverging and flat) else None
        zmax  = float(max(flat)) if (not use_diverging and flat) else None

        text_vals = [[_fmt(v) for v in row] for row in z_values]
        show_cell_text = n_rows <= 18 and n_cols_p <= 18

        fig = go.Figure(go.Heatmap(
            z=z_values,
            x=x_labels,
            y=y_labels,
            text=text_vals if show_cell_text else None,
            texttemplate="%{text}" if show_cell_text else None,
            textfont=dict(size=10, color="white"),
            colorscale=colorscale,
            zmid=zmid, zmin=zmin, zmax=zmax,
            hoverongaps=False,
            hovertemplate=(
                f"<b>{idx}:</b> %{{y}}<br>"
                f"<b>{cols}:</b> %{{x}}<br>"
                f"<b>{agg_label}({vals}):</b> %{{z:,.3f}}"
                "<extra></extra>"
            ),
            colorbar=dict(
                title=dict(
                    text=f"{agg_label}({vals})", side="right",
                    font=dict(color="#cbd5e1", size=11),
                ),
                tickfont=dict(color="#94a3b8", size=10),
                thickness=14, len=0.85,
                bgcolor="rgba(15,23,42,0.4)",
                bordercolor="rgba(100,116,139,0.3)", borderwidth=1,
            ),
        ))
        axis_common = dict(
            tickfont=dict(color="#94a3b8", size=10),
            gridcolor="rgba(100,116,139,0.15)",
            linecolor="rgba(100,116,139,0.25)",
            automargin=True,
        )
        _layout = chart_layout(height=height)
        _layout["margin"] = dict(l=10, r=100, t=58, b=90)
        fig.update_layout(
            **_layout,
            title=dict(text=base_title, font=dict(color="#e2e8f0", size=13)),
            xaxis=dict(**axis_common,
                       title=dict(text=cols, font=dict(color="#cbd5e1", size=12)),
                       tickangle=-30, side="bottom"),
            yaxis=dict(**axis_common,
                       title=dict(text=idx, font=dict(color="#cbd5e1", size=12)),
                       autorange="reversed"),
        )
        fig._lytrize_meta = {
            "analysis_type": "matrix_heatmap",
            "x_axis": cols, "y_axis": idx,
            "legend": f"{agg_label}({vals})",
            "supports_auto_insights": True, "supports_notes": True,
            "supports_axis_editing": True, "supports_legend_editing": True,
        }
        charts.append((f"Matrix Heatmap: {idx} × {cols}", fig))

    # ── Table view (Excel pivot-table style) ──────────────────────────────────
    else:
        # Row totals (one per data row)
        row_totals = []
        for row in z_values:
            clean = [v for v in row if v is not None and not (isinstance(v, float) and np.isnan(v))]
            row_totals.append(float(np.sum(clean)) if clean else np.nan)

        # Column totals (one per data column)
        col_totals = []
        for j in range(n_cols_p):
            clean = [row[j] for row in z_values
                     if row[j] is not None and not (isinstance(row[j], float) and np.isnan(row[j]))]
            col_totals.append(float(np.sum(clean)) if clean else np.nan)

        grand_total = sum(v for v in row_totals if not np.isnan(v))

        # Per-column value range for mini gradient (conditional formatting)
        col_ranges = []
        for j in range(n_cols_p):
            clean = [row[j] for row in z_values
                     if row[j] is not None and not (isinstance(row[j], float) and np.isnan(row[j]))]
            col_ranges.append((min(clean) if clean else 0, max(clean) if clean else 0))

        # ── Column widths (character-length-based) ──────────────────────────
        _idx_w  = min(max(max((len(str(r)) for r in y_labels), default=6), len(str(idx)), 8), 30)
        _data_w = min(max(max((len(str(v)) for row in z_values for v in row), default=6),
                          max((len(str(h)) for h in x_labels), default=4), 6), 16)
        _tot_w  = max(len("Total"), _data_w)
        _col_widths = [_idx_w] + [_data_w] * n_cols_p + [_tot_w]

        _px_per_unit = 9
        _fig_width   = min((sum(_col_widths)) * _px_per_unit + 24, 1100)

        # ── Build cell colour arrays (conditional gradient per column) ───────
        hdr_color = pal[0] if pal else "#4f46e5"
        hdr_alt   = pal[1] if len(pal) > 1 else "#6366f1"
        # Alternate header colours per column for visual grouping
        hdr_fills = [hdr_color] + [hdr_color if i % 2 == 0 else hdr_alt
                                    for i in range(n_cols_p)] + ["#1e3a5f"]

        # Row fill colours (alternating)
        n_data_rows = len(y_labels)
        row_fills_base = ["#1e293b" if i % 2 == 0 else "#0f172a" for i in range(n_data_rows)]

        # Per-cell fill colour: subtle gradient per column
        # go.Table needs column-major data: list of n_cols lists, each with n_rows values
        cell_fills_by_col = []
        # Index column (no gradient)
        cell_fills_by_col.append(row_fills_base)
        # Data columns
        for j in range(n_cols_p):
            vmin, vmax = col_ranges[j]
            col_bg = []
            for i, row in enumerate(z_values):
                v = row[j]
                base = "#1e293b" if i % 2 == 0 else "#0f172a"
                if v is not None and not (isinstance(v, float) and np.isnan(v)):
                    blend_hex = _cell_bg_gradient(v, vmin, vmax,
                                                   base_dark=base,
                                                   accent="#1e40af")
                    col_bg.append(blend_hex)
                else:
                    col_bg.append(base)
            cell_fills_by_col.append(col_bg)
        # Row total column
        row_tot_base = ["#1e3a5f" if i % 2 == 0 else "#172554" for i in range(n_data_rows)]
        cell_fills_by_col.append(row_tot_base)

        # ── Format cell values ───────────────────────────────────────────────
        index_vals_disp = y_labels
        data_cols_fmt   = [[_fmt(row[j]) for row in z_values] for j in range(n_cols_p)]
        row_totals_fmt  = [_fmt(v) for v in row_totals]

        # ── Header row ───────────────────────────────────────────────────────
        col_headers = [f"<b>{idx}</b>"] + [f"<b>{h}</b>" for h in x_labels] + ["<b>Total ▸</b>"]

        height = max(n_rows * 28 + 200, 480)

        fig = go.Figure(go.Table(
            columnwidth=_col_widths,
            header=dict(
                values=col_headers,
                fill_color=hdr_fills,
                font=dict(color="white", size=12, family="Inter, system-ui, sans-serif"),
                align=["left"] + ["right"] * n_cols_p + ["right"],
                line_color="rgba(255,255,255,0.1)",
                height=34,
            ),
            cells=dict(
                values=[index_vals_disp] + data_cols_fmt + [row_totals_fmt],
                fill_color=cell_fills_by_col,
                font=dict(color="#f1f5f9", size=11, family="Inter, system-ui, sans-serif"),
                align=["left"] + ["right"] * n_cols_p + ["right"],
                line_color="rgba(255,255,255,0.05)",
                height=26,
            ),
        ))

        # ── Column totals + grand total footer ───────────────────────────────
        totals_fmt  = [_fmt_total(v) for v in col_totals]
        grand_fmt   = _fmt_total(grand_total)
        footer_vals = [["<b>◀ Total</b>"]] + [[v] for v in totals_fmt] + [[grand_fmt]]
        footer_fills = ["#0f172a"] + ["#0f2a4a"] * n_cols_p + ["#0a1628"]

        fig.add_trace(go.Table(
            columnwidth=_col_widths,
            header=dict(values=[""] * len(col_headers),
                        fill_color="rgba(0,0,0,0)",
                        line_color="rgba(0,0,0,0)", height=0),
            cells=dict(
                values=footer_vals,
                fill_color=footer_fills,
                font=dict(color="#818cf8", size=12,
                          family="Inter, system-ui, sans-serif"),
                align=["left"] + ["right"] * n_cols_p + ["right"],
                line_color="rgba(100,116,139,0.2)",
                height=30,
            ),
        ))

        _layout = chart_layout(height=height)
        _layout["margin"] = dict(l=10, r=10, t=58, b=10)
        fig.update_layout(
            **_layout,
            width=_fig_width,
            title=dict(text=base_title, font=dict(color="#e2e8f0", size=13)),
        )
        fig._lytrize_meta = {
            "analysis_type": "matrix_table",
            "x_axis": cols, "y_axis": idx, "legend": None,
            "supports_auto_insights": True, "supports_notes": True,
            "supports_axis_editing": True,
        }
        charts.append((f"Matrix Table: {idx} × {cols}", fig))

    return charts
