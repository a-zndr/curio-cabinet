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


def test_dry_run_flags_id_collisions(conn, registry):
    insert_thing(conn, "0001", name="Existing")
    text = "id,name\n0001,DupOfExisting\n0002,Ok\n0002,DupInFile\nbad*id,Chars\n"
    report = import_csv(conn, registry, text, dry_run=True)
    assert report.imported == 1 and report.skipped == 3
    assert any("already exists" in e for e in report.errors)
    assert any("appears twice" in e for e in report.errors)
    assert any("letters, digits" in e for e in report.errors)
    (n,) = conn.execute('SELECT COUNT(*) FROM "things"').fetchone()
    assert n == 1  # dry run wrote nothing


def test_live_run_matches_dry_run_on_collisions(conn, registry):
    insert_thing(conn, "0001", name="Existing")
    text = "id,name\n0001,DupOfExisting\n0002,Ok\n0002,DupInFile\n"
    dry = import_csv(conn, registry, text, dry_run=True)
    live = import_csv(conn, registry, text)
    assert (dry.imported, dry.skipped) == (live.imported, live.skipped) == (1, 2)
    rows = conn.execute('SELECT "id", "name" FROM "things" ORDER BY "id"').fetchall()
    assert [(r["id"], r["name"]) for r in rows] == [("0001", "Existing"), ("0002", "Ok")]


def test_skipped_row_does_not_claim_its_id(conn, registry):
    # line 2 is invalid (missing required name) but carries id 0007; the
    # valid line 3 reusing 0007 must import, matching what a real run does
    text = "id,name\n0007,\n0007,Valid\n"
    report = import_csv(conn, registry, text)
    assert report.imported == 1 and report.skipped == 1
    row = conn.execute('SELECT "name" FROM "things" WHERE "id" = ?', ("0007",)).fetchone()
    assert row["name"] == "Valid"


def test_report_lists_mapped_columns(conn, registry):
    report = import_csv(conn, registry, CSV_BY_LABEL, dry_run=True)
    assert report.mapped == ["name", "kind", "length", "materials", "active"]


def test_huge_cell_is_a_clean_error_and_rolls_back(conn, registry):
    text = 'name\nBefore\n"' + "x" * 200_000 + '"\nAfter\n'
    report = import_csv(conn, registry, text)
    assert report.imported == 0
    assert any("not a readable CSV" in e for e in report.errors)
    (n,) = conn.execute('SELECT COUNT(*) FROM "things"').fetchone()
    assert n == 0  # the row before the bad cell was rolled back, not kept
