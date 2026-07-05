import copy

import pytest

from curio_cabinet.config import ConfigError, load_config
from tests.conftest import BASE_CONFIG, REPO, make_config


def _raw() -> dict:
    return copy.deepcopy(BASE_CONFIG)


def test_example_config_loads():
    config = load_config(REPO / "examples" / "impact-toys" / "collection.yaml")
    assert config.collection.slug == "toys"
    keys = [f.key for f in config.fields]
    assert "maker" in keys and "whip_type" in keys
    # every field ends up in a group
    grouped = {k for g in config.groups for k in g.fields}
    assert grouped == set(keys)


def test_ungrouped_fields_get_implicit_other_group():
    config = make_config()
    other = config.groups[-1]
    assert other.key == "other"
    assert "materials" in other.fields and "notes" in other.fields


def test_duplicate_field_keys_rejected():
    raw = _raw()
    raw["fields"].append({"key": "name", "label": "Name2", "type": "text"})
    with pytest.raises(Exception, match="duplicate"):
        make_config(raw)


def test_reserved_and_keyword_keys_rejected():
    for bad in ("id", "created_at", "order", "group"):
        raw = _raw()
        raw["fields"].append({"key": bad, "label": bad, "type": "text"})
        with pytest.raises(Exception):
            make_config(raw)


def test_field_in_two_groups_rejected():
    raw = _raw()
    raw["groups"].append({"key": "dup", "label": "Dup", "fields": ["name"]})
    with pytest.raises(Exception, match="appears in groups"):
        make_config(raw)


def test_link_must_point_at_url_field():
    raw = _raw()
    raw["fields"][0]["link"] = "notes"  # longtext, not url
    with pytest.raises(Exception, match="link"):
        make_config(raw)


def test_enum_requires_values():
    raw = _raw()
    raw["fields"].append({"key": "flavor", "label": "Flavor", "type": "enum"})
    with pytest.raises(Exception, match="values"):
        make_config(raw)


def test_slug_collision_with_engine_tables_rejected():
    raw = _raw()
    raw["collection"]["slug"] = "users"
    with pytest.raises(Exception, match="collides"):
        make_config(raw)


def test_when_condition():
    raw = _raw()
    raw["groups"].append(
        {
            "key": "widgetry",
            "label": "Widget Details",
            "when": {"field": "kind", "eq": "Widget"},
            "fields": ["count"],
        }
    )
    config = make_config(raw)
    group = next(g for g in config.groups if g.key == "widgetry")
    assert group.when.matches({"kind": "Widget"})
    assert not group.when.matches({"kind": "Gadget"})


def test_table_defaults_off():
    config = make_config()
    by_key = {f.key: f for f in config.fields}
    assert by_key["name"].in_table  # explicitly on
    assert not by_key["notes"].in_table  # default off
    assert not by_key["count"].in_table


def test_presets_validated():
    raw = _raw()
    raw["presets"] = [
        {"key": "whips", "label": "Whips",
         "filter": {"field": "kind", "in": ["Widget"]},
         "columns": ["name", "length"]},
    ]
    config = make_config(raw)
    assert config.presets[0].filter_values() == ("Widget",)

    bad_col = _raw()
    bad_col["presets"] = [{"key": "x", "label": "X",
                           "filter": {"field": "kind", "eq": "Widget"},
                           "columns": ["nope"]}]
    with pytest.raises(Exception, match="unknown column"):
        make_config(bad_col)

    bad_field = _raw()
    bad_field["presets"] = [{"key": "x", "label": "X",
                             "filter": {"field": "nope", "eq": "Widget"},
                             "columns": ["name"]}]
    with pytest.raises(Exception, match="unknown field"):
        make_config(bad_field)


def test_example_config_has_presets():
    config = load_config(REPO / "examples" / "impact-toys" / "collection.yaml")
    keys = {p.key for p in config.presets}
    assert {"whips", "floggers"} <= keys


def test_accent_hex_validated_and_normalized():
    raw = _raw()
    raw["collection"]["accent"] = "#3B6FD4"
    assert make_config(raw).collection.accent == "#3b6fd4"
    raw["collection"]["accent"] = "f0a"  # shorthand, no hash
    assert make_config(raw).collection.accent == "#ff00aa"
    raw["collection"]["accent"] = "not-a-color"
    with pytest.raises(Exception, match="hex color"):
        make_config(raw)


def test_config_sha_stable_and_schema_sensitive():
    a, b = make_config(), make_config()
    assert a.sha() == b.sha()
    raw = _raw()
    raw["fields"].append({"key": "extra", "label": "Extra", "type": "text"})
    assert make_config(raw).sha() != a.sha()
