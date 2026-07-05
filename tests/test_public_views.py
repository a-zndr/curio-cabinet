"""Public browse/detail/share view behavior via the Flask test client."""

import pytest

from curio_cabinet.app import create_app
from curio_cabinet.db import connect, ensure_engine_tables, utcnow

CONFIG = """\
collection:
  title: "Demo"
  slug: things
  id: {strategy: sequential, width: 4}
  title_field: name
  default_sort: {field: name, order: asc}

fields:
  - key: name
    label: Name
    type: text
    required: true
    searchable: true
    views: {table: true, card: primary}
  - key: maker
    label: Maker
    type: text
    link: site
    views: {table: true, card: secondary, filter: multi}
  - key: site
    label: Site
    type: url
  - key: length
    label: Length
    type: number
    unit: {dimension: length, store: cm, display: [in, cm]}
    views: {table: true, card: secondary, filter: range, pivot: [avg]}

presets:
  - key: acme
    label: Acme
    filter: {field: maker, eq: Acme}
    columns: [name, length]

groups:
  - key: core
    label: Core
    fields: [name, maker, site, length]
"""


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "instance"
    (root / "data").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "collection.yaml").write_text(CONFIG)
    (root / ".env").write_text("SECRET_KEY=test\nCABINET_COOKIE_SECURE=0\n")

    # create_app boots the schema (creates the items table); seed afterwards
    application = create_app(str(root))
    application.config["TESTING"] = True

    conn = connect(root / "data" / "catalog.db")
    ensure_engine_tables(conn)
    now = utcnow()
    for i, (name, maker, site, length) in enumerate(
        [
            ("Alpha", "Acme", "https://acme.example", 60.0),
            ("Beta", "Acme", None, 120.0),
            ("Gamma", "Globex", None, 30.0),
        ],
        start=1,
    ):
        conn.execute(
            'INSERT INTO "things" (id, name, maker, site, length, created_at, updated_at) '
            "VALUES (?,?,?,?,?,?,?)",
            (f"{i:04d}", name, maker, site, length, now, now),
        )
    conn.commit()
    conn.close()

    return application


@pytest.fixture
def client(app):
    return app.test_client()


def test_browse_full_page(client):
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "3 items" in body
    assert "<html" in body  # full page


def test_htmx_returns_fragment_only(client):
    r = client.get("/", headers={"HX-Request": "true"})
    body = r.get_data(as_text=True)
    assert '<section id="results"' in body
    assert "<html" not in body  # fragment, no shell


def test_filter_narrows_results(client):
    r = client.get("/?f.maker=Globex")
    assert "1 item" in r.get_data(as_text=True)


def test_range_filter_in_display_units(client):
    # length display[0] is inches; 20 in = 50.8 cm, so only the 30cm item is under
    r = client.get("/?view=table&max_length=20")
    body = r.get_data(as_text=True)
    assert "Gamma" in body and "Alpha" not in body


def test_sort_injection_ignored(client):
    r = client.get("/?sort=name);DROP+TABLE+things&order=desc")
    assert r.status_code == 200  # falls back to default sort, no error


def test_detail_page_and_og(client):
    r = client.get("/item/0001")
    body = r.get_data(as_text=True)
    assert "Alpha" in body
    assert 'property="og:title"' in body
    assert "acme.example" in body  # url rendered as link domain


def test_detail_404(client):
    assert client.get("/item/9999").status_code == 404


def test_share_list_preserves_order_and_reports_missing(client):
    r = client.get("/list?ids=0003,0001,9999")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    # order preserved: Gamma (0003) before Alpha (0001)
    assert body.index("Gamma") < body.index("Alpha")
    assert "no longer in the collection" in body
    assert 'name="robots" content="noindex"' in body


def test_share_list_title_not_in_og(client):
    r = client.get("/list?ids=0001&title=CLICK+HERE+FREE+MONEY")
    body = r.get_data(as_text=True)
    # user title appears in the body/page title but never in og:title
    import re

    og = re.search(r'property="og:title" content="([^"]*)"', body).group(1)
    assert "FREE MONEY" not in og
    assert "items from Demo" in og


def test_share_list_ignores_hostile_ids(client):
    # non-charset tokens are dropped, valid ones kept
    r = client.get("/list?ids=0001,../../etc/passwd,0002")
    body = r.get_data(as_text=True)
    assert "Alpha" in body and "Beta" in body
    assert "2 items" in body


def test_share_list_ids_capped_without_quadratic_blowup(client):
    # a huge id list must be bounded (no O(n^2) scan): cap at MAX_SHARE_IDS
    from curio_cabinet.views.public import MAX_SHARE_IDS, _parse_ids

    raw = ",".join(f"id{i}" for i in range(200_000))
    ids, truncated = _parse_ids(raw)
    assert len(ids) == MAX_SHARE_IDS
    assert truncated is True


def test_image_route_rejects_bad_input(client):
    assert client.get("/images/nothex/thumb").status_code == 404
    assert client.get("/images/" + "a" * 64 + "/evil").status_code == 404
    assert client.get("/images/" + "a" * 64 + "/thumb").status_code == 404  # valid shape, no file


def test_preset_scopes_rows_and_marks_active(client):
    r = client.get("/?view=table&preset=acme")
    body = r.get_data(as_text=True)
    assert "Alpha" in body and "Beta" in body and "Gamma" not in body
    assert "2 items" in body
    # the preset tab is rendered active
    assert 'preset-tab is-active' in body


def test_preset_only_applies_in_table_view(client):
    # cards view ignores preset scoping (presets are a table feature)
    r = client.get("/?view=cards&preset=acme")
    assert "3 items" in r.get_data(as_text=True)


def test_col_override_keeps_preset_scope(client):
    r = client.get("/?view=table&preset=acme&col=name")
    body = r.get_data(as_text=True)
    assert "Gamma" not in body  # still scoped to Acme
    assert "2 items" in body


def test_unknown_preset_is_ignored(client):
    r = client.get("/?view=table&preset=bogus")
    assert "3 items" in r.get_data(as_text=True)  # no scoping


def test_theme_css_served(client):
    r = client.get("/theme.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["Content-Type"]
