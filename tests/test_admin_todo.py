"""The dashboard "To finish" list: must-have fields that never block saves."""

import re

import pytest

from curio_cabinet import auth
from curio_cabinet.app import create_app
from curio_cabinet.db import connect, ensure_engine_tables

CONFIG = """\
collection:
  title: "Todo Test"
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
    values: [Widget, Gadget]
    strict: false
    must_have: true
  - key: weight
    label: Weight
    type: number
    must_have: true
  - key: notes
    label: Notes
    type: longtext
  - key: secret_notes
    label: Secret Notes
    type: longtext
    private: true
  - key: last_driven
    label: Last Driven
    type: date
    every_days: 60

presets:
  - key: widgets
    label: Widgets
    filter: {field: kind, eq: Widget}
    columns: [kind, weight]

groups:
  - key: core
    label: Core
    fields: [name, kind, weight, notes, secret_notes, last_driven]
"""

PW = "a sufficiently long password"


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "instance"
    (root / "data").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "collection.yaml").write_text(CONFIG)
    (root / ".env").write_text("SECRET_KEY=test-secret\nCABINET_COOKIE_SECURE=0\n")

    conn = connect(root / "data" / "catalog.db")
    ensure_engine_tables(conn)
    auth.create_admin_user(conn, "zee", PW)
    conn.close()

    application = create_app(str(root))
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client):
    resp = client.post(
        "/admin/login", data={"username": "zee", "password": PW},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    page = client.get("/admin/").get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match
    return match.group(1)


def test_must_have_never_blocks_a_save(client):
    csrf = _login(client)
    r = client.post(
        "/admin/items/new",
        data={"csrf_token": csrf, "name": "Bare"},  # no kind, no weight
        follow_redirects=False,
    )
    assert r.status_code == 302  # saved fine


def test_incomplete_items_listed_with_missing_chips(client):
    import datetime

    csrf = _login(client)
    driven = datetime.date.today().isoformat()  # keep maintenance quiet here
    client.post("/admin/items/new", data={
        "csrf_token": csrf, "name": "Complete", "kind": "Widget", "weight": "5",
        "last_driven": driven,
    })
    client.post("/admin/items/new", data={
        "csrf_token": csrf, "name": "Bare", "last_driven": driven,
    })

    body = client.get("/admin/").get_data(as_text=True)
    assert "To finish" in body
    # only "Bare" is incomplete: two chips (Kind, Weight), photos not required
    assert body.count("todo-chip") == 2
    assert ">Kind</span>" in body and ">Weight</span>" in body
    todo_block = body.split("To finish")[1].split("Recently updated")[0]
    assert "Bare" in todo_block and "Complete" not in todo_block


def test_todo_clears_when_data_is_filled_in(client):
    import datetime

    csrf = _login(client)
    client.post("/admin/items/new", data={"csrf_token": csrf, "name": "Bare"})
    client.post("/admin/items/0001/edit", data={
        "csrf_token": csrf, "name": "Bare", "kind": "Widget", "weight": "3",
        "last_driven": datetime.date.today().isoformat(),
    })
    body = client.get("/admin/").get_data(as_text=True)
    assert "todo-chip" not in body
    assert "nothing missing" in body  # designed empty state


def test_customize_field_checkbox_round_trip(client):
    csrf = _login(client)
    client.post("/admin/items/new", data={
        "csrf_token": csrf, "name": "Bare", "kind": "Widget", "weight": "5",
    })
    # check Notes as must-have too (form posts every checked box; kind/weight
    # stay checked, so they survive; an absent box would clear the flag)
    r = client.post("/admin/customize/fields", data={
        "csrf_token": csrf,
        "musthave__kind": "on", "musthave__weight": "on", "musthave__notes": "on",
    })
    assert r.status_code == 302
    body = client.get("/admin/").get_data(as_text=True)
    assert ">Notes</span>" in body and body.count("todo-chip") == 1

    # uncheck notes again -> item is complete
    client.post("/admin/customize/fields", data={
        "csrf_token": csrf, "musthave__kind": "on", "musthave__weight": "on",
    })
    assert "todo-chip" not in client.get("/admin/").get_data(as_text=True)


def _add(client, csrf, **extra):
    data = {"csrf_token": csrf, "name": "Car", "kind": "Widget", "weight": "1"}
    data.update(extra)
    return client.post("/admin/items/new", data=data)


def test_private_field_hidden_from_public(client):
    csrf = _login(client)
    _add(client, csrf, secret_notes="the combination is 1234",
         last_driven="2026-07-01")
    anon = client.application.test_client()  # fresh, unauthenticated
    detail = anon.get("/item/0001").get_data(as_text=True)
    assert "combination is 1234" not in detail and "Secret Notes" not in detail
    table = anon.get("/?view=table&col=secret_notes").get_data(as_text=True)
    assert "combination is 1234" not in table and "Secret Notes" not in table
    cards = anon.get("/?view=cards&cardf=secret_notes").get_data(as_text=True)
    assert "combination is 1234" not in cards
    # not offered in the public Fields picker either
    assert "secret_notes" not in anon.get("/?view=table").get_data(as_text=True)


def test_private_field_visible_to_admin(client):
    csrf = _login(client)
    _add(client, csrf, secret_notes="the combination is 1234",
         last_driven="2026-07-01")
    detail = client.get("/item/0001").get_data(as_text=True)
    assert "combination is 1234" in detail
    assert "private-badge" in detail  # marked so you know it's not public


def test_maintenance_dates_feed_the_todo_list(client):
    import datetime

    csrf = _login(client)
    today = datetime.date.today()
    fresh = (today - datetime.timedelta(days=10)).isoformat()
    stale = (today - datetime.timedelta(days=100)).isoformat()

    _add(client, csrf, last_driven=fresh)    # 0001: fine
    _add(client, csrf, last_driven=stale)    # 0002: overdue
    _add(client, csrf)                       # 0003: never driven

    body = client.get("/admin/").get_data(as_text=True)
    assert "Last Driven: 100d ago" in body
    assert "Last Driven: never" in body
    todo_block = body.split("To finish")[1].split("Recently updated")[0]
    assert "0002" in todo_block and "0003" in todo_block
    assert "0001" not in todo_block  # driven recently, nothing due


def test_marking_preset_column_private_strips_it_and_stops_the_leak(client):
    # review finding: preset columns bypassed private enforcement
    csrf = _login(client)
    _add(client, csrf, secret_notes="x")
    # weight is a column of the "widgets" preset; mark it private
    r = client.post("/admin/customize/fields", data={
        "csrf_token": csrf, "private__weight": "on",
    })
    assert r.status_code == 302
    anon = client.application.test_client()
    body = anon.get("/?view=table&preset=widgets").get_data(as_text=True)
    assert ">Weight<" not in body  # neither header nor value renders


def test_add_preset_refuses_private_columns(client):
    csrf = _login(client)
    _add(client, csrf, secret_notes="the combination is 1234")
    r = client.post("/admin/customize/presets/add", data={
        "csrf_token": csrf, "label": "Leaky", "key": "leaky",
        "filter_field": "kind", "filter_values": "Widget",
        "columns": ["kind", "secret_notes"],
    })
    assert r.status_code == 302
    anon = client.application.test_client()
    body = anon.get("/?view=table&preset=leaky").get_data(as_text=True)
    assert "combination is 1234" not in body and "Secret Notes" not in body


def test_marking_searchable_field_private_saves_cleanly(client):
    # review finding: the searchable re-set after the private strip bounced
    # the whole save; the form naturally posts both checkboxes
    csrf = _login(client)
    r = client.post("/admin/customize/fields", data={
        "csrf_token": csrf, "search__notes": "on", "private__notes": "on",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Field settings saved" in body
    assert 'name="private__notes" checked' in body
