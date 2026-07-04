"""Schema lifecycle: DDL from config, drift detection, rebuild migration.

There is exactly one migration mechanism: rebuild. Every migration copies
the DB to a verified backup (VACUUM INTO + integrity check), creates a new
items table from the current config, copies rows through the coercion
layer, and swaps. Additive drift is auto-applied at boot so a config push
never takes the site down; anything destructive or ambiguous refuses to
run outside an explicit ``curio-cabinet migrate``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .coerce import CoercionError, coerce_value
from .db import utcnow
from .registry import FieldRegistry

__all__ = [
    "SchemaError",
    "BackupError",
    "Drift",
    "detect_drift",
    "create_items_table",
    "backup_database",
    "rebuild",
]


class SchemaError(RuntimeError):
    pass


class BackupError(RuntimeError):
    pass


@dataclass
class Drift:
    kind: str  # "fresh" | "match" | "additive" | "destructive"
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)  # new_key -> old column
    retyped: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = []
        if self.added:
            parts.append(f"added: {', '.join(self.added)}")
        if self.renamed:
            parts.append(
                "renamed: " + ", ".join(f"{o}->{n}" for n, o in self.renamed.items())
            )
        if self.removed:
            parts.append(f"removed: {', '.join(self.removed)}")
        if self.retyped:
            parts.append(f"type changed: {', '.join(self.retyped)}")
        return f"{self.kind} ({'; '.join(parts)})" if parts else self.kind


def _items_ddl(registry: FieldRegistry, table_name: str) -> str:
    cols = ['"id" TEXT PRIMARY KEY']
    for f in registry.fields:
        cols.append(f'{registry.quoted(f.key)} {f.sql_type}')
    cols.append('"created_at" TEXT NOT NULL')
    cols.append('"updated_at" TEXT NOT NULL')
    return f'CREATE TABLE "{table_name}" (\n  ' + ",\n  ".join(cols) + "\n)"


def create_items_table(conn: sqlite3.Connection, registry: FieldRegistry) -> None:
    conn.execute(_items_ddl(registry, registry.table))
    _record_applied(conn, registry)
    conn.commit()


def _record_applied(conn: sqlite3.Connection, registry: FieldRegistry) -> None:
    snapshot = json.dumps(
        {f.key: f.sql_type for f in registry.fields}, separators=(",", ":")
    )
    conn.execute(
        "INSERT INTO _meta(key, value) VALUES('config_sha', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (registry.config.sha(),),
    )
    conn.execute(
        "INSERT INTO _meta(key, value) VALUES('applied_fields', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (snapshot,),
    )


def _existing_columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name, type FROM pragma_table_info(?)", (table,)
    ).fetchall()
    return {r["name"]: (r["type"] or "").upper() for r in rows}


def detect_drift(conn: sqlite3.Connection, registry: FieldRegistry) -> Drift:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (registry.table,),
    ).fetchone()
    if not table_exists:
        return Drift(kind="fresh")

    existing = _existing_columns(conn, registry.table)
    engine_cols = {"id", "created_at", "updated_at"}
    data_cols = {k: v for k, v in existing.items() if k not in engine_cols}

    drift = Drift(kind="match")
    config_keys = {f.key for f in registry.fields}

    for f in registry.fields:
        if f.key in data_cols:
            if data_cols[f.key] != f.sql_type:
                drift.retyped.append(f.key)
        elif f.rename_from and f.rename_from in data_cols and f.rename_from not in config_keys:
            drift.renamed[f.key] = f.rename_from
        else:
            drift.added.append(f.key)

    claimed_old = set(drift.renamed.values())
    for col in data_cols:
        if col not in config_keys and col not in claimed_old:
            drift.removed.append(col)

    if drift.removed or drift.retyped or drift.renamed:
        drift.kind = "destructive"
    elif drift.added:
        drift.kind = "additive"
    return drift


def backup_database(db_path: str | Path, backup_dir: str | Path) -> Path:
    """VACUUM INTO a timestamped copy, then verify it. Never a file copy."""
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().replace(":", "").replace("-", "").replace("T", "-").rstrip("Z")
    target = backup_dir / f"{db_path.stem}-{stamp}.db"
    if target.exists():
        raise BackupError(f"backup target already exists: {target}")

    source = sqlite3.connect(str(db_path))
    try:
        source.execute("VACUUM INTO ?", (str(target),))
    finally:
        source.close()

    check = sqlite3.connect(str(target))
    try:
        (status,) = check.execute("PRAGMA integrity_check").fetchone()
        if status != "ok":
            raise BackupError(f"backup failed integrity check: {status}")
    finally:
        check.close()
    return target


def rebuild(
    conn: sqlite3.Connection,
    registry: FieldRegistry,
    *,
    force: bool = False,
) -> list[str]:
    """Rebuild the items table to match the config. Returns warnings.

    The caller is responsible for taking a verified backup first (the CLI
    and boot path both do). Copies every row through coerce_value; any
    coercion failure aborts the transaction unless ``force`` (which nulls
    the offending value and records a warning).
    """
    drift = detect_drift(conn, registry)
    if drift.kind == "fresh":
        create_items_table(conn, registry)
        return []
    if drift.kind == "match":
        _record_applied(conn, registry)
        conn.commit()
        return []

    table = registry.table
    tmp = f"_rebuild_{table}"
    existing = _existing_columns(conn, table)
    warnings: list[str] = []
    problems: list[str] = []

    conn.commit()  # close any implicit transaction before managing our own
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
        conn.execute(_items_ddl(registry, tmp))

        col_names = ["id", *registry.column_list, "created_at", "updated_at"]
        placeholders = ", ".join("?" for _ in col_names)
        quoted_cols = ", ".join(f'"{c}"' for c in col_names)
        insert_sql = f'INSERT INTO "{tmp}" ({quoted_cols}) VALUES ({placeholders})'

        for row in conn.execute(f'SELECT * FROM "{table}"'):
            item = dict(row)
            values: list[object] = [item.get("id")]
            for f in registry.fields:
                if f.key in existing:
                    raw = item.get(f.key)
                elif f.key in drift.renamed:
                    raw = item.get(drift.renamed[f.key])
                else:
                    raw = f.default
                if raw is None:
                    values.append(None)
                    continue
                try:
                    values.append(coerce_value(f, raw))
                except CoercionError as exc:
                    if force:
                        warnings.append(f"row {item.get('id')}: {exc} -> stored NULL")
                        values.append(None)
                    else:
                        problems.append(f"row {item.get('id')}: {exc}")
                        values.append(None)
            values.append(item.get("created_at") or utcnow())
            values.append(item.get("updated_at") or utcnow())
            conn.execute(insert_sql, values)

        if problems:
            raise SchemaError(
                "migration would lose or mangle data; rerun with --force to "
                "store NULL for these values:\n  " + "\n  ".join(problems[:20])
                + ("" if len(problems) <= 20 else f"\n  ... and {len(problems) - 20} more")
            )

        conn.execute(f'DROP TABLE "{table}"')
        conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
        _record_applied(conn, registry)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return warnings
