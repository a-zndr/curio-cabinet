"""Admin CSV import flow (upload -> dry-run preview -> apply) over HTTP."""

import io
import re

import pytest

from curio_cabinet import auth
from curio_cabinet.app import create_app
from curio_cabinet.db import connect, ensure_engine_tables

CONFIG = """\
collection:
  title: "Import Test"
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

groups:
  - key: core
    label: Core
    fields: [name, kind]
"""

PW = "a sufficiently long password"

CSV_OK = "name,kind\nAlpha,Widget\nBeta,Gadget\n"


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
    assert match, "csrf token not found"
    return match.group(1)


def _count(app) -> int:
    inst = app.config["CABINET_INSTANCE"]
    conn = connect(inst.db_path, journal_mode=inst.journal_mode)
    (n,) = conn.execute('SELECT COUNT(*) FROM "things"').fetchone()
    conn.close()
    return n


def _post_csv(client, csrf, payload: bytes, filename="items.csv"):
    return client.post(
        "/admin/import",
        data={"csrf_token": csrf, "file": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )


def test_import_requires_login(client):
    resp = client.get("/admin/import")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_preview_then_apply(app, client):
    csrf = _login(client)
    resp = _post_csv(client, csrf, CSV_OK.encode())
    body = resp.get_data(as_text=True)
    assert "ready to import" in body and "items.csv" in body
    assert "Matched columns: name, kind" in body
    assert _count(app) == 0  # preview is a dry run

    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', body).group(1)
    resp = client.post(
        "/admin/import/apply",
        data={"csrf_token": csrf, "digest": digest},
        follow_redirects=True,
    )
    assert "Imported 2 items" in resp.get_data(as_text=True)
    assert _count(app) == 2


def test_apply_with_stale_digest_rejected(app, client):
    csrf = _login(client)
    _post_csv(client, csrf, CSV_OK.encode())
    resp = client.post(
        "/admin/import/apply",
        data={"csrf_token": csrf, "digest": "0" * 64},
        follow_redirects=True,
    )
    assert "upload it again" in resp.get_data(as_text=True)
    assert _count(app) == 0


def test_apply_without_pending_file_rejected(app, client):
    csrf = _login(client)
    resp = client.post(
        "/admin/import/apply",
        data={"csrf_token": csrf, "digest": "0" * 64},
        follow_redirects=True,
    )
    assert "Nothing to import" in resp.get_data(as_text=True)
    assert _count(app) == 0


def test_preview_reports_bad_rows(client):
    csrf = _login(client)
    resp = _post_csv(client, csrf, b"name,kind\n,Widget\n")
    body = resp.get_data(as_text=True)
    assert "line 2" in body and "required" in body
    assert "Nothing importable" in body


def test_row_cap_defers_to_cli(client, monkeypatch):
    from curio_cabinet.views import admin as admin_views

    monkeypatch.setattr(admin_views, "MAX_IMPORT_ROWS", 2)
    csrf = _login(client)
    big = "name\n" + "\n".join(f"row{i}" for i in range(10))
    resp = _post_csv(client, csrf, big.encode(), filename="big.csv")
    assert resp.status_code == 302  # flash + redirect, not a preview
    page = client.get("/admin/import").get_data(as_text=True)
    assert "command line" in page


def test_cp1252_fallback_decodes_with_note(app, client):
    csrf = _login(client)
    resp = _post_csv(client, csrf, "name,kind\nCafé,Widget\n".encode("cp1252"))
    body = resp.get_data(as_text=True)
    assert "Windows-1252" in body
    assert "Café" in body or "ready to import" in body


def test_crlf_csv_survives_preview_and_apply(app, client):
    # Excel writes CRLF; the digest must round-trip the stash byte-exactly
    csrf = _login(client)
    resp = _post_csv(client, csrf, b"name,kind\r\nAlpha,Widget\r\nBeta,Gadget\r\n")
    body = resp.get_data(as_text=True)
    digest = re.search(r'name="digest" value="([0-9a-f]{64})"', body).group(1)
    resp = client.post(
        "/admin/import/apply",
        data={"csrf_token": csrf, "digest": digest},
        follow_redirects=True,
    )
    assert "Imported 2 items" in resp.get_data(as_text=True)
    assert _count(app) == 2


def test_huge_cell_reports_cleanly_not_500(app, client):
    csrf = _login(client)
    payload = ('name\n"' + "x" * 200_000 + '"\n').encode()
    resp = _post_csv(client, csrf, payload, filename="huge.csv")
    assert resp.status_code == 200  # preview page, not a server error
    body = resp.get_data(as_text=True)
    assert "not a readable CSV" in body and "Nothing importable" in body
    assert _count(app) == 0


def test_utf16_bom_upload_decodes(client):
    csrf = _login(client)
    resp = _post_csv(client, csrf, "name,kind\nAlpha,Widget\n".encode("utf-16"))
    body = resp.get_data(as_text=True)
    assert "ready to import" in body
    assert "Windows-1252" not in body  # decoded properly, no mojibake note


def test_csv_import_fills_computed_fields():
    # computed fields must be populated on import (regression: apply_computed
    # was imported but never called)
    import tempfile
    from pathlib import Path

    from curio_cabinet.config import CollectionConfig
    from curio_cabinet.csvio import import_csv
    from curio_cabinet.db import connect, ensure_engine_tables
    from curio_cabinet.registry import FieldRegistry
    from curio_cabinet.schema import rebuild

    cfg = CollectionConfig.from_raw({
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
    })
    reg = FieldRegistry(cfg)
    conn = connect(Path(tempfile.mkdtemp()) / "t.db")
    ensure_engine_tables(conn)
    rebuild(conn, reg)
    report = import_csv(conn, reg, "name,weight,length\nA,343.9,60\n")
    assert report.imported == 1
    (wpm,) = conn.execute('SELECT wpm FROM things').fetchone()
    assert wpm == 573.17
