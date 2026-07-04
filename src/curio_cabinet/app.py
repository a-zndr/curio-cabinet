"""Flask application factory."""

from __future__ import annotations

import logging
import sqlite3

from flask import Flask, g
from werkzeug.middleware.proxy_fix import ProxyFix

from .db import connect, ensure_engine_tables
from .instance import Instance, resolve_instance
from .schema import backup_database, detect_drift, rebuild

log = logging.getLogger(__name__)

# One conscious tradeoff in this policy: 'unsafe-eval' is required by
# Alpine.js. It is defense-in-depth behind Jinja autoescape (no user data
# is ever rendered with |safe) and HttpOnly session cookies.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _boot_schema(inst: Instance) -> None:
    """Apply additive config drift automatically; refuse destructive drift."""
    conn = connect(inst.db_path, journal_mode=inst.journal_mode)
    try:
        ensure_engine_tables(conn)
        drift = detect_drift(conn, inst.registry)
        if drift.kind == "fresh":
            rebuild(conn, inst.registry)
            log.info("created items table %r", inst.registry.table)
        elif drift.kind == "additive":
            backup = backup_database(inst.db_path, inst.backups_dir)
            log.info("additive schema drift; backup at %s", backup)
            rebuild(conn, inst.registry)
            log.info("applied: %s", drift.describe())
        elif drift.kind == "destructive":
            raise SystemExit(
                f"refusing to start: destructive schema drift ({drift.describe()}). "
                "Review and run `curio-cabinet migrate` explicitly."
            )
    finally:
        conn.close()


def create_app(instance_root: str | None = None) -> Flask:
    inst = resolve_instance(instance_root)

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # type: ignore[method-assign]

    secret = inst.setting("SECRET_KEY")
    if not secret:
        raise SystemExit("SECRET_KEY is not set; run `curio-cabinet init` "
                         "or add it to the instance .env")
    app.config.update(
        SECRET_KEY=secret,
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        CABINET_INSTANCE=inst,
        COOKIE_SECURE=inst.setting("CABINET_COOKIE_SECURE", "1") != "0",
    )

    _boot_schema(inst)

    @app.before_request
    def _open_db() -> None:
        g.inst = inst
        g.registry = inst.registry
        g.db = connect(inst.db_path, journal_mode=inst.journal_mode)

    @app.teardown_request
    def _close_db(exc: BaseException | None) -> None:
        db: sqlite3.Connection | None = g.pop("db", None)
        if db is not None:
            if exc is None:
                db.commit()
            db.close()

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if app.config["COOKIE_SECURE"]:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000"
            )
        return resp

    @app.get("/healthz")
    def healthz():
        g.db.execute("SELECT 1")
        return {"ok": True}

    from .views.public import bp as public_bp

    app.register_blueprint(public_bp)

    try:
        from .views.admin import bp as admin_bp
    except ImportError:
        log.warning("admin views not available yet")
    else:
        app.register_blueprint(admin_bp, url_prefix="/admin")

    return app
