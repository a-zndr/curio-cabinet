"""End-to-end admin flow through the Flask test client."""

import io
import re

import pytest
from PIL import Image

from curio_cabinet import auth
from curio_cabinet.app import create_app
from curio_cabinet.db import connect, ensure_engine_tables

CONFIG = """\
collection:
  title: "Flow Test"
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
    views: {table: true}
  - key: kind
    label: Kind
    type: enum
    values: [Widget, Gadget]
    strict: false
  - key: length
    label: Length
    type: number
    unit: {dimension: length, store: cm, display: [cm, in]}
  - key: part_count
    label: Parts
    type: integer

groups:
  - key: core
    label: Core
    fields: [name, kind, length]
  - key: extras
    label: Gadget Details
    when: {field: kind, eq: Gadget}
    fields: [part_count]
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
    assert match, "csrf token not found in dashboard"
    return match.group(1)


def _jpeg_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (900, 700), (90, 60, 30)).save(out, format="JPEG")
    return out.getvalue()


def test_admin_requires_login(client):
    resp = client.get("/admin/")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_wrong_password_rejected_generically(client):
    resp = client.post(
        "/admin/login", data={"username": "zee", "password": "wrong password"},
    )
    body = resp.get_data(as_text=True)
    assert "Invalid credentials" in body
    resp2 = client.post(
        "/admin/login", data={"username": "ghost", "password": "wrong password"},
    )
    assert "Invalid credentials" in resp2.get_data(as_text=True)


def test_post_without_csrf_rejected(client):
    _login(client)
    resp = client.post("/admin/items/new", data={"name": "Sneaky"})
    assert resp.status_code == 400


def test_full_item_lifecycle(client):
    csrf = _login(client)

    # create (with a unit-suffixed measurement)
    resp = client.post(
        "/admin/items/new",
        data={"csrf_token": csrf, "name": "Test Gadget", "kind": "Gadget",
              "length": "6 ft", "part_count": "12"},
        follow_redirects=False,
    )
    assert resp.status_code == 302 and "/items/0001/edit" in resp.headers["Location"]

    # stored in canonical unit
    edit_page = client.get("/admin/items/0001/edit").get_data(as_text=True)
    assert 'value="182.88"' in edit_page

    # validation error round-trips with message
    resp = client.post(
        "/admin/items/0001/edit",
        data={"csrf_token": csrf, "name": "", "kind": "Gadget",
              "length": "abc", "part_count": ""},
    )
    body = resp.get_data(as_text=True)
    assert "required" in body and resp.status_code == 200

    # image upload
    resp = client.post(
        "/admin/items/0001/images",
        data={"csrf_token": csrf,
              "images": (io.BytesIO(_jpeg_bytes()), "photo.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "uploaded" in body
    match = re.search(r"/images/([0-9a-f]{64})/thumb", body)
    assert match

    # public image serving + immutability headers
    img = client.get(f"/images/{match.group(1)}/display")
    assert img.status_code == 200
    assert "immutable" in img.headers["Cache-Control"]

    # traversal / bad variant rejected
    assert client.get(f"/images/{match.group(1)}/../full").status_code == 404
    assert client.get(f"/images/{match.group(1)}/raw").status_code == 404
    assert client.get("/images/AAAA/thumb").status_code == 404

    # public detail page shows the conditional group
    detail = client.get("/item/0001").get_data(as_text=True)
    assert "Test Gadget" in detail and "Gadget Details" in detail

    # delete
    resp = client.post(
        "/admin/items/0001/delete", data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert client.get("/item/0001").status_code == 404


def test_upload_rejects_garbage(client):
    csrf = _login(client)
    client.post(
        "/admin/items/new",
        data={"csrf_token": csrf, "name": "Holder", "kind": "Widget"},
    )
    resp = client.post(
        "/admin/items/0001/images",
        data={"csrf_token": csrf,
              "images": (io.BytesIO(b"#!/bin/sh\nrm -rf /"), "evil.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "unsupported image format" in resp.get_data(as_text=True)


def test_totp_login_flow(app, client):
    # regression for the /admin/login/totp 500: full two-step login through HTTP
    import time

    import pyotp

    from curio_cabinet import auth
    from curio_cabinet.db import connect

    inst = app.config["CABINET_INSTANCE"]
    conn = connect(inst.db_path, journal_mode=inst.journal_mode)
    uid = conn.execute("SELECT id FROM users").fetchone()[0]
    auth.begin_totp_enrollment(conn, uid)
    secret = conn.execute("SELECT totp_secret FROM users").fetchone()[0]
    totp = pyotp.TOTP(secret)
    assert auth.confirm_totp_enrollment(conn, uid, totp.now())
    conn.close()

    r = client.post("/admin/login", data={"username": "zee", "password": PW})
    assert r.status_code == 302 and "/login/totp" in r.headers["Location"]

    code = totp.at(int(time.time()) + 30)  # next step (enrollment used the current one)
    r = client.post("/admin/login/totp", data={"code": code})
    assert r.status_code == 302
    assert r.headers["Location"].rstrip("/").endswith("/admin")
    assert client.get("/admin/").status_code == 200  # authenticated


def test_logout(client):
    csrf = _login(client)
    resp = client.post("/admin/logout", data={"csrf_token": csrf})
    assert resp.status_code == 302
    assert client.get("/admin/").status_code == 302  # back to login


def test_customize_general_live(app, client):
    csrf = _login(client)
    r = client.post("/admin/customize/general", data={
        "csrf_token": csrf, "title": "My Cabinet",
        "title_field": "name", "sort_field": "name", "sort_order": "asc",
        "accent": "#3b6fd4",
    })
    assert r.status_code == 302
    # change is live immediately (config hot-swapped, no restart)
    page = client.get("/").get_data(as_text=True)
    assert "My Cabinet" in page
    css = client.get("/theme.css").get_data(as_text=True)
    assert "--accent-override: #3b6fd4" in css  # picked color applied
    # the stylesheet link is versioned by the accent, so browsers refetch
    # /theme.css on a color change instead of serving the hour-long cache
    assert "/theme.css?v=3b6fd4" in page


def test_customize_add_field_live(app, client):
    csrf = _login(client)
    r = client.post("/admin/customize/field/new", data={
        "csrf_token": csrf, "key": "color", "label": "Colorway", "type": "text",
        "group": "core",
    })
    assert r.status_code == 302
    # new field is usable on the edit form right away
    form = client.get("/admin/items/new").get_data(as_text=True)
    assert 'name="color"' in form and "Colorway" in form


def test_customize_requires_login(client):
    assert client.get("/admin/customize").status_code == 302  # -> login


def test_security_headers_present(client):
    resp = client.get("/")
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "same-origin"


def test_admin_nav_on_every_admin_page(client):
    _login(client)
    for path in ("/admin/", "/admin/customize", "/admin/settings",
                 "/admin/import", "/admin/items/new"):
        body = client.get(path).get_data(as_text=True)
        for marker in ('href="/admin/customize"', 'href="/admin/settings"',
                       ">Sign out</button>"):
            assert marker in body, f"{marker} missing on {path}"
