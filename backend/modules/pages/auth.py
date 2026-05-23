"""
modules/pages/auth.py -- Authentication and profile management.

GUEST MODE (default after clean install)
  App starts without login. All analysis features work immediately.
  Profile page is the entry point for account management:
    - Guests   : centered card with "Create account" / "Sign in" options
    - Signed in: sync button, sign out, danger zone

SESSION PERSISTENCE (desktop)
  Login → create_token() → token written to ~/.local/share/lytrize/session.token
  gui.py reads the file on each app open → injects ?t= into the browser URL
  app.py validates the token → restores session state → keeps ?t= in URL

  The token is kept in the URL so that a browser page-refresh re-validates
  and restores the session without requiring the user to relaunch the app.
  app.py uses a _token_validated flag in session_state to prevent double-
  validation within the same Streamlit session (i.e. on widget reruns).

SUPABASE SYNC
  Sync is MANUAL ONLY. It is triggered exclusively by the "Sync" button on
  the Profile page. It is never called automatically on login or registration.
  Auto-sync is attempted after sign-in only if cloud is already configured
  and reachable — failures are non-blocking toasts, never hard errors.
"""

import os
import hashlib
import pathlib
import tempfile
import streamlit as st

from modules.database import (
    login_user,
    register_user,
    validate_token,
    create_token,
    revoke_token,
    log_activity,
    delete_user_db,
    merge_user_data,
)
from modules.ui.css import APP_NAME, APP_VERSION, inject_footer, logo_data_uri
from modules.sync import (
    sync_now,
    get_sync_status,
    delete_remote_user_data,
    ensure_cloud_session,
    cloud_bootstrap_local_account,
)


# ── Disk helpers ──────────────────────────────────────────────────────────────

def _data_dir() -> pathlib.Path:
    base = pathlib.Path(
        os.environ.get("XDG_DATA_HOME", str(pathlib.Path.home() / ".local" / "share"))
    )
    d = base / "lytrize"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except Exception:
        pass
    return d


def _secure_write_text(path: pathlib.Path, value: str) -> None:
    """Atomically write private text with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        try:
            path.chmod(0o600)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


def _sentinel_path() -> pathlib.Path:
    """Return the path to the first-launch sentinel file."""
    return _data_dir() / ".initialized"


def mark_initialized() -> None:
    """Write the first-launch sentinel so subsequent app opens go to Home.

    Called once, on the first action that establishes a session:
      - Sign in (local or cloud)
      - Register new account
      - Continue as Guest

    After this file exists, app.py defaults to "home" instead of "profile"
    so returning users land directly at their workspace.
    """
    try:
        p = _sentinel_path()
        if not p.exists():
            _secure_write_text(p, "1")
    except Exception:
        pass


def is_initialized() -> bool:
    """Return True if the user has completed first-launch onboarding."""
    try:
        return _sentinel_path().exists()
    except Exception:
        return False


def _write_token(token: str) -> None:
    """Write the login token to disk only (never to the URL).

    The file is immediately chmod-ed to 0o600 (owner read/write only)
    so the raw token cannot be read by other users on a shared system.
    """
    try:
        p = _data_dir() / "session.token"
        _secure_write_text(p, token)
    except Exception:
        pass


def _write_username(username: str) -> None:
    try:
        _secure_write_text(_data_dir() / "session.user", username)
    except Exception:
        pass


def _clear_token() -> None:
    try:
        p = _data_dir() / "session.token"
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _clear_username() -> None:
    try:
        p = _data_dir() / "session.user"
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _db_path() -> str:
    # Use 'or' so that an empty string in LYTRIZE_DB_PATH (blank .env entry)
    # falls through to the proper default, just like an unset variable.
    # os.environ.get(key, default) returns "" when the key exists but is empty;
    # only 'or' correctly treats "" the same as unset.
    _default = str(pathlib.Path.home() / ".local" / "share" / "lytrize" / "lytrize.db")
    return os.environ.get("LYTRIZE_DB_PATH") or _default


def _df_snapshot_paths(user_id: int) -> list[pathlib.Path]:
    """Return current and legacy dataframe snapshot paths for cleanup."""
    paths: list[pathlib.Path] = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        paths.append(pathlib.Path(runtime) / "lytrize" / f"df_{user_id}.parquet")
    else:
        cache_home = os.environ.get("XDG_CACHE_HOME", str(pathlib.Path.home() / ".cache"))
        paths.append(pathlib.Path(cache_home) / "lytrize" / f"df_{user_id}.parquet")
    paths.append(pathlib.Path(f"/tmp/lytrize_df_{user_id}.parquet"))
    return paths


# ── Legacy redirect ───────────────────────────────────────────────────────────

def page_auth():
    """Legacy ?p=auth deep-link handler — redirects to profile."""
    if "user_id" in st.session_state:
        st.session_state.page = "home"
        st.rerun()
        return
    token = st.query_params.get("t", "")
    if token:
        restored = validate_token(token)
        if restored:
            st.session_state.user_id  = restored[0]
            st.session_state.username = restored[1]
            st.session_state.page     = "home"
            st.rerun()
            return
        st.query_params.clear()
    st.session_state.page = "profile"
    st.rerun()


# ── Supabase account pull ─────────────────────────────────────────────────────


def _do_cloud_restore(email: str, password: str) -> tuple[bool, str]:
    """Pull an account from Supabase onto this device after local sign-in fails.

    Creates a local account mirroring the cloud one, signs in, and returns.
    Returns (True, "") on success — caller must st.rerun() after.
    Returns (False, error_message) on failure.
    """
    cloud_ok, cloud_err, payload = cloud_bootstrap_local_account(email, password)
    if not cloud_ok or not payload:
        return False, cloud_err or "Incorrect email or password."

    local_username = payload.get("username") or email.split("@", 1)[0]
    local_email    = payload.get("email") or email

    ok, msg = register_user(local_username, local_email, password)
    if not ok and "already" not in (msg or "").lower():
        return False, msg

    restored_user = login_user(local_username, password)
    if not restored_user:
        return False, "Could not create the local account on this device."

    guest_uid = st.session_state.get("user_id") if st.session_state.get("is_guest") else None
    st.session_state.user_id  = restored_user[0]
    st.session_state.username = restored_user[1]
    st.session_state.is_guest = False
    mark_initialized()
    st.session_state.page = "home"

    if guest_uid and guest_uid != restored_user[0]:
        merge_user_data(guest_uid, restored_user[0])

    tok = create_token(restored_user[0], restored_user[1])
    _write_token(tok)
    _write_username(restored_user[1])
    log_activity(restored_user[0], "login_restored", f"user={email}")
    return True, ""

# ── Sign-in widget ────────────────────────────────────────────────────────────


def _widget_sign_in(key_prefix: str = "p") -> None:
    """
    Sign-in form.

    On success: writes the token to the disk file only (never to query_params),
    merges any guest data, and navigates to home. If cloud sync is configured,
    the app quietly links the account to Supabase so later sync is a button click.
    """
    username = st.text_input(
        "Username or email", key=f"{key_prefix}_l_user", placeholder="Enter your username or email"
    )
    password = st.text_input(
        "Password", type="password", key=f"{key_prefix}_l_pass",
        placeholder="Enter your password",
    )
    st.markdown("<div style='height:.25rem'></div>", unsafe_allow_html=True)

    if st.button("Sign In →", use_container_width=True, type="primary",
                 key=f"{key_prefix}_sign_in_btn"):
        if not username or not password:
            st.error("Please fill in both fields.")
            return

        user = login_user(username, password)
        if user:
            guest_uid = (
                st.session_state.get("user_id")
                if st.session_state.get("is_guest")
                else None
            )
            st.session_state.user_id  = user[0]
            st.session_state.username = user[1]
            st.session_state.is_guest = False
            mark_initialized()
            st.session_state.page     = "home"

            if guest_uid and guest_uid != user[0]:
                merge_user_data(guest_uid, user[0])

            log_activity(user[0], "login", f"user={username}")
            _write_username(user[1])

            # Token lives on disk only — never written to the URL.
            tok = create_token(user[0], user[1])
            _write_token(tok)

            try:
                email_addr = user[2] if len(user) > 2 else ""
                if email_addr:
                    ensure_cloud_session(email_addr, password, user[1])
            except Exception as _se:
                import logging as _lg
                _lg.getLogger("lytrize.auth").warning("Cloud link on sign-in: %s", _se)

            st.rerun()
        else:
            # Local login failed. Try cloud restore so the user can sign in
            # from a new device even if the local account doesn't exist yet.
            #
            # Supabase only accepts email+password, not username+password.
            # Strategy:
            #   1. If the user typed an email → try cloud restore directly.
            #   2. If they typed a username → look up their email in the local DB
            #      first (works when the account exists on this device but the
            #      password was wrong, giving a better error); if not found locally
            #      and cloud sync is configured, ask them to re-enter with email.
            _net_keywords = (
                "translate host", "name resolution", "connection refused",
                "timed out", "network is unreachable", "no route to host",
                "could not connect", "ssl", "eof", "broken pipe",
            )

            _email_to_try = username if "@" in username else None

            # If a bare username was given, try to resolve it to an email
            # from the local DB (the account may exist but password was wrong).
            if not _email_to_try:
                try:
                    from modules.database import _connect as _dbc, _ph as _dph
                    _c2 = _dbc()
                    _r2 = _c2.cursor()
                    _r2.execute(_dph("SELECT email FROM users WHERE username=? LIMIT 1"), (username,))
                    _row2 = _r2.fetchone()
                    _c2.close()
                    if _row2 and _row2[0] and "@" in str(_row2[0]):
                        _email_to_try = _row2[0]
                except Exception:
                    pass

            if _email_to_try:
                with st.spinner("Checking cloud account…"):
                    _ok, _err_msg = _do_cloud_restore(_email_to_try, password)
                if _ok:
                    st.rerun()
                else:
                    if any(k in (_err_msg or "").lower() for k in _net_keywords):
                        st.error(
                            "⚠️ Cannot reach Supabase right now — check your internet connection "
                            "and try again. Your locally saved sessions are unaffected."
                        )
                    else:
                        st.error(
                            f"Incorrect username or password."
                            + (f" ({_err_msg})" if _err_msg and "credentials" not in _err_msg.lower() else "")
                        )
            else:
                # Username given, not in local DB, cloud sync may help but
                # needs an email — we don't have one to try automatically.
                _sync_configured = get_sync_status().get("configured", False)
                if _sync_configured:
                    st.error(
                        "No local account found for **\"" + username + "\"**. "
                        "If this account was created on another device, enter your "
                        "**email address** to restore it from the cloud automatically."
                    )
                else:
                    st.error("Incorrect username or password.")
    _sync_cap = get_sync_status().get("configured", False)
    st.markdown("<div style='height:.15rem'></div>", unsafe_allow_html=True)
    if _sync_cap:
        st.caption(
            "☁️ Cloud sync active. Sign in with your username (local) or email (to restore from cloud). "
            "Keep your password safe — recovery is planned for a future release."
        )
    else:
        st.caption(
            "🔒 Offline-first — sign in with your username or email. "
            "Keep your password safe; recovery is planned for a future release."
        )


# ── Register widget ───────────────────────────────────────────────────────────

def _widget_register(key_prefix: str = "p") -> None:
    """
    Account creation form.

    On success: creates the account, signs in, and navigates to home.
    Sync is NOT triggered here — the user can sync manually from the Profile page.
    """
    ru  = st.text_input("Username", key=f"{key_prefix}_r_u",
                         placeholder="Choose a username (3–40 characters)")
    re  = st.text_input("Email", key=f"{key_prefix}_r_e",
                         placeholder="your@email.com")
    rp  = st.text_input("Password", type="password", key=f"{key_prefix}_r_p",
                         placeholder="Minimum 8 characters")
    rp2 = st.text_input("Confirm Password", type="password", key=f"{key_prefix}_r_p2",
                         placeholder="Repeat your password")
    st.markdown("<div style='height:.25rem'></div>", unsafe_allow_html=True)

    st.warning(
        "⚠️ **Store your password safely before creating your account.** "
        "Lytrize is offline-first, so recovery is currently disabled. "
        "Sign in only with a password you can keep safely; recovery will come "
        "in a future release. If you forget your password before then, you may "
        "need to create a new account. "
        "We recommend saving it in a password manager (Bitwarden, KeePassXC, etc.).",
        icon=None,
    )

    if st.button("Create Account →", use_container_width=True, type="primary",
                 key=f"{key_prefix}_register_btn"):
        if not all([ru, re, rp, rp2]):
            st.error("All fields are required.")
            return
        if rp != rp2:
            st.error("Passwords don't match.")
            return

        ok, msg = register_user(ru, re, rp)
        if ok:
            user = login_user(ru, rp)
            if user:
                guest_uid = (
                    st.session_state.get("user_id")
                    if st.session_state.get("is_guest")
                    else None
                )
                st.session_state.user_id  = user[0]
                st.session_state.username = user[1]
                st.session_state.is_guest = False
                mark_initialized()
                st.session_state.page     = "home"

                if guest_uid and guest_uid != user[0]:
                    merge_user_data(guest_uid, user[0])

                log_activity(user[0], "register_and_login", f"new user={ru}")
                _write_username(user[1])

                # Token lives on disk only — never written to the URL.
                tok = create_token(user[0], user[1])
                _write_token(tok)

                try:
                    ensure_cloud_session(re, rp, user[1])
                except Exception as _se:
                    import logging as _lg
                    _lg.getLogger("lytrize.auth").warning("Cloud link on register: %s", _se)

                st.toast("🎉 Account created! Welcome to Lytrize.", icon="✅")
                st.rerun()
            else:
                st.success("Account created! Please sign in.")
                st.rerun()
        else:
            st.error(msg)


# ── Profile page ──────────────────────────────────────────────────────────────

def page_profile():
    """
    Profile page.

    GUEST  : Centered card (max 480 px) with branding, guest info, sign-in
             expander, and register expander.
    SIGNED IN: Full-width account management — manual sync button, sign out,
             danger zone.
    Recovery is intentionally disabled while Lytrize remains offline-first.
    Users are reminded to keep their password safe during account creation.
    """
    from modules.ui.css import render_logo
    render_logo()

    is_guest = st.session_state.get("is_guest", False)

    if not is_guest:
        if st.button("← Home"):
            st.session_state.page = "home"
            st.rerun()
        st.markdown("---")

    # ── Guest state ───────────────────────────────────────────────────────────
    if is_guest:
        logo_src  = logo_data_uri()
        icon_html = (
            f'<img src="{logo_src}" alt="{APP_NAME}" '
            'style="width:3rem;height:3rem;object-fit:contain;">'
            if logo_src
            else '<span style="font-size:2.6rem">&#128202;</span>'
        )

        st.markdown("""
        <style>
        .lyt-guest-outer { display:flex; justify-content:center; width:100%; margin-top:0.5rem; }
        .lyt-guest-card  { width:100%; max-width:480px; display:flex; flex-direction:column; gap:0; }
        </style>
        <div class="lyt-guest-outer"><div class="lyt-guest-card" id="lyt-guest-anchor">
        </div></div>
        """, unsafe_allow_html=True)

        _, mid, _ = st.columns([1, 2, 1])

        with mid:
            st.markdown(
                f'<div style="text-align:center;padding:0.4rem 0 0.9rem;">'
                f'<div class="brand" style="font-family:\'Sora\',sans-serif;font-size:3.3rem;font-weight:bold;'
                f'background:linear-gradient(135deg, #6163df, #8566fc);-webkit-background-clip:text;'
                f'-webkit-text-fill-color:transparent;'
                f'text-shadow: 0 0 12px rgba(97, 99, 223, 0.6);">{APP_NAME}</div>'
                f'<div style="font-size:0.79rem;margin-top:0.2rem;opacity:0.5;font-weight:bold;">'
                f'Offline Analytics That Respects Your Privacy</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            st.info(
                "👋 **You're using Lytrize as a guest.** Upload any CSV or Excel file "
                "and start analysing right away — your sessions are saved locally on this device. "
                "Sign in later to sync them across devices."
            )

            st.markdown("<div style='height:.2rem'></div>", unsafe_allow_html=True)
            if st.button(
                "🚀 Continue as Guest → Start Analysis",
                type="primary",
                use_container_width=True,
                key="guest_continue",
            ):
                mark_initialized()
                st.session_state.page = "home"
                st.rerun()

            st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)

            # ── Backup & Restore (available to guests) ──────────────────────
            st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)
            with st.expander("💾 Backup & Restore Sessions"):
                from modules.database import export_sessions_to_dict, import_sessions_from_dict
                import json, datetime as _dt

                _guest_uid = st.session_state.get("user_id")

                st.markdown("**Backup** — export your saved sessions as a portable JSON file.")
                st.caption(
                    "Saves session metadata and charts. "
                    "Does NOT include the original CSV/Excel files."
                )
                if st.button("📦 Prepare Backup", key="guest_btn_backup"):
                    _sessions = export_sessions_to_dict(
                        _guest_uid,
                        username="guest",
                        local_db_path=_db_path(),
                    )
                    if not _sessions:
                        st.warning("No saved sessions found to back up yet.")
                    else:
                        st.session_state["_backup_sessions_guest"] = _sessions

                # Show session selector once sessions are loaded
                if "_backup_sessions_guest" in st.session_state:
                    _sessions = st.session_state["_backup_sessions_guest"]
                    st.markdown(f"**{len(_sessions)} session(s) found.** Select which to include:")
                    _selected_names = []
                    for _s in _sessions:
                        _sname = _s.get("name", _s.get("session_name", "Unnamed"))
                        if st.checkbox(_sname, value=True, key=f"guest_bk_sel_{_s.get('id',_sname)}"):
                            _selected_names.append(_s)
                    if _selected_names:
                        _clean = [{k: v for k, v in s.items() if k != "_origin"} for s in _selected_names]
                        _payload = {
                            "lytrize_backup": True,
                            "version": "1.1",
                            "username": "guest",
                            "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                            "sessions": _clean,
                        }
                        _json_bytes = json.dumps(_payload, indent=2, default=str).encode("utf-8")
                        _ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
                        st.download_button(
                            label=f"⬇️ Save {len(_clean)} session(s) — lytrize_backup_guest_{_ts}.json",
                            data=_json_bytes,
                            file_name=f"lytrize_backup_guest_{_ts}.json",
                            mime="application/json",
                            key="guest_dl_backup",
                        )
                    else:
                        st.info("Select at least one session to download.")

                st.markdown("---")
                st.markdown("**Restore** — import sessions from a Lytrize backup file.")
                st.caption(
                    "Upload a backup JSON from a previous install. "
                    "Sessions already present are skipped — no duplicates."
                )
                _uploaded = st.file_uploader(
                    "Upload backup file (.json)",
                    type=["json"],
                    key="guest_backup_upload",
                )
                if _uploaded is not None:
                    try:
                        _backup_bytes = _uploaded.read()
                        _bpayload = json.loads(_backup_bytes.decode("utf-8"))
                        if not _bpayload.get("lytrize_backup"):
                            st.error("This does not look like a Lytrize backup file.")
                        else:
                            _to_import = _bpayload.get("sessions", [])
                            _bu = _bpayload.get("username", "unknown")
                            _bd = _bpayload.get("exported_at", "unknown")[:10]
                            st.info(
                                f"Found **{len(_to_import)}** session(s) from account **{_bu}** on {_bd}."
                            )
                            # Guard: if we already imported this file in a previous
                            # rerun cycle, skip the button entirely to prevent duplicates.
                            _import_done_key = f"guest_import_done_{hashlib.sha256(_backup_bytes).hexdigest()[:16]}"
                            if st.session_state.get(_import_done_key):
                                st.success(
                                    "✅ Sessions imported successfully! "
                                    "Click **Continue as Guest** above to see them on your dashboard."
                                )
                            elif st.button("📥 Import Sessions", key="guest_btn_restore", type="primary"):
                                _result = import_sessions_from_dict(_guest_uid, _to_import)
                                _imported, _updated, _skipped = (
                                    _result if len(_result) == 3 else (_result[0], 0, _result[1])
                                )
                                if _imported or _updated:
                                    st.session_state[_import_done_key] = True
                                    for _k in (
                                        "editing_session_id",
                                        "editing_session_name",
                                        "editing_file_name",
                                        "view_session_id",
                                        "_view_charts",
                                        "_view_session_id_loaded",
                                    ):
                                        st.session_state.pop(_k, None)
                                    st.session_state.page = "home"
                                    st.rerun()
                                else:
                                    st.info("All sessions already present — nothing changed.")
                    except Exception as _be:
                        st.error(f"Could not read backup file: {_be}")

            # ── Optional: sign in / create account ───────────────────────────
            st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)
            st.caption(
                "💡 **Optional:** Sign in to sync sessions across devices. "
                "All features work offline without an account."
            )
            with st.expander("🔐 Sign in to existing account", expanded=False):
                st.caption("Sign in to restore sessions you saved on this or another device. Use the email address from the cloud account if this is a new device.")
                _widget_sign_in(key_prefix="guest")

            with st.expander("✨ Create a new account", expanded=False):
                st.caption(
                    "Register to save analysis sessions and sync them across devices. "
                    "Free — no credit card required."
                )
                _widget_register(key_prefix="guest")

        inject_footer()
        return

    # ── Authenticated state ───────────────────────────────────────────────────
    username = st.session_state.get("username", "")
    user_id  = st.session_state.get("user_id")

    st.markdown(f"## 👤 {username}")
    st.markdown("---")

    # ── Compute sync status once — reused throughout ─────────────────────────
    import os as _os
    sync_status     = get_sync_status()
    sync_configured = sync_status["configured"]

    # ── Supabase Sync ─────────────────────────────────────────────────────────
    st.markdown("### ☁ Supabase Sync")

    if sync_configured:
        _linked = sync_status.get("linked", False)
        if _linked:
            st.success(f"✅ Cloud sync active — signed in as **{username}**")
        else:
            st.warning(
                "☁️ Cloud account not linked yet. "
                "Sign out and sign back in to link automatically."
            )

    st.caption(
        "Sync is manual. Your data stays on this device until you click the button below. "
        "Only session metadata (names, chart configs, KPIs) is synced — "
        "your raw data files are never uploaded. Cloud login is linked silently after sign-in."
    )

    if st.button("🔄 Sync my sessions now", type="primary"):
        if not sync_configured:
            st.error("Supabase is not configured on this installation.")
        else:
            with st.spinner("Connecting to Supabase…"):
                result = sync_now(username, _db_path())
            if result.ok:
                if result.pushed == 0 and result.pulled == 0:
                    st.success("✅ Already up to date — no changes.")
                else:
                    parts = []
                    if result.pushed:
                        parts.append(f"**{result.pushed}** session(s) uploaded")
                    if result.pulled:
                        parts.append(f"**{result.pulled}** session(s) downloaded")
                    st.success("✅ Sync complete — " + " | ".join(parts))
                    # Invalidate session cache so home immediately shows synced data.
                    from modules.database import get_user_sessions as _gus
                    _gus.clear()
                    st.rerun()
            else:
                # Friendly message for network errors
                _net_kw = ("translate host", "name resolution", "connection refused",
                           "timed out", "unreachable", "no route", "broken pipe")
                _msg = result.error or ""
                if any(k in _msg.lower() for k in _net_kw):
                    st.error("⚠️ Cannot reach Supabase — check your internet connection and try again.")
                else:
                    st.error(f"❌ Sync failed: {_msg}")

    st.markdown("---")

    # ── Sign out ──────────────────────────────────────────────────────────────
    with st.expander("🔓 Sign Out"):
        st.write("Sign out from this device. Your saved sessions are kept locally.")
        if st.button("Sign Out", type="secondary"):
            # Revoke the disk token from the DB before clearing it on disk.
            try:
                _tok_path = _data_dir() / "session.token"
                _stored_tok = _tok_path.read_text().strip() if _tok_path.exists() else ""
                if _stored_tok:
                    revoke_token(_stored_tok)
            except Exception:
                pass
            # Clean up dataframe snapshots to avoid leaving sensitive raw data.
            try:
                for _snap in _df_snapshot_paths(user_id):
                    if _snap.exists():
                        _snap.unlink()
            except Exception:
                pass
            _clear_token()
            _clear_username()
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.query_params.clear()
            st.session_state.page = "profile"
            st.rerun()

    # ── Backup & Restore ─────────────────────────────────────────────────────
    st.markdown("---")
    with st.expander("💾 Backup & Restore Sessions"):
        from modules.database import export_sessions_to_dict, import_sessions_from_dict
        import json, datetime

        st.markdown("**Backup** — export all your saved sessions as a portable JSON file.")
        st.caption(
            "The backup contains your session metadata and charts. "
            "It does NOT contain the original CSV/Excel files — only the analysis results."
        )

        if st.button("📦 Prepare Backup", key="btn_backup"):
            from modules.sync import get_sync_status as _gss
            _sync_cfg = _gss()["configured"]
            if _sync_cfg:
                st.toast("☁️ Fetching cloud sessions to include in backup…", icon="☁️")
            sessions = export_sessions_to_dict(
                user_id,
                username=username,
                local_db_path=_db_path(),
            )
            if not sessions:
                st.warning(
                    "No saved sessions found to back up. "
                    + ("If you expected cloud sessions, check Profile → Sync." if _sync_cfg else "")
                )
            else:
                st.session_state["_backup_sessions_user"] = sessions

        if "_backup_sessions_user" in st.session_state:
            sessions = st.session_state["_backup_sessions_user"]
            n_local = sum(1 for s in sessions if s.get("_origin") != "cloud")
            n_cloud = sum(1 for s in sessions if s.get("_origin") == "cloud")
            parts = [f"**{n_local}** local"]
            if n_cloud:
                parts.append(f"**{n_cloud}** cloud-only")
            st.markdown(f"**{' + '.join(parts)} session(s) found.** Select which to include:")
            selected = []
            for s in sessions:
                sname = s.get("name", s.get("session_name", "Unnamed"))
                origin_tag = " ☁️" if s.get("_origin") == "cloud" else ""
                if st.checkbox(f"{sname}{origin_tag}", value=True, key=f"bk_sel_{s.get('id', sname)}"):
                    selected.append(s)
            if selected:
                clean_sessions = [{k: v for k, v in s.items() if k != "_origin"} for s in selected]
                backup_payload = {
                    "lytrize_backup": True,
                    "version": "1.1",
                    "username": username,
                    "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "sessions": clean_sessions,
                }
                json_bytes = json.dumps(backup_payload, indent=2, default=str).encode("utf-8")
                ts    = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
                fname = f"lytrize_backup_{username}_{ts}.json"
                st.download_button(
                    label=f"⬇️ Save {len(clean_sessions)} session(s) — {fname}",
                    data=json_bytes,
                    file_name=fname,
                    mime="application/json",
                    key="dl_backup",
                )
            else:
                st.info("Select at least one session to download.")
        st.markdown("---")
        st.markdown("**Restore** — import sessions from a Lytrize backup file.")
        st.caption(
            "After reinstalling the app or on a new device, upload your backup JSON here. "
            "Sessions already present (matched by ID) are skipped — no duplicates."
        )

        uploaded_backup = st.file_uploader(
            "Upload backup file (.json)",
            type=["json"],
            key="backup_upload",
        )
        if uploaded_backup is not None:
            try:
                backup_bytes = uploaded_backup.read()
                payload = json.loads(backup_bytes.decode("utf-8"))
                if not payload.get("lytrize_backup"):
                    st.error("This does not look like a Lytrize backup file.")
                else:
                    sessions_to_import = payload.get("sessions", [])
                    backed_up_user = payload.get("username", "unknown")
                    backed_up_date = payload.get("exported_at", "unknown")[:10]
                    st.info(
                        f"Found **{len(sessions_to_import)}** session(s) backed up from "
                        f"account **{backed_up_user}** on {backed_up_date}."
                    )
                    if st.button("📥 Import Sessions", key="btn_restore", type="primary"):
                        # import_sessions_from_dict returns (imported, updated, skipped)
                        result = import_sessions_from_dict(user_id, sessions_to_import)
                        # Support both old 2-tuple (imported, skipped) and new 3-tuple
                        if len(result) == 3:
                            imported, updated_count, skipped = result
                        else:
                            imported, skipped = result
                            updated_count = 0

                        parts = []
                        if imported:
                            parts.append(f"**{imported}** new session(s) imported")
                        if updated_count:
                            parts.append(f"**{updated_count}** updated (backup was newer)")
                        if skipped:
                            parts.append(f"{len(skipped)} already up-to-date (skipped)")

                        if imported or updated_count:
                            # Clear stale UI state so restored rows can be deleted/edited immediately.
                            for _k in (
                                "editing_session_id",
                                "editing_session_name",
                                "editing_file_name",
                                "view_session_id",
                                "_view_charts",
                                "_view_session_id_loaded",
                            ):
                                st.session_state.pop(_k, None)
                            # Navigate to home so user immediately sees their restored sessions.
                            st.session_state.page = "home"
                            st.rerun()
                        elif skipped:
                            st.info(
                                f"All {len(skipped)} session(s) are already present and "
                                "up-to-date locally — nothing changed."
                            )
                        else:
                            st.warning("No sessions were found in the backup file.")
            except Exception as e:
                st.error(f"Could not read backup file: {e}")

    # ── Cloud data deletion (separate from full account delete) ───────────────
    if sync_configured:
        st.markdown("---")
        with st.expander("☁️ Delete Cloud Data"):
            st.markdown(
                "Delete **only** your cloud-synced data on Supabase, "
                "keeping your local sessions and account intact."
            )
            st.caption(
                "Use this if you want to start fresh on the cloud without losing your local history. "
                "You can re-sync local sessions to the cloud afterwards."
            )
            if not st.session_state.get("_confirm_del_cloud"):
                if st.button("🗑️ Delete my Supabase data", key="del_cloud_btn"):
                    st.session_state["_confirm_del_cloud"] = True
                    st.rerun()
            else:
                st.warning(
                    "This will permanently erase your sessions and account from Supabase. "
                    "Your **local** data is NOT affected. Continue?"
                )
                ca, cb = st.columns(2)
                with ca:
                    if st.button("✅ Yes, delete cloud data", type="primary",
                                 use_container_width=True, key="del_cloud_confirm"):
                        ok, err = delete_remote_user_data(username)
                        if ok:
                            st.success("✅ Your Supabase data has been deleted. Local data is intact.")
                        else:
                            st.error(f"Failed: {err}")
                        st.session_state.pop("_confirm_del_cloud", None)
                        st.rerun()
                with cb:
                    if st.button("Cancel", use_container_width=True, key="del_cloud_cancel"):
                        st.session_state.pop("_confirm_del_cloud", None)
                        st.rerun()

    # ── Danger zone ───────────────────────────────────────────────────────────

    # Build the scope description dynamically so it's always accurate.
    scope_local  = "all sessions, charts, KPIs and your login on <strong>this device</strong>"
    scope_remote = (
        " and all synced data on <strong>Supabase</strong>"
        if sync_configured else
        " (Supabase is not configured — no remote data to erase)"
    )

    st.markdown(
        '<div style="border:1.5px solid #ef4444;border-radius:12px;padding:1rem 1.2rem;'
        'background:rgba(239,68,68,0.06);margin-top:1.5rem;">'
        '<p style="color:#ef4444;font-weight:700;font-size:0.93rem;margin:0 0 0.45rem 0;">'
        '⚠️ Danger Zone</p>'
        '<p style="font-size:0.82rem;opacity:0.8;margin:0 0 0.3rem 0;">'
        'Deleting your account is <strong>permanent and irreversible</strong>.</p>'
        '<p style="font-size:0.82rem;opacity:0.8;margin:0;">'
        f'This will erase {scope_local}{scope_remote}.'
        '</p></div>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    if "confirm_delete_account" not in st.session_state:
        st.session_state.confirm_delete_account = False

    if not st.session_state.confirm_delete_account:
        if st.button("🗑️ Delete My Account", type="secondary"):
            st.session_state.confirm_delete_account = True
            st.rerun()
    else:
        # Confirmation warning — spell out exactly what will be deleted.
        if sync_configured:
            st.warning(
                "Are you sure? This will permanently delete your account, "
                "all saved sessions on this device, **and all synced data on Supabase**. "
                "This cannot be undone."
            )
        else:
            st.warning(
                "Are you sure? This will permanently delete your account and "
                "**all saved sessions on this device**. This cannot be undone."
            )

        col_yes, col_no, _ = st.columns([1, 1, 4])
        with col_yes:
            if st.button("✅ Yes, delete everything", type="primary",
                         use_container_width=True):

                # Step 1: erase remote data first (while we still know the username).
                remote_ok   = True
                remote_note = ""
                if sync_configured:
                    with st.spinner("Erasing data from Supabase…"):
                        remote_ok, remote_err = delete_remote_user_data(username)
                    if not remote_ok:
                        remote_note = (
                            f" (Supabase cleanup failed: {remote_err} — "
                            "your remote data may still exist)"
                        )

                # Step 2: erase local data.
                ok = delete_user_db(user_id)
                if ok:
                    # Revoke token from DB before clearing from disk.
                    try:
                        _tok_path = _data_dir() / "session.token"
                        _stored_tok = _tok_path.read_text().strip() if _tok_path.exists() else ""
                        if _stored_tok:
                            revoke_token(_stored_tok)
                    except Exception:
                        pass
                    # Clean up DF snapshot.
                    try:
                        for _snap in _df_snapshot_paths(user_id):
                            if _snap.exists():
                                _snap.unlink()
                    except Exception:
                        pass
                    _clear_token()
                    _clear_username()
                    for k in list(st.session_state.keys()):
                        del st.session_state[k]
                    st.query_params.clear()
                    st.session_state.page = "profile"
                    msg = f"Your account has been deleted.{remote_note}"
                    st.toast(msg, icon="🗑️")
                    st.rerun()
                else:
                    st.error(
                        "Local account could not be deleted — please try again."
                        + (f" Supabase: {remote_note}" if remote_note else "")
                    )
        with col_no:
            if st.button("✗ Cancel", use_container_width=True):
                st.session_state.confirm_delete_account = False
                st.rerun()

    inject_footer()
