from curio_cabinet.csvio import export_csv, import_csv, next_id
from tests.conftest import insert_thing


CSV_BY_LABEL = """\
Name,Kind,Length,Materials,Active
Alpha,widget,24 in,"leather, wood",yes
Beta,Gadget,50,nylon,no
"""

CSV_WITH_ERRORS = """\
name,length
Good,100
,50
Bad,not-a-length
"""


def test_import_by_label_header(conn, registry):
    report = import_csv(conn, registry, CSV_BY_LABEL)
    assert report.imported == 2 and report.skipped == 0
    rows = conn.execute('SELECT * FROM "things" ORDER BY "id"').fetchall()
    assert rows[0]["id"] == "0001"
    assert rows[0]["kind"] == "Widget"  # enum case normalized
    assert rows[0]["length"] == 60.96  # 24 in -> cm
    assert rows[0]["materials"] == '["leather", "wood"]' or "leather" in rows[0]["materials"]
    assert rows[0]["active"] == 1
    assert rows[1]["active"] == 0


def test_import_reports_errors_and_skips(conn, registry):
    report = import_csv(conn, registry, CSV_WITH_ERRORS)
    assert report.imported == 1
    assert report.skipped == 2
    assert any("required" in e for e in report.errors)
    assert any("not-a-length" in e or "length" in e for e in report.errors)


def test_dry_run_writes_nothing(conn, registry):
    report = import_csv(conn, registry, CSV_BY_LABEL, dry_run=True)
    assert report.imported == 2
    (n,) = conn.execute('SELECT COUNT(*) FROM "things"').fetchone()
    assert n == 0


def test_next_id_zero_padded(conn, registry):
    assert next_id(conn, registry) == "0001"
    insert_thing(conn, "0041", name="X")
    assert next_id(conn, registry) == "0042"


def test_export_round_trip(conn, registry):
    import_csv(conn, registry, CSV_BY_LABEL)
    text = export_csv(conn, registry)
    assert text.splitlines()[0].startswith("id,name,kind,length")
    assert "leather, wood" in text

    # re-import what we exported into a fresh table
    conn.execute('DELETE FROM "things"')
    report = import_csv(conn, registry, text)
    assert report.imported == 2 and report.skipped == 0
