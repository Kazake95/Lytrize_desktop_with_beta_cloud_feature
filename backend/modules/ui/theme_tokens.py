"""Shared theme tokens for Lytrize.

Centralises palette values so UI styling, charts and exports can stay aligned
with the dark brand theme.
"""

BRAND_NAME = "Lytrize"
BRAND_VERSION = "webapp-theme"

BRAND_1 = "#4f6ef7"
BRAND_2 = "#8b5cf6"
BRAND_3 = "#06b6d4"
DANGER = "#ef4444"
SUCCESS = "#10b981"
WARN = "#f59e0b"

DARK = {
    "text_primary": "#f1f5f9",
    "text_secondary": "#94a3b8",
    "text_muted": "#64748b",
    "surface": "rgba(15,23,42,0.65)",
    "surface_hover": "rgba(30,41,59,0.80)",
    "border": "rgba(255,255,255,0.08)",
    "background": "#0f172a",
}

CHART_COLORS = [BRAND_1, BRAND_2, BRAND_3, WARN, DANGER, SUCCESS, "#ec4899", "#f97316"]
DANGER_SCALE = ["#bbf7d0", "#fbbf24", DANGER]

PALETTES = {
    "🔵 Default Blue-Purple": CHART_COLORS,
    "🌈 Vibrant": ["#e63946", "#f4a261", "#2a9d8f", "#457b9d", "#e9c46a", "#264653", "#a8dadc", "#f1faee"],
    "🍃 Nature Green": ["#2d6a4f", "#40916c", "#52b788", "#74c69d", "#95d5b2", "#b7e4c7", "#d8f3dc", "#1b4332"],
    "🌅 Warm Sunset": ["#e76f51", "#f4a261", "#e9c46a", "#264653", "#2a9d8f", "#e63946", "#f1faee", "#457b9d"],
    "🩷 Pink & Coral": ["#ff6b6b", "#feca57", "#48dbfb", "#ff9ff3", "#54a0ff", "#5f27cd", "#01abc6", "#ff9f43"],
    "🌊 Ocean Blues": ["#03045e", "#0077b6", "#00b4d8", "#90e0ef", "#caf0f8", "#023e8a", "#0096c7", "#ade8f4"],
    "🟣 Monochrome Purple": ["#3c096c", "#5a189a", "#7b2fbe", "#9d4edd", "#c77dff", "#e0aaff", "#240046", "#10002b"],
}

EXPORT_DEFAULT_THEME = {
    "bg_color": DARK["background"],
    "card_bg": "#1e293b",
    "kpi_bg": "#1e293b",
    "accent_color": BRAND_1,
    "card_border": "#334155",
    "text_color": DARK["text_primary"],
    "insight_bg": "#1e2a45",
    "insight_border": BRAND_1,
    "notes_bg": "#1e1a2e",
    "notes_border": BRAND_2,
}

EXPORT_DARK_THEME = {
    "bg_color": DARK["background"],
    "card_bg": "#1e293b",
    "kpi_bg": "#1e293b",
    "accent_color": "#818cf8",
    "card_border": "#334155",
    "text_color": DARK["text_primary"],
    "insight_bg": "#1e2a45",
    "insight_border": "#818cf8",
    "notes_bg": "#1e1a2e",
    "notes_border": "#a78bfa",
}


EXPORT_PURPLE_THEME = {
    "bg_color": "#1a0533",
    "card_bg": "#2d1b4e",
    "kpi_bg": "#2d1b4e",
    "accent_color": "#c084fc",
    "card_border": "#6b21a8",
    "text_color": "#f5f3ff",
    "insight_bg": "#3b0764",
    "insight_border": "#c084fc",
    "notes_bg": "#1e0030",
    "notes_border": "#e879f9",
}

EXPORT_CORPORATE_THEME = {
    "bg_color": "#f0fdf4",
    "card_bg": "#ffffff",
    "kpi_bg": "#f0fdf4",
    "accent_color": "#16a34a",
    "card_border": "#bbf7d0",
    "text_color": "#14532d",
    "insight_bg": "#dcfce7",
    "insight_border": "#16a34a",
    "notes_bg": "#f7fee7",
    "notes_border": "#65a30d",
}
