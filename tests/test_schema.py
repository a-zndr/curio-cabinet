import copy
import sqlite3

import pytest

from curio_cabinet.db import ensure_engine_tables
from curio_cabinet.registry import FieldRegistry
from curio_cabinet.schema import (
    SchemaError,
    backup_database,
    create_items_table,
    detect_drift,
    rebuild,
)
from tests.conftest import BASE_CONFIG, insert_thing, make_config


def _registry_with(mutate) -> FieldRegistry:
    raw = copy.deepcopy(BASE_CONFIG)
    mutate(raw)
    return FieldRegistry(make_config(raw))


def test_fresh_and_match(conn, registry):
    assert detect_drift(conn, registry).kind == "match"
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    ensure_engine_tables(empty)
    assert detect_drift(empty, registry).kind == "fresh"


def test_additive_drift(conn):
    reg2 = _registry_with(
        lambda raw: raw["fields"].append(
            {"key": "color", "label": "Color", "type": "text"}
        )
    )
    drift = detect_drift(conn, reg2)
    assert drift.kind == "additive"
    assert drift.added == ["color"]


def test_removed_column_is_destructive(conn):
    reg2 = _registry_with(
        lambda raw: (
            raw["fields"].pop(),  # drop 'notes'
            raw.__setitem__(
                "groups", [{"key": "core", "label": "Core", "fields": ["name"]}]
            ),
        )
    )
    drift = detect_drift(conn, reg2)
    assert drift.kind == "destructive"
    assert "notes" in drift.removed


def test_rename_detected_via_hint(conn):
    def mutate(raw):
        field = next(f for f in raw["fields"] if f["key"] == "notes")
        field["key"] = "remarks"
        field["rename_from"] = "notes"

    reg2 = _registry_with(mutate)
    drift = detect_drift(conn, reg2)
    assert drift.renamed == {"remarks": "notes"}
    assert drift.kind == "destructive"  # renames only run via explicit migrate


def test_stale_rename_hint_ignored(conn):
    def mutate(raw):
        raw["fields"].append(
            {"key": "brand_new", "label": "New", "type": "text",
             "rename_from": "never_existed"}
        )

    reg2 = _registry_with(mutate)
    drift = detect_drift(conn, reg2)
    assert drift.kind == "additive"
    assert drift.added == ["brand_new"]


def test_rebuild_carries_data_through_rename(conn, registry):
    insert_thing(conn, "0001", name="Ball", notes="round")
    conn.commit()

    def mutate(raw):
        field = next(f for f in raw["fields"] if f["key"] == "notes")
        field["key"] = "remarks"
        field["rename_from"] = "notes"

    reg2 = _registry_with(mutate)
    warnings = rebuild(conn, reg2, force=False)
    assert warnings == []
    row = conn.execute('SELECT * FROM "things"').fetchone()
    assert row["remarks"] == "round"
    assert "notes" not in row.keys()
    assert detect_drift(conn, reg2).kind == "match"


def test_rebuild_aborts_on_coercion_failure_without_force(conn):
    insert_thing(conn, "0001", name="Ball", notes="not-a-number")
    conn.commit()

    def mutate(raw):
        field = next(f for f in raw["fields"] if f["key"] == "notes")
        field["type"] = "number"
        field.pop("searchable", None)

    reg2 = _registry_with(mutate)
    with pytest.raises(SchemaError, match="0001"):
        rebuild(conn, reg2, force=False)
    # aborted: original table intact
    row = conn.execute('SELECT * FROM "things"').fetchone()
    assert row["notes"] == "not-a-number"

    warnings = rebuild(conn, reg2, force=True)
    assert any("0001" in w for w in warnings)
    row = conn.execute('SELECT * FROM "things"').fetchone()
    assert row["notes"] is None


def test_rebuild_preserves_ids_and_timestamps(conn, registry):
    insert_thing(conn, "0042", name="Ball")
    conn.commit()
    before = conn.execute('SELECT * FROM "things"').fetchone()
    reg2 = _registry_with(
        lambda raw: raw["fields"].append(
            {"key": "color", "label": "Color", "type": "text"}
        )
    )
    rebuild(conn, reg2)
    after = conn.execute('SELECT * FROM "things"').fetchone()
    assert after["id"] == "0042"
    assert after["created_at"] == before["created_at"]
    assert after["color"] is None


def test_backup_database_verified(tmp_path, registry):
    db_path = tmp_path / "catalog.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_engine_tables(conn)
    create_items_table(conn, registry)
    insert_thing(conn, "0001", name="Ball")
    conn.commit()
    conn.close()

    backup = backup_database(db_path, tmp_path / "backups")
    assert backup.exists()
    check = sqlite3.connect(backup)
    (n,) = check.execute('SELECT COUNT(*) FROM "things"').fetchone()
    assert n == 1
