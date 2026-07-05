"""Admin blueprint: login, item CRUD, image management, settings.

Auth is enforced blueprint-wide in ``before_request`` (default-on; the
login routes are the explicit exceptions). All mutations are POST and
CSRF-checked against the server-side session's synchronizer token.
"""

from __future__ import annotations

import io

import qrcode
import qrcode.image.svg
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import auth, images
from ..coerce import coerce_row
from ..csvio import export_csv, next_id
from ..db import utcnow

bp = Blueprint("admin", __name__)

_LOGIN_EXEMPT = {"admin.login", "admin.login_totp"}


def cookie_name() -> str:
    # __Host- requires Secure; fall back for plain-HTTP local dev
    return "__Host-session" if current_app.config["COOKIE_SECURE"] else "cc_session"


def device_cookie_name() -> str:
    return "__Host-device" if current_app.config["COOKIE_SECURE"] else "cc_device"


def _pw_changed_at(username: str) -> str | None:
    row = g.db.execute(
        "SELECT password_changed_at FROM users WHERE username = ?", (username,)
    ).fetchone()
    return row["password_changed_at"] if row else None


def _is_known_device(username: str) -> bool:
    pw_changed = _pw_changed_at(username)
    if pw_changed is None:
        return False
    return auth.verify_device_token(
        current_app.config["SECRET_KEY"],
        username,
        pw_changed,
        request.cookies.get(device_cookie_name()),
    )


def _set_device_cookie(resp: Response, username: str) -> Response:
    pw_changed = _pw_changed_at(username)
    if pw_changed is None:
        return resp
    resp.set_cookie(
        device_cookie_name(),
        auth.issue_device_token(
            current_app.config["SECRET_KEY"], username, pw_changed
        ),
        max_age=int(auth.DEVICE_MAX_AGE.total_seconds()),
        secure=current_app.config["COOKIE_SECURE"],
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


def _set_session_cookie(resp: Response, token: str) -> Response:
    resp.set_cookie(
        cookie_name(),
        token,
        max_age=int(auth.SESSION_ABSOLUTE.total_seconds()),
        secure=current_app.config["COOKIE_SECURE"],
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


def _clear_session_cookie(resp: Response) -> Response:
    resp.delete_cookie(cookie_name(), path="/")
    return resp


def current_session():
    """Resolve the request's full session row (cached per request)."""
    if "session_row" not in g:
        g.session_row = auth.session_by_token(
            g.db, request.cookies.get(cookie_name())
        )
    return g.session_row


@bp.before_request
def _gate() -> Response | None:
    session = current_session()
    if request.method == "POST" and session is not None:
        submitted = request.form.get("csrf_token") or request.headers.get(
            "X-CSRF-Token"
        )
        if not auth.verify_csrf(session, submitted):
            abort(400, "CSRF token missing or invalid")
    if request.endpoint in _LOGIN_EXEMPT:
        return None
    if session is None:
        return redirect(url_for("admin.login", next=request.path))
    return None


# Template globals (registry/admin_user/csrf_token) are injected app-wide
# in app.create_app so public pages can render the shared base template.


# Login / logout -------------------------------------------------------------


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_session():
        return redirect(url_for("admin.dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # known devices (successful past login) bypass the username throttle,
        # so an attacker hammering the admin username can't lock the admin out
        wait = 0 if _is_known_device(username) else auth.login_delay_remaining(
            g.db, username
        )
        if wait:
            error = f"Too many attempts — try again in {wait}s."
        else:
            user = auth.check_password(g.db, username, password)
            auth.record_attempt(
                g.db, username, request.remote_addr, success=user is not None
            )
            if user is None:
                error = "Invalid credentials."
            elif user["totp_enabled"]:
                token, _ = auth.create_session(g.db, user["id"], stage="pre_totp")
                resp = redirect(url_for("admin.login_totp"))
                return _set_session_cookie(resp, token)
            else:
                token, _ = auth.create_session(g.db, user["id"])
                resp = redirect(request.args.get("next") or url_for("admin.dashboard"))
                return _set_device_cookie(
                    _set_session_cookie(_safe_redirect(resp), token), username
                )
    return render_template("admin/login.html", error=error, step="password")


@bp.route("/login/totp", methods=["GET", "POST"])
def login_totp():
    pre = auth.session_by_token(
        g.db, request.cookies.get(cookie_name()), stage="pre_totp"
    )
    if pre is None:
        return redirect(url_for("admin.login"))
    error = None
    if request.method == "POST":
        username = pre["username"]
        wait = 0 if _is_known_device(username) else auth.login_delay_remaining(
            g.db, username
        )
        code = request.form.get("code", "")
        if wait:
            error = f"Too many attempts — try again in {wait}s."
        elif auth.verify_totp(g.db, pre, code):
            auth.record_attempt(g.db, username, request.remote_addr, success=True)
            token, _ = auth.promote_session(
                g.db, request.cookies.get(cookie_name())
            )
            resp = _set_session_cookie(redirect(url_for("admin.dashboard")), token)
            return _set_device_cookie(resp, username)
        else:
            auth.record_attempt(g.db, username, request.remote_addr, success=False)
            error = "Invalid code."
    return render_template("admin/login.html", error=error, step="totp")


def _safe_redirect(resp: Response) -> Response:
    location = resp.headers.get("Location", "")
    if not location.startswith("/") or location.startswith("//"):
        resp.headers["Location"] = url_for("admin.dashboard")
    return resp


@bp.post("/logout")
def logout():
    auth.destroy_session(g.db, request.cookies.get(cookie_name()))
    return _clear_session_cookie(redirect(url_for("public.index")))


# Dashboard -------------------------------------------------------------------


@bp.get("/")
def dashboard():
    table = g.registry.table
    (count,) = g.db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    recent = g.db.execute(
        f'SELECT * FROM "{table}" ORDER BY "updated_at" DESC LIMIT 8'
    ).fetchall()
    return render_template("admin/dashboard.html", count=count, recent=recent)


# Item CRUD --------------------------------------------------------------------


def _form_raw() -> dict:
    """Collect raw form values for every registered field."""
    raw = {}
    for f in g.registry.fields:
        raw[f.key] = request.form.get(f.key, "")
    return raw


def _item_or_404(item_id: str):
    row = g.db.execute(
        f'SELECT * FROM "{g.registry.table}" WHERE "id" = ?', (item_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/items/new", methods=["GET", "POST"])
def item_new():
    errors: dict[str, str] = {}
    raw: dict = {}
    if request.method == "POST":
        raw = _form_raw()
        values, errors = coerce_row(g.registry.fields, raw)
        if not errors:
            item_id = next_id(g.db, g.registry)
            cols = ["id", *values.keys(), "created_at", "updated_at"]
            quoted = ", ".join(f'"{c}"' for c in cols)
            marks = ", ".join("?" for _ in cols)
            now = utcnow()
            g.db.execute(
                f'INSERT INTO "{g.registry.table}" ({quoted}) VALUES ({marks})',
                [item_id, *values.values(), now, now],
            )
            g.db.commit()
            flash(f"Added {item_id}")
            return redirect(url_for("admin.item_edit", item_id=item_id))
    return render_template(
        "admin/form.html", item=None, raw=raw, errors=errors, gallery=[]
    )


@bp.route("/items/<item_id>/edit", methods=["GET", "POST"])
def item_edit(item_id: str):
    item = _item_or_404(item_id)
    errors: dict[str, str] = {}
    raw = dict(item)
    if request.method == "POST":
        raw = _form_raw()
        values, errors = coerce_row(g.registry.fields, raw)
        if not errors:
            sets = ", ".join(f'{g.registry.quoted(k)} = ?' for k in values)
            g.db.execute(
                f'UPDATE "{g.registry.table}" SET {sets}, "updated_at" = ? '
                f'WHERE "id" = ?',
                [*values.values(), utcnow(), item_id],
            )
            g.db.commit()
            flash("Saved")
            return redirect(url_for("admin.item_edit", item_id=item_id))
    gallery = images.images_for_item(g.db, item_id)
    return render_template(
        "admin/form.html", item=item, raw=raw, errors=errors, gallery=gallery
    )


@bp.post("/items/<item_id>/delete")
def item_delete(item_id: str):
    item = _item_or_404(item_id)
    for row in images.images_for_item(g.db, item_id):
        images.remove_image(g.db, g.inst.images_dir, row["id"])
    g.db.execute(f'DELETE FROM "{g.registry.table}" WHERE "id" = ?', (item_id,))
    g.db.commit()
    flash(f"Deleted {item_id}")
    return redirect(url_for("admin.dashboard"))


# Images -------------------------------------------------------------------------


MAX_UPLOAD_FILES = 12  # bound in-request Pillow work so one POST can't wedge the worker


@bp.post("/items/<item_id>/images")
def item_images_upload(item_id: str):
    _item_or_404(item_id)
    files = request.files.getlist("images")
    if len(files) > MAX_UPLOAD_FILES:
        flash(f"Upload up to {MAX_UPLOAD_FILES} images at a time.", "error")
        files = files[:MAX_UPLOAD_FILES]
    stored_any = False
    for file in files:
        data = file.read()
        if not data:
            continue
        try:
            stored = images.process_upload(data, g.inst.images_dir)
        except images.UploadError as exc:
            flash(f"{file.filename}: {exc}", "error")
            continue
        images.add_image(g.db, item_id, stored)
        stored_any = True
    if stored_any:
        flash("Image(s) uploaded")
    return redirect(url_for("admin.item_edit", item_id=item_id))


@bp.post("/images/<int:image_id>/primary")
def image_primary(image_id: int):
    row = g.db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        abort(404)
    images.set_position(g.db, row["item_id"], image_id, 0)
    return redirect(url_for("admin.item_edit", item_id=row["item_id"]))


@bp.post("/images/<int:image_id>/move")
def image_move(image_id: int):
    row = g.db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        abort(404)
    delta = 1 if request.form.get("dir") == "down" else -1
    images.set_position(g.db, row["item_id"], image_id, row["position"] + delta)
    return redirect(url_for("admin.item_edit", item_id=row["item_id"]))


@bp.post("/images/<int:image_id>/delete")
def image_delete(image_id: int):
    row = g.db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        abort(404)
    images.remove_image(g.db, g.inst.images_dir, image_id)
    return redirect(url_for("admin.item_edit", item_id=row["item_id"]))


@bp.post("/images/<int:image_id>/focal")
def image_focal(image_id: int):
    row = g.db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        abort(404)
    try:
        fx = float(request.form.get("x", ""))
        fy = float(request.form.get("y", ""))
    except ValueError:
        abort(400)
    if not images.set_focal_point(g.db, g.inst.images_dir, image_id, fx, fy):
        flash("This photo is shared by other items; its crop wasn't changed.", "error")
    return redirect(url_for("admin.item_edit", item_id=row["item_id"]))


# Settings ---------------------------------------------------------------------


@bp.route("/settings", methods=["GET"])
def settings():
    session = current_session()
    user = g.db.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()
    return render_template(
        "admin/settings.html", user=user, totp_uri=None, totp_svg=None
    )


@bp.post("/settings/password")
def settings_password():
    session = current_session()
    try:
        auth.change_password(
            g.db,
            session["user_id"],
            request.form.get("current", ""),
            request.form.get("new", ""),
        )
    except auth.AuthError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.settings"))
    token, _ = auth.create_session(g.db, session["user_id"])
    flash("Password changed; other sessions logged out")
    return _set_session_cookie(redirect(url_for("admin.settings")), token)


@bp.post("/settings/totp/begin")
def totp_begin():
    session = current_session()
    uri = auth.begin_totp_enrollment(g.db, session["user_id"])
    svg = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    svg.save(buffer)
    user = g.db.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()
    return render_template(
        "admin/settings.html",
        user=user,
        totp_uri=uri,
        totp_svg=buffer.getvalue().decode(),
    )


@bp.post("/settings/totp/confirm")
def totp_confirm():
    session = current_session()
    if auth.confirm_totp_enrollment(
        g.db, session["user_id"], request.form.get("code", "")
    ):
        flash("Two-factor authentication enabled")
    else:
        flash("Code didn't match — scan the QR again and retry", "error")
    return redirect(url_for("admin.settings"))


@bp.post("/settings/totp/disable")
def totp_disable():
    session = current_session()
    auth.disable_totp(g.db, session["user_id"])
    flash("Two-factor authentication disabled")
    return redirect(url_for("admin.settings"))


@bp.get("/export.csv")
def export():
    text = export_csv(g.db, g.registry)
    return Response(
        text,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={g.registry.table}.csv"
        },
    )
