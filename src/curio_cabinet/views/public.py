"""Public views: browse, item detail, share lists, image serving.

The browse UI is minimal until the design pass lands; the routes and
their contracts (URL state, content negotiation, strict image serving)
are final.
"""

from __future__ import annotations

from flask import Blueprint, abort, g, render_template, request, send_file
from markupsafe import escape

from .. import images
from ..query import build_select, count_items, parse_params

bp = Blueprint("public", __name__)

MAX_SHARE_IDS = 100


@bp.get("/")
def index():
    params = parse_params(g.registry, request.args)
    total = count_items(g.db, g.registry, params)
    sql, binds = build_select(g.registry, params)
    rows = g.db.execute(sql, binds).fetchall()
    title_field = g.registry.collection.title_field
    lines = "".join(
        f"<li><a href='/item/{escape(row['id'])}'>{escape(row['id'])}: "
        f"{escape(row[title_field])}</a></li>"
        for row in rows
    )
    return (
        f"<h1>{escape(g.registry.collection.title)}</h1>"
        f"<p>{total} items</p><ol>{lines}</ol>"
    )


@bp.get("/item/<item_id>")
def item_detail(item_id: str):
    row = g.db.execute(
        f'SELECT * FROM "{g.registry.table}" WHERE "id" = ?', (item_id,)
    ).fetchone()
    if row is None:
        abort(404)
    gallery = images.images_for_item(g.db, item_id)
    return render_template("detail.html", item=dict(row), gallery=gallery)


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
