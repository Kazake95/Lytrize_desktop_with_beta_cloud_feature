"""
modules/ui/css.py — Global CSS injection
=========================================

Hides all Streamlit chrome, keeps the purple brand palette, and provides
all shared component styles used across every page.

FIXES IN THIS VERSION
---------------------
1. Nav button squeeze (Profile "Pro file" wrapping)
       root cause: align-items: left is not a valid flex value → browser
                   ignores it and collapses the container; button shrinks.
       fix: align-items: center  +  white-space: nowrap on each button.

2. KPI card inconsistent height/width
       root cause: row-direction flex with variable-length labels caused
                   taller cards when labels wrapped to two lines.
       fix: column-direction flex + min-height + white-space: nowrap on label.

3. Analysis cards unstyled
       root cause: .ag-card / .ag-icon / .ag-name / .ag-desc classes were
                   referenced in analysis.py but never defined in this file.
       fix: full definition matching the KPI card visual language.
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path

import streamlit as st


APP_NAME    = "Lytrize"
APP_VERSION = "1.0"
LOGO_PATH   = Path(__file__).resolve().parents[2] / "assets" / "lytrize.ico"
THEME_MODES = ("dark",)


# ── Theme persistence (dark-only for now) ─────────────────────────────────────

def _data_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    d = base / "lytrize"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_theme_mode()  -> str: return "dark"
def write_theme_mode(m: str) -> None: return
def get_theme_mode()   -> str: return "dark"


# ── Logo ──────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def logo_data_uri() -> str:
    try:
        data = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
        return f"data:image/x-icon;base64,{data}"
    except Exception:
        return ""


# ── Google Fonts ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _font_link() -> str:
    # Non-blocking font load via media=print swap.
    # preconnect tags are intentionally OMITTED: they open real TCP connections
    # immediately, regardless of media attribute — causing a DNS stall on every
    # page load when the desktop app is used offline.
    return (
        "<link rel='stylesheet'"
        " href='https://fonts.googleapis.com/css2?"
        "family=Inter:wght@300;400;500;600;700;800"
        "&family=Sora:wght@600;700;800"
        "&family=JetBrains+Mono:wght@400;500&display=swap'"
        " media='print'"
        " onload=\"this.media='all'\">"
        "<noscript><link rel='stylesheet'"
        " href='https://fonts.googleapis.com/css2?"
        "family=Inter:wght@300;400;500;600;700;800"
        "&family=Sora:wght@600;700;800"
        "&family=JetBrains+Mono:wght@400;500&display=swap'></noscript>"
    )


# ── Public injection entry point ──────────────────────────────────────────────

def inject_css() -> None:
    """Inject fonts + full stylesheet into the Streamlit page."""
    st.markdown(_font_link(), unsafe_allow_html=True)
    st.markdown(_css_block(), unsafe_allow_html=True)


@lru_cache(maxsize=3)
def _css_block(theme_mode: str | None = None) -> str:
    return _build_css(theme_mode or get_theme_mode())


# ── CSS colour tokens ─────────────────────────────────────────────────────────

def _theme_vars(_mode: str) -> str:
    """CSS custom-property block — always dark palette."""
    return """
:root {
    --bg-primary:      #0e1428;
    --bg-secondary:    rgba(18, 26, 48, 0.92);
    --surface-1:       rgba(20, 28, 53, 0.95);
    --surface-2:       rgba(25, 35, 64, 0.96);
    --surface-3:       rgba(14, 20, 38, 0.96);
    --surface-muted:   rgba(255,255,255,0.06);
    --border-subtle:   rgba(137, 154, 255, 0.20);
    --border-strong:   rgba(137, 154, 255, 0.35);
    --text-primary:    #eef2ff;
    --text-secondary:  #a7b3d1;
    --muted-link:      #95a2ff;
    --chip-bg:         rgba(111,99,255,0.20);
    --input-bg:        rgba(12, 18, 36, 0.95);
    --input-text:      #eef2ff;
    --input-placeholder: #7f8db0;
    --page-bg:         linear-gradient(180deg, #0e1428 0%, #10182e 100%);
}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Full stylesheet
# ═══════════════════════════════════════════════════════════════════════════════

def _build_css(theme_mode: str) -> str:
    vars_css = _theme_vars(theme_mode)
    return f"""
<style>

/* ═══════════════════════════════════════════════════
   HIDE ALL STREAMLIT CHROME
   ═══════════════════════════════════════════════════ */
#MainMenu, footer, header,
.stDeployButton, .stAppDeployButton,
.stStatusWidget, .stActionButton,
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
button[aria-label="More options"],
button[aria-label="Record a screencast"],
button[aria-label="Settings"],
iframe[title="streamlit-menu"] {{
    display:    none       !important;
    visibility: hidden     !important;
}}
header,
[data-testid="stHeader"],
[data-testid="stDecoration"],
[data-testid="stToolbar"] {{
    height:     0          !important;
    min-height: 0          !important;
    max-height: 0          !important;
    padding:    0          !important;
    margin:     0          !important;
    overflow:   hidden     !important;
}}


/* ═══════════════════════════════════════════════════
   CSS CUSTOM PROPERTIES (TOKENS)
   ═══════════════════════════════════════════════════ */
:root {{
    --accent-purple:        #6f63ff;
    --accent-purple-strong: #5a49ff;
    --accent-cyan:          #62c8ff;
    --accent-gradient:      linear-gradient(135deg, #6e63ff 0%, #8d5cff 56%, #5ec8ff 100%);
    --button-text:          #ffffff;
    --shadow-soft:          0 10px 28px rgba(56, 65, 99, 0.10);
    --shadow-strong:        0 16px 34px rgba(56, 65, 99, 0.16);
}}
{vars_css}


/* ═══════════════════════════════════════════════════
   PAGE BACKGROUND & LAYOUT
   ═══════════════════════════════════════════════════ */
.stApp,
[data-testid="stAppViewContainer"] {{
    background: var(--page-bg) !important;
    color:      var(--text-primary) !important;
}}
.main .block-container {{
    padding-top:    3.2rem  !important;
    padding-bottom: 3.2rem  !important;
    max-width: 1480px;
}}


/* ═══════════════════════════════════════════════════
   GLOBAL TEXT
   ═══════════════════════════════════════════════════ */
body, p, span, label, li, a, small, strong, b, code, pre, input, textarea {{
    color: var(--text-primary) !important;
}}
h1, h2, h3, h4, h5, h6,
[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3 {{
    color: var(--text-primary) !important;
}}
a {{
    color:           var(--muted-link) !important;
    text-decoration: none;
}}


/* ═══════════════════════════════════════════════════
   TOP BAR / BRAND
   ═══════════════════════════════════════════════════ */
.lytrize-topbar {{
    position:        fixed;
    top:             0;
    left:            0;
    right:           0;
    z-index:         110;
    display:         flex;
    justify-content: center;
    align-items:     center;
    gap:             0.5rem;
    min-height:      44px;
    padding:         0.35rem 1rem;
    background:      color-mix(in srgb, var(--bg-primary) 88%, transparent);
    backdrop-filter: blur(12px);
    /* contain: layout style isolates the fixed bar repaint from page scrolls */
    contain:         layout style;
}}
.lytrize-topbar-brand {{
    position:        absolute;
    left:            50%;
    transform:       translateX(-50%);
    display:         inline-flex;
    align-items:     center;
    gap:             6px;
    pointer-events:  none;
}}
.brand {{
    background:              var(--accent-gradient);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}


/* ═══════════════════════════════════════════════════
   TOP-RIGHT NAV BUTTONS
   FIX 1: align-items was "left" (invalid) → changed to
           "center". Added white-space: nowrap so the
           "👤 Profile" label never wraps to two lines.
   ═══════════════════════════════════════════════════ */
div[data-testid="stVerticalBlock"]:has(#nav-target):not(:has(div[data-testid="stVerticalBlock"])) {{
    position:        fixed          !important;
    top:             6px            !important;
    right:           24px           !important;
    z-index:         120            !important;
    display:         flex           !important;
    flex-direction:  row            !important;
    gap:             0.45rem        !important;
    align-items:     center         !important;  /* FIX: was "left" — not a valid value */
    width:           auto           !important;  /* FIX: was fit-content; auto is safer  */
}}

#nav-target {{ display: none !important; }}

/* All nav buttons — gradient style */
div[data-testid="stVerticalBlock"]:has(#nav-target) [data-testid="stButton"] button {{
    background:       var(--accent-gradient) !important;
    color:            #fff                   !important;
    border:           none                   !important;
    border-radius:    8px                    !important;
    padding:          0.28rem 0.9rem         !important;
    font-weight:      600                    !important;
    font-family:      'Inter', sans-serif    !important;
    font-size:        0.82rem                !important;
    line-height:      1.4                    !important;
    cursor:           pointer                !important;
    min-height:       auto                   !important;
    height:           auto                   !important;

    /* FIX: prevent "👤 Profile" from wrapping to "Pro\nfile" */
    white-space:      nowrap                 !important;
    min-width:        max-content            !important;

    box-shadow:       0 4px 12px rgba(111, 99, 255, 0.25) !important;
    transition:       transform 0.15s ease, box-shadow 0.15s ease !important;
}}

div[data-testid="stVerticalBlock"]:has(#nav-target) [data-testid="stButton"] button:hover {{
    transform:  translateY(-1px)                      !important;
    box-shadow: 0 6px 16px rgba(111, 99, 255, 0.35)  !important;
}}

/* Secondary nav buttons (e.g. sign-out icon) — ghost style */
div[data-testid="stVerticalBlock"]:has(#nav-target) [data-testid="baseButton-secondary"] button {{
    background: rgba(255,255,255,0.10)  !important;
    box-shadow: none                    !important;
}}


/* ═══════════════════════════════════════════════════
   WELCOME BANNER
   ═══════════════════════════════════════════════════ */
.welcome-banner {{
    margin-top:    0.2rem   !important;
    margin-bottom: 1rem     !important;
    padding:       1rem 1.35rem !important;
    border-radius: 18px     !important;
    text-align:    center   !important;
    overflow:      hidden;
    background:    linear-gradient(
        135deg,
        rgba(111, 99, 255, 0.86),
        rgba(91,  84, 255, 0.74),
        rgba(98, 200, 255, 0.62)
    ) !important;
    box-shadow: var(--shadow-strong);
}}
.welcome-banner,
.welcome-banner * {{
    color:                  #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}}


/* ═══════════════════════════════════════════════════
   SECTION LABEL
   ═══════════════════════════════════════════════════ */
.sec-label {{
    font-size:   0.88rem;
    font-weight: 700;
    color:       var(--text-secondary);
    margin:      0.3rem 0 0.6rem;
}}


/* ═══════════════════════════════════════════════════
   GLOBAL BUTTONS
   ═══════════════════════════════════════════════════ */
.stButton > button,
.stDownloadButton > button,
button[kind="primary"],
[data-testid="baseButton-primary"] {{
    background:    var(--accent-gradient)              !important;
    color:         var(--button-text)                  !important;
    border:        0                                   !important;
    border-radius: 12px                                !important;
    box-shadow:    0 8px 20px rgba(111, 99, 255, 0.22) !important;
    font-weight:   700                                 !important;
    min-height:    2.6rem                              !important;
}}
.stButton > button:hover,
[data-testid="baseButton-primary"]:hover {{
    opacity: 0.92 !important;
}}


/* ═══════════════════════════════════════════════════
   KPI CARDS
   FIX 2: changed from row → column layout so all three
   cards are the same height regardless of label length.
   Added min-height, white-space: nowrap on the label,
   and centre-aligned the content.
   ═══════════════════════════════════════════════════ */
.kpi-card {{
    background:      linear-gradient(160deg, var(--surface-1), var(--surface-2)) !important;
    border:          1px solid var(--border-subtle) !important;
    border-radius:   16px                           !important;

    /* FIX: column layout — stacks icon → value → label vertically */
    display:         flex;
    flex-direction:  column;
    align-items:     center;
    justify-content: center;
    text-align:      center;
    gap:             0.2rem;

    /* FIX: fixed min-height makes all cards equal regardless of label length */
    min-height:      90px;
    padding:         1rem 0.85rem !important;
    box-sizing:      border-box;

    /* Explicit base state prevents first-hover layout shift */
    transform:       translateY(0);
    will-change:     transform;
    backface-visibility: hidden;

    transition: border-color 0.18s, box-shadow 0.18s, transform 0.12s ease;
}}
.kpi-card:hover {{
    border-color: var(--border-strong)                       !important;
    box-shadow:   0 6px 18px rgba(111, 99, 255, 0.12)       !important;
    transform:    translateY(-1px);
}}
.kpi-icon {{
    font-size:   1.45rem;
    line-height: 1;
    flex-shrink: 0;
}}
.kpi-val {{
    background:              var(--accent-gradient);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-size:               1.75rem;
    font-weight:             800;
    line-height:             1.1;
}}
.kpi-lbl {{
    font-size:   0.76rem;
    font-weight: 500;
    color:       var(--text-secondary) !important;
    line-height: 1.3;

    /* FIX: prevents "Datasets\nAnalysed" two-line wrapping */
    white-space: nowrap;
}}
.kpi-body {{
    display:         flex;
    flex-direction:  column;
    align-items:     center;
    gap:             0.1rem;
}}


/* ═══════════════════════════════════════════════════
   SESSION CARDS (Previous Sessions list)
   ═══════════════════════════════════════════════════ */
.sess-card {{
    background:    linear-gradient(160deg, var(--surface-1), var(--surface-2)) !important;
    border:        1px solid var(--border-subtle)  !important;
    border-radius: 12px                            !important;
    padding:       0.6rem 0.9rem                   !important;
    margin-bottom: 0.1rem;
    transition:    border-color 0.18s;
}}
.sess-card:hover {{
    border-color: var(--border-strong) !important;
}}


/* ═══════════════════════════════════════════════════
   ANALYSIS CARDS  (.ag-card / .ag-icon / .ag-name / .ag-desc)
   FIX 3: these classes were used in analysis.py but
   were never defined here, leaving cards completely
   unstyled. Now match the KPI card visual language:
   gradient surface, border, rounded corners, hover glow.
   ═══════════════════════════════════════════════════ */
.ag-card {{
    background:    linear-gradient(160deg, var(--surface-1), var(--surface-2)) !important;
    border:        1px solid var(--border-subtle) !important;
    border-radius: 14px                           !important;
    padding:       0.9rem 0.8rem 0.65rem          !important;

    display:        flex;
    flex-direction: column;
    align-items:    flex-start;
    gap:            0.2rem;
    min-height:     108px;
    box-sizing:     border-box;

    /* Flush bottom so the Streamlit Select button sits tight below */
    margin-bottom:  0.1rem;

    /* GPU-composite the hover transform so it doesn't repaint the surrounding
       layout on every frame — prevents the visible "jump" on first hover. */
    transform:       translateY(0);
    will-change:     transform;
    backface-visibility: hidden;

    transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.12s ease;
}}
.ag-card:hover {{
    border-color: var(--border-strong)                  !important;
    box-shadow:   0 6px 18px rgba(111, 99, 255, 0.14)  !important;
    transform:    translateY(-1px);
}}

/* Selected state applied via inline style in analysis.py */

.ag-icon {{
    font-size:     1.5rem;
    line-height:   1;
    margin-bottom: 0.1rem;
    flex-shrink:   0;
}}
.ag-name {{
    font-size:   0.88rem;
    font-weight: 700;
    color:       var(--text-primary)   !important;
    line-height: 1.25;
}}
.ag-desc {{
    font-size:  0.73rem;
    color:      var(--text-secondary)  !important;
    line-height: 1.35;
    opacity:    0.88;
    /* Clamp to two lines maximum — prevents layout shifts */
    display:            -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow:           hidden;
}}

/* Make the Select button directly below each ag-card full-width and
   flush to the card so they read as a single unit */
[data-testid="stVerticalBlock"] > div > [data-testid="stVerticalBlock"] [data-testid="stMarkdownContainer"]:has(.ag-card) + div [data-testid="stBaseButton-secondary"] > button,
[data-testid="column"] [data-testid="stMarkdownContainer"]:has(.ag-card) + div [data-testid="stBaseButton-secondary"] > button {{
    border-radius:  0 0 12px 12px  !important;
    margin-top:    -2px            !important;
    width:          100%           !important;
    min-height:     2rem           !important;
    font-size:      0.78rem        !important;
    padding:        0.25rem 0.5rem !important;
}}


/* ═══════════════════════════════════════════════════
   FORM INPUTS & SELECTS
   ═══════════════════════════════════════════════════ */
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] > div,
[data-testid="stMultiSelect"] > div,
[data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea {{
    background:    var(--input-bg)   !important;
    color:         var(--input-text) !important;
    border:        1px solid var(--border-subtle) !important;
    border-radius: 8px               !important;
}}
[data-testid="stTextInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder {{
    color: var(--input-placeholder) !important;
}}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {{
    border-color: var(--accent-purple) !important;
    box-shadow:   0 0 0 2px rgba(111, 99, 255, 0.20) !important;
}}


/* ═══════════════════════════════════════════════════
   TABS
   ═══════════════════════════════════════════════════ */
[data-testid="stTabs"] [data-baseweb="tab"] {{
    color:       var(--text-secondary) !important;
    font-weight: 500;
    border-bottom: 2px solid transparent;
    transition:  color 0.15s, border-color 0.15s;
}}
[data-testid="stTabs"] [aria-selected="true"] {{
    color:        var(--accent-purple) !important;
    border-color: var(--accent-purple) !important;
    font-weight:  700;
}}


/* ═══════════════════════════════════════════════════
   EXPANDERS
   ═══════════════════════════════════════════════════ */
[data-testid="stExpander"] {{
    background:    var(--surface-1) !important;
    border:        1px solid var(--border-subtle) !important;
    border-radius: 10px !important;
    overflow:      hidden;
}}
[data-testid="stExpander"] summary {{
    color:       var(--text-primary)  !important;
    font-weight: 600;
    padding:     0.55rem 0.9rem;
}}
[data-testid="stExpander"] summary:hover {{
    background: var(--surface-muted) !important;
}}


/* ═══════════════════════════════════════════════════
   DATAFRAME / TABLE
   ═══════════════════════════════════════════════════ */
[data-testid="stDataFrame"] {{
    border:        1px solid var(--border-subtle) !important;
    border-radius: 10px                           !important;
    overflow:      hidden;
}}
[data-testid="stDataFrame"] th {{
    background: var(--surface-2)   !important;
    color:      var(--text-primary) !important;
    font-weight: 700;
}}
[data-testid="stDataFrame"] td {{
    color: var(--text-secondary) !important;
}}


/* ═══════════════════════════════════════════════════
   METRICS
   ═══════════════════════════════════════════════════ */
[data-testid="stMetric"] {{
    background:    var(--surface-1) !important;
    border:        1px solid var(--border-subtle) !important;
    border-radius: 12px             !important;
    padding:       0.75rem 1rem     !important;
}}
[data-testid="stMetricLabel"] {{
    color: var(--text-secondary) !important;
}}
[data-testid="stMetricValue"] {{
    color:       var(--text-primary) !important;
    font-weight: 800 !important;
}}
[data-testid="stMetricDelta"] {{
    font-weight: 600 !important;
}}


/* ═══════════════════════════════════════════════════
   DIVIDER
   ═══════════════════════════════════════════════════ */
hr {{
    border-color: var(--border-subtle) !important;
    margin: 1rem 0 !important;
}}


/* ═══════════════════════════════════════════════════
   FOOTER
   ═══════════════════════════════════════════════════ */
.lytrize-footer {{
    position:        fixed;
    left:            0;
    right:           0;
    bottom:          0;
    z-index:         100;
    display:         flex;
    justify-content: center;
    align-items:     center;
    gap:             0.8rem;
    padding:         0.6rem 1rem;
    font-size:       0.74rem;
    color:           var(--text-secondary);
    background:      color-mix(in srgb, var(--bg-primary) 92%, transparent);
    backdrop-filter: blur(12px);
    border-top:      1px solid var(--border-subtle);
    contain:         layout style;
}}


/* ═══════════════════════════════════════════════════
   SCROLLBAR (Webkit)
   ═══════════════════════════════════════════════════ */
::-webkit-scrollbar       {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: var(--bg-primary); }}
::-webkit-scrollbar-thumb {{ background: var(--border-subtle); border-radius: 3px; }}
::-webkit-scrollbar-thumb:hover {{ background: var(--border-strong); }}


/* ═══════════════════════════════════════════════════
   TOOLTIPS / POPOVER
   ═══════════════════════════════════════════════════ */
[data-baseweb="tooltip"] {{
    background:    var(--surface-2) !important;
    border:        1px solid var(--border-subtle) !important;
    border-radius: 8px              !important;
}}


/* ═══════════════════════════════════════════════════
   RESPONSIVE
   ═══════════════════════════════════════════════════ */
@media (max-width: 800px) {{
    .main .block-container {{
        padding-left:  1rem !important;
        padding-right: 1rem !important;
    }}
    /* Stack nav buttons vertically on narrow screens */
    div[data-testid="stVerticalBlock"]:has(#nav-target):not(:has(div[data-testid="stVerticalBlock"])) {{
        top:   4px  !important;
        right: 8px  !important;
        gap:   0.3rem !important;
    }}
    .kpi-lbl {{
        white-space: normal;  /* Allow wrap on very small screens */
        font-size:   0.7rem;
    }}
    .ag-card {{ min-height: 90px; }}
}}

</style>
"""


# ═══════════════════════════════════════════════════════════════════════════════
# render_logo — top bar + right-side navigation
# ═══════════════════════════════════════════════════════════════════════════════

def render_logo() -> None:
    """
    Render the fixed top bar (brand logo + name) and the top-right nav buttons.

    Navigation buttons are injected into a fixed-position container via
    a CSS :has(#nav-target) selector trick. The #nav-target div is hidden;
    it only serves as an anchor for the CSS selector.

    The container is pinned to top: 6px; right: 24px via CSS, independent
    of Streamlit's column layout — so it never shifts when columns are added.
    """
    logo_src  = logo_data_uri()
    icon_html = (
        f'<img src="{logo_src}" alt="{APP_NAME}"'
        f' style="width:1.15rem;height:1.15rem;vertical-align:middle;">'
        if logo_src else '<span style="font-size:1rem">📊</span>'
    )

    # Fixed top bar — brand name centred
    st.markdown(
        '<div style="height:0;overflow:visible;margin:0;padding:0;">'
        f'<div class="lytrize-topbar">'
        f'  <span class="lytrize-topbar-brand"'
        f'        style="display:inline-flex;align-items:center;gap:6px;">'
        f'    {icon_html}'
        f'    <span class="brand"'
        f'          style="font-family:\'Sora\',sans-serif;font-size:0.99rem;'
        f'                 font-weight:800;letter-spacing:0.02em;">'
        f'      {APP_NAME}'
        f'    </span>'
        f'  </span>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Top-right navigation — pinned by CSS selector on #nav-target
    is_guest = st.session_state.get("is_guest", False)
    with st.container():
        st.markdown('<div id="nav-target"></div>', unsafe_allow_html=True)

        if st.button("👤 Profile", key="global_nav_profile", help="Profile & Sync"):
            st.session_state.page = "profile"
            st.rerun()

        if not is_guest:
            # Render sign-out as a separate clearly-labelled button.
            # Previously it was an icon-only "⏻" button rendered immediately
            # after Profile — the two overlapped visually in the top-right corner.
            # Now it has text, a divider character, and its own CSS class so the
            # app's stylesheet can position it with a gap from the Profile button.
            st.markdown(
                "<span style='color:rgba(148,163,184,0.5);margin:0 2px;'>|</span>",
                unsafe_allow_html=True,
            )
            if st.button("⏻ Sign Out", key="global_nav_logout",
                         help="Sign out of your account"):
                _do_logout()


def _do_logout() -> None:
    """Revoke the session token, clear state, and redirect to the profile page."""
    from modules.database import revoke_token, log_activity

    try:
        base     = Path(__file__).resolve().parents[2]
        tok_path = base / ".local" / "share" / "lytrize" / "session.token"
        token    = tok_path.read_text().strip() if tok_path.exists() else ""
        if token:
            revoke_token(token)
        for fname in ("session.token", "session.user"):
            p = base / ".local" / "share" / "lytrize" / fname
            if p.exists():
                p.write_text("")
    except Exception:
        pass

    try:
        log_activity(st.session_state.get("user_id", 0), "logout")
    except Exception:
        pass

    st.query_params.clear()
    st.session_state.clear()
    st.session_state.page = "profile"
    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# inject_footer
# ═══════════════════════════════════════════════════════════════════════════════

def inject_footer() -> None:
    """Render the fixed bottom footer bar."""
    st.markdown(
        f'<div class="lytrize-footer">'
        f'  <span>{APP_NAME} v{APP_VERSION}</span>'
        f'  <span>|</span>'
        f'  <span style="color:var(--text-secondary);">Private &amp; Offline</span>'
        f'  <span>|</span>'
        f'  <a href="https://github.com/VidalNat/Lytrize/discussions/categories/q-a" style="color:var(--text-secondary); text-decoration: none;">Support</a>'
        f'</div>',
        unsafe_allow_html=True,
    )
