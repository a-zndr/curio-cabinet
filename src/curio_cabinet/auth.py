"""Authentication: passwords, TOTP, server-side sessions, login throttling.

Deliberately framework-free — every function takes a sqlite3 connection so
the whole module is testable without Flask. The Flask glue (cookies,
decorators, routes) lives in views/admin.py.

Design invariants:
- Only vetted primitives: argon2-cffi, pyotp, secrets, hmac.compare_digest.
- Raw session tokens are never stored; the DB holds sha256(token), so a
  leaked database or backup cannot hijack live sessions.
- There is no signup. The admin account is created by CLI only, and
  recovery is CLI-over-shell only (documented tradeoff: losing both the
  password and shell access is unrecoverable).
- Login throttling is a progressive delay, not a hard lockout: an
  attacker hammering the fixed admin username cannot lock the admin out,
  they can only slow themselves down.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import secrets
import sqlite3

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .db import utcnow

__all__ = [
    "AuthError",
    "UserExistsError",
    "ThrottledError",
    "create_admin_user",
    "reset_admin_password",
    "change_password",
    "check_password",
    "login_delay_remaining",
    "record_attempt",
    "begin_totp_enrollment",
    "confirm_totp_enrollment",
    "disable_totp",
    "verify_totp",
    "create_session",
    "session_by_token",
    "promote_session",
    "destroy_session",
    "destroy_user_sessions",
    "verify_csrf",
]

# RFC 9106 low-memory profile. If hashing OOMs on a constrained host,
# drop memory_cost to 32 MiB and raise time_cost to 4.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1)

# Verified against a real hash so unknown-username logins burn the same
# argon2 work as wrong-password logins (no timing-based user enumeration).
_DUMMY_HASH = _hasher.hash("curio-cabinet-dummy-password")

SESSION_ABSOLUTE = _dt.timedelta(days=7)
SESSION_IDLE = _dt.timedelta(hours=24)
PRE_TOTP_TTL = _dt.timedelta(minutes=5)
MIN_PASSWORD_LEN = 12


class AuthError(Exception):
    pass


class UserExistsError(AuthError):
    pass


class ThrottledError(AuthError):
    def __init__(self, wait_seconds: int):
        self.wait_seconds = wait_seconds
        super().__init__(f"too many attempts; wait {wait_seconds}s")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _ts(moment: _dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(text: str) -> _dt.datetime:
    return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc
    )


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Accounts ----------------------------------------------------------------


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")


def create_admin_user(conn: sqlite3.Connection, username: str, password: str) -> None:
    username = username.strip()
    if not username:
        raise AuthError("username is required")
    _validate_password(password)
    (count,) = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    if count:
        raise UserExistsError(
            "an admin account already exists (use reset-password to recover)"
        )
    now = utcnow()
    conn.execute(
        "INSERT INTO users (username, password_hash, created_at, password_changed_at) "
        "VALUES (?, ?, ?, ?)",
        (username, _hasher.hash(password), now, now),
    )
    conn.commit()


def reset_admin_password(conn: sqlite3.Connection, password: str) -> str:
    """CLI recovery path. Resets the (single) admin and kills all sessions."""
    _validate_password(password)
    user = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    if user is None:
        raise AuthError("no admin account exists; run create-admin")
    conn.execute(
        "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
        (_hasher.hash(password), utcnow(), user["id"]),
    )
    destroy_user_sessions(conn, user["id"])
    conn.commit()
    return user["username"]


def change_password(
    conn: sqlite3.Connection, user_id: int, current: str, new: str
) -> None:
    """Authenticated password change: re-verifies, then logs out everywhere."""
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or not _verify_hash(user["password_hash"], current):
        raise AuthError("current password is incorrect")
    _validate_password(new)
    conn.execute(
        "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
        (_hasher.hash(new), utcnow(), user_id),
    )
    destroy_user_sessions(conn, user_id)
    conn.commit()


def _verify_hash(stored: str, password: str) -> bool:
    try:
        _hasher.verify(stored, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def check_password(
    conn: sqlite3.Connection, username: str, password: str
) -> sqlite3.Row | None:
    """Constant-work credential check. Returns the user row or None."""
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()
    if user is None:
        _verify_hash(_DUMMY_HASH, password)  # burn equivalent work
        return None
    if not _verify_hash(user["password_hash"], password):
        return None
    if _hasher.check_needs_rehash(user["password_hash"]):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_hasher.hash(password), user["id"]),
        )
        conn.commit()
    return user


# Throttling ---------------------------------------------------------------
#
# Progressive delay on consecutive failures for a username:
#   failures:  1  2  3   4   5   6  ...
#   delay (s): 0  2  8  30  60  300 (capped)
# The clock restarts from the most recent failure; success clears it.
#
# A username-keyed throttle alone is a denial-of-service handle: an
# attacker hammering the (single, guessable) admin username keeps the
# delay window rolling forever and locks the real admin out. Devices that
# have successfully signed in before hold a signed "known device" cookie
# that bypasses the username throttle — the attacker stays throttled, the
# admin's own browser never is.

_DELAYS = (0, 0, 2, 8, 30, 60, 300)


DEVICE_MAX_AGE = _dt.timedelta(days=30)


def _device_sig(secret: str, username: str, pw_changed_at: str, issued: str) -> str:
    msg = f"known-device:{username}:{pw_changed_at}:{issued}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def issue_device_token(secret: str, username: str, pw_changed_at: str) -> str:
    """Mint a device token for a browser that just authenticated.

    Bound to the account's password_changed_at (so a password change or CLI
    reset revokes every device token) and stamped with an issue time (so it
    expires). It is NOT a permanent credential: it only lets a returning
    browser skip the anti-lockout throttle, and it dies on any credential
    change or after DEVICE_MAX_AGE.
    """
    issued = _ts(_now())
    return f"{issued}.{_device_sig(secret, username, pw_changed_at, issued)}"


def verify_device_token(
    secret: str, username: str, pw_changed_at: str, token: str | None
) -> bool:
    if not token or "." not in token:
        return False
    issued, _, sig = token.partition(".")
    if not hmac.compare_digest(_device_sig(secret, username, pw_changed_at, issued), sig):
        return False
    try:
        return _now() - _parse_ts(issued) <= DEVICE_MAX_AGE
    except ValueError:
        return False


def login_delay_remaining(conn: sqlite3.Connection, username: str) -> int:
    rows = conn.execute(
        "SELECT attempted_at, success FROM login_attempts WHERE username = ? "
        "ORDER BY id DESC LIMIT 10",
        (username.strip(),),
    ).fetchall()
    failures = 0
    latest: _dt.datetime | None = None
    for row in rows:
        if row["success"]:
            break
        failures += 1
        if latest is None:
            latest = _parse_ts(row["attempted_at"])
    if not failures or latest is None:
        return 0
    delay = _DELAYS[min(failures, len(_DELAYS) - 1)]
    elapsed = (_now() - latest).total_seconds()
    return max(0, int(delay - elapsed))


def record_attempt(
    conn: sqlite3.Connection, username: str, ip: str | None, success: bool
) -> None:
    conn.execute(
        "INSERT INTO login_attempts (username, ip, attempted_at, success) "
        "VALUES (?, ?, ?, ?)",
        (username.strip(), ip, utcnow(), int(success)),
    )
    # opportunistic cleanup; the table never needs history beyond a day
    conn.execute(
        "DELETE FROM login_attempts WHERE attempted_at < ?",
        (_ts(_now() - _dt.timedelta(days=1)),),
    )
    conn.commit()


# TOTP ----------------------------------------------------------------------


def begin_totp_enrollment(conn: sqlite3.Connection, user_id: int) -> str:
    """Store a fresh secret (disabled) and return the otpauth:// URI."""
    secret = pyotp.random_base32()
    conn.execute(
        "UPDATE users SET totp_secret = ?, totp_enabled = 0, "
        "totp_last_counter = NULL WHERE id = ?",
        (secret, user_id),
    )
    conn.commit()
    user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return pyotp.TOTP(secret).provisioning_uri(
        name=user["username"], issuer_name="Curio-Cabinet"
    )


def confirm_totp_enrollment(conn: sqlite3.Connection, user_id: int, code: str) -> bool:
    """Enable TOTP only after the user proves the authenticator works."""
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or not user["totp_secret"] or user["totp_enabled"]:
        return False
    if not _totp_check(conn, user, code):
        return False
    conn.execute("UPDATE users SET totp_enabled = 1 WHERE id = ?", (user_id,))
    conn.commit()
    return True


def disable_totp(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET totp_secret = NULL, totp_enabled = 0, "
        "totp_last_counter = NULL WHERE id = ?",
        (user_id,),
    )
    conn.commit()


def verify_totp(conn: sqlite3.Connection, user: sqlite3.Row, code: str) -> bool:
    if not user["totp_enabled"] or not user["totp_secret"]:
        return False
    return _totp_check(conn, user, code)


def _totp_check(conn: sqlite3.Connection, user: sqlite3.Row, code: str) -> bool:
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    totp = pyotp.TOTP(user["totp_secret"])
    now = _now()
    counter = int(now.timestamp()) // 30
    last = user["totp_last_counter"]
    for offset in (0, -1, 1):
        candidate = counter + offset
        if last is not None and candidate <= last:
            continue  # replay guard: each time-step token is single-use
        expected = totp.at(candidate * 30)
        if hmac.compare_digest(expected, code):
            conn.execute(
                "UPDATE users SET totp_last_counter = ? WHERE id = ?",
                (candidate, user["id"]),
            )
            conn.commit()
            return True
    return False


# Sessions -------------------------------------------------------------------


def create_session(
    conn: sqlite3.Connection, user_id: int, *, stage: str = "full"
) -> tuple[str, str]:
    """Mint a fresh session. Returns (raw_token, csrf_token)."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = _now()
    ttl = PRE_TOTP_TTL if stage == "pre_totp" else SESSION_ABSOLUTE
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, stage, "
        "created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_sha256(token), user_id, csrf, stage, _ts(now), _ts(now + ttl), _ts(now)),
    )
    conn.commit()
    return token, csrf


def session_by_token(
    conn: sqlite3.Connection, token: str | None, *, stage: str = "full"
) -> sqlite3.Row | None:
    """Resolve a cookie token to a live session row (with user columns)."""
    if not token:
        return None
    row = conn.execute(
        "SELECT s.token_hash, s.user_id, s.csrf_token, s.stage, s.created_at, "
        "s.expires_at, s.last_seen_at, u.username, u.totp_enabled, u.totp_secret, "
        "u.totp_last_counter, u.id AS uid "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (_sha256(token),),
    ).fetchone()
    if row is None or row["stage"] != stage:
        return None
    now = _now()
    if now >= _parse_ts(row["expires_at"]):
        destroy_session(conn, token)
        return None
    last_seen = _parse_ts(row["last_seen_at"])
    if stage == "full" and now - last_seen > SESSION_IDLE:
        destroy_session(conn, token)
        return None
    # throttle last_seen writes to one per 5 minutes
    if now - last_seen > _dt.timedelta(minutes=5):
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (_ts(now), row["token_hash"]),
        )
        conn.commit()
    return row


def promote_session(conn: sqlite3.Connection, pre_token: str) -> tuple[str, str]:
    """Swap a pre-TOTP session for a full one (fresh token: fixation-proof)."""
    row = session_by_token(conn, pre_token, stage="pre_totp")
    if row is None:
        raise AuthError("login step expired; start over")
    destroy_session(conn, pre_token)
    return create_session(conn, row["user_id"], stage="full")


def destroy_session(conn: sqlite3.Connection, token: str | None) -> None:
    if not token:
        return
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))
    conn.commit()


def destroy_user_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()


def verify_csrf(session_row: sqlite3.Row, submitted: str | None) -> bool:
    if not submitted:
        return False
    return hmac.compare_digest(session_row["csrf_token"], submitted)
