"""
modules/sync.py -- Cloud sync for Lytrize via Supabase Auth + PostgREST.

This module keeps the desktop app offline-first. Cloud features are optional:
when configured, the app authenticates through Supabase Auth using the user's
email/password and syncs session rows through the public REST API.

No PostgreSQL connection string or database password is shipped to the end
user. The desktop app only needs:
  - the Supabase project URL
  - the public anon key

Cloud session tokens are stored locally in:
  ~/.local/share/lytrize/cloud.session.json

Sync behaviour:
  - local SQLite remains the primary offline store
  - cloud sync is triggered manually from the Profile page
  - the first successful sign-in/register can create and cache the cloud
    session automatically, so later sync is a single button click
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

APP_NAME = "lytrize"

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

def _data_dir() -> Path:
    """Return the Lytrize data directory, respecting XDG_DATA_HOME."""
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except Exception:
        pass
    return d


def _cloud_session_path() -> Path:
    """Return path to the cloud session file (respects XDG_DATA_HOME at call time)."""
    return _data_dir() / "cloud.session.json"


def _supabase_project_url() -> str:
    """
    Return the Supabase project URL, e.g. https://xxxx.supabase.co.

    Accepted env vars (first non-empty wins):
      - LYTRIZE_SUPABASE_URL          (preferred)
      - LYTRIZE_SUPABASE_PROJECT_URL  (legacy alias)
    """
    return (
        os.environ.get("LYTRIZE_SUPABASE_URL", "")
        or os.environ.get("LYTRIZE_SUPABASE_PROJECT_URL", "")
        or ""
    ).rstrip("/")


def _supabase_anon_key() -> str:
    return (
        os.environ.get("LYTRIZE_SUPABASE_ANON_KEY", "")
        or os.environ.get("LYTRIZE_SUPABASE_PUBLIC_KEY", "")
        or ""
    )


def _is_configured() -> bool:
    return _supabase_project_url().startswith("https://") and bool(_supabase_anon_key())


def _auth_base() -> str:
    return f"{_supabase_project_url()}/auth/v1"


def _rest_base() -> str:
    return f"{_supabase_project_url()}/rest/v1"


def _headers(access_token: Optional[str] = None) -> dict[str, str]:
    token = access_token or _supabase_anon_key()
    return {
        "apikey": _supabase_anon_key(),
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(
    method: str,
    url: str,
    *,
    access_token: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: int = 20,
    prefer: Optional[str] = None,
) -> requests.Response:
    headers = _headers(access_token)
    if prefer:
        headers["Prefer"] = prefer
    resp = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
        timeout=timeout,
    )
    return resp


# ── Legacy compatibility shims ──────────────────────────────────────────────
# Older code paths referenced direct PostgreSQL helpers. The current desktop
# architecture uses Supabase REST + local SQLite instead.

def _pg_connect():
    """Compatibility shim for stale imports.

    Direct PostgreSQL access is intentionally disabled in the desktop client.
    """
    raise RuntimeError(
        "Direct PostgreSQL access is disabled in Lytrize desktop sync. "
        "Use the Supabase REST sync path instead."
    )


def _ensure_remote_tables(_conn) -> None:
    """Compatibility no-op for legacy callers."""
    return None


def _pull_sessions(_conn, _remote_id, _username, _db_path) -> int:
    """Compatibility shim for the removed direct-Postgres import path."""
    raise RuntimeError(
        "Legacy direct-Postgres account import is no longer supported."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cloud session handling
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CloudSession:
    access_token: str
    refresh_token: str
    token_type: str
    expires_at: str
    user_id: str
    email: str
    username: str = ""

    def is_expired(self, leeway_seconds: int = 60) -> bool:
        try:
            expires = _dt.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=_dt.timezone.utc)
            return _dt.datetime.now(_dt.timezone.utc) >= (expires - _dt.timedelta(seconds=leeway_seconds))
        except Exception:
            return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
            "email": self.email,
            "username": self.username,
        }


def _normalise_session(payload: dict[str, Any], fallback_username: str = "", fallback_email: str = "") -> CloudSession:
    session = payload.get("session") or payload
    user = payload.get("user") or session.get("user") or {}
    access_token = session.get("access_token") or payload.get("access_token") or ""
    refresh_token = session.get("refresh_token") or payload.get("refresh_token") or ""
    token_type = session.get("token_type") or payload.get("token_type") or "bearer"
    expires_in = session.get("expires_in") or payload.get("expires_in") or 3600

    if not access_token:
        raise ValueError("Supabase auth did not return an access token")

    auth_uid = user.get("id") or session.get("user", {}).get("id") or payload.get("user_id") or ""
    email = user.get("email") or fallback_email or ""
    meta = user.get("user_metadata") or user.get("app_metadata") or {}
    username = (
        meta.get("username")
        or meta.get("name")
        or payload.get("username")
        or fallback_username
        or ""
    )

    if isinstance(expires_in, str):
        try:
            expires_in = int(expires_in)
        except Exception:
            expires_in = 3600

    expires_at = session.get("expires_at") or payload.get("expires_at")
    if not expires_at:
        expires_at = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=int(expires_in))).isoformat()

    return CloudSession(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=token_type,
        expires_at=expires_at,
        user_id=str(auth_uid),
        email=str(email),
        username=str(username),
    )


def load_cloud_session() -> Optional[CloudSession]:
    path = _cloud_session_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        session = CloudSession(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            token_type=data.get("token_type", "bearer"),
            expires_at=data["expires_at"],
            user_id=str(data.get("user_id", "")),
            email=str(data.get("email", "")),
            username=str(data.get("username", "")),
        )
        return session
    except Exception:
        return None


def save_cloud_session(session: CloudSession | dict[str, Any]) -> None:
    try:
        _data_dir().mkdir(parents=True, exist_ok=True)
        if isinstance(session, CloudSession):
            payload = session.as_dict()
        else:
            payload = dict(session)
        path = _cloud_session_path()
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
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
    except Exception as e:
        log.debug("save_cloud_session: %s", e)


def clear_cloud_session() -> None:
    try:
        p = _cloud_session_path()
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _refresh_cloud_session(session: CloudSession) -> Optional[CloudSession]:
    if not session.refresh_token:
        return None
    try:
        resp = _request(
            "POST",
            f"{_auth_base()}/token?grant_type=refresh_token",
            json_body={"refresh_token": session.refresh_token},
        )
        if not resp.ok:
            return None
        data = resp.json()
        refreshed = _normalise_session(data, fallback_username=session.username, fallback_email=session.email)
        save_cloud_session(refreshed)
        return refreshed
    except Exception as e:
        log.debug("_refresh_cloud_session: %s", e)
        return None


def _ensure_valid_cloud_session() -> Optional[CloudSession]:
    session = load_cloud_session()
    if not session:
        return None
    if not session.is_expired():
        return session
    refreshed = _refresh_cloud_session(session)
    return refreshed or session


def cloud_sign_in(email: str, password: str, username: str = "") -> tuple[bool, str, Optional[CloudSession]]:
    if not _is_configured():
        return False, "Cloud sync is not configured.", None
    if not email or not password:
        return False, "Email and password are required.", None
    try:
        resp = _request(
            "POST",
            f"{_auth_base()}/token?grant_type=password",
            json_body={"email": email, "password": password},
        )
        if not resp.ok:
            try:
                err = resp.json().get("msg") or resp.json().get("error_description") or resp.text
            except Exception:
                err = resp.text
            return False, err or "Cloud sign-in failed.", None
        session = _normalise_session(resp.json(), fallback_username=username, fallback_email=email)
        save_cloud_session(session)
        _upsert_cloud_profile(session, username=username, email=email)
        return True, "", session
    except Exception as e:
        return False, str(e), None


def cloud_sign_up(email: str, password: str, username: str) -> tuple[bool, str, Optional[CloudSession]]:
    """Create a Supabase Auth account and return a live session.

    Requires email confirmation to be disabled in the Supabase project
    (run supabase_rls.sql which sets mailer_autoconfirm = true).
    """
    if not _is_configured():
        return False, "Cloud sync is not configured.", None
    if not email or not password:
        return False, "Email and password are required.", None
    try:
        resp = _request(
            "POST",
            f"{_auth_base()}/signup",
            json_body={"email": email, "password": password, "data": {"username": username}},
        )
        if not resp.ok:
            body = {}
            try:
                body = resp.json()
            except Exception:
                pass
            err = body.get("msg") or body.get("error_description") or body.get("error") or resp.text
            # Already registered — treat as sign-in
            if "already registered" in (err or "").lower():
                return cloud_sign_in(email, password, username=username)
            return False, err or "Cloud registration failed.", None

        data = resp.json()

        # If no session in the response, email confirmation is still enabled
        # in the Supabase dashboard — run supabase_rls.sql to fix this.
        session = None
        try:
            session = _normalise_session(data, fallback_username=username, fallback_email=email)
            save_cloud_session(session)
        except Exception:
            pass

        if session is None:
            ok, msg, session = cloud_sign_in(email, password, username=username)
            if not ok:
                return False, (
                    "Registration succeeded but sign-in failed. "
                    "Make sure you have run supabase_rls.sql in your Supabase project "
                    "to disable email confirmation, then try again."
                ), None

        _upsert_cloud_profile(session, username=username, email=email)
        return True, "", session
    except Exception as e:
        return False, str(e), None


def ensure_cloud_session(email: str, password: str, username: str) -> tuple[bool, str, Optional[CloudSession]]:
    """
    Sign in to Supabase if possible; if the account does not exist yet, create it.

    This is called after the user has already authenticated locally, so the app
    can store a cloud session once and keep syncing under the hood.
    """
    if not _is_configured():
        return False, "Cloud sync is not configured.", None

    current = _ensure_valid_cloud_session()
    if current and current.email and email and current.email.lower() == email.lower():
        _upsert_cloud_profile(current, username=username, email=email)
        return True, "", current

    ok, msg, session = cloud_sign_in(email, password, username=username)
    if ok and session:
        return True, "", session

    # If the account does not exist yet, create it using the same credentials.
    if "invalid login credentials" in (msg or "").lower() or "invalid_credentials" in (msg or "").lower() or "user not found" in (msg or "").lower():
        return cloud_sign_up(email, password, username)

    # For other failures, still try to create the account if the auth service permits it.
    ok2, msg2, session2 = cloud_sign_up(email, password, username)
    if ok2:
        return ok2, msg2, session2
    return False, msg or msg2 or "Could not link cloud account.", None


def _session_auth_headers(session: CloudSession) -> dict[str, str]:
    return _headers(session.access_token)


# ─────────────────────────────────────────────────────────────────────────────
# Local database helpers
# ─────────────────────────────────────────────────────────────────────────────

def _local_db_path(local_db_path: str | None = None) -> Path:
    if local_db_path:
        return Path(local_db_path)
    return Path(os.environ.get("LYTRIZE_DB_PATH") or (_data_dir() / "lytrize.db"))


def _sqlite_connect(local_db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_local_db_path(local_db_path)), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _local_user_row(conn: sqlite3.Connection, identifier: str) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute(
        "SELECT id, username, email, password_hash, is_guest, sync_enabled, uuid "
        "FROM users WHERE username=? OR email=? LIMIT 1",
        (identifier, identifier),
    )
    return c.fetchone()


def _local_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute(
        "SELECT id, username, email, password_hash, is_guest, sync_enabled, uuid "
        "FROM users WHERE username=? LIMIT 1",
        (username,),
    )
    return c.fetchone()


def _local_sessions(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    c = conn.cursor()
    c.execute(
        """
        SELECT id, user_id, session_uuid, session_name, file_name, rows_count, cols_count,
               analysis_types, charts_json, dashboard_title, kpis_json, layout_mode,
               source, created_at
        FROM sessions
        WHERE user_id=?
        ORDER BY COALESCE(updated_at, created_at) ASC, id ASC
        """,
        (user_id,),
    )
    rows = [dict(r) for r in c.fetchall()]
    for row in rows:
        if not row.get("session_uuid"):
            row["session_uuid"] = uuid.uuid4().hex
            c.execute("UPDATE sessions SET session_uuid=? WHERE id=?", (row["session_uuid"], row["id"]))
    conn.commit()
    return rows


def _session_payload_from_local(row: dict[str, Any], source: str = "local") -> dict[str, Any]:
    return {
        "session_uuid": row.get("session_uuid") or uuid.uuid4().hex,
        "session_name": row.get("session_name") or "",
        "file_name": row.get("file_name") or "",
        "rows_count": row.get("rows_count"),
        "cols_count": row.get("cols_count"),
        "analysis_types": row.get("analysis_types") or "",
        "charts_json": row.get("charts_json") or "[]",
        "dashboard_title": row.get("dashboard_title") or "",
        "kpis_json": row.get("kpis_json") or "[]",
        "layout_mode": row.get("layout_mode") or "portrait",
        "source": source,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at") or row.get("created_at"),
    }


def _remote_profile(session: CloudSession) -> Optional[dict[str, Any]]:
    """
    Fetch the user's public.users row from Supabase.

    Tries UUID first (the fast, indexed path). Falls back to email if the UUID
    lookup returns nothing — this handles accounts created before the uuid column
    was populated, or rows whose uuid was never updated after first sync.
    """
    def _get(params: dict) -> Optional[dict[str, Any]]:
        try:
            resp = _request(
                "GET",
                f"{_rest_base()}/users",
                access_token=session.access_token,
                params={**params, "select": "id,username,email,uuid,sync_enabled", "limit": 1},
            )
            if not resp.ok:
                return None
            rows = resp.json()
            return rows[0] if rows else None
        except Exception as e:
            log.debug("_remote_profile GET: %s", e)
            return None

    # Primary: look up by Supabase auth UID.
    profile = _get({"uuid": f"eq.{session.user_id}"})
    if profile:
        return profile

    # Fallback: look up by email (handles pre-uuid or uuid-mismatch rows).
    if session.email:
        profile = _get({"email": f"eq.{session.email}"})
        if profile:
            # Back-fill the uuid so future lookups hit the fast path.
            try:
                _request(
                    "PATCH",
                    f"{_rest_base()}/users",
                    access_token=session.access_token,
                    params={"id": f"eq.{profile['id']}"},
                    json_body={"uuid": session.user_id},
                )
            except Exception as e:
                log.debug("_remote_profile uuid back-fill: %s", e)
            return profile

    return None


def _upsert_cloud_profile(session: CloudSession, username: str, email: str) -> Optional[dict[str, Any]]:
    """Upsert the user's public profile row into Supabase via PostgREST.

    Strategy:
      1. POST with on_conflict=uuid (fast path — handles returning users).
      2. If that fails with a unique violation on username or email (HTTP 409 or
         23505 detail), PATCH the existing row's uuid to the current auth UID and
         return the updated row. This repairs orphaned rows from before uuid was
         populated, or rows created on a previous device install.
      3. If all else fails, return None so sync_now can report the error cleanly.
    """
    if not session or not session.access_token:
        return None

    resolved_username = username or session.username or (email or session.email or "").split("@", 1)[0]
    resolved_email    = email or session.email

    payload = {
        "username":     resolved_username,
        "email":        resolved_email,
        "uuid":         session.user_id,
        "sync_enabled": True,
    }

    # ── Attempt 1: upsert on uuid conflict ───────────────────────────────────
    try:
        resp = _request(
            "POST",
            f"{_rest_base()}/users",
            access_token=session.access_token,
            params={"on_conflict": "uuid"},
            json_body=[payload],
            prefer="resolution=merge-duplicates,return=representation",
        )
        if resp.ok:
            try:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                if isinstance(data, dict) and data:
                    return data
            except Exception:
                pass
            return payload

        status = resp.status_code
        body   = resp.text[:400]
        log.debug("_upsert_cloud_profile attempt 1: HTTP %s — %s", status, body)

        # ── Attempt 2: unique conflict on email/username → patch uuid on existing row
        is_conflict = status in (409, 422) or "23505" in body or "unique" in body.lower()
        if not is_conflict:
            return None

    except Exception as e:
        log.debug("_upsert_cloud_profile attempt 1 exception: %s", e)
        return None

    # ── Attempt 2: upsert on email conflict ──────────────────────────────────
    # The row exists under this email but with a different (or NULL) uuid.
    # Now that RLS SELECT/UPDATE policies also allow access via auth.email(),
    # a merge-on-email upsert will find the row and overwrite uuid correctly.
    try:
        resp2 = _request(
            "POST",
            f"{_rest_base()}/users",
            access_token=session.access_token,
            params={"on_conflict": "email"},
            json_body=[payload],
            prefer="resolution=merge-duplicates,return=representation",
        )
        if resp2.ok:
            try:
                data = resp2.json()
                if isinstance(data, list) and data:
                    return data[0]
                if isinstance(data, dict) and data:
                    return data
            except Exception:
                pass
            return payload
        log.debug("_upsert_cloud_profile attempt 2 (on_conflict=email): HTTP %s — %s",
                  resp2.status_code, resp2.text[:200])
    except Exception as e:
        log.debug("_upsert_cloud_profile attempt 2 exception: %s", e)

    # ── Attempt 3: PATCH via email or username ────────────────────────────────
    # Last resort: direct PATCH. Requires the RLS UPDATE policy to allow
    # access via auth.email() when uuid IS NULL (bootstrap repair).
    for filter_key, filter_val in [("email", resolved_email), ("username", resolved_username)]:
        if not filter_val:
            continue
        try:
            patch_resp = _request(
                "PATCH",
                f"{_rest_base()}/users",
                access_token=session.access_token,
                params={filter_key: f"eq.{filter_val}"},
                json_body={"uuid": session.user_id, "sync_enabled": True},
                prefer="return=representation",
            )
            if patch_resp.ok:
                try:
                    data = patch_resp.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    if isinstance(data, dict) and data:
                        return data
                except Exception:
                    pass
                return payload
            log.debug("_upsert_cloud_profile PATCH by %s: HTTP %s — %s",
                      filter_key, patch_resp.status_code, patch_resp.text[:200])
        except Exception as e:
            log.debug("_upsert_cloud_profile PATCH by %s exception: %s", filter_key, e)

    return None


_REMOTE_PAGE_SIZE = 500  # PostgREST rows per page


def _remote_sessions(session: CloudSession, remote_user_id: int) -> list[dict[str, Any]]:
    """Fetch all remote sessions for the given user, paginating in chunks."""
    results: list[dict[str, Any]] = []
    offset = 0
    try:
        while True:
            resp = _request(
                "GET",
                f"{_rest_base()}/sessions",
                access_token=session.access_token,
                params={
                    "select": "*",
                    "user_id": f"eq.{remote_user_id}",
                    "order": "updated_at.asc",
                    "limit": _REMOTE_PAGE_SIZE,
                    "offset": offset,
                },
            )
            if not resp.ok:
                log.debug("_remote_sessions: HTTP %s at offset %d", resp.status_code, offset)
                break
            page = resp.json()
            if not isinstance(page, list):
                break
            results.extend(page)
            if len(page) < _REMOTE_PAGE_SIZE:
                break  # last page
            offset += _REMOTE_PAGE_SIZE
    except Exception as e:
        log.debug("_remote_sessions: %s", e)
    return results


def _remote_session_upsert(session: CloudSession, remote_user_id: int, row: dict[str, Any]) -> bool:
    payload = {
        "user_id": remote_user_id,
        "session_uuid": row.get("session_uuid") or uuid.uuid4().hex,
        "session_name": row.get("session_name") or "",
        "file_name": row.get("file_name") or "",
        "rows_count": row.get("rows_count"),
        "cols_count": row.get("cols_count"),
        "analysis_types": row.get("analysis_types") or "",
        "charts_json": row.get("charts_json") or "[]",
        "dashboard_title": row.get("dashboard_title") or "",
        "kpis_json": row.get("kpis_json") or "[]",
        "layout_mode": row.get("layout_mode") or "portrait",
        "source": row.get("source") or "local",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at") or row.get("created_at") or _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    try:
        # PostgREST upsert: on_conflict belongs ONLY in query params, not the URL path.
        # json_body must be a list — PostgREST requires an array for upsert.
        resp = _request(
            "POST",
            f"{_rest_base()}/sessions",
            access_token=session.access_token,
            params={"on_conflict": "session_uuid"},
            json_body=[payload],
            prefer="resolution=merge-duplicates,return=representation",
        )
        if resp.ok:
            return True
        log.warning(
            "_remote_session_upsert: HTTP %s — %s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    except Exception as e:
        log.debug("_remote_session_upsert: %s", e)
        return False


def _remote_delete_user(session: CloudSession, remote_user_id: int) -> bool:
    try:
        _request(
            "DELETE",
            f"{_rest_base()}/sessions",
            access_token=session.access_token,
            params={"user_id": f"eq.{remote_user_id}"},
        )
        resp = _request(
            "DELETE",
            f"{_rest_base()}/users",
            access_token=session.access_token,
            params={"id": f"eq.{remote_user_id}"},
        )
        return resp.ok
    except Exception as e:
        log.debug("_remote_delete_user: %s", e)
        return False


def _apply_remote_row_to_local(conn: sqlite3.Connection, local_user_id: int, row: dict[str, Any]) -> bool:
    cur = conn.cursor()
    session_uuid = row.get("session_uuid") or uuid.uuid4().hex
    created_at = row.get("created_at") or _dt.datetime.now(_dt.timezone.utc).isoformat()
    updated_at = row.get("updated_at") or created_at
    source = row.get("source") or "cloud"

    existing = cur.execute(
        "SELECT id, updated_at, created_at FROM sessions WHERE session_uuid=? AND user_id=?",
        (session_uuid, local_user_id),
    ).fetchone()
    if existing:
        db_updated = existing[1] or existing[2] or ""
        try:
            db_dt = _dt.datetime.fromisoformat(str(db_updated).replace("Z", "+00:00"))
            if db_dt.tzinfo is None:
                db_dt = db_dt.replace(tzinfo=_dt.timezone.utc)
            remote_dt = _dt.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            if remote_dt.tzinfo is None:
                remote_dt = remote_dt.replace(tzinfo=_dt.timezone.utc)
            if remote_dt <= db_dt:
                return False
        except Exception:
            pass

        cur.execute(
            """
            UPDATE sessions
               SET session_name=?, file_name=?, rows_count=?, cols_count=?,
                   analysis_types=?, charts_json=?, dashboard_title=?, kpis_json=?,
                   layout_mode=?, source=?, updated_at=?
             WHERE id=? AND user_id=?
            """,
            (
                row.get("session_name") or "",
                row.get("file_name") or "",
                row.get("rows_count"),
                row.get("cols_count"),
                row.get("analysis_types") or "",
                row.get("charts_json") or "[]",
                row.get("dashboard_title") or "",
                row.get("kpis_json") or "[]",
                row.get("layout_mode") or "portrait",
                source,
                updated_at,
                existing[0],
                local_user_id,
            ),
        )
        return True

    cur.execute(
        """
        INSERT INTO sessions
            (user_id, session_uuid, session_name, file_name, rows_count, cols_count,
             analysis_types, charts_json, dashboard_title, kpis_json, layout_mode,
             source, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            local_user_id,
            session_uuid,
            row.get("session_name") or "",
            row.get("file_name") or "",
            row.get("rows_count"),
            row.get("cols_count"),
            row.get("analysis_types") or "",
            row.get("charts_json") or "[]",
            row.get("dashboard_title") or "",
            row.get("kpis_json") or "[]",
            row.get("layout_mode") or "portrait",
            source,
            created_at,
            updated_at,
        ),
    )
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

class SyncResult:
    def __init__(self, pushed: int = 0, pulled: int = 0, error: Optional[str] = None, configured: bool = True):
        self.pushed = pushed
        self.pulled = pulled
        self.error = error
        self.configured = configured

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        if not self.configured:
            return "Cloud sync not configured"
        if self.error:
            return f"Sync failed: {self.error}"
        parts = []
        if self.pushed:
            parts.append(f"pushed {self.pushed}")
        if self.pulled:
            parts.append(f"pulled {self.pulled}")
        return "Up to date" if not parts else " | ".join(parts)


def get_sync_status() -> dict[str, Any]:
    session = load_cloud_session()
    return {
        "configured": _is_configured(),
        "provider": "Supabase Auth" if _is_configured() else "not configured",
        "linked": bool(session and session.access_token),
        "account": session.email if session else "",
    }


def sync_now(username: str, local_db_path: str) -> SyncResult:
    """
    Manual sync button: push local sessions to the cloud, then pull remote
    sessions down into the local SQLite database.

    Cloud session must already be linked once via login/register, which stores
    a refresh token locally. No raw password is stored.
    """
    if not username:
        return SyncResult(error="No signed-in account.", configured=_is_configured())
    if not _is_configured():
        return SyncResult(configured=False)

    session = _ensure_valid_cloud_session()
    if not session:
        return SyncResult(error="Cloud account not linked yet. Sign in once to enable sync.")

    try:
        conn = _sqlite_connect(local_db_path)
    except Exception as e:
        return SyncResult(error=f"Cannot open local database: {e}")

    pushed = 0
    pulled = 0

    try:
        local_user = _local_user_by_username(conn, username)
        if not local_user:
            return SyncResult(error=f"Local account '{username}' was not found.")

        email = local_user["email"] or session.email
        profile = _remote_profile(session) or _upsert_cloud_profile(session, username=local_user["username"], email=email)
        if not profile:
            return SyncResult(error=(
                "Could not resolve your cloud profile. "
                "This usually means the Supabase Row Level Security policies have not been applied yet. "
                "Open the Supabase SQL Editor for your project and run the contents of "
                "backend/sql_db_config/supabase_rls.sql, then try syncing again."
            ))

        remote_user_id = int(profile["id"])

        # Push local sessions.
        local_rows = _local_sessions(conn, int(local_user["id"]))
        for row in local_rows:
            if _remote_session_upsert(session, remote_user_id, row):
                pushed += 1

        # Pull remote sessions.
        remote_rows = _remote_sessions(session, remote_user_id)
        for r in remote_rows:
            if _apply_remote_row_to_local(conn, int(local_user["id"]), r):
                pulled += 1

        conn.commit()
        return SyncResult(pushed=pushed, pulled=pulled)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return SyncResult(error=str(e))
    finally:
        conn.close()


def export_cloud_sessions(username: str, local_db_path: str) -> list[dict[str, Any]]:
    if not _is_configured():
        return []
    session = _ensure_valid_cloud_session()
    if not session:
        return []

    try:
        conn = _sqlite_connect(local_db_path)
        try:
            local_user = _local_user_by_username(conn, username)
            if not local_user:
                return []
            profile = _remote_profile(session)
            if not profile:
                return []
            rows = _remote_sessions(session, int(profile["id"]))
            results = []
            for r in rows:
                d = dict(r)
                d["_origin"] = "cloud"
                results.append(d)
            return results
        finally:
            conn.close()
    except Exception as e:
        log.debug("export_cloud_sessions: %s", e)
        return []


def delete_remote_user_data(username: str) -> tuple[bool, str]:
    if not _is_configured():
        return False, "not configured"
    session = _ensure_valid_cloud_session()
    if not session:
        return False, "cloud account not linked"

    try:
        conn = _sqlite_connect(None)
        try:
            local_user = _local_user_by_username(conn, username)
            if not local_user:
                return False, "local user not found"
            profile = _remote_profile(session)
            if not profile:
                return True, ""
            ok = _remote_delete_user(session, int(profile["id"]))
            return (ok, "" if ok else "could not delete cloud data")
        finally:
            conn.close()
    except Exception as e:
        return False, str(e)



def cloud_bootstrap_local_account(identifier: str, password: str) -> tuple[bool, str, Optional[dict[str, Any]]]:
    """
    Use after a local sign-in failure. If the user entered their cloud email and
    password, this tries to authenticate against Supabase and returns the cloud
    profile so the caller can create the local account.

    BUG FIX: previously returned immediately on cloud_sign_in failure even when
    the error was "invalid login credentials" -- meaning the user has a
    public.users profile row in Supabase but was never registered in Supabase
    Auth (auth.users). This happens when the sync ran but the Supabase Auth
    sign-up step failed (e.g. network drop). We now fall through to
    cloud_sign_up in that case, mirroring the same fallback in ensure_cloud_session.
    """
    if not _is_configured():
        return False, "Cloud sync is not configured.", None

    email = identifier if "@" in identifier else ""
    if not email:
        return False, "Enter your email to sign in on a new device.", None

    ok, msg, session = cloud_sign_in(email=email, password=password, username="")

    if not ok or not session:
        _cred_errors = (
            "invalid login credentials",
            "invalid_credentials",
            "user not found",
        )
        if any(k in (msg or "").lower() for k in _cred_errors):
            fallback_username = email.split("@", 1)[0]
            ok2, msg2, session = cloud_sign_up(
                email=email, password=password, username=fallback_username
            )
            if not ok2 or not session:
                return False, msg or msg2, None
        else:
            return False, msg, None

    profile = _remote_profile(session) or {}
    return True, "", {
        "email": profile.get("email") or email,
        "username": profile.get("username") or email.split("@", 1)[0],
        "cloud_user_id": profile.get("id"),
        "cloud_uid": profile.get("uuid") or session.user_id,
        "session": session.as_dict(),
    }
