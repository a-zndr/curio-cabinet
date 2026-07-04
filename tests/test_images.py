import io
import sqlite3

import pytest
from PIL import Image

from curio_cabinet import images
from curio_cabinet.db import ensure_engine_tables


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_engine_tables(conn)
    return conn


def _jpeg(width=800, height=600, color=(120, 80, 40), exif=False, gradient=False) -> bytes:
    img = Image.new("RGB", (width, height), color)
    if gradient:
        for x in range(0, width, max(1, width // 50)):
            for y in range(height):
                img.putpixel((x, y), (x * 255 // width, 90, 90))
    out = io.BytesIO()
    if exif:
        exif_data = Image.Exif()
        exif_data[0x0112] = 6  # orientation: rotate 270
        img.save(out, format="JPEG", exif=exif_data)
    else:
        img.save(out, format="JPEG")
    return out.getvalue()


def _png(width=100, height=100) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), (0, 200, 0, 128)).save(out, format="PNG")
    return out.getvalue()


def test_process_upload_writes_all_variants(tmp_path):
    stored = images.process_upload(_jpeg(3000, 2000), tmp_path)
    assert stored.width == 2000 and stored.height == 1333  # downscaled to long edge
    for variant in images.VARIANTS:
        path = images.variant_path(tmp_path, stored.content_hash, variant)
        assert path.is_file(), variant
    og = Image.open(images.variant_path(tmp_path, stored.content_hash, "og"))
    assert og.size == (1200, 630)


def test_reencode_strips_exif_and_applies_orientation(tmp_path):
    stored = images.process_upload(_jpeg(800, 600, exif=True), tmp_path)
    # orientation 6 rotates: stored master is portrait now
    assert (stored.width, stored.height) == (600, 800)
    master = Image.open(images.variant_path(tmp_path, stored.content_hash, "full"))
    assert dict(master.getexif()) == {}


def test_png_and_alpha_flattened(tmp_path):
    stored = images.process_upload(_png(), tmp_path)
    master = Image.open(images.variant_path(tmp_path, stored.content_hash, "full"))
    assert master.mode == "RGB"


def test_garbage_rejected(tmp_path):
    with pytest.raises(images.UploadError, match="unsupported"):
        images.process_upload(b"MZ\x90\x00 not an image", tmp_path)
    # valid PNG magic, corrupt body: a polyglot-style fake
    with pytest.raises(images.UploadError, match="not a valid image"):
        images.process_upload(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, tmp_path)


def test_heic_rejected_without_extra(tmp_path):
    fake_heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32
    if images.HEIC_SUPPORTED:
        pytest.skip("pillow-heif installed")
    with pytest.raises(images.UploadError, match="HEIC"):
        images.process_upload(fake_heic, tmp_path)


def test_pixel_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 10_000)
    with pytest.raises(images.UploadError, match="too large"):
        images.process_upload(_jpeg(200, 200), tmp_path)


def test_safe_variant_path_validation(tmp_path):
    stored = images.process_upload(_jpeg(), tmp_path)
    ok = images.safe_variant_path(tmp_path, stored.content_hash, "thumb")
    assert ok is not None and ok.is_file()

    assert images.safe_variant_path(tmp_path, "../../../etc/passwd", "thumb") is None
    assert images.safe_variant_path(tmp_path, stored.content_hash.upper(), "thumb") is None
    assert images.safe_variant_path(tmp_path, stored.content_hash, "raw") is None
    assert images.safe_variant_path(tmp_path, stored.content_hash, "../full") is None
    assert images.safe_variant_path(tmp_path, "a" * 64, "thumb") is None  # valid shape, no file


def test_portrait_og_letterboxes(tmp_path):
    stored = images.process_upload(_jpeg(500, 2000), tmp_path)
    og = Image.open(images.variant_path(tmp_path, stored.content_hash, "og"))
    assert og.size == (1200, 630)
    # letterboxed: corners are dominant-color fill, not subject
    assert og.getpixel((5, 5)) == og.getpixel((1195, 5))


def test_db_gallery_lifecycle(tmp_path, db):
    a = images.process_upload(_jpeg(color=(10, 10, 10)), tmp_path)
    b = images.process_upload(_jpeg(color=(200, 200, 200)), tmp_path)
    ida = images.add_image(db, "0001", a)
    idb = images.add_image(db, "0001", b)

    gallery = images.images_for_item(db, "0001")
    assert [r["id"] for r in gallery] == [ida, idb]
    assert images.primary_image(db, "0001")["id"] == ida

    images.set_position(db, "0001", idb, 0)  # "make primary"
    assert images.primary_image(db, "0001")["id"] == idb

    images.remove_image(db, tmp_path, idb)
    assert images.primary_image(db, "0001")["id"] == ida
    assert not images.variant_path(tmp_path, b.content_hash, "full").exists()
    assert images.variant_path(tmp_path, a.content_hash, "full").exists()


def test_shared_hash_files_survive_one_removal(tmp_path, db):
    data = _jpeg(color=(1, 2, 3))
    stored = images.process_upload(data, tmp_path)
    id1 = images.add_image(db, "0001", stored)
    images.add_image(db, "0002", images.process_upload(data, tmp_path))

    images.remove_image(db, tmp_path, id1)
    # the other item still references the same content hash
    assert images.variant_path(tmp_path, stored.content_hash, "full").exists()


def test_focal_point_regenerates_og(tmp_path, db):
    stored = images.process_upload(_jpeg(2000, 1000, gradient=True), tmp_path)
    image_id = images.add_image(db, "0001", stored)
    og_path = images.variant_path(tmp_path, stored.content_hash, "og")
    before = og_path.read_bytes()
    images.set_focal_point(db, tmp_path, image_id, 0.0, 0.0)
    row = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    assert (row["focal_x"], row["focal_y"]) == (0.0, 0.0)
    assert og_path.read_bytes() != before
