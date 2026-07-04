"""Public browse views. Placeholder until the full UI lands (Phase 3)."""

from __future__ import annotations

from flask import Blueprint, g, request
from markupsafe import escape

from ..query import build_select, count_items, parse_params

bp = Blueprint("public", __name__)


@bp.get("/")
def index():
    params = parse_params(g.registry, request.args)
    total = count_items(g.db, g.registry, params)
    sql, binds = build_select(g.registry, params)
    rows = g.db.execute(sql, binds).fetchall()
    title_field = g.registry.collection.title_field
    lines = "".join(
        f"<li>{escape(row['id'])}: {escape(row[title_field])}</li>" for row in rows
    )
    return (
        f"<h1>{escape(g.registry.collection.title)}</h1>"
        f"<p>{total} items</p><ol>{lines}</ol>"
    )
