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

# Strict CSP with no 'unsafe-eval': the frontend is htmx + small vanilla
# modules precisely so this policy can hold. Adding a framework that needs
# eval (e.g. standard Alpine.js) would silently break under this header —
# that is intentional friction.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _boot_schema(inst: Instance) -> None:
    """Apply additive config drift automatically; refuse destructive drift.

    Serialized across workers with an exclusive file lock so concurrent
    gunicorn workers can't race the backup/rebuild; each worker re-detects
    drift after acquiring the lock.
    """
    import fcntl

    lock_path = inst.root / "data" / ".boot.lock"
    lock_file = open(lock_path, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    try:
        _boot_schema_locked(inst)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _boot_schema_locked(inst: Instance) -> None:
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

    _register_filters(app)

    @app.get("/healthz")
    def healthz():
        g.db.execute("SELECT 1")
        return {"ok": True}

    from .views.admin import bp as admin_bp, current_session
    from .views.public import bp as public_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    @app.context_processor
    def _template_globals():
        session = current_session()
        return {
            "registry": g.registry,
            "csrf_token": session["csrf_token"] if session else "",
            "admin_user": session["username"] if session else None,
        }

    return app


def _register_filters(app: Flask) -> None:
    import json
    from urllib.parse import urlsplit

    from .units import format_measure

    @app.template_filter("parse_tags")
    def parse_tags(value):
        if not value:
            return []
        if isinstance(value, list):
            return value
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [str(value)]
        except (json.JSONDecodeError, TypeError):
            return [part.strip() for part in str(value).split(",") if part.strip()]

    @app.template_filter("domain_only")
    def domain_only(url):
        try:
            host = urlsplit(url).netloc
            return host.removeprefix("www.") or url
        except ValueError:
            return url

    @app.template_filter("fmt_measure")
    def fmt_measure(field, value, all_units=False):
        unit = field.unit
        if not unit or not unit.store or not unit.dimension:
            suffix = f" {unit.label}" if unit and unit.label else ""
            return f"{value:g}{suffix}"
        shown = unit.display if all_units else unit.display[:1]
        return " · ".join(
            format_measure(
                float(value), store=unit.store, display=d, dimension=unit.dimension
            )
            for d in shown
        )


