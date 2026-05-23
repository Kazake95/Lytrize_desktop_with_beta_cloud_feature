"""
modules/export.py -- HTML export engine for Lytrize dashboards.
===============================================================

Generates a self-contained HTML dashboard. Open in any browser and use
File → Print → Save as PDF for pixel-perfect export — no server dependencies.

COLOUR CUSTOMISATION (new)
──────────────────────────
generate_html_report() accepts a `theme` dict that controls the visual
appearance of the exported HTML. The dashboard page shows a colour picker
panel before download so users can personalise their output.

Theme keys (all optional — defaults used for any omitted key):
    bg_color       : page background colour        default "#121a2e"
    card_bg        : chart card background          default "#1b2245"
    kpi_bg         : KPI card background            default "#1b2245"
    accent_color   : heading, KPI value, accents    default "#6163df"
    card_border    : card border colour             default "#2c3564"
    text_color     : body text colour               default "#f5f7ff"
    insight_bg     : insights panel background      default "#1a2441"
    insight_border : insights left border colour    default "#6163df"
    notes_bg       : notes panel background         default "#1a1732"
    notes_border   : notes left border colour       default "#8566fc"
    card_radius    : card corner radius in px        default 12
    gap            : dashboard grid gap in px        default 24
    body_padding   : page padding in px              default 32
    chart_height   : chart height in px              default 400
    max_width      : wrapper max width CSS value     default by orientation
    show_meta      : show generated timestamp        default True
    show_print_hint: show print helper text          default True
"""

import re
import copy
import datetime
from html import escape
from modules.charts import clean_insight_text


# ── HTML helpers ─────────────────────────────────────────────────────────────

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FFFF"
    "\U00002500-\U00002BFF"
    "\U00002100-\U000024FF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]+", flags=re.UNICODE)

_ICON_MAP = {
    "💰": "$", "📊": "[chart]", "📐": "[med]", "🔢": "#",
    "⬇️": "v", "⬆️": "^", "📈": "%", "🔍": "[?]",
    "📅": "[dt]", "🏆": "1st", "📉": "[low]",
    "💡": "*", "📝": "Note:", "•": "-",
}


def _clean_pdf(text: str) -> str:
    """Strip emojis and replace known icons for PDF-safe text."""
    s = str(text)
    for em, rep in _ICON_MAP.items():
        s = s.replace(em, rep)
    s = _EMOJI_RE.sub("", s)
    return s.encode("latin-1", "replace").decode("latin-1")


def _h(text) -> str:
    """HTML-escape a value."""
    return escape(str(text), quote=True)


# ── Default export theme ──────────────────────────────────────────────────────
def _default_text_style() -> dict:
    """Return the default typography settings used in exported charts."""
    return {
        "family": "Inter, system-ui, sans-serif",
        "header_size": 28,
        "header_color": "#6163df",
        "subtitle_size": 11,
        "subtitle_color": "#64748b",
        "legend_title_size": 12,
        "legend_title_color": "#cbd5e1",
        "legend_item_size": 11,
        "legend_item_color": "#e2e8f0",
        "axis_title_size": 12,
        "axis_title_color": "#cbd5e1",
        "axis_tick_size": 10,
        "axis_tick_color": "#94a3b8",
    }


def _merge_text_style(raw: dict | None) -> dict:
    """Merge a stored text-style dict over the defaults."""
    style = _default_text_style()
    if not isinstance(raw, dict):
        return style
    for key, value in raw.items():
        if value in (None, ""):
            continue
        if key in style:
            style[key] = value
    return style


# ── Default export theme ──────────────────────────────────────────────────────

DEFAULT_THEME = {
    "bg_color":       "#121a2e",
    "card_bg":        "#1b2245",
    "kpi_bg":         "#1b2245",
    "accent_color":   "#6163df",
    "card_border":    "#2c3564",
    "text_color":     "#f5f7ff",
    "insight_bg":     "#1a2441",
    "insight_border": "#6163df",
    "notes_bg":       "#1a1732",
    "notes_border":   "#8566fc",
    "card_radius":    12,
    "gap":            24,
    "body_padding":   32,
    "chart_height":   400,
    "max_width":      "",
    "show_meta":      True,
    "show_print_hint": True,
}


def _merge_theme(user_theme: dict) -> dict:
    """Merge user overrides into the default theme. Missing keys use defaults."""
    t = dict(DEFAULT_THEME)
    if user_theme:
        t.update({k: v for k, v in user_theme.items() if v not in (None, "")})
    return t


# ── HTML Export ───────────────────────────────────────────────────────────────

def _apply_axes(fig, x_lbl, y_lbl, text_style: dict | None = None):
    """Apply axis titles and axis text styling to a chart copy."""
    try:
        f2 = copy.deepcopy(fig)
        style = _merge_text_style(text_style)
        axis_title_font = dict(
            size=int(style["axis_title_size"]),
            color=str(style["axis_title_color"]),
            family=str(style["family"]),
        )
        axis_tick_font = dict(
            size=int(style["axis_tick_size"]),
            color=str(style["axis_tick_color"]),
            family=str(style["family"]),
        )
        if x_lbl:
            f2.update_xaxes(title_text=x_lbl, title_font=axis_title_font)
        else:
            f2.update_xaxes(title_font=axis_title_font)
        if y_lbl:
            f2.update_yaxes(title_text=y_lbl, title_font=axis_title_font)
        else:
            f2.update_yaxes(title_font=axis_title_font)
        f2.update_xaxes(tickfont=axis_tick_font)
        f2.update_yaxes(tickfont=axis_tick_font)
        return f2
    except Exception:
        return fig


def _apply_legend_names(fig, legend_names: dict, legend_title: str = "", text_style: dict | None = None):
    """Rename traces and style the legend title/items."""
    try:
        f2 = copy.deepcopy(fig)
        style = _merge_text_style(text_style)
        legend_font = dict(
            size=int(style["legend_item_size"]),
            color=str(style["legend_item_color"]),
            family=str(style["family"]),
        )
        legend_title_font = dict(
            size=int(style["legend_title_size"]),
            color=str(style["legend_title_color"]),
            family=str(style["family"]),
        )
        if legend_names:
            for trace in f2.data:
                original = getattr(trace, "name", None)
                if original is not None and str(original) in legend_names:
                    custom = legend_names[str(original)]
                    if custom:
                        trace.name = custom
        if legend_title:
            f2.update_layout(
                legend_title_text=legend_title,
                legend=dict(font=legend_font, title=dict(font=legend_title_font)),
            )
        elif legend_title == "":
            f2.update_layout(legend=dict(font=legend_font, title=dict(font=legend_title_font)))
        return f2
    except Exception:
        return fig


# ── Offline Plotly JS injection ───────────────────────────────────────────────
#
# Plotly.js is embedded via fig.to_html(include_plotlyjs=True) on the FIRST
# chart in every report. Plotly internally calls plotly.offline.get_plotlyjs()
# which returns the full minified JS bundle from the INSTALLED package on disk —
# no CDN, no network request.  Subsequent charts use include_plotlyjs=False so
# the bundle is included exactly once.
#
# The exported HTML is therefore 100 % self-contained and works offline.
#


def generate_html_report(
    charts,
    session_name,
    orientation="portrait",
    kpis=None,
    dashboard_title="",
    grid_cols_n=2,
    inline_plotly=True,   # always True — offline-first desktop app
    theme: dict = None,
) -> str:
    """
    Generate a fully self-contained HTML dashboard file.

    All Plotly.js is embedded inline from the installed package (no CDN).
    The file works completely offline once downloaded.

    Parameters
    ----------
    charts       : list of (uid, title, fig, notes [, auto_insights, desc, meta])
    session_name : fallback title if dashboard_title is empty
    orientation  : "portrait" | "landscape"  controls max-width and grid columns
    kpis         : list of KPI dicts (icon, label, value, prefix, suffix, change_pct)
    dashboard_title : display title (overrides session_name)
    grid_cols_n  : number of CSS grid columns (1 or 2)
    inline_plotly: legacy param — kept for API compatibility, always treated as True
    theme        : colour customisation dict (see module docstring)

    Returns
    -------
    HTML string ready to be written to a file or returned via st.download_button.
    """
    inline_plotly = True  # enforce — never use CDN for an offline-first app
    t            = _merge_theme(theme or {})
    is_landscape = orientation == "landscape"
    max_width    = t.get("max_width") or ("1400px" if is_landscape else "1100px")
    grid_css     = f"repeat({grid_cols_n}, 1fr)"
    title        = dashboard_title or session_name
    safe_title   = _h(title)
    now_str      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    card_radius  = int(t.get("card_radius", 12))
    gap_px       = int(t.get("gap", 24))
    padding_px   = int(t.get("body_padding", 32))
    chart_height = int(t.get("chart_height", 400))
    meta_html    = (
        f'<div class="meta">Generated by Lytrize &middot; {now_str}</div>'
        if t.get("show_meta", True) else ""
    )
    print_hint_html = (
        """
  <div class="print-hint">
    &#128438; To save as PDF: <strong>Ctrl+P &rarr; Print &rarr; Save as PDF.</strong>
    &nbsp;&middot;&nbsp; Keep <strong>Background graphics</strong> enabled for best results.
  </div>"""
        if t.get("show_print_hint", True) else ""
    )

    # ── KPI strip ──────────────────────────────────────────────────────────────
    kpi_html = ""
    if kpis:
        def _change_style(k):
            if "change_pct" not in k:
                return ""
            clr = "#10b981" if k.get("change_pct", 0) >= 0 else "#ef4444"
            return f"color:{clr};font-weight:700"

        def _arrow(k):
            if "change_pct" not in k:
                return ""
            return "▲ " if k.get("change_pct", 0) >= 0 else "▼ "

        def _kpi_val_style(k, base):
            full = f'{k.get("prefix","")}{k.get("value","--")}{k.get("suffix","")}'
            size = "0.95rem" if len(full) > 16 else ("1.1rem" if len(full) > 12 else "1.4rem")
            wrap = "white-space:normal;word-break:break-word;overflow-wrap:anywhere;" if len(full) > 12 else ""
            return f"font-size:{size};{wrap}{base}"

        kpi_items = "".join(
            f'<div class="kpi-card">'
            f'<div class="kpi-icon">{_h(k.get("icon","📊"))}</div>'
            f'<div class="kpi-value" style="{_kpi_val_style(k, _change_style(k))}">'
            f'{_h(_arrow(k))}{_h(k.get("prefix",""))}{_h(k.get("value","--"))}{_h(k.get("suffix",""))}</div>'
            f'<div class="kpi-label">{_h(k.get("label","KPI"))}</div>'
            f'</div>'
            for k in kpis
        )
        kpi_html = f'<div class="kpi-row">{kpi_items}</div><hr>'

    # ── Chart blocks ───────────────────────────────────────────────────────────
    chart_blocks = ""
    for idx, item in enumerate(charts):
        uid, chart_title, fig, notes = item[:4]
        auto_insights = item[4] if len(item) > 4 else []
        meta          = item[6] if len(item) > 6 else {}

        display_title = meta.get("custom_title") or chart_title
        subtitle      = meta.get("subtitle", "")
        col_span      = "grid-column: 1 / -1;" if meta.get("full_width") else ""
        text_style    = _merge_text_style(meta.get("text_style", {}))

        # Clone figure — remove embedded title (shown as <h2> instead).
        fig_r = copy.deepcopy(fig)
        fig_r.update_layout(title_text="")
        fig_r = _apply_axes(fig_r, meta.get("x_label", ""), meta.get("y_label", ""), text_style)
        fig_r = _apply_legend_names(fig_r, meta.get("legend_names", {}), meta.get("legend_title", ""), text_style)
        is_horiz = any(
            getattr(tr, "orientation", "v") == "h"
            for tr in fig_r.data if hasattr(tr, "orientation")
        )
        if is_horiz:
            fig_r.update_yaxes(automargin=True)
            fig_r.update_xaxes()
            fig_r.update_layout(margin=dict(l=130, r=30, t=20, b=30))
        else:
            fig_r.update_xaxes(tickangle=-35, automargin=True)
            fig_r.update_yaxes(automargin=True)
            fig_r.update_layout(margin=dict(l=30, r=30, t=20, b=80))

        # Apply theme colours to chart paper/plot background.
        fig_r.update_layout(
            autosize=True, width=None, height=chart_height,
            paper_bgcolor=t["card_bg"],
            plot_bgcolor=t["card_bg"],
        )

        # Always embed Plotly JS from the local package in the FIRST chart block.
        # Subsequent charts get include_plotlyjs=False because Plotly is already
        # loaded from the first block.  This produces one self-contained file with
        # no external dependencies — works completely offline.
        include_js = True if idx == 0 else False
        chart_html = fig_r.to_html(
            full_html=False,
            include_plotlyjs=include_js,
            config={"responsive": True, "displayModeBar": "hover"},
        )

        # Insights panel
        insight_html = ""
        if auto_insights and meta.get("show_auto_insights", True):
            hidden  = set(meta.get("hidden_insights", []))
            visible = [ins for i, ins in enumerate(auto_insights) if i not in hidden]
            if visible:
                items = "".join(f"<li>{_h(clean_insight_text(ins))}</li>" for ins in visible)
                insight_html = (
                    f'<div class="insights"><strong>Insights</strong>'
                    f'<ul>{items}</ul></div>'
                )

        # Notes panel
        notes_str  = str(notes).strip() if notes else ""
        notes_html = (
            f'<div class="notes"><strong>Analysis Notes:</strong> {_h(notes_str)}</div>'
            if notes_str else ""
        )

        chart_blocks += (
            f'<div class="chart-card" style="{col_span}">'
            f'<h2 style="font-size:{text_style["header_size"] / 16:.2f}rem;'
            f'font-family:{text_style["family"]};color:{text_style["header_color"]};">'
            f'{_h(display_title)}</h2>'
            + (f'<p class="subtitle" style="font-size:{text_style["subtitle_size"] / 16:.2f}rem;'
               f'font-family:{text_style["family"]};color:{text_style["subtitle_color"]};">'
               f'{_h(subtitle)}</p>' if subtitle else "")
            + f'<div class="chart-wrap">{chart_html}</div>'
            + insight_html + notes_html
            + "</div>"
        )

    # ── Plotly JS block (injected before </body> for fast first paint) ────────
    # get_plotlyjs() is already embedded inline in the first chart's HTML via
    # include_plotlyjs=True above.  This script block just adds the relayout
    # helper that fires after the page loads.
    plotly_guard = ""  # JS already embedded in first chart's HTML

    # ── Full HTML document ────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title} -- Lytrize</title>
  <style>
    /*
     * No external @import — the file must work 100 % offline.
     * Inter is preferred; system-ui / sans-serif are the offline fallback.
     */
    @font-face {{
      font-family: 'Inter';
      font-style: normal;
      font-weight: 100 900;
      src: local('Inter'), local('Inter-Regular');
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont,
                   'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      padding: {padding_px}px;
      background: {t['bg_color']};
      color: {t['text_color']};
    }}
    .wrapper {{ max-width: {max_width}; margin: auto; width: 100%; }}

    .report-header {{ text-align: center; margin-bottom: 2rem; }}
    .report-header h1 {{ font-size: 2rem; font-weight: 800; color: {t['accent_color']}; }}
    .report-header .meta {{ font-size: 0.8rem; color: #64748b; margin-top: 0.4rem; }}

    .print-hint {{
      text-align: center;
      padding: 0.5rem 3rem;
      margin: 2rem auto 1.2rem;
      width: fit-content;
      font-size: 0.78rem;
      color: #94a3b8;
    }}

    /* ── KPI strip ── */
    .kpi-row {{
      display: flex; gap: {gap_px}px; flex-wrap: wrap;
      margin-bottom: 1.5rem; justify-content: center;
    }}
    .kpi-card {{
      background: {t['kpi_bg']};
      border-radius: {card_radius}px;
      padding: 1rem 1.4rem;
      text-align: center;
      box-shadow: 0 2px 12px rgba(0,0,0,0.07);
      min-width: 130px; flex: 1; max-width: 220px;
      border: 1px solid {t['card_border']};
    }}
    .kpi-icon  {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
    .kpi-value {{
      font-size: 1.4rem; font-weight: 800;
      color: {t['accent_color']};
      line-height: 1.2; overflow: visible;
    }}
    .kpi-label {{
      font-size: 0.72rem; color: #64748b;
      margin-top: 0.2rem; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.06em;
    }}

    hr {{ border: none; border-top: 1px solid {t['card_border']}; margin: 1.5rem 0; }}

    /* ── Chart grid ── */
    .grid {{
      display: grid;
      grid-template-columns: {grid_css};
      gap: {gap_px}px;
      width: 100%;
    }}
    .chart-card {{
      background: {t['card_bg']};
      border-radius: {card_radius}px;
      padding: 1.2rem;
      box-shadow: 0 2px 16px rgba(0,0,0,0.07);
      border: 1px solid {t['card_border']};
      min-width: 0; width: 100%; overflow: hidden;
    }}
    .chart-card h2 {{
      font-size: 0.95rem; font-weight: 700;
      margin-bottom: 0.15rem; color: {t['text_color']};
    }}
    .subtitle {{ font-size: 0.78rem; color: #64748b; margin-bottom: 0.6rem; }}
    .chart-wrap {{ width: 100%; overflow: hidden; display: block; }}
    .chart-wrap .js-plotly-plot {{ width: 100% !important; display: block !important; }}
    .chart-wrap .plotly          {{ width: 100% !important; }}
    .chart-wrap .plot-container  {{ width: 100% !important; }}
    .chart-wrap svg.main-svg     {{ width: 100% !important; }}

    /* ── Insights ── */
    .insights {{
      background: {t['insight_bg']};
      border-left: 3px solid {t['insight_border']};
      border-radius: 6px;
      padding: 0.6rem 0.9rem;
      margin-top: 0.7rem;
      font-size: 0.8rem;
    }}
    .insights strong {{ display: block; margin-bottom: 0.25rem; }}
    .insights ul {{ margin-left: 1rem; }}
    .insights li {{ margin-bottom: 0.2rem; line-height: 1.5; }}

    /* ── Notes ── */
    .notes {{
      background: {t['notes_bg']};
      padding: 0.6rem 0.9rem;
      border-left: 4px solid {t['notes_border']};
      margin-top: 0.7rem;
      border-radius: 4px;
      font-size: 0.82rem;
      font-style: italic;
    }}

    @page {{ margin: 12mm 10mm; }}
    @media print {{
      * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
      body {{
        background: {t['bg_color']} !important;
        padding: 0 !important;
        margin: 0 !important;
      }}
      .print-hint {{ display: none !important; }}
      .wrapper {{
        max-width: 100% !important;
        padding: 0 !important;
        margin: 0 !important;
      }}
      .report-header {{
        margin-bottom: 1.2rem;
        padding-top: 0.5rem;
        break-after: avoid;
      }}
      /* Force single-column in print — browsers cannot reliably honour
         CSS grid column counts across page breaks, so a single-column
         layout eliminates blank half-pages and uneven vertical rhythm. */
      .grid {{
        display: block !important;
        width: 100% !important;
      }}
      .chart-card {{
        display: block !important;
        width: 100% !important;
        box-sizing: border-box !important;
        break-inside: avoid;
        page-break-inside: avoid;
        border: 1px solid {t['card_border']} !important;
        box-shadow: none !important;
        margin-bottom: 1rem !important;
        padding: 0.9rem !important;
      }}
      /* Full-width chart cards keep their expanded width. */
      .chart-card[style*="grid-column"] {{
        width: 100% !important;
      }}
      .kpi-row {{
        break-inside: avoid;
        page-break-inside: avoid;
        margin-bottom: 1rem;
      }}
      .kpi-card {{
        box-shadow: none !important;
        border: 1px solid {t['card_border']} !important;
      }}
      .chart-wrap svg.main-svg {{ width: 100% !important; height: auto !important; }}
      hr {{ border-top: 1px solid {t['card_border']} !important; }}
    }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="report-header">
      <h1>&#128202; {safe_title}</h1>
      {meta_html}
    </div>
    {kpi_html}
    <div class="grid">{chart_blocks}</div>
  </div>
{print_hint_html}
  <script>
    /*
     * Plotly.js is already embedded inline above (no CDN required).
     * This script just triggers a relayout pass once the page is fully
     * loaded so every chart fills its container correctly.
     */
    function relayoutAll() {{
      if (typeof Plotly === 'undefined') return;
      document.querySelectorAll('.js-plotly-plot').forEach(function(el) {{
        try {{
          var w = el.closest('.chart-wrap');
          var width = w ? w.offsetWidth : el.offsetWidth;
          if (width > 0) Plotly.relayout(el, {{width: width, autosize: true}});
        }} catch(e) {{}}
      }});
    }}
    window.addEventListener('load',        function() {{ setTimeout(relayoutAll, 150); }});
    window.addEventListener('resize',      function() {{ setTimeout(relayoutAll,  80); }});
    window.addEventListener('beforeprint', function() {{ relayoutAll(); }});
  </script>
</body>
</html>"""
