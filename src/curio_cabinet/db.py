"""SQLite connection handling and engine-owned tables.

Deployment note: on network-attached storage (e.g. NearlyFreeSpeech) WAL's
shared-memory index is unreliable across processes, so production runs a
single writer process and the journal mode is configurable. The requested
mode is applied per connection; if SQLite refuses it we keep whatever it
gave us and warn rather than fail.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["connect", "ensure_engine_tables", "utcnow"]

ENGINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    totp_last_counter INTEGER,
    created_at TEXT NOT NULL,
    password_changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'full',  -- 'pre_totp' between password and TOTP steps
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY,
    username TEXT,
    ip TEXT,
    attempted_at TEXT NOT NULL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_time ON login_attempts(attempted_at);
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    focal_x REAL,
    focal_y REAL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_item ON images(item_id, position);
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path, *, journal_mode: str = "WAL") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    requested = journal_mode.upper()
    if requested not in {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"}:
        raise ValueError(f"invalid journal_mode {journal_mode!r}")
    (applied,) = conn.execute(f"PRAGMA journal_mode = {requested}").fetchone()
    if applied.upper() != requested:
        log.warning("journal_mode %s not applied (got %s)", requested, applied)
    if applied.upper() == "WAL":
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure_engine_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(ENGINE_SCHEMA)
    conn.commit()
