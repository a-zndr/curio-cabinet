import json

import pytest

from curio_cabinet.coerce import CoercionError, coerce_row, coerce_value, display_value
from tests.conftest import make_config


CFG = make_config()
BY_KEY = {f.key: f for f in CFG.fields}


def test_required_empty_rejected():
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["name"], "")
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["name"], None)


def test_optional_empty_is_null():
    assert coerce_value(BY_KEY["notes"], "") is None
    assert coerce_value(BY_KEY["kind"], None) is None


def test_number_with_unit_parses_to_store():
    assert coerce_value(BY_KEY["length"], "24 in") == pytest.approx(60.96)
    assert coerce_value(BY_KEY["length"], 61) == 61.0
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["length"], "24 furlongs")


def test_integer_wholeness():
    assert coerce_value(BY_KEY["count"], "12") == 12
    assert coerce_value(BY_KEY["count"], 12.0) == 12
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["count"], "12.5")


def test_boolean_synonyms():
    assert coerce_value(BY_KEY["active"], "true") == 1
    assert coerce_value(BY_KEY["active"], "Yes") == 1
    assert coerce_value(BY_KEY["active"], "no") == 0
    assert coerce_value(BY_KEY["active"], False) == 0
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["active"], "maybe")


def test_tags_from_comma_string_dedupes():
    stored = coerce_value(BY_KEY["materials"], "leather, wood, leather, ")
    assert json.loads(stored) == ["leather", "wood"]


def test_tags_from_json_and_list():
    assert json.loads(coerce_value(BY_KEY["materials"], '["a", "b"]')) == ["a", "b"]
    assert json.loads(coerce_value(BY_KEY["materials"], ["a", "b"])) == ["a", "b"]


def test_enum_case_normalizes_and_accepts_new_when_lax():
    assert coerce_value(BY_KEY["kind"], "widget") == "Widget"
    assert coerce_value(BY_KEY["kind"], "Doohickey") == "Doohickey"  # strict: false


def test_enum_strict_rejects_unknown():
    strict_cfg = make_config()
    field = next(f for f in strict_cfg.fields if f.key == "kind")
    import dataclasses

    strict_field = dataclasses.replace(field, strict=True)
    with pytest.raises(CoercionError):
        coerce_value(strict_field, "Doohickey")
    assert coerce_value(strict_field, "gadget") == "Gadget"


def test_url_scheme_required():
    assert coerce_value(BY_KEY["site"], "https://example.com") == "https://example.com"
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["site"], "example.com")
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["site"], "javascript:alert(1)")


def test_date_iso_only():
    assert coerce_value(BY_KEY["acquired"], "2026-07-04") == "2026-07-04"
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["acquired"], "07/04/2026")


def test_coerce_row_collects_errors():
    values, errors = coerce_row(
        CFG.fields, {"name": "Ball", "length": "nonsense", "count": "3"}
    )
    assert values["name"] == "Ball"
    assert values["count"] == 3
    assert "length" in errors and "length" not in values


def test_display_value():
    assert display_value(BY_KEY["active"], 1) == "Yes"
    assert display_value(BY_KEY["materials"], '["a","b"]') == "a, b"
    assert display_value(BY_KEY["length"], 60.96) == "24 in"  # display[0] is inches
    assert display_value(BY_KEY["name"], None) == ""


def test_apply_computed_fills_and_blanks():
    from curio_cabinet.coerce import apply_computed
    from curio_cabinet.config import CollectionConfig

    fields = CollectionConfig.from_raw({
        "collection": {"title": "T", "slug": "things", "title_field": "name",
                       "default_sort": {"field": "name", "order": "asc"}},
        "fields": [
            {"key": "name", "label": "Name", "type": "text", "required": True},
            {"key": "weight", "label": "Weight", "type": "number"},
            {"key": "length", "label": "Length", "type": "number"},
            {"key": "wpm", "label": "W/m", "type": "number",
             "computed": "weight / (length / 100)"},
        ],
        "groups": [{"key": "g", "label": "G",
                    "fields": ["name", "weight", "length", "wpm"]}],
    }).fields

    values, errors = coerce_row(fields, {"name": "A", "weight": "343.9", "length": "60"})
    apply_computed(fields, values)
    assert errors == {} and values["wpm"] == 573.17
    # a blank operand yields a blank computed value, never an error
    v2, _ = coerce_row(fields, {"name": "B", "weight": "100"})
    apply_computed(fields, v2)
    assert v2["wpm"] is None
    # computed fields take no user input even if a value is posted
    v3, _ = coerce_row(fields, {"name": "C", "weight": "50", "length": "50", "wpm": "999"})
    apply_computed(fields, v3)
    assert v3["wpm"] == 100.0
