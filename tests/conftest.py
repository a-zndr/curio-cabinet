from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from curio_cabinet.config import CollectionConfig
from curio_cabinet.db import ensure_engine_tables
from curio_cabinet.registry import FieldRegistry
from curio_cabinet.schema import create_items_table

REPO = Path(__file__).resolve().parents[1]

BASE_CONFIG: dict = {
    "collection": {
        "title": "Test Things",
        "slug": "things",
        "id": {"strategy": "sequential", "width": 4},
        "title_field": "name",
        "default_sort": {"field": "name", "order": "asc"},
    },
    "fields": [
        {
            "key": "name",
            "label": "Name",
            "type": "text",
            "required": True,
            "searchable": True,
            "views": {"table": True},
        },
        {
            "key": "kind",
            "label": "Kind",
            "type": "enum",
            "values": ["Widget", "Gadget"],
            "views": {"filter": "multi", "pivot": ["group"]},
        },
        {
            "key": "length",
            "label": "Length",
            "type": "number",
            "unit": {"dimension": "length", "store": "cm", "display": ["in", "cm"]},
            "views": {"filter": "range", "pivot": ["avg", "min", "max"]},
        },
        {"key": "count", "label": "Count", "type": "integer"},
        {"key": "active", "label": "Active", "type": "boolean"},
        {"key": "materials", "label": "Materials", "type": "tags"},
        {"key": "site", "label": "Site", "type": "url"},
        {"key": "acquired", "label": "Acquired", "type": "date"},
        {"key": "notes", "label": "Notes", "type": "longtext", "searchable": True},
    ],
    "groups": [
        {"key": "core", "label": "Core", "fields": ["name", "kind", "length"]},
    ],
}


def make_config(overrides: dict | None = None) -> CollectionConfig:
    import copy

    raw = copy.deepcopy(BASE_CONFIG)
    if overrides:
        raw.update(overrides)
    return CollectionConfig.model_validate(raw)


@pytest.fixture
def registry() -> FieldRegistry:
    return FieldRegistry(make_config())


@pytest.fixture
def conn(registry: FieldRegistry) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    ensure_engine_tables(c)
    create_items_table(c, registry)
    return c


def insert_thing(conn: sqlite3.Connection, item_id: str, **cols) -> None:
    from curio_cabinet.db import utcnow

    cols.setdefault("name", f"Thing {item_id}")
    names = ["id", *cols.keys(), "created_at", "updated_at"]
    quoted = ", ".join(f'"{n}"' for n in names)
    marks = ", ".join("?" for _ in names)
    now = utcnow()
    conn.execute(
        f'INSERT INTO "things" ({quoted}) VALUES ({marks})',
        [item_id, *cols.values(), now, now],
    )
