"""CSV import/export mapped through the registry and the one coercion path.

Import semantics are whole-row, not patch: every registered field is
coerced for every row (missing columns count as empty), so required-field
enforcement and defaults apply exactly as they do on admin writes.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field

from .coerce import coerce_row
from .config import FieldType
from .db import utcnow
from .registry import FieldRegistry

__all__ = ["ImportReport", "import_csv", "export_csv", "next_id"]

# Cells starting with these can execute as formulas when the CSV is opened
# in Excel/Sheets; we prefix a ' on export and strip it back on import.
# (OWASP set: = + - @ TAB CR. Leading whitespace is stripped on import, so
# TAB/CR can't survive to a stored value, but we neutralize on export anyway.)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    mapped: list[str] = field(default_factory=list)  # field keys, header order


# Share URLs assume this charset (views/public.py _parse_ids drops anything
# else); explicit ids from a CSV must not smuggle other characters in.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _header_map(
    registry: FieldRegistry, header: list[str], report: ImportReport
) -> dict[int, str] | None:
    """Map CSV columns to field keys. Accepts keys or labels
    (case-insensitive). Duplicate targets are an error, unknown columns a
    note — both surfaced, never silent."""
    lookup: dict[str, str] = {}
    for f in registry.fields:
        lookup[f.key.lower()] = f.key
        lookup[f.label.lower()] = f.key
    lookup["id"] = "id"

    mapping: dict[int, str] = {}
    claimed: dict[str, str] = {}
    unknown: list[str] = []
    for i, name in enumerate(header):
        cleaned = name.strip().lstrip("﻿").strip().lower()
        key = lookup.get(cleaned)
        if key is None:
            if cleaned:
                unknown.append(name.strip())
            continue
        if key in claimed:
            report.errors.append(
                f"header: columns {claimed[key]!r} and {name.strip()!r} both "
                f"map to field {key!r} — remove one"
            )
            return None
        claimed[key] = name.strip()
        mapping[i] = key
    if unknown:
        report.notes.append(f"ignored columns: {', '.join(unknown)}")
    if not any(v != "id" for v in mapping.values()):
        report.errors.append("no recognizable columns in header")
        return None
    return mapping


def next_id(conn: sqlite3.Connection, registry: FieldRegistry) -> str:
    width = registry.collection.id.width
    row = conn.execute(
        f'SELECT MAX(CAST("id" AS INTEGER)) FROM "{registry.table}"'
    ).fetchone()
    current = row[0] or 0
    return str(int(current) + 1).zfill(width)


def _unquote_formula(cell: str) -> str:
    if cell.startswith("'") and cell[1:2] in _FORMULA_PREFIXES:
        return cell[1:]
    return cell


def import_csv(
    conn: sqlite3.Connection,
    registry: FieldRegistry,
    text: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    report = ImportReport()
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        report.errors.append("empty CSV")
        return report
    except csv.Error as exc:
        report.errors.append(f"not a readable CSV: {exc}")
        return report
    mapping = _header_map(registry, header, report)
    if mapping is None:
        return report
    mapped_keys = set(mapping.values())
    report.mapped = [mapping[i] for i in sorted(mapping)]

    # pre-check explicit ids so a dry run reports the same collisions a real
    # import would hit (within the file and against existing rows)
    existing_ids = {
        r[0] for r in conn.execute(f'SELECT "id" FROM "{registry.table}"')
    }
    seen_ids: set[str] = set()

    try:
        for line_no, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue  # blank line
            cells = {mapping[i]: _unquote_formula(cell) for i, cell in enumerate(row) if i in mapping}
            explicit_id = cells.pop("id", "").strip()
            # whole-row semantics: absent columns are empty input, so required
            # checks and defaults run for every field on every row
            raw = {f.key: cells.get(f.key, "") for f in registry.fields}
            values, errors = coerce_row(registry.fields, raw)
            if explicit_id:
                if not _ID_RE.match(explicit_id):
                    errors["id"] = "ids may only use letters, digits, - and _"
                elif explicit_id in existing_ids:
                    errors["id"] = f"id {explicit_id!r} already exists in the collection"
                elif explicit_id in seen_ids:
                    errors["id"] = f"id {explicit_id!r} appears twice in this file"
            if errors:
                report.skipped += 1
                for key, reason in errors.items():
                    report.errors.append(f"line {line_no}, {key}: {reason}")
                continue
            if explicit_id:
                # claimed only by rows that actually import, so a duplicate on a
                # skipped row doesn't block a later valid row (matches real runs)
                seen_ids.add(explicit_id)
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
    except csv.Error as exc:
        # e.g. a cell over the csv field-size limit; treat the whole file as
        # bad rather than half-importing it (or 500ing the caller)
        if not dry_run:
            conn.rollback()
        report.skipped += report.imported
        report.imported = 0
        report.errors.append(
            f"around line {reader.line_num}: not a readable CSV ({exc}) — "
            "nothing was imported"
        )
        return report
    if "id" not in mapped_keys and report.imported:
        report.notes.append("no id column: ids were assigned sequentially")
    if not dry_run:
        conn.commit()
    return report


def _export_cell(registry: FieldRegistry, key: str, value):
    if value is None:
        return value
    f = registry.by_key.get(key)
    if f and f.type is FieldType.tags and isinstance(value, str):
        try:
            tags = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            tags = None
        if isinstance(tags, list):
            # keep the friendly comma form unless a tag embeds a comma,
            # in which case only the JSON form survives a round trip
            if any("," in str(t) for t in tags):
                value = json.dumps(tags, ensure_ascii=False)
            else:
                value = ", ".join(str(t) for t in tags)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        value = "'" + value
    return value


def export_csv(conn: sqlite3.Connection, registry: FieldRegistry) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    keys = ["id", *registry.column_list]
    writer.writerow(keys)
    quoted = ", ".join(f't."{k}"' for k in keys)
    for row in conn.execute(
        f'SELECT {quoted} FROM "{registry.table}" AS t ORDER BY t."id"'
    ):
        writer.writerow(
            [_export_cell(registry, key, value) for key, value in zip(keys, row)]
        )
    return out.getvalue()
