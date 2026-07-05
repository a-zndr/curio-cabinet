"""Public views: browse (cards/table/pivot), item detail, share lists, images.

The browse UI uses one URL per state: filter/sort/view params live in the
query string, and htmx requests to the SAME route return just the results
fragment (content-negotiated on the HX-Request header). So hx-push-url
records the real, shareable URL — refresh and back-button work.
"""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from urllib.parse import urlencode

from flask import (
    Blueprint,
    abort,
    g,
    render_template,
    request,
    send_file,
    url_for,
)

from .. import images
from ..config import FieldType
from ..coerce import display_value
from ..query import (
    build_select,
    count_items,
    filter_options,
    histogram,
    parse_params,
    pivot,
)
from ..units import convert

bp = Blueprint("public", __name__)

MAX_SHARE_IDS = 100
MAX_TITLE_LEN = 30
VALID_VIEWS = ("cards", "table", "pivot")


def _clean_title(raw: str) -> str:
    """Sanitize the user-supplied share-list title.

    HTML-injection safety already comes from Jinja autoescaping (the title
    renders only in autoescaped text contexts, never in OG tags or a JS
    context). This is input hygiene / defense-in-depth: drop control and
    format characters — including bidi overrides (U+202E) and zero-width
    joiners used for display spoofing — collapse runs of whitespace, and
    cap the length.
    """
    # Keep whitespace (so word boundaries survive) but drop control/format
    # chars — tabs/newlines are whitespace and get normalized by the split
    # below; bidi overrides and zero-width joiners are format chars and go.
    stripped = "".join(
        ch for ch in (raw or "")
        if ch.isspace() or unicodedata.category(ch)[0] != "C"
    )
    return " ".join(stripped.split())[:MAX_TITLE_LEN]


def _view_mode() -> str:
    view = request.args.get("view", "cards")
    return view if view in VALID_VIEWS else "cards"


def _primary_images(rows) -> dict[str, str]:
    """content_hash of each item's primary image, keyed by item id."""
    ids = [r["id"] for r in rows]
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    hits = g.db.execute(
        "SELECT item_id, content_hash FROM images "
        f"WHERE item_id IN ({marks}) AND position = 0",
        ids,
    ).fetchall()
    return {r["item_id"]: r["content_hash"] for r in hits}


def _url_dropping(drop_pairs=(), drop_keys=()) -> str:
    """Current query string with specific (key,value) pairs or whole keys
    removed (and paging reset). Used for one-click chip removal."""
    drop_keys = set(drop_keys) | {"page"}
    out: list[tuple[str, str]] = []
    for key in request.args:
        for value in request.args.getlist(key):
            if key in drop_keys or (key, value) in drop_pairs:
                continue
            out.append((key, value))
    qs = urlencode(out)
    return "?" + qs if qs else request.path


def _active_filters(registry, args) -> list[dict]:
    """Human-readable chips for every active filter, each with a remove URL."""
    chips: list[dict] = []
    if args.get("q"):
        chips.append({
            "label": "Search", "value": args["q"],
            "url": _url_dropping(drop_keys=["q"]),
        })
    for f in registry.multi_filter_fields:
        for v in args.getlist(f"f.{f.key}"):
            chips.append({
                "label": f.label, "value": v,
                "url": _url_dropping(drop_pairs=[(f"f.{f.key}", v)]),
            })
    for f in registry.range_filter_fields:
        lo, hi = args.get(f"min_{f.key}"), args.get(f"max_{f.key}")
        if not lo and not hi:
            continue
        unit = ""
        if f.unit and f.unit.display:
            unit = f" {f.unit.display[0]}"
        elif f.unit and f.unit.label:
            unit = f" {f.unit.label}"
        if lo and hi:
            text = f"{lo}–{hi}{unit}"
        elif lo:
            text = f"≥ {lo}{unit}"
        else:
            text = f"≤ {hi}{unit}"
        chips.append({
            "label": f.label, "value": text,
            "url": _url_dropping(drop_keys=[f"min_{f.key}", f"max_{f.key}"]),
        })
    return chips


def _parse_measure(measure: str, registry) -> tuple[str | None, str]:
    """Composite 'field:op' -> (field, op); anything else -> count. Validates
    against the field's declared pivot ops (this is the fix for the old bug
    where the op arrived without a field)."""
    if not measure or measure == "count" or ":" not in measure:
        return None, "count"
    key, _, op = measure.partition(":")
    field = registry.by_key.get(key)
    if field is None or op not in field.pivot_ops or op == "group":
        return None, "count"
    return key, op


def _group_label(field, grp: str) -> str:
    if field is not None and field.type is FieldType.boolean:
        return {"1": "Yes", "0": "No"}.get(str(grp), "(unknown)")
    return "(none)" if grp == "—" else grp


def _to_display(field, value: float) -> float:
    unit = field.unit if field else None
    if unit and unit.dimension and unit.store and unit.display:
        return round(convert(value, unit.store, unit.display[0], unit.dimension), 1)
    return round(value, 2)


def _pivot_context(registry, params) -> dict:
    agg_fields = registry.pivot_agg_fields
    group_fields = registry.pivot_group_fields
    mode = request.args.get("mode", "breakdown")
    if mode != "distribution" or not agg_fields:
        mode = "breakdown"
    ctx: dict = {
        "mode": mode,
        "group_fields": group_fields,
        "agg_fields": agg_fields,
    }

    if mode == "distribution":
        keys = {f.key for f in agg_fields}
        dist_key = request.args.get("dist")
        if dist_key not in keys:
            dist_key = agg_fields[0].key
        hist = histogram(g.db, registry, params, dist_key)
        bars, unit_label, stats = [], "", None
        if hist and hist.get("bins"):
            f = hist["field"]
            hmax = hist["max"] or 1
            for i, c in enumerate(hist["bins"]):
                bars.append({
                    "count": c,
                    "bar": round(c / hmax * 100, 1),
                    "lo": _to_display(f, hist["edges"][i]),
                    "hi": _to_display(f, hist["edges"][i + 1]),
                })
            u = f.unit
            unit_label = (u.display[0] if u and u.display else (u.label if u and u.label else ""))
            stats = {"n": hist["n"], "lo": _to_display(f, hist["lo"]), "hi": _to_display(f, hist["hi"])}
        ctx.update(dist_key=dist_key, hist_bars=bars, hist_unit=unit_label,
                   hist_stats=stats, hist_n=(hist["n"] if hist else 0))
        return ctx

    # breakdown
    gkeys = {f.key for f in group_fields}
    group_key = request.args.get("group")
    if group_key not in gkeys:
        group_key = group_fields[0].key if group_fields else None
    measure = request.args.get("measure", "count")
    agg_key, agg_op = _parse_measure(measure, registry)
    sort = "label" if request.args.get("sort") == "label" else "value"

    rows = []
    if group_key:
        try:
            rows = pivot(g.db, registry, params, group_key=group_key,
                         agg_op=agg_op, agg_key=agg_key)
        except ValueError:
            agg_key, agg_op, measure = None, "count", "count"
            rows = pivot(g.db, registry, params, group_key=group_key, agg_op="count")

    gfield = registry.by_key.get(group_key)
    agg_field = registry.by_key.get(agg_key) if agg_key else None
    disp = []
    for r in rows:
        bar_val = (r["val"] if agg_op != "count" else r["n"]) or 0
        value_text = None
        if agg_op != "count" and r["val"] is not None:
            value_text = display_value(agg_field, r["val"]) if agg_field else str(r["val"])
        disp.append({
            "label": _group_label(gfield, r["grp"]),
            "count": r["n"], "bar_val": bar_val, "value_text": value_text,
        })
    disp.sort(key=(lambda d: str(d["label"]).lower()) if sort == "label"
              else (lambda d: d["bar_val"]), reverse=(sort != "label"))
    bar_max = max((d["bar_val"] for d in disp), default=0) or 1
    for d in disp:
        d["bar"] = round(d["bar_val"] / bar_max * 100, 1)

    ctx.update(pivot_rows=disp, group_key=group_key, measure=measure,
               agg_op=agg_op, sort=sort)
    return ctx


def _browse_context():
    registry = g.registry
    params = parse_params(registry, request.args)
    view = _view_mode()

    # Specialty tables: a preset scopes the rows (its filter) and picks the
    # columns. Only meaningful in table view.
    preset = registry.preset(request.args.get("preset")) if view == "table" else None
    if preset is not None:
        field = preset.filter.field
        merged = dict(params.multi)
        existing = merged.get(field, ())
        merged[field] = tuple(existing) + tuple(
            v for v in preset.filter_values() if v not in existing
        )
        params = replace(params, multi=merged)

    total = count_items(g.db, registry, params)

    title_field = registry.collection.title_field
    ctx = {
        "view": view,
        "params": params,
        "total": total,
        "options": filter_options(g.db, registry),
        "args": request.args,
        "query_string": request.query_string.decode(),
        "active_preset": preset.key if preset else None,
        "chips": _active_filters(registry, request.args),
        # fields the user may show/hide in table columns or on cards
        "pickable_fields": [
            f for f in registry.fields
            if f.in_detail and not f.private and f.key != title_field
        ],
    }

    if view == "pivot":
        ctx.update(_pivot_context(registry, params))
    else:
        sql, binds = build_select(registry, params)
        rows = g.db.execute(sql, binds).fetchall()
        ctx.update(rows=rows, thumbs=_primary_images(rows))
        if view == "table":
            # column precedence: explicit ?col= > preset columns > defaults.
            # The title field is always shown as the sticky "Item" column, so
            # exclude it here to avoid a duplicate column.
            requested = [
                c for c in request.args.getlist("col")
                if c in registry.by_key and not registry.by_key[c].private
            ]
            if requested:
                keys = requested
            elif preset is not None:
                keys = [
                    c for c in preset.columns
                    if not registry.by_key[c].private
                ]
            else:
                keys = list(registry.table_default_keys)
            keys = [k for k in keys if k != title_field]
            ctx["columns"] = [registry.by_key[c] for c in keys]
            ctx["field_param"] = "col"
            ctx["selected_field_keys"] = keys
        elif view == "cards":
            # card meta fields: ?cardf= override > config `card: secondary`
            requested = [
                c for c in request.args.getlist("cardf")
                if c in registry.by_key and not registry.by_key[c].private
            ]
            default = [
                f.key for f in registry.card_fields
                if f.card_slot == "secondary" and f.key != title_field
            ]
            keys = requested or default
            ctx["card_fields"] = [registry.by_key[c] for c in keys]
            ctx["field_param"] = "cardf"
            ctx["selected_field_keys"] = keys
    return ctx


@bp.get("/")
def index():
    ctx = _browse_context()
    template = "_results.html" if request.headers.get("HX-Request") else "browse.html"
    return render_template(template, **ctx)


@bp.get("/item/<item_id>")
def item_detail(item_id: str):
    row = g.db.execute(
        f'SELECT * FROM "{g.registry.table}" WHERE "id" = ?', (item_id,)
    ).fetchone()
    if row is None:
        abort(404)
    gallery = images.images_for_item(g.db, item_id)
    og = _og_for(item_id, gallery)
    return render_template("detail.html", item=dict(row), gallery=gallery, og=og)


@bp.get("/list")
def share_list():
    """Read-only view of a selection encoded entirely in the URL.
    No server state, no anonymous writes."""
    raw = request.args.get("ids", "")
    ids, truncated = _parse_ids(raw)
    rows, missing = [], 0
    if ids:
        marks = ", ".join("?" for _ in ids)
        found = {
            r["id"]: r
            for r in g.db.execute(
                f'SELECT * FROM "{g.registry.table}" WHERE "id" IN ({marks})', ids
            ).fetchall()
        }
        rows = [dict(found[i]) for i in ids if i in found]  # preserve URL order
        missing = len(ids) - len(rows)

    title = _clean_title(request.args.get("title", ""))
    return render_template(
        "list.html",
        rows=rows,
        thumbs=_primary_images(rows),
        missing=missing,
        truncated=truncated,
        list_title=title,
        og=_list_og(rows, title),
    )


def _parse_ids(raw: str) -> tuple[list[str], bool]:
    # Bound the work: stop once we hit the cap (O(n) with a set, not O(n^2)
    # over a growing list) so a huge ?ids= can't burn CPU on the public route.
    seen: list[str] = []
    seen_set: set[str] = set()
    truncated = False
    for token in raw.split(","):
        token = token.strip()
        # match the config's id charset; ignore anything hostile
        if not token or not token.replace("_", "").replace("-", "").isalnum():
            continue
        if token in seen_set:
            continue
        if len(seen) >= MAX_SHARE_IDS:
            truncated = True
            break
        seen.append(token)
        seen_set.add(token)
    return seen, truncated


def _og_for(item_id: str, gallery) -> dict:
    title_field = g.registry.collection.title_field
    row = g.db.execute(
        f'SELECT "{title_field}" AS t FROM "{g.registry.table}" WHERE "id" = ?',
        (item_id,),
    ).fetchone()
    og = {
        "title": f"{row['t']} · {g.registry.collection.title}" if row else
                 g.registry.collection.title,
        "url": url_for("public.item_detail", item_id=item_id, _external=True),
    }
    if gallery:
        og["image"] = url_for(
            "public.image", content_hash=gallery[0]["content_hash"],
            variant="og", _external=True,
        )
    return og


def _list_og(rows, title: str) -> dict:
    coll = g.registry.collection.title
    # never reflect the user-supplied title into OG tags (spoofing vector);
    # OG title is always the generic, server-controlled string
    og_title = f"{len(rows)} items from {coll}"
    og = {"title": og_title, "url": request.url}
    if rows:
        thumbs = _primary_images(rows)
        first = next((r["id"] for r in rows if r["id"] in thumbs), None)
        if first:
            og["image"] = url_for(
                "public.image", content_hash=thumbs[first],
                variant="og", _external=True,
            )
    return og


@bp.get("/theme.css")
def theme_css():
    """Config-driven theme overrides, served as CSS so the strict CSP
    (no inline styles) holds. A full hex `accent` wins over `accent_hue`."""
    from ..colors import contrast_hex

    coll = g.registry.collection
    if coll.accent:
        body = (
            ":root { "
            f"--accent-override: {coll.accent}; "
            f"--accent-contrast-override: {contrast_hex(coll.accent)}; "
            "}\n"
        )
    elif coll.accent_hue is not None:
        body = f":root {{ --accent-hue: {int(coll.accent_hue)}; }}\n"
    else:
        body = ""
    return body, 200, {
        "Content-Type": "text/css",
        "Cache-Control": "public, max-age=3600",
    }


@bp.get("/favicon.svg")
def favicon():
    """Config-driven favicon: the collection's monogram (or title initial)
    on an accent-colored tile. Generated like theme.css so it follows the
    theme; the base.html link carries a version param for cache busting."""
    from markupsafe import escape

    from ..colors import contrast_hex, oklch_to_hex

    coll = g.registry.collection
    if coll.accent:
        bg = coll.accent
    else:
        hue = coll.accent_hue if coll.accent_hue is not None else 45
        bg = oklch_to_hex(0.55, 0.125, hue)
    letter = coll.monogram or next((ch for ch in coll.title if ch.isalnum()), "?")
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="14" fill="{bg}"/>'
        '<text x="32" y="33" text-anchor="middle" dominant-baseline="central" '
        'font-family="system-ui, -apple-system, sans-serif" '
        f'font-size="{40 if len(letter) == 1 else 30}" font-weight="600" '
        f'fill="{contrast_hex(bg)}">{escape(letter)}</text></svg>'
    )
    return body, 200, {
        "Content-Type": "image/svg+xml",
        "Cache-Control": "public, max-age=86400",
    }


@bp.get("/images/<content_hash>/<variant>")
def image(content_hash: str, variant: str):
    """Content-addressed image serving. Validation before any file access:
    hash must be 64 lowercase hex chars, variant from a literal allowlist."""
    path = images.safe_variant_path(g.inst.images_dir, content_hash, variant)
    if path is None:
        abort(404)
    response = send_file(path, mimetype="image/jpeg", conditional=True)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
