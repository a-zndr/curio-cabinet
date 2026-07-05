import copy

import pytest

from curio_cabinet import configio
from curio_cabinet.db import connect, ensure_engine_tables
from curio_cabinet.instance import resolve_instance
from curio_cabinet.schema import create_items_table

CONFIG = """\
collection:
  title: "Things"
  slug: things
  id: {strategy: sequential, width: 4}
  title_field: name
  default_sort: {field: name, order: asc}

fields:
  - key: name
    label: Name
    type: text
    required: true
    views: {table: true}
  - key: kind
    label: Kind
    type: enum
    values: [A, B]
    views: {filter: multi, pivot: [group]}

groups:
  - key: core
    label: Core
    fields: [name, kind]
"""


def make_instance(tmp_path):
    root = tmp_path / "inst"
    (root / "data").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "collection.yaml").write_text(CONFIG)
    (root / ".env").write_text("SECRET_KEY=x\n")
    inst = resolve_instance(str(root))
    conn = connect(inst.db_path)
    ensure_engine_tables(conn)
    create_items_table(conn, inst.registry)
    conn.close()
    return inst


def _cols(inst):
    conn = connect(inst.db_path)
    names = [r["name"] for r in conn.execute('SELECT name FROM pragma_table_info("things")')]
    conn.close()
    return names


def test_view_only_edit_applies(tmp_path):
    inst = make_instance(tmp_path)
    raw = configio.load_raw(inst)
    raw["collection"]["title"] = "Renamed"
    raw["collection"]["accent_hue"] = 200
    new = configio.apply_config(inst, raw)
    assert new.config.collection.title == "Renamed"
    assert new.config.collection.accent_hue == 200
    # persisted to disk
    assert "Renamed" in (inst.root / "collection.yaml").read_text()


def test_enum_values_edit(tmp_path):
    inst = make_instance(tmp_path)
    raw = configio.load_raw(inst)
    raw["fields"][1]["values"] = ["A", "B", "C"]
    new = configio.apply_config(inst, raw)
    assert new.registry.by_key["kind"].values == ("A", "B", "C")


def test_add_field_migrates(tmp_path):
    inst = make_instance(tmp_path)
    assert "color" not in _cols(inst)
    raw = configio.load_raw(inst)
    raw["fields"].append({"key": "color", "label": "Color", "type": "text"})
    raw["groups"][0]["fields"].append("color")
    new = configio.apply_config(inst, raw)
    assert "color" in new.registry.by_key
    assert "color" in _cols(inst)  # additive migration ran


def test_destructive_edit_refused(tmp_path):
    inst = make_instance(tmp_path)
    raw = configio.load_raw(inst)
    raw["fields"] = [f for f in raw["fields"] if f["key"] != "kind"]
    raw["groups"][0]["fields"] = ["name"]
    with pytest.raises(configio.ConfigEditError, match="CLI|change the database"):
        configio.apply_config(inst, raw)
    # config on disk unchanged (kind still present)
    assert "kind" in (inst.root / "collection.yaml").read_text()


def test_invalid_config_refused(tmp_path):
    inst = make_instance(tmp_path)
    raw = configio.load_raw(inst)
    raw["fields"][0]["type"] = "bogus"
    with pytest.raises(configio.ConfigEditError):
        configio.apply_config(inst, raw)
