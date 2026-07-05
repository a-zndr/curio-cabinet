"""Admin blueprint: login, item CRUD, image management, settings.

Auth is enforced blueprint-wide in ``before_request`` (default-on; the
login routes are the explicit exceptions). All mutations are POST and
CSRF-checked against the server-side session's synchronizer token.
"""

from __future__ import annotations

import hashlib
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
from ..csvio import export_csv, import_csv, next_id
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


def _form_extras() -> dict:
    """Autocomplete data for the admin form: existing distinct values for
    `suggest` text fields, and a map of value->linked-value so picking a known
    maker can pre-fill its website."""
    registry = g.registry
    table = registry.table
    suggestions: dict[str, list[str]] = {}
    autofill: dict[str, dict] = {}
    for f in registry.fields:
        if f.suggest:
            col = registry.quoted(f.key)
            rows = g.db.execute(
                f'SELECT DISTINCT {col} AS v FROM "{table}" '
                f"WHERE {col} IS NOT NULL AND {col} != '' ORDER BY v COLLATE NOCASE"
            ).fetchall()
            suggestions[f.key] = [r["v"] for r in rows]
        if f.link and f.link in registry.by_key:
            src, tgt = registry.quoted(f.key), registry.quoted(f.link)
            rows = g.db.execute(
                f'SELECT {src} AS k, {tgt} AS v FROM "{table}" '
                f"WHERE {src} != '' AND {tgt} IS NOT NULL AND {tgt} != '' "
                f"ORDER BY \"updated_at\""
            ).fetchall()
            mapping = {r["k"]: r["v"] for r in rows}  # later rows win (most recent)
            if mapping:
                autofill[f.key] = {"target": f.link, "map": mapping}
    return {"suggestions": suggestions, "autofill": autofill}


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
        "admin/form.html", item=None, raw=raw, errors=errors, gallery=[],
        **_form_extras()
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
        "admin/form.html", item=item, raw=raw, errors=errors, gallery=gallery,
        **_form_extras()
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


# CSV import: upload -> dry-run preview -> hash-verified apply ----------------

_PENDING_IMPORT = "pending-import.csv"
MAX_IMPORT_ROWS = 20_000  # bigger jobs belong on the CLI, not a web request


def _pending_import_path():
    return g.inst.db_path.parent / _PENDING_IMPORT


def _decode_csv_upload(data: bytes) -> tuple[str, str | None]:
    """UTF-8 (BOM tolerated) first, UTF-16 by BOM; fall back to Windows-1252
    — what Excel writes for plain "CSV" on most machines — with a note."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), None
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return (
            data.decode("cp1252", errors="replace"),
            "file was not UTF-8; decoded as Windows-1252 — check accented text",
        )


@bp.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "GET":
        return render_template("admin/import.html", report=None, applied=False)
    file = request.files.get("file")
    if file is None or not file.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("admin.import_page"))
    text, encoding_note = _decode_csv_upload(file.read())
    if text.count("\n") > MAX_IMPORT_ROWS:
        flash(
            f"That file has more than {MAX_IMPORT_ROWS} rows — use "
            "`curio-cabinet import-csv` on the command line instead.",
            "error",
        )
        return redirect(url_for("admin.import_page"))
    report = import_csv(g.db, g.registry, text, dry_run=True)
    if encoding_note:
        report.notes.insert(0, encoding_note)
    # stash and hash the exact same bytes; text-mode IO would translate CRLF
    # on the way back and the digest check would refuse every Excel CSV
    stashed = text.encode("utf-8")
    _pending_import_path().write_bytes(stashed)
    return render_template(
        "admin/import.html",
        report=report,
        applied=False,
        filename=file.filename,
        digest=hashlib.sha256(stashed).hexdigest(),
    )


@bp.post("/import/apply")
def import_apply():
    pending = _pending_import_path()
    if not pending.is_file():
        flash("Nothing to import — upload the CSV again.", "error")
        return redirect(url_for("admin.import_page"))
    stashed = pending.read_bytes()
    if hashlib.sha256(stashed).hexdigest() != request.form.get("digest", ""):
        # a newer upload replaced the previewed file; never apply blind
        flash("The pending file changed since the preview — upload it again.", "error")
        return redirect(url_for("admin.import_page"))
    text = stashed.decode("utf-8")
    report = import_csv(g.db, g.registry, text)
    pending.unlink(missing_ok=True)
    if report.imported:
        msg = f"Imported {report.imported} item{'' if report.imported == 1 else 's'}"
        if report.skipped:
            msg += f" ({report.skipped} skipped)"
        flash(msg)
        return redirect(url_for("admin.dashboard"))
    return render_template(
        "admin/import.html", report=report, applied=True, filename=None
    )


# Customize (live config editor) ------------------------------------------------

_GROUP_TYPES = ("enum", "tags", "boolean", "text")
_NUM_TYPES = ("number", "integer")


def _apply(new_raw: dict, ok_msg: str):
    from .. import configio

    try:
        new_inst = configio.apply_config(g.inst, new_raw)
    except configio.ConfigEditError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.customize"))
    current_app.config["CABINET_INSTANCE"] = new_inst
    flash(ok_msg)
    return redirect(url_for("admin.customize"))


def _raw():
    from .. import configio

    return configio.load_raw(g.inst)


@bp.get("/customize")
def customize():
    from ..colors import oklch_to_hex

    coll = g.registry.collection
    if coll.accent:
        seed = coll.accent
    else:
        seed = oklch_to_hex(0.55, 0.125, coll.accent_hue if coll.accent_hue is not None else 45)
    return render_template("admin/customize.html", accent_seed=seed)


@bp.post("/customize/general")
def customize_general():
    raw = _raw()
    c = raw.setdefault("collection", {})
    title = request.form.get("title", "").strip()
    if title:
        c["title"] = title
    color = request.form.get("accent", "").strip()
    if color:
        from ..colors import normalize_hex

        norm = normalize_hex(color)
        if norm:
            c["accent"] = norm
            c.pop("accent_hue", None)  # a chosen color supersedes the legacy hue
    else:
        c.pop("accent", None)  # cleared → fall back to hue / default
    tf = request.form.get("title_field")
    if tf:
        c["title_field"] = tf
    sf = request.form.get("sort_field")
    if sf:
        c["default_sort"] = {
            "field": sf,
            "order": "desc" if request.form.get("sort_order") == "desc" else "asc",
        }
    return _apply(raw, "Collection settings saved")


def _update_field_views(f: dict, key: str) -> None:
    ftype = f.get("type")
    views = dict(f.get("views") or {})
    views["table"] = f"table__{key}" in request.form
    views["detail"] = f"detail__{key}" in request.form
    card = request.form.get(f"card__{key}")
    if card in ("primary", "secondary", "hidden"):
        views["card"] = card
    filt = request.form.get(f"filter__{key}")
    if filt in ("none", "multi", "range"):
        views["filter"] = filt
    # analytics: toggle the field's participation, preserving existing ops
    pivot = list(views.get("pivot") or [])
    want = f"pivot__{key}" in request.form
    if ftype in _GROUP_TYPES:
        if want and "group" not in pivot:
            pivot.append("group")
        elif not want:
            pivot = [p for p in pivot if p != "group"]
    elif ftype in _NUM_TYPES:
        aggs = [p for p in pivot if p in ("avg", "min", "max", "sum")]
        if want and not aggs:
            pivot.append("avg")
        elif not want:
            pivot = [p for p in pivot if p not in ("avg", "min", "max", "sum")]
    if pivot:
        views["pivot"] = pivot
    else:
        views.pop("pivot", None)
    f["views"] = views
    if ftype in ("text", "longtext", "tags"):
        f["searchable"] = f"search__{key}" in request.form
    if ftype == "text":
        f["suggest"] = f"suggest__{key}" in request.form
    if ftype == "enum":
        vals = [v.strip() for v in request.form.get(f"values__{key}", "").split(",") if v.strip()]
        if vals:
            f["values"] = vals


@bp.post("/customize/fields")
def customize_fields():
    raw = _raw()
    for f in raw.get("fields", []):
        key = f.get("key")
        if not key:
            continue
        lbl = request.form.get(f"label__{key}", "").strip()
        if lbl:
            f["label"] = lbl
        _update_field_views(f, key)
    return _apply(raw, "Field settings saved")


@bp.post("/customize/groups")
def customize_groups():
    raw = _raw()
    for gp in raw.get("groups", []):
        lbl = request.form.get(f"grouplabel__{gp.get('key')}", "").strip()
        if lbl:
            gp["label"] = lbl
    return _apply(raw, "Section names saved")


@bp.post("/customize/field/new")
def customize_add_field():
    raw = _raw()
    key = request.form.get("key", "").strip().lower()
    label = request.form.get("label", "").strip()
    ftype = request.form.get("type", "text")
    if not key or not label:
        flash("A new field needs both a key and a label.", "error")
        return redirect(url_for("admin.customize"))

    newf: dict = {"key": key, "label": label, "type": ftype}
    if ftype in _NUM_TYPES:
        dim = request.form.get("dim", "")
        store = request.form.get("store", "").strip()
        unit_label = request.form.get("unit_label", "").strip()
        if dim and store:
            newf["unit"] = {"dimension": dim, "store": store}
        elif unit_label:
            newf["unit"] = {"label": unit_label}
    if ftype == "enum":
        newf["values"] = [
            v.strip() for v in request.form.get("values", "").split(",") if v.strip()
        ]
    raw.setdefault("fields", []).append(newf)

    groups = raw.setdefault("groups", [])
    target = request.form.get("group", "")
    gp = next((x for x in groups if x.get("key") == target), None)
    if gp is None and groups:
        gp = groups[0]
    if gp is not None:
        gp.setdefault("fields", []).append(key)
    return _apply(raw, f"Added field “{label}”")


@bp.post("/customize/presets/add")
def customize_add_preset():
    raw = _raw()
    key = request.form.get("key", "").strip().lower()
    label = request.form.get("label", "").strip()
    field = request.form.get("filter_field", "")
    values = [v.strip() for v in request.form.get("filter_values", "").split(",") if v.strip()]
    columns = request.form.getlist("columns")
    if not key or not label or not field or not values or not columns:
        flash("A specialty table needs a name, a filter, and at least one column.", "error")
        return redirect(url_for("admin.customize"))
    preset = {
        "key": key, "label": label,
        "filter": {"field": field, "in": values},
        "columns": columns,
    }
    raw.setdefault("presets", []).append(preset)
    return _apply(raw, f"Added specialty table “{label}”")


@bp.post("/customize/presets/<pkey>/delete")
def customize_delete_preset(pkey: str):
    raw = _raw()
    raw["presets"] = [p for p in raw.get("presets", []) if p.get("key") != pkey]
    return _apply(raw, "Specialty table removed")
