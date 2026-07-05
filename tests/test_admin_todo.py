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

groups:
  - key: core
    label: Core
    fields: [name, kind, weight, notes]
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
    csrf = _login(client)
    client.post("/admin/items/new", data={
        "csrf_token": csrf, "name": "Complete", "kind": "Widget", "weight": "5",
    })
    client.post("/admin/items/new", data={"csrf_token": csrf, "name": "Bare"})

    body = client.get("/admin/").get_data(as_text=True)
    assert "To finish" in body
    # only "Bare" is incomplete: two chips (Kind, Weight), photos not required
    assert body.count("todo-chip") == 2
    assert ">Kind</span>" in body and ">Weight</span>" in body
    todo_block = body.split("To finish")[1].split("Recently updated")[0]
    assert "Bare" in todo_block and "Complete" not in todo_block


def test_todo_clears_when_data_is_filled_in(client):
    csrf = _login(client)
    client.post("/admin/items/new", data={"csrf_token": csrf, "name": "Bare"})
    client.post("/admin/items/0001/edit", data={
        "csrf_token": csrf, "name": "Bare", "kind": "Widget", "weight": "3",
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
