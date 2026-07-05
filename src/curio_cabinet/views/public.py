"""Public views: browse (cards/table/pivot), item detail, share lists, images.

The browse UI uses one URL per state: filter/sort/view params live in the
query string, and htmx requests to the SAME route return just the results
fragment (content-negotiated on the HX-Request header). So hx-push-url
records the real, shareable URL — refresh and back-button work.
"""

from __future__ import annotations

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
from ..query import (
    build_select,
    count_items,
    filter_options,
    parse_params,
    pivot,
)

bp = Blueprint("public", __name__)

MAX_SHARE_IDS = 100
VALID_VIEWS = ("cards", "table", "pivot")


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
            f for f in registry.fields if f.in_detail and f.key != title_field
        ],
    }

    if view == "pivot":
        group_key = request.args.get("group") or (
            registry.pivot_group_fields[0].key if registry.pivot_group_fields else None
        )
        agg_key = request.args.get("agg") or ""
        agg_op = request.args.get("op", "count")
        rows, max_n = [], 1
        if group_key:
            try:
                rows = pivot(
                    g.db, registry, params,
                    group_key=group_key, agg_op=agg_op,
                    agg_key=agg_key or None,
                )
            except ValueError:
                rows, agg_op = [], "count"
            max_n = max((r["n"] for r in rows), default=1)
        ctx.update(
            pivot_rows=rows, pivot_max=max_n,
            group_key=group_key, agg_key=agg_key, agg_op=agg_op,
        )
    else:
        sql, binds = build_select(registry, params)
        rows = g.db.execute(sql, binds).fetchall()
        ctx.update(rows=rows, thumbs=_primary_images(rows))
        if view == "table":
            # column precedence: explicit ?col= > preset columns > defaults.
            # The title field is always shown as the sticky "Item" column, so
            # exclude it here to avoid a duplicate column.
            requested = [c for c in request.args.getlist("col") if c in registry.by_key]
            if requested:
                keys = requested
            elif preset is not None:
                keys = list(preset.columns)
            else:
                keys = list(registry.table_default_keys)
            keys = [k for k in keys if k != title_field]
            ctx["columns"] = [registry.by_key[c] for c in keys]
            ctx["field_param"] = "col"
            ctx["selected_field_keys"] = keys
        elif view == "cards":
            # card meta fields: ?cardf= override > config `card: secondary`
            requested = [c for c in request.args.getlist("cardf") if c in registry.by_key]
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

    title = (request.args.get("title", "") or "").strip()[:80]
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
    (no inline styles) holds."""
    hue = g.registry.collection.accent_hue
    body = f":root {{ --accent-hue: {int(hue)}; }}\n" if hue is not None else ""
    return body, 200, {
        "Content-Type": "text/css",
        "Cache-Control": "public, max-age=3600",
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
