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
    page = client.get("/admin/customize").get_data(as_text=True)
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


def test_missing_fields_grouped_under_cabinet_cleanup(client):
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

    body = client.get("/admin/cleanup").get_data(as_text=True)
    # one group per missing must-have field, listing only the incomplete item
    assert "Missing Kind" in body and "Missing Weight" in body
    assert "Bare" in body and "Complete" not in body


def test_todo_clears_when_data_is_filled_in(client):
    import datetime

    csrf = _login(client)
    client.post("/admin/items/new", data={"csrf_token": csrf, "name": "Bare"})
    client.post("/admin/items/0001/edit", data={
        "csrf_token": csrf, "name": "Bare", "kind": "Widget", "weight": "3",
        "last_driven": datetime.date.today().isoformat(),
    })
    body = client.get("/admin/cleanup").get_data(as_text=True)
    assert "nothing missing" in body  # nothing outstanding


def test_customize_field_checkbox_round_trip(client):
    import datetime

    csrf = _login(client)
    client.post("/admin/items/new", data={
        "csrf_token": csrf, "name": "Bare", "kind": "Widget", "weight": "5",
        "last_driven": datetime.date.today().isoformat(),
    })
    # check Notes as must-have too (form posts every checked box; kind/weight
    # stay checked, so they survive; an absent box would clear the flag)
    r = client.post("/admin/customize/fields", data={
        "csrf_token": csrf,
        "musthave__kind": "on", "musthave__weight": "on", "musthave__notes": "on",
    })
    assert r.status_code == 302
    assert "Missing Notes" in client.get("/admin/cleanup").get_data(as_text=True)

    # uncheck notes again -> notes no longer tracked
    client.post("/admin/customize/fields", data={
        "csrf_token": csrf, "musthave__kind": "on", "musthave__weight": "on",
    })
    assert "Missing Notes" not in client.get("/admin/cleanup").get_data(as_text=True)


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

    body = client.get("/admin/todos").get_data(as_text=True)
    assert "Last Driven" in body
    assert "overdue" in body and "never done" in body
    assert "0002" in body and "0003" in body


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
        "csrf_token": csrf, "tab": "fields",
        "search__notes": "on", "private__notes": "on",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Field settings saved" in body
    assert 'name="private__notes" checked' in body  # saved (didn't bounce)


def test_maintenance_done_sets_date_on_selected_items(client):
    csrf = _login(client)
    _add(client, csrf)  # 0001, no last_driven -> "never"
    _add(client, csrf)  # 0002, no last_driven -> "never"
    r = client.post("/admin/maintenance/done", data={
        "csrf_token": csrf, "field": "last_driven",
        "done_date": "2026-07-05", "item_ids": ["0001", "0002"],
    }, follow_redirects=True)
    assert "Marked Last Driven done on 2 item(s)" in r.get_data(as_text=True)
    body = client.get("/admin/").get_data(as_text=True)
    assert "Last Driven" not in body.split("Recently updated")[0]  # both cleared


def test_maintenance_done_rejects_non_maintenance_field(client):
    csrf = _login(client)
    _add(client, csrf)
    r = client.post("/admin/maintenance/done", data={
        "csrf_token": csrf, "field": "weight",  # not an every_days field
        "done_date": "2026-07-05", "item_ids": ["0001"],
    })
    assert r.status_code == 400


def test_cleanup_fill_updates_each_item_individually(client):
    csrf = _login(client)
    driven = "2026-07-05"
    # two items missing weight (kind set so only weight is missing)
    client.post("/admin/items/new", data={
        "csrf_token": csrf, "name": "A", "kind": "Widget", "last_driven": driven})
    client.post("/admin/items/new", data={
        "csrf_token": csrf, "name": "B", "kind": "Widget", "last_driven": driven})
    r = client.post("/admin/cleanup/fill", data={
        "csrf_token": csrf, "field": "weight",
        "val__0001": "10", "val__0002": "20",
    }, follow_redirects=True)
    assert "Updated Weight on 2 item(s)" in r.get_data(as_text=True)
    assert "Missing Weight" not in client.get("/admin/").get_data(as_text=True)


def test_cleanup_fill_reports_bad_value_and_skips_it(client):
    csrf = _login(client)
    client.post("/admin/items/new", data={
        "csrf_token": csrf, "name": "A", "kind": "Widget",
        "last_driven": "2026-07-05"})
    r = client.post("/admin/cleanup/fill", data={
        "csrf_token": csrf, "field": "weight", "val__0001": "not-a-number",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "0001" in body and "Missing Weight" in body  # still outstanding


def test_all_todo_views_render(client):
    import datetime
    csrf = _login(client)
    stale = (datetime.date.today() - datetime.timedelta(days=100)).isoformat()
    _add(client, csrf, last_driven=stale)
    _add(client, csrf)  # never
    for view in ("list", "gantt", "calendar", "buckets"):
        r = client.get(f"/admin/todos?view={view}")
        assert r.status_code == 200
        assert "To-Dos" in r.get_data(as_text=True)
    # list/gantt/buckets show the field; calendar only shows items due in the
    # visible month (overdue items live in a past month) so it's checked apart
    for view in ("list", "gantt", "buckets"):
        assert "Last Driven" in client.get(f"/admin/todos?view={view}").get_data(as_text=True)
    assert "<svg" in client.get("/admin/todos?view=gantt").get_data(as_text=True)
    assert "Overdue" in client.get("/admin/todos?view=buckets").get_data(as_text=True)
    assert "cal-grid" in client.get("/admin/todos?view=calendar").get_data(as_text=True)


def test_todos_bad_view_falls_back_to_list(client):
    _login(client)
    r = client.get("/admin/todos?view=../../etc")
    assert r.status_code == 200 and "To-Dos" in r.get_data(as_text=True)


def test_overview_shows_counts_and_recent(client):
    csrf = _login(client)
    _add(client, csrf)  # missing photo? no, must_have_photos not set here
    body = client.get("/admin/").get_data(as_text=True)
    assert "Recently updated" in body
    assert 'href="/admin/todos"' in body and 'href="/admin/cleanup"' in body


def test_customize_tabs_render(client):
    _login(client)
    for tab, marker in [("general", "Collection name"), ("fields", "Save fields"),
                        ("add", "Add field"), ("presets", "specialty table")]:
        body = client.get(f"/admin/customize?tab={tab}").get_data(as_text=True)
        assert marker in body, f"{marker} missing on tab {tab}"
    # unknown tab falls back to general
    assert "Collection name" in client.get("/admin/customize?tab=xxx").get_data(as_text=True)


def test_far_future_date_does_not_brick_todos(client):
    # a 9999 typo for 1999: stored fine, must not 500 the overview or any view
    csrf = _login(client)
    _add(client, csrf, last_driven="9999-12-01")
    assert client.get("/admin/").status_code == 200
    for view in ("list", "gantt", "calendar", "buckets"):
        assert client.get(f"/admin/todos?view={view}").status_code == 200


def test_calendar_boundary_months_do_not_500(client):
    _login(client)
    for month in ("9999-12", "1-1", "0001-01", "2026-13", "abc", ""):
        assert client.get(f"/admin/todos?view=calendar&month={month}").status_code == 200


def test_view_switch_marks_active_with_seg_class(client):
    _login(client)
    body = client.get("/admin/todos?view=gantt").get_data(as_text=True)
    # the active view uses the real segmented-control class, not a phantom one
    assert 'class="seg is-active"' in body
    assert "seg-btn" not in body
