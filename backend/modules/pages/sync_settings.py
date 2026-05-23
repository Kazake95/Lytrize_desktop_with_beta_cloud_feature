"""
modules/pages/sync_settings.py -- DEPRECATED: Sync settings page.
==================================================================

This page is no longer used. Sync is now accessed via the Profile page
(modules/pages/auth.py → page_profile) with a single "Sync now" button.
Cloud sync is configured via environment variables and the Profile page.

This file is kept as a no-op redirect to avoid ImportError if any external
code still imports or references page_sync_settings.
"""

import streamlit as st


def page_sync_settings():
    """Redirect to the profile page which now hosts all sync controls."""
    st.session_state.page = "profile"
    st.rerun()
