# Runtime constants that apply to both the launcher and the Streamlit backend.
#
# APP_HOST / APP_PORT are authoritative in desktop/gui.py (passed to
# `streamlit run` via CLI flags) and in backend/.streamlit/config.toml.
# Modules that need these values should read them from those sources rather
# than importing from here, so there is a single source of truth.
#
# This file is intentionally kept minimal; add shared constants here only when
# they are genuinely needed by more than one module.
