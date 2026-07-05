import pytest

from curio_cabinet.query import (
    build_select,
    count_items,
    filter_options,
    parse_params,
    pivot,
)
from tests.conftest import insert_thing


@pytest.fixture
def populated(conn):
    insert_thing(conn, "0001", name="Alpha", kind="Widget", length=100.0,
                 materials='["leather","wood"]', active=1, notes="soft thud")
    insert_thing(conn, "0002", name="Beta", kind="Gadget", length=50.0,
                 materials='["nylon"]', active=0, notes="sharp sting")
    insert_thing(conn, "0003", name="Gamma", kind="Widget", length=200.0,
                 materials='["leather"]', active=1)
    conn.commit()
    return conn


def _rows(conn, registry, args):
    params = parse_params(registry, args)
    sql, binds = build_select(registry, params)
    return [r["id"] for r in conn.execute(sql, binds).fetchall()]


def test_default_sort(populated, registry):
    assert _rows(populated, registry, {}) == ["0001", "0002", "0003"]


def test_sort_whitelist_falls_back(populated, registry):
    ids = _rows(populated, registry, {"sort": "evil; DROP TABLE things"})
    assert ids == ["0001", "0002", "0003"]  # fell back to default sort


def test_sort_desc_with_nulls_last(populated, registry):
    ids = _rows(populated, registry, {"sort": "length", "order": "desc"})
    assert ids == ["0003", "0001", "0002"]


class FakeArgs(dict):
    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


def test_multi_filter_enum(populated, registry):
    ids = _rows(populated, registry, FakeArgs({"f.kind": "Widget"}))
    assert ids == ["0001", "0003"]


def test_multi_filter_tags(populated, registry):
    ids = _rows(populated, registry, FakeArgs({"f.materials": "leather"}))
    assert ids == ["0001", "0003"]
    ids = _rows(populated, registry, FakeArgs({"f.materials": ["nylon", "wood"]}))
    assert ids == ["0001", "0002"]


def test_range_filter_converts_display_unit(populated, registry):
    # length display[0] is inches; stored cm. 30 in = 76.2 cm
    ids = _rows(populated, registry, FakeArgs({"min_length": "30"}))
    assert ids == ["0001", "0003"]
    ids = _rows(populated, registry, FakeArgs({"max_length": "30"}))
    assert ids == ["0002"]


def test_search_hits_searchable_fields_only(populated, registry):
    ids = _rows(populated, registry, {"q": "thud"})
    assert ids == ["0001"]
    # LIKE wildcards in user input are escaped, not interpreted
    assert _rows(populated, registry, {"q": "%"}) == []


def test_count(populated, registry):
    params = parse_params(registry, FakeArgs({"f.kind": "Widget"}))
    assert count_items(populated, registry, params) == 2


def test_pivot_count(populated, registry):
    rows = pivot(populated, registry, parse_params(registry, {}), group_key="kind")
    data = {r["grp"]: r["n"] for r in rows}
    assert data == {"Widget": 2, "Gadget": 1}


def test_pivot_avg(populated, registry):
    rows = pivot(
        populated, registry, parse_params(registry, {}),
        group_key="kind", agg_op="avg", agg_key="length",
    )
    data = {r["grp"]: r["val"] for r in rows}
    assert data["Widget"] == pytest.approx(150.0)


def test_pivot_tags_multicounts(populated, registry):
    rows = pivot(populated, registry, parse_params(registry, {}), group_key="materials")
    data = {r["grp"]: r["n"] for r in rows}
    assert data == {"leather": 2, "wood": 1, "nylon": 1}


def test_pivot_rejects_unregistered(populated, registry):
    with pytest.raises(ValueError):
        pivot(populated, registry, parse_params(registry, {}), group_key="name")
    with pytest.raises(ValueError):
        pivot(
            populated, registry, parse_params(registry, {}),
            group_key="kind", agg_op="sum", agg_key="length",  # sum not declared
        )


def test_histogram(populated, registry):
    from curio_cabinet.query import histogram

    # lengths present: 100, 50, 200 (stored cm)
    h = histogram(populated, registry, parse_params(registry, {}), "length")
    assert h["n"] == 3 and h["lo"] == 50.0 and h["hi"] == 200.0
    assert isinstance(h["bins"], list) and sum(h["bins"]) == 3
    assert len(h["edges"]) == len(h["bins"]) + 1


def test_histogram_too_sparse(populated, registry):
    from curio_cabinet.query import histogram

    # only one item has a plait_count-like value -> not chartable
    insert_thing(populated, "0099", name="Solo", count=5)
    populated.commit()
    h = histogram(populated, registry, parse_params(registry, {}), "count")
    assert h is not None and h["bins"] is None  # too few values to bin


def test_filter_options(populated, registry):
    opts = filter_options(populated, registry)
    assert opts["multi"]["kind"] == ["Gadget", "Widget"]
    assert set(opts["multi"]["materials"]) == {"leather", "wood", "nylon"}
    assert opts["multi"]["active"] == ["Yes", "No"]
    # range bounds are expressed in display[0] units (inches here)
    assert opts["range"]["length"] == (pytest.approx(19.69, abs=0.01),
                                       pytest.approx(78.74, abs=0.01))
