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
from ..coerce import apply_computed, coerce_row
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


# Overview / To-Dos / Cabinet cleanup -----------------------------------------

# a maintenance date is "due soon" this many days before its next-due date
_SOON_DAYS = 14


def _maintenance_schedule(rows) -> list[dict]:
    """Full maintenance schedule grouped by date field (every_days set).

    Each entry has last-done + next-due dates and a status, so every To-Do
    view (list, gantt, calendar, buckets) can render from one dataset.
    """
    import datetime

    reg = g.registry
    title = reg.collection.title_field
    today = datetime.date.today()
    groups = []
    for f in (f for f in reg.fields if f.every_days is not None):
        entries = []
        for row in rows:
            # a cadence can be scoped to matching items only (e.g. condition
            # only the whips): items outside the condition aren't on this schedule
            if f.every_days_when is not None and not f.every_days_when.matches(dict(row)):
                continue
            v = row[f.key]
            last = due = None
            if v:
                try:
                    last = datetime.date.fromisoformat(str(v)[:10])
                    # OverflowError: a near-MAXYEAR date (e.g. a 9999 typo)
                    # pushed past date.max — treat it as unscheduled, don't 500
                    due = last + datetime.timedelta(days=f.every_days)
                except (ValueError, OverflowError):
                    last = due = None
            if due is None:
                status, days_until, rank = "never", None, -10 ** 9
            else:
                days_until = (due - today).days
                if days_until < 0:
                    status = "overdue"
                elif days_until <= _SOON_DAYS:
                    status = "soon"
                else:
                    status = "ok"
                rank = days_until
            entries.append({
                "id": row["id"], "title": row[title],
                "last": last.isoformat() if last else None,
                "due": due.isoformat() if due else None,
                "days_until": days_until, "status": status, "rank": rank,
            })
        entries.sort(key=lambda e: e["rank"])
        groups.append({"field": f, "entries": entries})
    return groups


def _cleanup_groups(rows, photo_ids) -> tuple[list[dict], list[dict]]:
    """Items missing must-have data, grouped for inline batch entry:
    one group per must-have field, plus a photos-missing list."""
    reg = g.registry
    title = reg.collection.title_field
    field_groups = []
    for f in (f for f in reg.fields if f.must_have and f.computed is None):
        items = [
            {"id": r["id"], "title": r[title]} for r in rows
            if r[f.key] is None or (isinstance(r[f.key], str)
                                    and r[f.key].strip() in ("", "[]"))
        ]
        if items:
            field_groups.append({"field": f, "entries": items})
    photo_items = []
    if reg.collection.must_have_photos:
        photo_items = [
            {"id": r["id"], "title": r[title]} for r in rows
            if r["id"] not in photo_ids
        ]
    return field_groups, photo_items


def _all_rows_and_photos():
    reg = g.registry
    rows = g.db.execute(f'SELECT * FROM "{reg.table}" ORDER BY "id"').fetchall()
    photo_ids = {
        r["item_id"] for r in g.db.execute('SELECT DISTINCT "item_id" FROM images')
    }
    return rows, photo_ids


@bp.get("/")
def dashboard():
    """Lightweight overview: item count, what needs attention, and a
    recently-updated section, with shortcuts into each area."""
    reg = g.registry
    rows, photo_ids = _all_rows_and_photos()
    recent = g.db.execute(
        f'SELECT * FROM "{reg.table}" ORDER BY "updated_at" DESC LIMIT 8'
    ).fetchall()
    schedule = _maintenance_schedule(rows)
    due_count = sum(
        1 for grp in schedule for e in grp["entries"]
        if e["status"] in ("overdue", "never")
    )
    cleanup, cleanup_photos = _cleanup_groups(rows, photo_ids)
    cleanup_count = sum(len(grp["entries"]) for grp in cleanup) + len(cleanup_photos)
    return render_template(
        "admin/overview.html",
        count=len(rows),
        recent=recent,
        due_count=due_count,
        cleanup_count=cleanup_count,
        has_maintenance=bool(schedule),
    )


TODO_VIEWS = ("list", "gantt", "calendar", "buckets")


@bp.get("/todos")
def todos():
    import datetime

    view = request.args.get("view", "list")
    if view not in TODO_VIEWS:
        view = "list"
    rows, _ = _all_rows_and_photos()
    schedule = _maintenance_schedule(rows)
    ctx = {
        "view": view,
        "schedule": schedule,
        "today": datetime.date.today().isoformat(),
    }
    if view == "gantt":
        ctx["gantt"] = _gantt_model(schedule)
    elif view == "calendar":
        ctx["calendar"] = _calendar_model(schedule, request.args.get("month"))
    elif view == "buckets":
        ctx["buckets"] = _bucket_model(schedule)
    return render_template("admin/todos.html", **ctx)


@bp.get("/cleanup")
def cleanup():
    rows, photo_ids = _all_rows_and_photos()
    groups, photos = _cleanup_groups(rows, photo_ids)
    return render_template("admin/cleanup.html", cleanup=groups, cleanup_photos=photos)


def _gantt_model(schedule) -> dict:
    """Swimlane-per-field timeline. Computes a shared date axis so every
    lane maps a date to the same x%; entries without a cycle are flagged."""
    import datetime

    today = datetime.date.today()
    dates = [today]
    for grp in schedule:
        for e in grp["entries"]:
            for k in ("last", "due"):
                if e[k]:
                    dates.append(datetime.date.fromisoformat(e[k]))
    lo, hi = min(dates), max(dates)
    pad = max((hi - lo).days // 20, 3)
    try:
        lo = lo - datetime.timedelta(days=pad)
    except OverflowError:
        lo = datetime.date.min
    try:
        hi = hi + datetime.timedelta(days=pad)
    except OverflowError:
        hi = datetime.date.max
    span = max((hi - lo).days, 1)

    def pct(d: str) -> float:
        return round((datetime.date.fromisoformat(d) - lo).days / span * 100, 2)

    lanes = []
    for grp in schedule:
        bars = []
        for e in grp["entries"]:
            if e["last"] and e["due"]:
                x0, x1 = pct(e["last"]), min(pct(e["due"]), 100.0)
                bars.append({**e, "x": x0, "w": max(x1 - x0, 0.6)})
            else:
                bars.append({**e, "x": None, "w": None})  # never done
        lanes.append({"field": grp["field"], "bars": bars})
    return {
        "lanes": lanes,
        "today_pct": pct(today.isoformat()),
        "start": lo.isoformat(), "end": hi.isoformat(),
    }


def _calendar_model(schedule, month_arg) -> dict:
    """Month grid with each entry placed on its next-due day."""
    import calendar as _cal
    import datetime

    today = datetime.date.today()
    try:
        y, m = (int(x) for x in (month_arg or "").split("-"))
        first = datetime.date(y, m, 1)
    except (ValueError, TypeError):
        first = today.replace(day=1)
    # keep clear of MINYEAR/MAXYEAR so the 6-week grid and prev/next month
    # arithmetic can't spill past the representable date range and 500
    if not (2 <= first.year <= 9998):
        first = today.replace(day=1)
    by_day: dict[int, list] = {}
    for grp in schedule:
        for e in grp["entries"]:
            if not e["due"]:
                continue
            d = datetime.date.fromisoformat(e["due"])
            if d.year == first.year and d.month == first.month:
                by_day.setdefault(d.day, []).append({**e, "field": grp["field"].label})
    weeks = _cal.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)
    grid = [[{
        "day": d.day,
        "in_month": d.month == first.month,
        "is_today": d == today,
        "entries": by_day.get(d.day, []) if d.month == first.month else [],
    } for d in week] for week in weeks]
    prev = (first - datetime.timedelta(days=1)).replace(day=1)
    nxt = (first + datetime.timedelta(days=31)).replace(day=1)
    return {
        "grid": grid,
        "label": first.strftime("%B %Y"),
        "prev": prev.strftime("%Y-%m"), "next": nxt.strftime("%Y-%m"),
        "weekdays": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    }


def _bucket_model(schedule) -> list[dict]:
    """Triage columns: Overdue · This week · This month · Later."""
    buckets = {"overdue": [], "week": [], "month": [], "later": []}
    for grp in schedule:
        for e in grp["entries"]:
            item = {**e, "field": grp["field"].label}
            du = e["days_until"]
            if du is None or du < 0:
                buckets["overdue"].append(item)
            elif du <= 7:
                buckets["week"].append(item)
            elif du <= 31:
                buckets["month"].append(item)
            else:
                buckets["later"].append(item)
    return [
        {"key": "overdue", "label": "Overdue", "entries": buckets["overdue"]},
        {"key": "week", "label": "This week", "entries": buckets["week"]},
        {"key": "month", "label": "This month", "entries": buckets["month"]},
        {"key": "later", "label": "Later", "entries": buckets["later"]},
    ]


@bp.post("/maintenance/done")
def maintenance_done():
    """Mark a maintenance date field as done (a chosen date) on one or more
    items at once — the "done + when" action in the To-Dos list view."""
    from ..coerce import CoercionError, coerce_value

    reg = g.registry
    field = reg.by_key.get(request.form.get("field", ""))
    if field is None or field.every_days is None:
        abort(400)
    ids = request.form.getlist("item_ids")
    date_raw = request.form.get("done_date", "").strip()
    back = redirect(url_for("admin.todos"))
    if not ids:
        flash("Select at least one item to mark done.", "error")
        return back
    try:
        value = coerce_value(field, date_raw)
    except CoercionError:
        value = None
    if not value:
        flash("Pick a valid date.", "error")
        return back
    marks = ", ".join("?" for _ in ids)
    g.db.execute(
        f'UPDATE "{reg.table}" SET {reg.quoted(field.key)} = ?, "updated_at" = ? '
        f'WHERE "id" IN ({marks})',
        [value, utcnow(), *ids],
    )
    g.db.commit()
    flash(f"Marked {field.label} done on {len(ids)} item(s).")
    return back


@bp.post("/cleanup/fill")
def cleanup_fill():
    """Fill one missing field across several items from the cleanup page —
    per-item values (not a single bulk value), recomputing derived fields."""
    from ..coerce import CoercionError, apply_computed, coerce_value

    reg = g.registry
    field = reg.by_key.get(request.form.get("field", ""))
    if field is None or field.computed is not None:
        abort(400)
    saved, errors = 0, []
    for form_key, raw_val in request.form.items():
        if not form_key.startswith("val__"):
            continue
        item_id, raw_val = form_key[len("val__"):], raw_val.strip()
        if not raw_val:
            continue
        try:
            value = coerce_value(field, raw_val)
        except CoercionError as exc:
            errors.append(f"{item_id}: {exc.reason}")
            continue
        row = g.db.execute(
            f'SELECT * FROM "{reg.table}" WHERE "id" = ?', (item_id,)
        ).fetchone()
        if row is None:
            continue
        merged = dict(row)
        merged[field.key] = value
        apply_computed(reg.fields, merged)  # keep derived fields consistent
        cols = {field.key: value}
        for cf in reg.fields:
            if cf.computed is not None:
                cols[cf.key] = merged[cf.key]
        sets = ", ".join(f"{reg.quoted(k)} = ?" for k in cols)
        g.db.execute(
            f'UPDATE "{reg.table}" SET {sets}, "updated_at" = ? WHERE "id" = ?',
            [*cols.values(), utcnow(), item_id],
        )
        saved += 1
    g.db.commit()
    if errors:
        flash("; ".join(errors[:5]), "error")
    if saved:
        flash(f"Updated {field.label} on {saved} item(s).")
    return redirect(url_for("admin.cleanup"))


# Item CRUD --------------------------------------------------------------------


def _form_raw() -> dict:
    """Collect raw form values for every registered field."""
    raw = {}
    for f in g.registry.fields:
        if f.computed is None:
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
            apply_computed(g.registry.fields, values)
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
            apply_computed(g.registry.fields, values)
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


def _customize_redirect():
    """Back to the tab the form was submitted from (hidden `tab` field)."""
    tab = request.form.get("tab", "general")
    if tab not in CUSTOMIZE_TABS:
        tab = "general"
    return redirect(url_for("admin.customize", tab=tab))


def _apply(new_raw: dict, ok_msg: str):
    from .. import configio

    try:
        new_inst = configio.apply_config(g.inst, new_raw)
    except configio.ConfigEditError as exc:
        flash(str(exc), "error")
        return _customize_redirect()
    current_app.config["CABINET_INSTANCE"] = new_inst
    flash(ok_msg)
    return _customize_redirect()


def _raw():
    from .. import configio

    return configio.load_raw(g.inst)


CUSTOMIZE_TABS = ("general", "fields", "add", "presets")


@bp.get("/customize")
def customize():
    from ..colors import oklch_to_hex

    tab = request.args.get("tab", "general")
    if tab not in CUSTOMIZE_TABS:
        tab = "general"
    coll = g.registry.collection
    if coll.accent:
        seed = coll.accent
    else:
        seed = oklch_to_hex(0.55, 0.125, coll.accent_hue if coll.accent_hue is not None else 45)
    return render_template("admin/customize.html", accent_seed=seed, tab=tab)


@bp.post("/customize/general")
def customize_general():
    raw = _raw()
    c = raw.setdefault("collection", {})
    title = request.form.get("title", "").strip()
    if title:
        c["title"] = title
    mono = request.form.get("monogram", "").strip()
    if mono:
        c["monogram"] = mono[:2]
    else:
        c.pop("monogram", None)  # cleared → fall back to the title initial
    if request.form.get("must_have_photos"):
        c["must_have_photos"] = True
    else:
        c.pop("must_have_photos", None)
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
    if f"musthave__{key}" in request.form:
        f["must_have"] = True
    else:
        f.pop("must_have", None)
    if ftype == "date":
        days = request.form.get(f"everydays__{key}", "").strip()
        if days.isdecimal() and int(days) > 0:
            f["every_days"] = int(days)
            # optional: scope the cadence to items matching a condition
            cond_field = request.form.get(f"ewfield__{key}", "").strip()
            cond_vals = [
                v.strip() for v in
                request.form.get(f"ewvalues__{key}", "").split(",") if v.strip()
            ]
            if cond_field and cond_vals:
                f["every_days_when"] = {"field": cond_field, "in": cond_vals}
            else:
                f.pop("every_days_when", None)
        else:
            f.pop("every_days", None)
            f.pop("every_days_when", None)
    if ftype in ("text", "longtext", "tags"):
        f["searchable"] = f"search__{key}" in request.form
    if ftype == "text":
        f["suggest"] = f"suggest__{key}" in request.form
    if ftype == "enum":
        vals = [v.strip() for v in request.form.get(f"values__{key}", "").split(",") if v.strip()]
        if vals:
            f["values"] = vals
    # private wins — runs LAST so no later assignment (searchable above)
    # can reintroduce public exposure and bounce the whole save
    if f"private__{key}" in request.form:
        f["private"] = True
        for k in ("table", "card", "filter", "sort", "pivot"):
            views.pop(k, None)
        f["views"] = views
        f.pop("searchable", None)
    else:
        f.pop("private", None)


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
    private_keys = {f["key"] for f in raw.get("fields", []) if f.get("private")}
    if private_keys:
        # private wins everywhere: presets and links may not expose the field
        kept = []
        for p in raw.get("presets", []):
            p["columns"] = [c for c in p.get("columns", []) if c not in private_keys]
            if p["columns"]:
                kept.append(p)
        if raw.get("presets"):
            raw["presets"] = kept
            if not kept:
                raw.pop("presets", None)
        for f in raw.get("fields", []):
            if f.get("link") in private_keys and not f.get("private"):
                f.pop("link", None)
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
        return redirect(url_for("admin.customize", tab="add"))

    newf: dict = {"key": key, "label": label, "type": ftype}
    if request.form.get("private"):
        newf["private"] = True
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
    columns = [
        c for c in request.form.getlist("columns")
        if not (g.registry.by_key.get(c) and g.registry.by_key[c].private)
    ]
    if not key or not label or not field or not values or not columns:
        flash("A specialty table needs a name, a filter, and at least one column.", "error")
        return redirect(url_for("admin.customize", tab="presets"))
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
