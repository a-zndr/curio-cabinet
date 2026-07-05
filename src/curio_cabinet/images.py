"""Image pipeline: validation, re-encoding, derivatives, storage.

Trust boundary rules:
- Stored bytes are ALWAYS our own Pillow output. Uploads are sniffed,
  verified, re-opened, EXIF-transposed, converted to RGB, and re-encoded —
  which strips metadata (GPS!) and any polyglot payload.
- Filenames are content hashes of the re-encoded master; user input never
  names a file. Serving validates hash + variant against strict patterns
  before any filesystem access.
- Decompression bombs are rejected before full decode via a pixel cap.

HEIC/HEIF is accepted only when the optional pillow-heif extra is
installed (Docker adopters); otherwise it's rejected with a clear message.
iOS Safari transcodes HEIC to JPEG automatically when the file input's
accept list omits HEIC, so phone uploads rarely hit this.
"""

from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .db import utcnow

try:  # optional extra: pip install curio-cabinet[heic]
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

__all__ = [
    "UploadError",
    "HEIC_SUPPORTED",
    "VARIANTS",
    "StoredImage",
    "process_upload",
    "variant_path",
    "safe_variant_path",
    "delete_image_files",
    "add_image",
    "images_for_item",
    "primary_image",
    "set_position",
    "set_focal_point",
    "remove_image",
]

MAX_PIXELS = 50_000_000  # ~50 Mpix; a phone photo is ~12-48 Mpix
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

# variant name -> (max long edge, None) or exact (w, h) for crops
VARIANTS: dict[str, tuple[int, int] | int] = {
    "full": 2000,
    "display": 1200,
    "thumb": 400,
    "og": (1200, 630),
}

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_JPEG_QUALITY = 82

_MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"RIFF": "webp",  # + WEBP at offset 8, checked below
}


class UploadError(ValueError):
    """User-facing upload rejection; message is safe to display."""


@dataclass(frozen=True)
class StoredImage:
    content_hash: str
    width: int
    height: int


def _sniff(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypheif", b"ftypmif1"):
        if HEIC_SUPPORTED:
            return "heif"
        raise UploadError(
            "HEIC images aren't supported on this server — please export "
            "as JPEG and try again."
        )
    raise UploadError("unsupported image format (use JPEG, PNG, or WebP)")


def _decode(data: bytes) -> Image.Image:
    _sniff(data)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            probe = Image.open(io.BytesIO(data))
            probe.verify()
        except UploadError:
            raise
        except Image.DecompressionBombWarning:
            raise UploadError("image is too large (pixel count)") from None
        except Exception:
            raise UploadError("file is not a valid image") from None
        try:
            image = Image.open(io.BytesIO(data))
            if image.width * image.height > MAX_PIXELS:
                raise UploadError("image is too large (pixel count)")
            image = ImageOps.exif_transpose(image)
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.load()
        except UploadError:
            raise
        except Image.DecompressionBombWarning:
            raise UploadError("image is too large (pixel count)") from None
        except Exception:
            raise UploadError("file is not a valid image") from None
    return image


def _encode_jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _scaled(image: Image.Image, long_edge: int) -> Image.Image:
    if max(image.size) <= long_edge:
        return image
    copy = image.copy()
    copy.thumbnail((long_edge, long_edge), Image.LANCZOS)
    return copy


def _dominant_color(image: Image.Image) -> tuple[int, int, int]:
    r, g, b = image.resize((1, 1), Image.LANCZOS).getpixel((0, 0))[:3]
    return (r, g, b)


def _og_crop(
    image: Image.Image, focal: tuple[float, float] | None
) -> Image.Image:
    """1200x630 crop. Very-portrait sources letterbox on the dominant color
    instead of cropping most of the subject away."""
    w, h = VARIANTS["og"]  # type: ignore[misc]
    src_ratio = image.width / image.height
    target_ratio = w / h
    if src_ratio < target_ratio * 0.62:  # cropping would discard >~38% of height
        canvas = Image.new("RGB", (w, h), _dominant_color(image))
        scaled = _scaled(image, h)
        if scaled.height > h:
            scaled = scaled.resize(
                (int(scaled.width * h / scaled.height), h), Image.LANCZOS
            )
        canvas.paste(scaled, ((w - scaled.width) // 2, (h - scaled.height) // 2))
        return canvas
    centering = focal if focal else (0.5, 0.45)
    return ImageOps.fit(image, (w, h), Image.LANCZOS, centering=centering)


def process_upload(
    data: bytes,
    images_dir: str | Path,
    *,
    focal: tuple[float, float] | None = None,
) -> StoredImage:
    """Validate, re-encode, and write all variants. Returns the master record."""
    image = _decode(data)

    master = _scaled(image, VARIANTS["full"])  # type: ignore[arg-type]
    master_bytes = _encode_jpeg(master)
    content_hash = hashlib.sha256(master_bytes).hexdigest()

    directory = Path(images_dir) / content_hash[:2]
    directory.mkdir(parents=True, exist_ok=True)

    (directory / f"{content_hash}_full.jpg").write_bytes(master_bytes)
    for variant in ("display", "thumb"):
        edge: int = VARIANTS[variant]  # type: ignore[assignment]
        (directory / f"{content_hash}_{variant}.jpg").write_bytes(
            _encode_jpeg(_scaled(master, edge))
        )
    (directory / f"{content_hash}_og.jpg").write_bytes(
        _encode_jpeg(_og_crop(master, focal))
    )
    return StoredImage(content_hash, master.width, master.height)


def variant_path(images_dir: str | Path, content_hash: str, variant: str) -> Path:
    return Path(images_dir) / content_hash[:2] / f"{content_hash}_{variant}.jpg"


def safe_variant_path(
    images_dir: str | Path, content_hash: str, variant: str
) -> Path | None:
    """Strictly validated path for the public serving route, or None.

    hash must be 64 lowercase hex chars; variant must be in the literal
    allowlist. The path is built only from validated tokens — request
    strings never touch the filesystem otherwise.
    """
    if not _HASH_RE.fullmatch(content_hash) or variant not in VARIANTS:
        return None
    path = variant_path(images_dir, content_hash, variant)
    return path if path.is_file() else None


def regenerate_og(
    images_dir: str | Path, content_hash: str, focal: tuple[float, float] | None
) -> None:
    full = variant_path(images_dir, content_hash, "full")
    image = Image.open(full)
    image.load()
    variant_path(images_dir, content_hash, "og").write_bytes(
        _encode_jpeg(_og_crop(image, focal))
    )


def delete_image_files(images_dir: str | Path, content_hash: str) -> None:
    for variant in VARIANTS:
        variant_path(images_dir, content_hash, variant).unlink(missing_ok=True)


# DB records -----------------------------------------------------------------


def add_image(
    conn: sqlite3.Connection, item_id: str, stored: StoredImage
) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM images WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    cursor = conn.execute(
        "INSERT INTO images (item_id, content_hash, width, height, position, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, stored.content_hash, stored.width, stored.height, row[0], utcnow()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def images_for_item(conn: sqlite3.Connection, item_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM images WHERE item_id = ? ORDER BY position, id",
        (item_id,),
    ).fetchall()


def primary_image(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    rows = images_for_item(conn, item_id)
    return rows[0] if rows else None


def set_position(
    conn: sqlite3.Connection, item_id: str, image_id: int, new_position: int
) -> None:
    """Move an image within an item's gallery (0 = primary)."""
    rows = images_for_item(conn, item_id)
    ids = [r["id"] for r in rows]
    if image_id not in ids:
        return
    ids.remove(image_id)
    new_position = max(0, min(new_position, len(ids)))
    ids.insert(new_position, image_id)
    for position, iid in enumerate(ids):
        conn.execute("UPDATE images SET position = ? WHERE id = ?", (position, iid))
    conn.commit()


def set_focal_point(
    conn: sqlite3.Connection,
    images_dir: str | Path,
    image_id: int,
    focal_x: float,
    focal_y: float,
) -> bool:
    """Set an image's focal point and regenerate its OG crop.

    Returns False (without regenerating) when the same photo is used by other
    items: OG files are content-addressed and shared, so rewriting one would
    silently change every item using that image. The caller can surface this.
    """
    focal_x = max(0.0, min(1.0, focal_x))
    focal_y = max(0.0, min(1.0, focal_y))
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE images SET focal_x = ?, focal_y = ? WHERE id = ?",
        (focal_x, focal_y, image_id),
    )
    conn.commit()
    (shared,) = conn.execute(
        "SELECT COUNT(*) FROM images WHERE content_hash = ? AND id != ?",
        (row["content_hash"], image_id),
    ).fetchone()
    if shared:
        return False
    regenerate_og(images_dir, row["content_hash"], (focal_x, focal_y))
    return True


def remove_image(
    conn: sqlite3.Connection, images_dir: str | Path, image_id: int
) -> None:
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
    conn.commit()
    (still_used,) = conn.execute(
        "SELECT COUNT(*) FROM images WHERE content_hash = ?", (row["content_hash"],)
    ).fetchone()
    if not still_used:
        delete_image_files(images_dir, row["content_hash"])
