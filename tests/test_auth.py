import datetime as dt
import sqlite3

import pyotp
import pytest

from curio_cabinet import auth
from curio_cabinet.db import ensure_engine_tables

PW = "correct horse battery staple"


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_engine_tables(conn)
    return conn


@pytest.fixture
def admin(db) -> sqlite3.Connection:
    auth.create_admin_user(db, "zee", PW)
    return db


def _user(db):
    return db.execute("SELECT * FROM users WHERE username = 'zee'").fetchone()


# Accounts ------------------------------------------------------------------


def test_create_admin_refuses_second_account(admin):
    with pytest.raises(auth.UserExistsError):
        auth.create_admin_user(admin, "mallory", "another long password")


def test_short_password_rejected(db):
    with pytest.raises(auth.AuthError, match="12 characters"):
        auth.create_admin_user(db, "zee", "short")


def test_check_password(admin):
    assert auth.check_password(admin, "zee", PW)["username"] == "zee"
    assert auth.check_password(admin, "zee", "wrong password!!") is None
    assert auth.check_password(admin, "nobody", PW) is None


def test_reset_password_kills_sessions(admin):
    token, _ = auth.create_session(admin, _user(admin)["id"])
    auth.reset_admin_password(admin, "a brand new passphrase")
    assert auth.session_by_token(admin, token) is None
    assert auth.check_password(admin, "zee", "a brand new passphrase")


def test_change_password_requires_current(admin):
    uid = _user(admin)["id"]
    with pytest.raises(auth.AuthError):
        auth.change_password(admin, uid, "wrong current!!", "a brand new passphrase")
    token, _ = auth.create_session(admin, uid)
    auth.change_password(admin, uid, PW, "a brand new passphrase")
    assert auth.session_by_token(admin, token) is None


# Throttling ------------------------------------------------------------------


def test_progressive_delay(admin):
    assert auth.login_delay_remaining(admin, "zee") == 0
    for _ in range(2):
        auth.record_attempt(admin, "zee", None, success=False)
    assert 0 < auth.login_delay_remaining(admin, "zee") <= 2
    for _ in range(3):
        auth.record_attempt(admin, "zee", None, success=False)
    assert auth.login_delay_remaining(admin, "zee") > 8


def test_success_clears_delay(admin):
    for _ in range(4):
        auth.record_attempt(admin, "zee", None, success=False)
    auth.record_attempt(admin, "zee", None, success=True)
    assert auth.login_delay_remaining(admin, "zee") == 0


def test_delay_decays_with_time(admin):
    for _ in range(3):
        auth.record_attempt(admin, "zee", None, success=False)
    past = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    admin.execute("UPDATE login_attempts SET attempted_at = ?", (past,))
    assert auth.login_delay_remaining(admin, "zee") == 0


# TOTP -------------------------------------------------------------------------


def _enroll(db) -> pyotp.TOTP:
    uid = _user(db)["id"]
    uri = auth.begin_totp_enrollment(db, uid)
    secret = _user(db)["totp_secret"]
    assert secret in uri
    totp = pyotp.TOTP(secret)
    assert auth.confirm_totp_enrollment(db, uid, totp.now())
    return totp


def test_totp_enrollment_and_verify(admin):
    totp = _enroll(admin)
    assert _user(admin)["totp_enabled"] == 1
    # the enrollment confirmation consumed the current code; move to next step
    future = totp.at(int(dt.datetime.now().timestamp()) + 30)
    assert auth.verify_totp(admin, _user(admin), future)


def test_verify_totp_with_session_row(admin):
    # regression: login_totp passes the session-join row (which has no `id`
    # key — it's aliased) to verify_totp; resolving the user id must not crash.
    totp = _enroll(admin)
    uid = _user(admin)["id"]
    token, _ = auth.create_session(admin, uid, stage="pre_totp")
    row = auth.session_by_token(admin, token, stage="pre_totp")
    assert "id" not in row.keys()  # session row has user_id/uid, not id
    code = totp.at(int(dt.datetime.now().timestamp()) + 30)
    assert auth.verify_totp(admin, row, code)


def test_totp_replay_rejected(admin):
    totp = _enroll(admin)
    code = totp.at(int(dt.datetime.now().timestamp()) + 30)
    assert auth.verify_totp(admin, _user(admin), code)
    assert not auth.verify_totp(admin, _user(admin), code)  # same step reused


def test_totp_garbage_rejected(admin):
    _enroll(admin)
    assert not auth.verify_totp(admin, _user(admin), "abc123")
    assert not auth.verify_totp(admin, _user(admin), "000000")


def test_totp_disable(admin):
    _enroll(admin)
    auth.disable_totp(admin, _user(admin)["id"])
    user = _user(admin)
    assert user["totp_enabled"] == 0 and user["totp_secret"] is None


# Sessions -----------------------------------------------------------------------


def test_session_roundtrip(admin):
    uid = _user(admin)["id"]
    token, csrf = auth.create_session(admin, uid)
    row = auth.session_by_token(admin, token)
    assert row["username"] == "zee"
    assert auth.verify_csrf(row, csrf)
    assert not auth.verify_csrf(row, "forged")
    assert not auth.verify_csrf(row, None)


def test_bogus_token_rejected(admin):
    assert auth.session_by_token(admin, None) is None
    assert auth.session_by_token(admin, "not-a-real-token") is None


def test_raw_token_never_stored(admin):
    token, _ = auth.create_session(admin, _user(admin)["id"])
    stored = admin.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert token not in stored and stored != token


def test_expired_session_rejected(admin):
    token, _ = auth.create_session(admin, _user(admin)["id"])
    admin.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00Z'")
    assert auth.session_by_token(admin, token) is None
    (n,) = admin.execute("SELECT COUNT(*) FROM sessions").fetchone()
    assert n == 0  # expired row cleaned up


def test_idle_session_rejected(admin):
    token, _ = auth.create_session(admin, _user(admin)["id"])
    stale = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    admin.execute("UPDATE sessions SET last_seen_at = ?", (stale,))
    assert auth.session_by_token(admin, token) is None


def test_pre_totp_stage_isolated(admin):
    uid = _user(admin)["id"]
    pre_token, _ = auth.create_session(admin, uid, stage="pre_totp")
    # a pre-TOTP session is NOT a valid full session
    assert auth.session_by_token(admin, pre_token) is None
    full_token, _ = auth.promote_session(admin, pre_token)
    assert auth.session_by_token(admin, full_token)["stage"] == "full"
    # promotion consumed the pre-token and minted a different token
    assert full_token != pre_token
    assert auth.session_by_token(admin, pre_token, stage="pre_totp") is None


def test_promote_expired_pre_session_fails(admin):
    uid = _user(admin)["id"]
    pre_token, _ = auth.create_session(admin, uid, stage="pre_totp")
    admin.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00Z'")
    with pytest.raises(auth.AuthError):
        auth.promote_session(admin, pre_token)
