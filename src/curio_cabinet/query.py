"""Read queries derived from the registry.

Request parameters select *which registered field* participates; the SQL
identifier always comes from the registry, and every value is a bind
parameter. Unknown parameters are ignored.

All queries alias the items table as ``t`` so column references stay
unambiguous next to json_each() (whose own columns include "value").

Filter parameter conventions (query string):
    f.<key>=<value>      repeatable; multi filters (enum/tags/boolean/text)
    min_<key>, max_<key> range filters, expressed in the field's first
                         display unit (converted to the stored unit here)
    q=<text>             search across searchable fields
    sort=<key>&order=asc|desc
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from .config import FieldSpec, FieldType
from .registry import FieldRegistry
from .units import UnitError, convert, parse_measure

__all__ = [
    "QueryParams",
    "parse_params",
    "build_select",
    "count_items",
    "pivot",
    "filter_options",
]


@dataclass(frozen=True)
class QueryParams:
    multi: dict[str, tuple[str, ...]]
    ranges: dict[str, tuple[float | None, float | None]]  # in STORE units
    search: str | None
    sort: str
    order: str
    page: int
    per_page: int


def _range_bound(field: FieldSpec, raw: str) -> float | None:
    """Parse a min_/max_ bound into STORE units.

    Bare numbers are interpreted in the field's first display unit (what
    the filter widget shows); unit suffixes ("24 in") are honored via the
    same parser as every other write path. Non-finite values (nan/inf
    would bind as NULL and silently match nothing) are dropped.
    """
    import math

    unit = field.unit
    try:
        if unit and unit.dimension and unit.store and unit.display:
            value = parse_measure(
                raw, dimension=unit.dimension, store=unit.display[0]
            )
            value = convert(value, unit.display[0], unit.store, unit.dimension)
        else:
            value = float(raw)
    except (UnitError, ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def parse_params(
    registry: FieldRegistry, args: Mapping[str, Any], *, per_page: int = 200
) -> QueryParams:
    """Digest request args (a Flask MultiDict or plain dict) safely."""
    getlist = getattr(args, "getlist", lambda k: [args[k]] if k in args else [])

    multi: dict[str, tuple[str, ...]] = {}
    for f in registry.multi_filter_fields:
        values = tuple(v for v in getlist(f"f.{f.key}") if v != "")
        if values:
            multi[f.key] = values

    ranges: dict[str, tuple[float | None, float | None]] = {}
    for f in registry.range_filter_fields:
        lo = args.get(f"min_{f.key}", "")
        hi = args.get(f"max_{f.key}", "")
        lo_v = _range_bound(f, lo) if lo != "" else None
        hi_v = _range_bound(f, hi) if hi != "" else None
        if lo_v is not None or hi_v is not None:
            ranges[f.key] = (lo_v, hi_v)

    search = str(args.get("q", "")).strip() or None

    sort = str(args.get("sort", "")) or registry.collection.default_sort.field
    if sort not in registry.sortable_keys:
        sort = registry.collection.default_sort.field
    order = str(args.get("order", "")).lower()
    if order not in ("asc", "desc"):
        order = registry.collection.default_sort.order

    try:
        page = min(max(1, int(args.get("page", 1))), 100_000)
    except (TypeError, ValueError):
        page = 1

    return QueryParams(
        multi=multi, ranges=ranges, search=search,
        sort=sort, order=order, page=page, per_page=per_page,
    )


def _col(registry: FieldRegistry, key: str) -> str:
    return f"t.{registry.quoted(key)}"


def _where(registry: FieldRegistry, params: QueryParams) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    binds: list[Any] = []

    for key, values in params.multi.items():
        f = registry.by_key[key]
        col = _col(registry, key)
        marks = ", ".join("?" for _ in values)
        if f.type is FieldType.tags:
            clauses.append(
                f"EXISTS (SELECT 1 FROM json_each({col}) AS je "
                f"WHERE je.value IN ({marks}))"
            )
            binds.extend(values)
        elif f.type is FieldType.boolean:
            clauses.append(f"{col} IN ({marks})")
            binds.extend(
                1 if str(v).lower() in ("1", "true", "yes") else 0 for v in values
            )
        else:
            clauses.append(f"{col} IN ({marks})")
            binds.extend(values)

    for key, (lo, hi) in params.ranges.items():
        col = _col(registry, key)
        if lo is not None:
            clauses.append(f"{col} >= ?")
            binds.append(lo)
        if hi is not None:
            clauses.append(f"{col} <= ?")
            binds.append(hi)

    if params.search and registry.searchable_keys:
        term = (
            params.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        like = f"%{term}%"
        ors = []
        for key in registry.searchable_keys:
            ors.append(f"{_col(registry, key)} LIKE ? ESCAPE '\\'")
            binds.append(like)
        clauses.append("(" + " OR ".join(ors) + ")")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, binds


def _from(registry: FieldRegistry) -> str:
    return f'FROM "{registry.table}" AS t'


def build_select(
    registry: FieldRegistry, params: QueryParams
) -> tuple[str, list[Any]]:
    where, binds = _where(registry, params)
    order_col = _col(registry, params.sort)
    direction = "DESC" if params.order == "desc" else "ASC"
    offset = (params.page - 1) * params.per_page
    sql = (
        f"SELECT t.* {_from(registry)}{where} "
        f'ORDER BY {order_col} IS NULL, {order_col} {direction}, t."id" ASC '
        f"LIMIT {int(params.per_page)} OFFSET {int(offset)}"
    )
    return sql, binds


def count_items(
    conn: sqlite3.Connection, registry: FieldRegistry, params: QueryParams
) -> int:
    where, binds = _where(registry, params)
    (n,) = conn.execute(f"SELECT COUNT(*) {_from(registry)}{where}", binds).fetchone()
    return int(n)


def pivot(
    conn: sqlite3.Connection,
    registry: FieldRegistry,
    params: QueryParams,
    *,
    group_key: str,
    agg_op: str = "count",
    agg_key: str | None = None,
) -> list[sqlite3.Row]:
    """One GROUP BY query. Tags fields group via json_each (an item with
    three tags appears in three groups — the UI labels this)."""
    group_field = registry.by_key.get(group_key)
    if group_field is None or "group" not in group_field.pivot_ops:
        raise ValueError(f"field {group_key!r} is not pivotable")

    agg_sql = "COUNT(*)"
    if agg_op != "count":
        agg_field = registry.by_key.get(agg_key or "")
        if (
            agg_field is None
            or agg_op not in ("avg", "min", "max", "sum")
            or agg_op not in agg_field.pivot_ops
        ):
            raise ValueError(f"aggregate {agg_op!r} on {agg_key!r} is not allowed")
        agg_sql = f"ROUND({agg_op.upper()}({_col(registry, agg_key)}), 2)"

    where, binds = _where(registry, params)

    if group_field.type is FieldType.tags:
        col = _col(registry, group_key)
        sql = (
            f"SELECT grp_each.value AS grp, {agg_sql} AS val, COUNT(*) AS n "
            f"{_from(registry)}, json_each({col}) AS grp_each"
            f"{where} GROUP BY grp ORDER BY n DESC, grp ASC"
        )
    else:
        col = _col(registry, group_key)
        sql = (
            f"SELECT COALESCE(CAST({col} AS TEXT), '—') AS grp, {agg_sql} AS val, "
            f"COUNT(*) AS n {_from(registry)}"
            f"{where} GROUP BY {col} ORDER BY n DESC, grp ASC"
        )
    return conn.execute(sql, binds).fetchall()


def histogram(
    conn: sqlite3.Connection,
    registry: FieldRegistry,
    params: QueryParams,
    field_key: str,
    *,
    max_bins: int = 12,
) -> dict[str, Any] | None:
    """Distribution of a numeric field over the filtered rows. Returns bin
    counts (respecting active filters), or None if there's too little data to
    chart. Values are bucketed in stored units; the caller converts edge
    labels to display units."""
    import math

    field = registry.by_key.get(field_key)
    if field is None or field.type not in (FieldType.number, FieldType.integer):
        return None

    where, binds = _where(registry, params)
    col = _col(registry, field_key)
    vals = [
        r["v"]
        for r in conn.execute(
            f"SELECT {col} AS v {_from(registry)}{where}", binds
        ).fetchall()
        if r["v"] is not None
    ]
    n = len(vals)
    lo, hi = (min(vals), max(vals)) if vals else (0.0, 0.0)
    if n < 3 or lo == hi:
        return {"field": field, "n": n, "lo": lo, "hi": hi, "bins": None}

    bins = min(max_bins, max(5, math.ceil(math.sqrt(n))))
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        idx = int((v - lo) / width)
        counts[min(idx, bins - 1)] += 1
    edges = [lo + i * width for i in range(bins + 1)]
    return {
        "field": field, "n": n, "lo": lo, "hi": hi,
        "bins": counts, "edges": edges, "width": width, "max": max(counts),
    }


def filter_options(
    conn: sqlite3.Connection, registry: FieldRegistry
) -> dict[str, Any]:
    """Options for the filter panel: distinct values / min-max bounds."""
    out: dict[str, Any] = {"multi": {}, "range": {}}
    for f in registry.multi_filter_fields:
        col = _col(registry, f.key)
        if f.type is FieldType.tags:
            rows = conn.execute(
                f"SELECT DISTINCT je.value AS v {_from(registry)}, "
                f"json_each({col}) AS je ORDER BY v"
            ).fetchall()
        elif f.type is FieldType.boolean:
            out["multi"][f.key] = ["Yes", "No"]
            continue
        else:
            rows = conn.execute(
                f"SELECT DISTINCT {col} AS v {_from(registry)} "
                f"WHERE {col} IS NOT NULL AND {col} != '' ORDER BY v"
            ).fetchall()
        out["multi"][f.key] = [r["v"] for r in rows]
    for f in registry.range_filter_fields:
        col = _col(registry, f.key)
        row = conn.execute(
            f"SELECT MIN({col}) AS lo, MAX({col}) AS hi {_from(registry)}"
        ).fetchone()
        lo, hi = row["lo"], row["hi"]
        # bounds are shown/entered in the first display unit, so convert
        unit = f.unit
        if unit and unit.dimension and unit.store and unit.display:
            shown = unit.display[0]
            if shown != unit.store:
                if lo is not None:
                    lo = round(convert(lo, unit.store, shown, unit.dimension), 2)
                if hi is not None:
                    hi = round(convert(hi, unit.store, shown, unit.dimension), 2)
        out["range"][f.key] = (lo, hi)
    return out
