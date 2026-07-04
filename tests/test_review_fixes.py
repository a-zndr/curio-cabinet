"""Regression tests for the verified findings from the engine-core review."""

import copy
import sqlite3

import pytest

from curio_cabinet import auth
from curio_cabinet.coerce import CoercionError, coerce_value
from curio_cabinet.csvio import export_csv, import_csv
from curio_cabinet.db import ensure_engine_tables
from curio_cabinet.instance import load_dotenv
from curio_cabinet.query import filter_options, parse_params
from curio_cabinet.registry import FieldRegistry
from curio_cabinet.schema import detect_drift, rebuild
from tests.conftest import BASE_CONFIG, insert_thing, make_config


def _registry_with(mutate) -> FieldRegistry:
    raw = copy.deepcopy(BASE_CONFIG)
    mutate(raw)
    return FieldRegistry(make_config(raw))


# -- critical: logical drift invisible to affinity comparison ----------------


def test_same_affinity_type_change_is_destructive(conn):
    """longtext -> tags shares TEXT affinity; _meta must catch it."""

    def mutate(raw):
        field = next(f for f in raw["fields"] if f["key"] == "notes")
        field["type"] = "tags"
        field.pop("searchable", None)

    reg2 = _registry_with(mutate)
    drift = detect_drift(conn, reg2)
    assert drift.kind == "destructive"
    assert "notes" in drift.retyped


def test_type_change_rebuild_coerces_rows(conn):
    insert_thing(conn, "0001", name="Ball", notes="wood, brass")
    conn.commit()

    def mutate(raw):
        field = next(f for f in raw["fields"] if f["key"] == "notes")
        field["type"] = "tags"
        field.pop("searchable", None)

    reg2 = _registry_with(mutate)
    rebuild(conn, reg2)
    row = conn.execute('SELECT "notes" FROM "things"').fetchone()
    assert row["notes"] == '["wood", "brass"]'
    assert detect_drift(conn, reg2).kind == "match"


# -- critical: unit.store change was invisible destructive drift -------------


def test_unit_store_change_detected_and_converted(conn):
    insert_thing(conn, "0001", name="Ball", length=60.96)  # cm
    conn.commit()

    def mutate(raw):
        field = next(f for f in raw["fields"] if f["key"] == "length")
        field["unit"] = {"dimension": "length", "store": "in", "display": ["in"]}

    reg2 = _registry_with(mutate)
    drift = detect_drift(conn, reg2)
    assert drift.kind == "destructive"
    assert "length" in drift.reunited

    rebuild(conn, reg2)
    row = conn.execute('SELECT "length" FROM "things"').fetchone()
    assert row["length"] == pytest.approx(24.0)  # converted, not reinterpreted


# -- major: additive rebuild must not choke on historic nonconforming data ---


def test_additive_rebuild_copies_bad_history_verbatim(conn):
    # a value that would fail today's enum coercion if it were re-coerced
    insert_thing(conn, "0001", name="Ball", kind="LegacyJunk")
    conn.commit()

    reg2 = _registry_with(
        lambda raw: raw["fields"].append(
            {"key": "color", "label": "Color", "type": "text"}
        )
    )
    warnings = rebuild(conn, reg2)  # must not raise
    assert warnings == []
    row = conn.execute('SELECT * FROM "things"').fetchone()
    assert row["kind"] == "LegacyJunk" and row["color"] is None


# -- coercion edges ------------------------------------------------------------


CFG = make_config()
BY_KEY = {f.key: f for f in CFG.fields}


@pytest.mark.parametrize("bad", ["inf", "-inf", "1e309", "nan"])
def test_nonfinite_numbers_rejected(bad):
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["count"], bad)
    with pytest.raises(CoercionError):
        coerce_value(BY_KEY["length"], bad)


def test_default_validated_at_config_load():
    raw = copy.deepcopy(BASE_CONFIG)
    raw["fields"].append(
        {"key": "rating", "label": "Rating", "type": "integer", "default": "banana"}
    )
    with pytest.raises(Exception, match="invalid default"):
        make_config(raw)


def test_label_collision_rejected():
    raw = copy.deepcopy(BASE_CONFIG)
    raw["fields"].append({"key": "maker", "label": "Name", "type": "text"})
    with pytest.raises(Exception, match="collides"):
        make_config(raw)


# -- CSV ------------------------------------------------------------------------


def test_bom_header_still_maps_first_column(conn, registry):
    text = "﻿name,kind\nAlpha,Widget\n"
    report = import_csv(conn, registry, text)
    assert report.imported == 1 and not report.errors
    row = conn.execute('SELECT * FROM "things"').fetchone()
    assert row["name"] == "Alpha"


def test_missing_required_column_errors_per_line(conn, registry):
    report = import_csv(conn, registry, "kind,count\nWidget,3\n")
    assert report.imported == 0 and report.skipped == 1
    assert any("name" in e and "required" in e for e in report.errors)


def test_duplicate_header_is_an_error(conn, registry):
    report = import_csv(conn, registry, "Name,name,kind\nA,B,Widget\n")
    assert report.imported == 0
    assert any("both map" in e for e in report.errors)


def test_unknown_columns_noted(conn, registry):
    report = import_csv(conn, registry, "name,mystery\nAlpha,42\n")
    assert report.imported == 1
    assert any("mystery" in n for n in report.notes)


def test_tags_with_embedded_comma_round_trip(conn, registry):
    report = import_csv(
        conn, registry, 'name,materials\nAlpha,"[""oak, aged"", ""brass""]"\n'
    )
    assert report.imported == 1, report.errors
    text = export_csv(conn, registry)
    conn.execute('DELETE FROM "things"')
    report2 = import_csv(conn, registry, text)
    assert report2.imported == 1, report2.errors
    row = conn.execute('SELECT "materials" FROM "things"').fetchone()
    assert row["materials"] == '["oak, aged", "brass"]'


def test_formula_cells_neutralized_and_restored(conn, registry):
    report = import_csv(conn, registry, "name,notes\n=HYPERLINK(1),hi\n")
    assert report.imported == 1
    text = export_csv(conn, registry)
    assert "'=HYPERLINK" in text  # neutralized on disk
    conn.execute('DELETE FROM "things"')
    import_csv(conn, registry, text)
    row = conn.execute('SELECT "name" FROM "things"').fetchone()
    assert row["name"] == "=HYPERLINK(1)"  # restored on import


# -- query hardening ---------------------------------------------------------------


class FakeArgs(dict):
    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


def test_range_bound_accepts_unit_suffix(registry):
    params = parse_params(registry, FakeArgs({"min_length": "2 ft"}))
    lo, hi = params.ranges["length"]
    assert lo == pytest.approx(60.96)  # 2 ft in stored cm


def test_range_bound_rejects_nonfinite(registry):
    params = parse_params(registry, FakeArgs({"min_length": "nan", "max_length": "inf"}))
    assert "length" not in params.ranges


def test_huge_page_clamped(registry):
    params = parse_params(registry, {"page": "9" * 30})
    assert params.page == 100_000


def test_filter_options_range_in_display_units(conn, registry):
    insert_thing(conn, "0001", name="A", length=50.0)   # cm
    insert_thing(conn, "0002", name="B", length=200.0)
    conn.commit()
    opts = filter_options(conn, registry)
    lo, hi = opts["range"]["length"]
    assert lo == pytest.approx(19.69, abs=0.01)  # shown in inches (display[0])
    assert hi == pytest.approx(78.74, abs=0.01)


# -- dotenv / device throttle -------------------------------------------------------


def test_dotenv_inline_comment_stripped(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "SECRET_KEY=abc123\n"
        "CABINET_JOURNAL_MODE=TRUNCATE  # use TRUNCATE on network filesystems\n"
    )
    values = load_dotenv(env)
    assert values["CABINET_JOURNAL_MODE"] == "TRUNCATE"
    assert values["SECRET_KEY"] == "abc123"


def test_known_device_token_roundtrip():
    token = auth.device_token("secret", "zee")
    assert auth.verify_device_token("secret", "zee", token)
    assert not auth.verify_device_token("secret", "zee", token[:-2] + "xx")
    assert not auth.verify_device_token("secret", "other", token)
    assert not auth.verify_device_token("secret", "zee", None)


def test_known_device_bypasses_username_throttle():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ensure_engine_tables(db)
    auth.create_admin_user(db, "zee", "correct horse battery staple")
    for _ in range(6):
        auth.record_attempt(db, "zee", None, success=False)
    # attacker (no device cookie) is throttled hard
    assert auth.login_delay_remaining(db, "zee") > 60
    # the admin's own browser presents a valid device token and skips the
    # throttle at the view layer — validated by the token check itself
    assert auth.verify_device_token("s", "zee", auth.device_token("s", "zee"))
