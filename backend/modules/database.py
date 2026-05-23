"""
modules/database.py -- All database operations for Lytrize.

Handles: schema creation, authentication, session CRUD, token management,
activity logging, and draft session persistence.

DATABASE BACKEND
  Default : SQLite at ~/.local/share/lytrize/lytrize.db (or $LYTRIZE_DB_PATH)
  Optional: direct Postgres backend mode for local development/deployment only
            via DATABASE_URL=postgresql://user:pass@host/dbname

  Three backend-agnostic helpers hide driver differences:
    _connect()  -- returns a new connection
    _ph(sql)    -- swaps ? → %s for Postgres
    _last_id(c) -- returns last-inserted row ID

SCHEMA
  users          -- registered accounts (no password_hash is ever sent remotely)
  sessions       -- saved analysis dashboards
  user_activity  -- append-only audit log
  login_tokens   -- 7-day persistent login tokens
  draft_sessions -- auto-saved in-progress work (one row per user)

PASSWORD SECURITY
  PBKDF2-HMAC-SHA256, 260 000 iterations, random per-user salt.
  Stored as "<salt>$<hex-digest>".
  Legacy bare-SHA-256 hashes are still verified but upgraded on next login.

AUTH RATE LIMITING
  Five failed login attempts within five minutes locks the account in memory
  for the remainder of that window. The lock is per-process and resets on
  app restart — sufficient for the local desktop threat model.
"""

import json
import re
import time
import uuid
import os
import hashlib
import hmac
import datetime
import logging
from collections import defaultdict
from contextlib import contextmanager
from typing import Optional

import streamlit as st

log = logging.getLogger(__name__)

# ── Environment ───────────────────────────────────────────────────────────────

import pathlib as _pathlib
# Use 'or' so that an empty string in LYTRIZE_DB_PATH (e.g. from a blank
# .env entry) falls through to the proper user-data default, just like an
# unset variable would.  os.environ.get(key, default) does NOT do this —
# it returns the empty string because the key exists in the environment.
_default_db = str(
    _pathlib.Path.home() / ".local" / "share" / "lytrize" / "lytrize.db"
)
DB_PATH = os.environ.get("LYTRIZE_DB_PATH") or _default_db
DB_URL  = os.environ.get("DATABASE_URL", "")
_PG     = DB_URL.startswith(("postgresql://", "postgres://"))

# ── In-memory login rate limiter ──────────────────────────────────────────────
# Maps username → list of failed-attempt timestamps (UTC epoch seconds).
# Resets when the process restarts; suitable for a local desktop app.

_FAILED_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_MAX_ATTEMPTS    = 5
_WINDOW_SECONDS  = 300  # 5 minutes


def _check_rate_limit(username: str) -> bool:
    """Return True if the user may attempt a login, False if they are locked out."""
    now      = time.time()
    attempts = [t for t in _FAILED_ATTEMPTS[username] if now - t < _WINDOW_SECONDS]
    _FAILED_ATTEMPTS[username] = attempts
    return len(attempts) < _MAX_ATTEMPTS


def _record_failed_attempt(username: str) -> None:
    """Record one failed login attempt for rate-limit tracking."""
    _FAILED_ATTEMPTS[username].append(time.time())


def _clear_attempts(username: str) -> None:
    """Clear the failed-attempt counter after a successful login."""
    _FAILED_ATTEMPTS.pop(username, None)


# ── Connection helpers ────────────────────────────────────────────────────────

def _connect():
    """Return a fresh DB connection for the configured backend."""
    if _PG:
        import psycopg2
        conn = psycopg2.connect(DB_URL)
        conn.autocommit = False
        return conn
    import sqlite3
    # Ensure the parent directory exists before opening the DB file.
    # Required on first launch or after a clean install where
    # ~/.local/share/lytrize/ may not yet exist.
    _pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # WAL mode: readers never block writers and writers never block readers —
    # essential on desktop where the Streamlit server and potential background
    # sync both hit the DB.  These PRAGMAs persist for the connection lifetime.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, ~3× faster than FULL
    conn.execute("PRAGMA cache_size=-8000")     # 8 MB page cache (negative = KiB)
    conn.execute("PRAGMA temp_store=MEMORY")    # temp tables / indices in RAM
    conn.execute("PRAGMA mmap_size=134217728")  # 128 MB memory-mapped I/O
    return conn


@contextmanager
def _db():
    """
    Context manager that opens a connection, commits on success, and
    rolls back + closes on any exception. Prevents connection leaks.

    Usage:
        with _db() as conn:
            _execute(conn, "INSERT ...", params)
    """
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _ph(sql: str) -> str:
    """Translate SQLite ? placeholders to Postgres %s."""
    if _PG:
        return re.sub(r"\?", "%s", sql)
    return sql


def _last_id(cursor) -> int:
    """Return the last auto-generated row ID in a backend-agnostic way."""
    if _PG:
        cursor.execute("SELECT lastval()")
        return cursor.fetchone()[0]
    return cursor.lastrowid


def _execute(conn, query: str, params=()):
    cur = conn.cursor()
    cur.execute(_ph(query), params)
    return cur


def _execute_fetchone(conn, query: str, params=()):
    cur = conn.cursor()
    cur.execute(_ph(query), params)
    row = cur.fetchone()
    cur.close()
    return row


def _execute_fetchall(conn, query: str, params=()):
    cur = conn.cursor()
    cur.execute(_ph(query), params)
    rows = cur.fetchall()
    cur.close()
    return rows


# ── Schema helpers ────────────────────────────────────────────────────────────

_GUEST_USERNAME = "__lytrize_guest__"
_GUEST_EMAIL    = "guest@local.invalid"

# Allowlist for dynamic column selection — never allow user-supplied column names.
_ALLOWED_USER_COLS = frozenset({
    "id", "username", "email", "password_hash",
    "created_at", "is_guest", "sync_enabled", "uuid",
})


def _column_exists(conn, table: str, column: str) -> bool:
    """Return True when a table already contains the given column."""
    try:
        c = conn.cursor()
        if _PG:
            c.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name=%s",
                (table, column),
            )
            return c.fetchone() is not None
        c.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column for row in c.fetchall())
    except Exception:
        return False


def _ensure_index(conn, index_sql: str) -> None:
    try:
        conn.cursor().execute(index_sql)
    except Exception:
        pass


def _guest_row_id(conn) -> Optional[int]:
    """Return the permanent local guest user ID if it exists."""
    c = conn.cursor()
    if _column_exists(conn, "users", "is_guest"):
        c.execute(_ph("SELECT id FROM users WHERE is_guest=? LIMIT 1"), (True,))
        row = c.fetchone()
        if row:
            return row[0]
    c.execute(_ph("SELECT id FROM users WHERE username=? LIMIT 1"), (_GUEST_USERNAME,))
    row = c.fetchone()
    return row[0] if row else None


# ── Schema: CREATE IF NOT EXISTS ──────────────────────────────────────────────

def init_db() -> None:
    """
    Create all required tables. Safe to call every startup (IF NOT EXISTS).
    For SQLite, also runs ALTER TABLE migrations for columns added in later
    versions so existing databases are upgraded automatically.
    """
    conn = _connect()
    c    = conn.cursor()

    if _PG:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_guest      BOOLEAN DEFAULT FALSE,
            sync_enabled  BOOLEAN DEFAULT TRUE,
            uuid          TEXT UNIQUE
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id             SERIAL PRIMARY KEY,
            user_id        INTEGER NOT NULL,
            session_uuid   TEXT UNIQUE,
            session_name   TEXT NOT NULL,
            file_name      TEXT,
            rows_count     INTEGER,
            cols_count     INTEGER,
            analysis_types TEXT,
            charts_json    TEXT,
            dashboard_title TEXT DEFAULT '',
            kpis_json      TEXT DEFAULT '[]',
            layout_mode    TEXT DEFAULT 'portrait',
            source         TEXT DEFAULT 'cloud',
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS user_activity (
            id            SERIAL PRIMARY KEY,
            user_id       INTEGER NOT NULL,
            session_id    INTEGER,
            action_type   TEXT NOT NULL,
            action_detail TEXT,
            ts            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS login_tokens (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            username   TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS draft_sessions (
            user_id              INTEGER PRIMARY KEY,
            page                 TEXT DEFAULT 'home',
            charts_json          TEXT DEFAULT '[]',
            file_name            TEXT DEFAULT '',
            editing_session_id   INTEGER,
            editing_session_name TEXT,
            dashboard_title      TEXT DEFAULT '',
            kpis_json            TEXT DEFAULT '[]',
            chart_meta_json      TEXT DEFAULT '{}',
            layout_mode          TEXT DEFAULT 'portrait',
            updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""")

        for ddl in [
            "ALTER TABLE users    ADD COLUMN is_guest     BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users    ADD COLUMN sync_enabled BOOLEAN DEFAULT TRUE",
            "ALTER TABLE users    ADD COLUMN uuid         TEXT",
            # Core session columns — guard against very old schema versions.
            "ALTER TABLE sessions ADD COLUMN session_name    TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN file_name       TEXT",
            "ALTER TABLE sessions ADD COLUMN rows_count      INTEGER",
            "ALTER TABLE sessions ADD COLUMN cols_count      INTEGER",
            "ALTER TABLE sessions ADD COLUMN analysis_types  TEXT",
            "ALTER TABLE sessions ADD COLUMN charts_json     TEXT",
            # Later additions.
            "ALTER TABLE sessions ADD COLUMN session_uuid    TEXT",
            "ALTER TABLE sessions ADD COLUMN source          TEXT DEFAULT 'cloud'",
            "ALTER TABLE sessions ADD COLUMN dashboard_title TEXT DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN kpis_json       TEXT DEFAULT '[]'",
            "ALTER TABLE sessions ADD COLUMN layout_mode     TEXT DEFAULT 'portrait'",
            "ALTER TABLE sessions ADD COLUMN updated_at      TIMESTAMP",  # last-write-wins sync
        ]:
            try:
                c.execute(ddl)
            except Exception:
                pass

    else:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_guest      INTEGER DEFAULT 0,
            sync_enabled  INTEGER DEFAULT 1,
            uuid          TEXT UNIQUE
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            session_uuid    TEXT UNIQUE,
            session_name    TEXT NOT NULL,
            file_name       TEXT,
            rows_count      INTEGER,
            cols_count      INTEGER,
            analysis_types  TEXT,
            charts_json     TEXT,
            dashboard_title TEXT DEFAULT '',
            kpis_json       TEXT DEFAULT '[]',
            layout_mode     TEXT DEFAULT 'portrait',
            source          TEXT DEFAULT 'local',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS user_activity (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            session_id    INTEGER,
            action_type   TEXT NOT NULL,
            action_detail TEXT,
            ts            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS login_tokens (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            username   TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS draft_sessions (
            user_id              INTEGER PRIMARY KEY,
            page                 TEXT DEFAULT 'home',
            charts_json          TEXT DEFAULT '[]',
            file_name            TEXT DEFAULT '',
            editing_session_id   INTEGER,
            editing_session_name TEXT,
            dashboard_title      TEXT DEFAULT '',
            kpis_json            TEXT DEFAULT '[]',
            chart_meta_json      TEXT DEFAULT '{}',
            layout_mode          TEXT DEFAULT 'portrait',
            updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""")

        # Migrations: add columns introduced in later schema versions.
        for ddl in [
            "ALTER TABLE users    ADD COLUMN is_guest     INTEGER DEFAULT 0",
            "ALTER TABLE users    ADD COLUMN sync_enabled INTEGER DEFAULT 1",
            "ALTER TABLE users    ADD COLUMN uuid         TEXT",
            # Core session columns — guard against very old schema versions where
            # these weren't in the original CREATE TABLE statement.
            "ALTER TABLE sessions ADD COLUMN session_name    TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN file_name       TEXT",
            "ALTER TABLE sessions ADD COLUMN rows_count      INTEGER",
            "ALTER TABLE sessions ADD COLUMN cols_count      INTEGER",
            "ALTER TABLE sessions ADD COLUMN analysis_types  TEXT",
            "ALTER TABLE sessions ADD COLUMN charts_json     TEXT",
            # Later-version additions.
            "ALTER TABLE sessions ADD COLUMN session_uuid    TEXT",
            "ALTER TABLE sessions ADD COLUMN source          TEXT DEFAULT 'local'",
            "ALTER TABLE sessions ADD COLUMN dashboard_title TEXT DEFAULT ''",
            "ALTER TABLE sessions ADD COLUMN kpis_json       TEXT DEFAULT '[]'",
            "ALTER TABLE sessions ADD COLUMN layout_mode     TEXT DEFAULT 'portrait'",
            # updated_at enables last-write-wins sync conflict resolution and
            # is referenced in save_session_db INSERT — must exist before first save.
            "ALTER TABLE sessions ADD COLUMN updated_at      TIMESTAMP",
        ]:
            try:
                c.execute(ddl)
            except Exception:
                pass

        # Seed permanent guest account if missing.
        try:
            c.execute("SELECT COUNT(*) FROM users WHERE username=?", (_GUEST_USERNAME,))
            if c.fetchone()[0] == 0:
                c.execute(
                    "INSERT INTO users "
                    "(username, email, password_hash, is_guest, sync_enabled, uuid) "
                    "VALUES (?,?,?,?,?,?)",
                    (_GUEST_USERNAME, _GUEST_EMAIL, _hash(uuid.uuid4().hex), 1, 0, uuid.uuid4().hex),
                )
        except Exception:
            pass

        # Backfill session UUIDs for legacy rows that have none.
        try:
            c.execute(
                "SELECT id, session_name, created_at FROM sessions "
                "WHERE session_uuid IS NULL OR session_uuid='' ORDER BY id"
            )
            for sid, sname, created_at in c.fetchall():
                seed = f"{sid}:{sname}:{created_at}"
                c.execute(
                    "UPDATE sessions SET session_uuid=? WHERE id=?",
                    (uuid.uuid5(uuid.NAMESPACE_URL, seed).hex, sid),
                )
        except Exception:
            pass

    _ensure_index(
        conn,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_session_uuid ON sessions(session_uuid)",
    )

    conn.commit()
    conn.close()


# ── Password hashing ──────────────────────────────────────────────────────────

def _hash(pw: str, salt: Optional[str] = None) -> str:
    """
    Hash a password with PBKDF2-HMAC-SHA256, 260 000 iterations, random salt.

    Returns "<salt>$<hex-digest>" for storage in users.password_hash.
    Pass an existing salt to reproduce the hash for verification.
    """
    if salt is None:
        salt = uuid.uuid4().hex
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000)
    return f"{salt}${dk.hex()}"


def _verify(pw: str, stored: str) -> bool:
    """
    Verify a plain-text password against a stored hash in constant time.

    Accepts both the new salted format ("salt$hash") and the legacy bare
    SHA-256 format so old accounts can still log in.
    """
    if "$" in stored:
        salt, _ = stored.split("$", 1)
        return hmac.compare_digest(_hash(pw, salt), stored)
    # Legacy bare SHA-256 — still accepted, upgraded on successful login.
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)


# ── Activity logging ──────────────────────────────────────────────────────────

def log_activity(
    user_id: int,
    action_type: str,
    detail: str = "",
    session_id=None,
) -> None:
    """
    Append an event to the audit log. Never raises — silently no-ops on error
    so logging failures never crash the app.
    Detail is truncated to 1 000 characters.
    """
    try:
        with _db() as conn:
            _execute(
                conn,
                "INSERT INTO user_activity "
                "(user_id, session_id, action_type, action_detail) "
                "VALUES (?,?,?,?)",
                (user_id, session_id, action_type, str(detail)[:1000]),
            )
    except Exception:
        pass


# ── Authentication ────────────────────────────────────────────────────────────

def _validate_registration_inputs(
    username: str, email: str, password: str
) -> Optional[str]:
    """
    Validate registration inputs. Returns an error message, or None if valid.
    All checks are done before touching the database.
    """
    if not 3 <= len(username) <= 40:
        return "Username must be 3–40 characters."
    if not re.match(r"^[A-Za-z0-9_.\-]+$", username):
        return "Username may only contain letters, numbers, underscores, dots, and hyphens."
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return "Please enter a valid email address."
    if len(email) > 254:
        return "Email address is too long."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 1024:
        return "Password is too long."
    return None


def register_user(username: str, email: str, password: str) -> tuple:
    """
    Create a new user account.

    Returns (True, "Account created!") on success,
            (False, "<reason>")        on failure.

    Validates all inputs before writing to the database.
    Raw DB errors are never returned to the caller.
    """
    err = _validate_registration_inputs(username, email, password)
    if err:
        return False, err

    # Pre-check uniqueness before INSERT to give accurate error messages.
    # SQLite's constraint error messages can vary across versions.
    try:
        with _db() as conn:
            c = conn.cursor()
            c.execute(_ph("SELECT 1 FROM users WHERE username=? LIMIT 1"), (username,))
            if c.fetchone():
                return False, "Username already taken."
            c.execute(_ph("SELECT 1 FROM users WHERE email=? LIMIT 1"), (email,))
            if c.fetchone():
                return False, "Email already registered."
    except Exception as e:
        log.error("register_user: pre-check error: %s", e)
        return False, "Registration failed — please try again."

    try:
        with _db() as conn:
            _execute(
                conn,
                "INSERT INTO users (username, email, password_hash) VALUES (?,?,?)",
                (username, email, _hash(password)),
            )
        return True, "Account created!"
    except Exception as e:
        msg = str(e).lower()
        if "username" in msg:
            return False, "Username already taken."
        if "email" in msg:
            return False, "Email already registered."
        log.error("register_user: unexpected error: %s", e)
        return False, "Registration failed — please try again."


def login_user(username: str, password: str) -> Optional[tuple]:
    """
    Validate login credentials with rate limiting.

    Returns (user_id, username, email) on success, None on failure.

    After five failed attempts within five minutes the account is locked for
    the remainder of that window (in-process only; resets on app restart).

    Upgrades legacy bare-SHA-256 hashes to PBKDF2 silently on success.
    """
    if not _check_rate_limit(username):
        log.warning("login_user: rate limit hit for '%s'", username)
        return None

    try:
        conn = _connect()
        c    = conn.cursor()
        c.execute(
            _ph("SELECT id, username, email, password_hash "
               "FROM users WHERE username=? OR email=? LIMIT 1"),
            (username, username),
        )
        row = c.fetchone()
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not row:
        _record_failed_attempt(username)
        return None

    uid, uname, email, stored_hash = row

    if not _verify(password, stored_hash):
        _record_failed_attempt(username)
        return None

    _clear_attempts(username)

    # Silently upgrade bare SHA-256 legacy hashes to PBKDF2.
    if "$" not in stored_hash:
        try:
            with _db() as conn:
                _execute(conn, _ph("UPDATE users SET password_hash=? WHERE id=?"),
                         (_hash(password), uid))
        except Exception:
            pass

    return uid, uname, email


def update_local_password(user_id: int, new_password: str) -> bool:
    """
    Update the local PBKDF2 password hash for a user after a successful
    Supabase cloud password reset.  Called once the cloud update is confirmed
    so the local SQLite credentials stay in sync.
    """
    try:
        with _db() as conn:
            _execute(
                conn,
                _ph("UPDATE users SET password_hash=? WHERE id=?"),
                (_hash(new_password), user_id),
            )
            # NOTE: do NOT call conn.commit() here — the _db() context manager
            # commits on a clean exit.  A second commit is a no-op on SQLite but
            # raises an error on some Postgres drivers and is misleading to readers.
        return True
    except Exception as e:
        log.warning("update_local_password: %s", e)
        return False


# ── Login tokens ──────────────────────────────────────────────────────────────

def create_token(user_id: int, username: str) -> str:
    """
    Create a 7-day persistent login token and store it in login_tokens.
    Returns the 32-char hex token string.
    The token is stored on disk by auth.py — it is never written to the URL.
    """
    token   = uuid.uuid4().hex
    expires = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
    ).isoformat()

    with _db() as conn:
        if _PG:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO login_tokens (token, user_id, username, expires_at)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT(token) DO UPDATE SET expires_at = EXCLUDED.expires_at""",
                (token, user_id, username, expires),
            )
        else:
            _execute(
                conn,
                "INSERT OR REPLACE INTO login_tokens "
                "(token, user_id, username, expires_at) VALUES (?,?,?,?)",
                (token, user_id, username, expires),
            )

    return token


def validate_token(token: str) -> Optional[tuple]:
    """
    Validate a login token. Returns (user_id, username) if valid and
    unexpired, None otherwise. Handles both string and datetime expiry fields.
    """
    if not token:
        return None

    conn = None
    try:
        conn = _connect()
        c    = conn.cursor()
        c.execute(
            _ph("SELECT user_id, username, expires_at FROM login_tokens WHERE token=?"),
            (token,),
        )
        row = c.fetchone()
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not row:
        return None

    expires_raw = row[2]
    if isinstance(expires_raw, datetime.datetime):
        expires_dt = (
            expires_raw if expires_raw.tzinfo
            else expires_raw.replace(tzinfo=datetime.timezone.utc)
        )
    else:
        try:
            expires_dt = datetime.datetime.fromisoformat(
                str(expires_raw).replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=datetime.timezone.utc)

    if datetime.datetime.now(datetime.timezone.utc) >= expires_dt:
        return None

    return row[0], row[1]


def revoke_token(token: str) -> None:
    """Delete a login token (called on sign-out)."""
    if not token:
        return
    try:
        with _db() as conn:
            _execute(conn, _ph("DELETE FROM login_tokens WHERE token=?"), (token,))
    except Exception:
        pass


def cleanup_expired_tokens() -> None:
    """Delete all login_tokens rows that have already expired.

    Called opportunistically at startup by app.py so the table does not
    grow unboundedly on long-lived installations. Never raises.
    """
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with _db() as conn:
            _execute(conn, _ph("DELETE FROM login_tokens WHERE expires_at <= ?"), (now_iso,))
    except Exception:
        pass


# ── Guest user ────────────────────────────────────────────────────────────────

def get_or_create_guest_user() -> dict:
    """Return the permanent local guest user row, creating it if needed."""
    conn = _connect()
    try:
        uid = _guest_row_id(conn)
        if uid:
            c = conn.cursor()
            c.execute(_ph("SELECT id, username FROM users WHERE id=?"), (uid,))
            row = c.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "is_guest": True}

        c    = conn.cursor()
        cols = {"username": _GUEST_USERNAME, "email": _GUEST_EMAIL,
                "password_hash": _hash(uuid.uuid4().hex)}
        if _column_exists(conn, "users", "is_guest"):
            cols["is_guest"] = True
        if _column_exists(conn, "users", "sync_enabled"):
            cols["sync_enabled"] = False
        if _column_exists(conn, "users", "uuid"):
            cols["uuid"] = uuid.uuid4().hex

        keys = ", ".join(cols.keys())
        phs  = ", ".join(["%s" if _PG else "?"] * len(cols))
        try:
            c.execute(_ph(f"INSERT INTO users ({keys}) VALUES ({phs})"), tuple(cols.values()))
            conn.commit()
        except Exception:
            conn.rollback()

        uid = _guest_row_id(conn)
        if uid:
            c.execute(_ph("SELECT id, username FROM users WHERE id=?"), (uid,))
            row = c.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "is_guest": True}
    finally:
        conn.close()

    # Last-ditch fallback: try a direct SELECT in case INSERT raced with another process.
    try:
        conn2 = _connect()
        uid2  = _guest_row_id(conn2)
        conn2.close()
        if uid2:
            return {"id": uid2, "username": _GUEST_USERNAME, "is_guest": True}
    except Exception:
        pass
    log.error("get_or_create_guest_user: failed to obtain a valid guest user_id")
    # Return a stable sentinel that callers can guard with `if user_id is None`.
    return {"id": None, "username": _GUEST_USERNAME, "is_guest": True}


def merge_user_data(source_user_id: int, target_user_id: int) -> None:
    """Reassign local data from a guest account to a newly signed-in account.

    Called immediately after sign-in or registration so any analysis work done
    as a guest is visible under the real account.

    draft_sessions has user_id as PRIMARY KEY (one row per user), so a plain
    UPDATE SET user_id=target WHERE user_id=source would hit a UNIQUE constraint
    if the target already has a draft (e.g. the user signed in on this device
    before, generating a draft, then signed out and continued as guest).
    The fix: delete the target's stale draft first, then reassign the guest draft.
    The guest draft is fresher — it reflects what the user was just working on.
    """
    if not source_user_id or not target_user_id or source_user_id == target_user_id:
        return
    try:
        with _db() as conn:
            # sessions and user_activity: safe bulk reassignment (no PK collision risk).
            for table in ("sessions", "user_activity"):
                _execute(
                    conn,
                    _ph(f"UPDATE {table} SET user_id=? WHERE user_id=?"),
                    (target_user_id, source_user_id),
                )

            # draft_sessions: user_id IS the PRIMARY KEY — must avoid collision.
            # Delete any stale target draft before moving the guest draft across.
            _execute(
                conn,
                _ph("DELETE FROM draft_sessions WHERE user_id=?"),
                (target_user_id,),
            )
            _execute(
                conn,
                _ph("UPDATE draft_sessions SET user_id=? WHERE user_id=?"),
                (target_user_id, source_user_id),
            )

            if _column_exists(conn, "sessions", "source"):
                _execute(
                    conn,
                    _ph("UPDATE sessions SET source='local' WHERE user_id=?"),
                    (target_user_id,),
                )
        # Invalidate the session-list cache so the next get_user_sessions()
        # call reflects the newly merged sessions immediately.
        try:
            get_user_sessions.clear()
        except Exception:
            pass
    except Exception as e:
        log.warning("merge_user_data: %s", e)


# ── Draft sessions ────────────────────────────────────────────────────────────

def save_draft(
    user_id: int,
    page: str,
    charts_json: str,
    file_name: str = "",
    editing_session_id=None,
    editing_session_name=None,
    dashboard_title: str = "",
    kpis_json: str = "[]",
    chart_meta_json: str = "{}",
    layout_mode: str = "portrait",
) -> None:
    """
    Upsert the user's current in-progress state to draft_sessions.
    One draft row per user; silently no-ops on any error.
    """
    try:
        with _db() as conn:
            if _PG:
                _execute(conn, """
                    INSERT INTO draft_sessions
                        (user_id, page, charts_json, file_name, editing_session_id,
                         editing_session_name, dashboard_title, kpis_json,
                         chart_meta_json, layout_mode, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        page=EXCLUDED.page,
                        charts_json=EXCLUDED.charts_json,
                        file_name=EXCLUDED.file_name,
                        editing_session_id=EXCLUDED.editing_session_id,
                        editing_session_name=EXCLUDED.editing_session_name,
                        dashboard_title=EXCLUDED.dashboard_title,
                        kpis_json=EXCLUDED.kpis_json,
                        chart_meta_json=EXCLUDED.chart_meta_json,
                        layout_mode=EXCLUDED.layout_mode,
                        updated_at=CURRENT_TIMESTAMP""",
                    (user_id, page, charts_json, file_name,
                     editing_session_id, editing_session_name,
                     dashboard_title, kpis_json, chart_meta_json, layout_mode),
                )
            else:
                _execute(conn, """
                    INSERT OR REPLACE INTO draft_sessions
                        (user_id, page, charts_json, file_name, editing_session_id,
                         editing_session_name, dashboard_title, kpis_json,
                         chart_meta_json, layout_mode, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (user_id, page, charts_json, file_name,
                     editing_session_id, editing_session_name,
                     dashboard_title, kpis_json, chart_meta_json, layout_mode),
                )
    except Exception:
        pass


def get_draft(user_id: int) -> Optional[dict]:
    """
    Retrieve the stored draft for a user.
    Uses cursor.description so the mapping survives schema migrations.
    Returns a dict of column → value, or None.
    """
    conn = None
    try:
        conn = _connect()
        c    = conn.cursor()
        c.execute(_ph("SELECT * FROM draft_sessions WHERE user_id=?"), (user_id,))
        row  = c.fetchone()
        desc = c.description
        if row and desc:
            return {col[0]: val for col, val in zip(desc, row)}
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def clear_draft(user_id: int) -> None:
    """Delete the draft row after a successful session save."""
    try:
        with _db() as conn:
            _execute(conn, _ph("DELETE FROM draft_sessions WHERE user_id=?"), (user_id,))
    except Exception:
        pass


# ── Sessions CRUD ─────────────────────────────────────────────────────────────

def save_session_db(
    user_id: int,
    session_name: str,
    file_name: str,
    rows: int,
    cols: int,
    analysis_types: list,
    charts_json: str,
    dashboard_title: str = "",
    kpis_json: str = "[]",
    layout_mode: str = "portrait",
    session_uuid: Optional[str] = None,
    source: str = "local",
) -> int:
    """Insert a new saved session and return its DB row ID."""
    session_uuid = session_uuid or uuid.uuid4().hex
    with _db() as conn:
        c = conn.cursor()
        c.execute(
            _ph("""INSERT INTO sessions
               (user_id, session_uuid, session_name, file_name, rows_count, cols_count,
                analysis_types, charts_json, dashboard_title, kpis_json, layout_mode,
                source, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)"""),
            (user_id, session_uuid, session_name, file_name, rows, cols,
             json.dumps(analysis_types), charts_json,
             dashboard_title, kpis_json, layout_mode, source),
        )
        sid = _last_id(c)
    log_activity(user_id, "dashboard_saved",
                 f"session='{session_name}' file='{file_name}'", sid)
    get_user_sessions.clear()  # invalidate session list cache
    return sid


def get_session_uuid(session_id: int, user_id=None) -> Optional[str]:
    """Return the logical UUID for a session row."""
    conn = None
    try:
        conn = _connect()
        c    = conn.cursor()
        if user_id is None:
            c.execute(_ph("SELECT session_uuid FROM sessions WHERE id=?"), (session_id,))
        else:
            c.execute(
                _ph("SELECT session_uuid FROM sessions WHERE id=? AND user_id=?"),
                (session_id, user_id),
            )
        row = c.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def rename_session_db(session_id: int, new_name: str, user_id=None) -> None:
    """Rename a saved session. user_id guard prevents cross-account renames."""
    with _db() as conn:
        if user_id is None:
            _execute(conn,
                     _ph("UPDATE sessions SET session_name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"),
                     (new_name, session_id))
        else:
            _execute(conn,
                     _ph("UPDATE sessions SET session_name=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?"),
                     (new_name, session_id, user_id))
    get_user_sessions.clear()  # invalidate session list cache





def sanitize_restored_session(
    session_data: dict,
    current_user_id: int,
    *,
    preserve_uuid: bool = True,
) -> dict:
    """
    Normalize imported backup sessions before inserting into the local DB.

    Backups may come from:
      - the same device/account
      - a different device/account
      - a previous schema version

    This helper keeps the portable analysis payload but rewrites runtime-
    specific fields so restored rows behave like native local sessions.
    """

    cleaned = dict(session_data or {})

    # Backups do not carry local ownership metadata; always bind to the
    # current user when restoring into this device's SQLite database.
    cleaned["user_id"] = current_user_id

    # Keep the original logical session UUID when available so cloud sync can
    # de-duplicate by identity.  If the backup does not carry one, or the
    # caller explicitly wants a fresh local record, generate one.
    if not preserve_uuid or not cleaned.get("session_uuid"):
        cleaned["session_uuid"] = str(uuid.uuid4())

    # Never restore internal transport/soft-delete metadata.
    for key in (
        "cloud_id",
        "device_id",
        "deleted_at",
        "is_deleted",
        "remote_id",
        "remote_uuid",
    ):
        cleaned.pop(key, None)

    return cleaned



def delete_session_db(session_id: int, user_id: int) -> bool:
    """Delete a saved session and clear cached session listings.

    The first delete is ownership-guarded. If a restored backup row was
    imported with stale metadata or the user_id guard no longer matches,
    fall back to the row's session UUID and finally to a local row-id delete.
    This keeps imported backups deletable even after older restore formats or
    partial migrations.
    """
    deleted = False
    with _db() as conn:
        cur = conn.cursor()
        # Primary path: delete by current ownership.
        cur.execute(_ph("DELETE FROM sessions WHERE id=? AND user_id=?"),
                    (session_id, user_id))
        deleted = getattr(cur, "rowcount", 0) > 0

        if not deleted:
            # Fallback 1: resolve the session UUID by id alone (no user_id
            # guard), then delete by that UUID.
            #
            # The primary DELETE used "WHERE id=? AND user_id=?" and returned
            # rowcount 0, which means either the row doesn't exist or the
            # stored user_id no longer matches (e.g. after a backup restore or
            # cross-account migration).  Querying with the same two-column
            # predicate would also return 0 rows — we only need id here.
            sess_uuid = None
            try:
                row = _execute_fetchone(
                    conn,
                    _ph("SELECT session_uuid FROM sessions WHERE id=?"),
                    (session_id,),
                )
                if row and row[0]:
                    sess_uuid = row[0]
            except Exception:
                sess_uuid = None

            if sess_uuid:
                cur.execute(_ph("DELETE FROM sessions WHERE session_uuid=? AND user_id=?"),
                            (sess_uuid, user_id))
                deleted = getattr(cur, "rowcount", 0) > 0

            if not deleted:
                # Fallback 2: delete by row id only. This is intentionally
                # conservative and only used when the row is clearly a stale
                # restore artifact or when ownership metadata has drifted.
                cur.execute(_ph("DELETE FROM sessions WHERE id=?"), (session_id,))
                deleted = getattr(cur, "rowcount", 0) > 0

        # _db() context manager commits on clean exit — no manual commit needed.

    try:
        get_user_sessions.clear()  # invalidate session list cache
    except Exception:
        pass

    try:
        st.cache_data.clear()
    except Exception:
        pass

    if not deleted:
        log.warning(
            "delete_session_db: no row deleted for session_id=%s user_id=%s",
            session_id,
            user_id,
        )
    return deleted


def update_session_db(
    session_id: int,
    session_name: str,
    charts_json: str,
    analysis_types: list,
    user_id: int,
    dashboard_title: str = "",
    kpis_json: str = "[]",
    layout_mode: str = "portrait",
    session_uuid: Optional[str] = None,
) -> None:
    """Overwrite a saved session in-place. user_id guard enforces ownership."""
    with _db() as conn:
        if session_uuid is not None:
            _execute(conn, _ph(
                "UPDATE sessions SET session_uuid=? "
                "WHERE id=? AND user_id=? AND (session_uuid IS NULL OR session_uuid='')"),
                (session_uuid, session_id, user_id),
            )
        _execute(conn, _ph(
            "UPDATE sessions "
            "SET session_name=?, charts_json=?, analysis_types=?, "
            "    dashboard_title=?, kpis_json=?, layout_mode=?, "
            "    updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND user_id=?"),
            (session_name, charts_json, json.dumps(analysis_types),
             dashboard_title, kpis_json, layout_mode, session_id, user_id),
        )
    log_activity(user_id, "session_updated",
                 f"session_id={session_id} name='{session_name}'")
    get_user_sessions.clear()  # invalidate session list cache


@st.cache_data(ttl=30, show_spinner=False)
def get_user_sessions(user_id: int) -> list:
    """Return the 20 most recent sessions for a user, newest first.
    Cached for 30 s so rapid Streamlit reruns don't hammer SQLite.
    Call get_user_sessions.clear() after any session write to invalidate.
    """
    if user_id is None:
        return []
    conn = _connect()
    wanted = ["id", "session_name", "file_name", "rows_count",
              "cols_count", "analysis_types", "created_at"]
    try:
        c = conn.cursor()
        if _PG:
            # PostgreSQL: query information_schema for existing columns.
            c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='sessions'",
            )
            available_cols = {row[0] for row in c.fetchall()}
        else:
            # SQLite: use PRAGMA — guards against old DB schemas.
            c.execute("PRAGMA table_info(sessions)")
            available_cols = {row[1] for row in c.fetchall()}

        select_cols = ", ".join(c for c in wanted if c in available_cols)
        if not select_cols:
            return []
        # Order by most recent modification time. updated_at is present in all
        # current schemas; COALESCE falls back to created_at for legacy rows.
        order_expr = (
            "COALESCE(updated_at, created_at)"
            if "updated_at" in available_cols
            else "created_at"
        )
        c.execute(
            _ph(f"SELECT {select_cols} FROM sessions "
                f"WHERE user_id=? ORDER BY {order_expr} DESC LIMIT 20"),
            (user_id,),
        )
        rows = c.fetchall()
        # Pad to always return 7-tuples so callers are schema-agnostic.
        n_got = len(select_cols.split(", "))
        if n_got < len(wanted):
            rows = [tuple(r) + (None,) * (len(wanted) - n_got) for r in rows]
        return rows
    except Exception as e:
        log.warning("get_user_sessions: %s", e)
        return []
    finally:
        conn.close()


def get_session_meta(session_id: int, user_id=None) -> Optional[dict]:
    """Fetch dashboard metadata (title, KPIs, layout) for a session."""
    conn = None
    try:
        conn = _connect()
        c    = conn.cursor()
        if user_id is None:
            c.execute(
                _ph("SELECT dashboard_title, kpis_json, layout_mode FROM sessions WHERE id=?"),
                (session_id,),
            )
        else:
            c.execute(
                _ph("SELECT dashboard_title, kpis_json, layout_mode "
                    "FROM sessions WHERE id=? AND user_id=?"),
                (session_id, user_id),
            )
        row = c.fetchone()
        if row:
            return {
                "dashboard_title": row[0] or "",
                "kpis_json":       row[1] or "[]",
                "layout_mode":     row[2] or "portrait",
            }
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def get_session_charts(session_id: int, user_id=None) -> list:
    """
    Load and deserialise charts from a saved session.
    Returns a list of (uid, title, fig, desc, auto_insights, chart_type, meta) tuples.
    Entries that fail to deserialise are skipped silently.
    """
    import plotly.io as pio

    conn = _connect()
    try:
        c = conn.cursor()
        if user_id is None:
            c.execute(_ph("SELECT charts_json FROM sessions WHERE id=?"), (session_id,))
        else:
            c.execute(
                _ph("SELECT charts_json FROM sessions WHERE id=? AND user_id=?"),
                (session_id, user_id),
            )
        row = c.fetchone()
    finally:
        conn.close()

    if not (row and row[0]):
        return []

    try:
        raw_items = json.loads(row[0])
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("get_session_charts: failed to parse charts_json for session %s: %s",
                    session_id, exc)
        return []

    charts = []
    for item in raw_items:
        try:
            uid           = item.get("uid", uuid.uuid4().hex[:8])
            desc          = item.get("desc", "")
            auto_insights = item.get("auto_insights", [])
            chart_type    = item.get("chart_type", "")
            meta          = item.get("meta", {})
            fig           = pio.from_json(item["fig_json"])
            charts.append((uid, item["title"], fig, desc, auto_insights, chart_type, meta))
        except Exception:
            pass
    return charts


def delete_user_db(user_id: int) -> bool:
    """
    Permanently delete a user account and all associated data.

    Deletes in FK-dependency order:
      login_tokens → draft_sessions → user_activity → sessions → users
    """
    try:
        with _db() as conn:
            _execute(conn, _ph("DELETE FROM login_tokens   WHERE user_id=?"), (user_id,))
            _execute(conn, _ph("DELETE FROM draft_sessions WHERE user_id=?"), (user_id,))
            _execute(conn, _ph("DELETE FROM user_activity  WHERE user_id=?"), (user_id,))
            _execute(conn, _ph("DELETE FROM sessions        WHERE user_id=?"), (user_id,))
            _execute(conn, _ph("DELETE FROM users           WHERE id=?"),      (user_id,))
        return True
    except Exception as e:
        log.error("delete_user_db: %s", e)
        return False


# ── Backup / Restore ──────────────────────────────────────────────────────────

def export_sessions_to_dict(
    user_id: int,
    username: str = "",
    local_db_path: str = "",
) -> list[dict]:
    """
    Export ALL sessions for user_id as a list of dicts for JSON backup.

    Sources merged (deduplicated by session_uuid):
      1. Local SQLite   — always included (the primary store).
      2. Supabase cloud — fetched live when Supabase is configured and
                          username is provided.  Cloud-only sessions (those
                          not yet pulled to this device) are included so the
                          backup is truly complete.

    Parameters
    ----------
    user_id       Local SQLite user id.
    username      Account username — required to fetch cloud sessions.
    local_db_path Path to lytrize.db — passed to sync layer for auth lookup.
                  Defaults to the path used by _db() if blank.
    """
    local_sessions: list[dict] = []
    try:
        with _db() as conn:
            c = conn.cursor()
            # Build column list dynamically to handle old DB schemas gracefully.
            c.execute("PRAGMA table_info(sessions)")
            available = {row[1] for row in c.fetchall()}
            wanted = [
                "session_uuid", "session_name", "file_name", "rows_count",
                "cols_count", "analysis_types", "charts_json", "dashboard_title",
                "kpis_json", "layout_mode", "source", "created_at",
            ]
            select_parts = [col for col in wanted if col in available]
            # updated_at: use COALESCE so older rows without the column still export.
            if "updated_at" in available:
                select_parts.append("COALESCE(updated_at, created_at) AS updated_at")
            elif "created_at" in available:
                select_parts.append("created_at AS updated_at")
            select_sql = ", ".join(select_parts)
            c.execute(
                f"SELECT {select_sql} FROM sessions WHERE user_id=? ORDER BY created_at",
                (user_id,),
            )
            cols = [d[0] for d in c.description]
            local_sessions = [dict(zip(cols, row)) for row in c.fetchall()]
    except Exception as e:
        log.warning("export_sessions_to_dict (local): %s", e)

    # ── Fetch cloud sessions and merge ────────────────────────────────────────
    cloud_sessions: list[dict] = []
    if username and username.strip():
        try:
            from modules.sync import export_cloud_sessions as _ecs
            _db_path = local_db_path or str(DB_PATH)
            cloud_sessions = _ecs(username, _db_path)
        except Exception as e:
            log.warning("export_sessions_to_dict (cloud): %s", e)

    if not cloud_sessions:
        return local_sessions

    # Merge: local wins on conflict (local is the user's primary editable store).
    # Cloud sessions not present locally are appended so nothing is lost.
    local_uuids = {s.get("session_uuid") for s in local_sessions if s.get("session_uuid")}
    cloud_only  = [s for s in cloud_sessions if s.get("session_uuid") not in local_uuids]

    if cloud_only:
        log.info("export_sessions_to_dict: appending %d cloud-only session(s)", len(cloud_only))

    return local_sessions + cloud_only




def import_sessions_from_dict(
    user_id: int,
    sessions: list[dict],
) -> tuple[int, int, list[str]]:
    """
    Import sessions from a backup dict list into the local SQLite DB.

    Restore behaviour:
      - bind all imported rows to the current local user_id
      - keep the portable session_uuid where possible
      - update existing local copies when the backup is newer
      - re-key locally if a UNIQUE collision would otherwise skip the row
    """
    imported = 0
    updated  = 0
    skipped: list[str] = []

    def _parse_ts(value) -> Optional[datetime.datetime]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.datetime.fromtimestamp(value)
            except Exception:
                return None
        try:
            s = str(value).strip()
            if not s:
                return None
            s = s.replace("Z", "+00:00")
            return datetime.datetime.fromisoformat(s)
        except Exception:
            try:
                return datetime.datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None

    def _ts_for_compare(row: dict) -> datetime.datetime:
        return (
            _parse_ts(row.get("updated_at"))
            or _parse_ts(row.get("created_at"))
            or datetime.datetime.min
        )

    def _normalize_payload(raw: dict) -> dict:
        raw = {k: v for k, v in raw.items() if k != "_origin"}
        return sanitize_restored_session(raw, user_id, preserve_uuid=True)

    try:
        with _db() as conn:
            c = conn.cursor()
            for raw in sessions:
                if not isinstance(raw, dict):
                    skipped.append("invalid-session")
                    continue

                s = _normalize_payload(raw)
                sess_uuid = s.get("session_uuid") or ""
                sname     = s.get("session_name", "Restored Session")
                source    = s.get("source") or "local"

                existing = None
                if sess_uuid:
                    c.execute(
                        _ph("SELECT id, COALESCE(updated_at, created_at) "
                            "FROM sessions WHERE session_uuid=? AND user_id=?"),
                        (sess_uuid, user_id),
                    )
                    existing = c.fetchone()

                if existing:
                    local_id      = existing[0]
                    local_updated  = _ts_for_compare({"updated_at": existing[1]})
                    backup_updated = _ts_for_compare(s)

                    # Last-write-wins: only update if the backup is newer.
                    if backup_updated and backup_updated > local_updated:
                        try:
                            _execute(
                                conn,
                                _ph("""UPDATE sessions SET
                                       session_name=?, file_name=?, rows_count=?,
                                       cols_count=?, analysis_types=?, charts_json=?,
                                       dashboard_title=?, kpis_json=?, layout_mode=?,
                                       source=?, updated_at=CURRENT_TIMESTAMP
                                       WHERE id=? AND user_id=?"""),
                                (sname, s.get("file_name"), s.get("rows_count"),
                                 s.get("cols_count"), s.get("analysis_types"),
                                 s.get("charts_json"), s.get("dashboard_title", ""),
                                 s.get("kpis_json", "[]"), s.get("layout_mode", "portrait"),
                                 source, local_id, user_id),
                            )
                            updated += 1
                        except Exception as exc:
                            log.warning("import_sessions_from_dict: update failed: %s", exc)
                            skipped.append(sname)
                    else:
                        skipped.append(sname)
                    continue

                def _insert_row(payload: dict) -> None:
                    _execute(
                        conn,
                        _ph("""INSERT INTO sessions
                               (user_id, session_uuid, session_name, file_name, rows_count,
                                cols_count, analysis_types, charts_json, dashboard_title,
                                kpis_json, layout_mode, source, created_at, updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)"""),
                        (
                            user_id,
                            payload.get("session_uuid") or None,
                            payload.get("session_name", "Restored Session"),
                            payload.get("file_name"),
                            payload.get("rows_count"),
                            payload.get("cols_count"),
                            payload.get("analysis_types"),
                            payload.get("charts_json"),
                            payload.get("dashboard_title", ""),
                            payload.get("kpis_json", "[]"),
                            payload.get("layout_mode", "portrait"),
                            payload.get("source") or "local",
                            payload.get("created_at"),
                        ),
                    )

                try:
                    _insert_row(s)
                    imported += 1
                except Exception as exc:
                    # UNIQUE collision on session_uuid or a stale restore row.
                    # Re-key locally and retry once.
                    try:
                        s2 = dict(s)
                        s2["session_uuid"] = uuid.uuid4().hex
                        _insert_row(s2)
                        imported += 1
                    except Exception as exc2:
                        log.warning(
                            "import_sessions_from_dict: insert failed for %s: %s / %s",
                            sname, exc, exc2
                        )
                        skipped.append(sname)

        # Invalidate session list cache so home page reflects the restore.
        try:
            get_user_sessions.clear()
        except Exception:
            pass

        try:
            st.cache_data.clear()
        except Exception:
            pass

    except Exception as e:
        log.warning("import_sessions_from_dict: %s", e)

    return imported, updated, skipped
