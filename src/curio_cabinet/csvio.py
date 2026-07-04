"""CSV import/export mapped through the registry and the one coercion path."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field

from .coerce import coerce_row
from .db import utcnow
from .registry import FieldRegistry

__all__ = ["ImportReport", "import_csv", "export_csv", "next_id"]


@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _header_map(registry: FieldRegistry, header: list[str]) -> dict[int, str]:
    """Map CSV columns to field keys. Accepts keys or labels (case-insensitive)."""
    lookup: dict[str, str] = {}
    for f in registry.fields:
        lookup[f.key.lower()] = f.key
        lookup[f.label.lower()] = f.key
    lookup["id"] = "id"
    mapping: dict[int, str] = {}
    for i, name in enumerate(header):
        key = lookup.get(name.strip().lower())
        if key:
            mapping[i] = key
    return mapping


def next_id(conn: sqlite3.Connection, registry: FieldRegistry) -> str:
    width = registry.collection.id.width
    row = conn.execute(
        f'SELECT MAX(CAST("id" AS INTEGER)) FROM "{registry.table}"'
    ).fetchone()
    current = row[0] or 0
    return str(int(current) + 1).zfill(width)


def import_csv(
    conn: sqlite3.Connection,
    registry: FieldRegistry,
    text: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    report = ImportReport()
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        report.errors.append("empty CSV")
        return report
    mapping = _header_map(registry, header)
    if not any(v != "id" for v in mapping.values()):
        report.errors.append("no recognizable columns in header")
        return report

    for line_no, row in enumerate(reader, start=2):
        raw = {mapping[i]: cell for i, cell in enumerate(row) if i in mapping}
        explicit_id = raw.pop("id", "").strip()
        values, errors = coerce_row(registry.fields, raw)
        if errors:
            report.skipped += 1
            for key, reason in errors.items():
                report.errors.append(f"line {line_no}, {key}: {reason}")
            continue
        if not dry_run:
            item_id = explicit_id or next_id(conn, registry)
            cols = ["id", *values.keys(), "created_at", "updated_at"]
            quoted = ", ".join(f'"{c}"' for c in cols)
            marks = ", ".join("?" for _ in cols)
            now = utcnow()
            try:
                conn.execute(
                    f'INSERT INTO "{registry.table}" ({quoted}) VALUES ({marks})',
                    [item_id, *values.values(), now, now],
                )
            except sqlite3.IntegrityError as exc:
                report.skipped += 1
                report.errors.append(f"line {line_no}: {exc}")
                continue
        report.imported += 1
    if not dry_run:
        conn.commit()
    return report


def export_csv(conn: sqlite3.Connection, registry: FieldRegistry) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    keys = ["id", *registry.column_list]
    writer.writerow(keys)
    quoted = ", ".join(f't."{k}"' for k in keys)
    for row in conn.execute(f'SELECT {quoted} FROM "{registry.table}" AS t ORDER BY t."id"'):
        values = []
        for key, value in zip(keys, row):
            f = registry.by_key.get(key)
            if f and f.type.value == "tags" and value:
                try:
                    value = ", ".join(json.loads(value))
                except (json.JSONDecodeError, TypeError):
                    pass
            values.append(value)
        writer.writerow(values)
    return out.getvalue()
