#!/usr/bin/env python3
"""Migrate a V1 toy-db (Flask + SQLite `toys` table) into a Curio-Cabinet
instance built from examples/impact-toys/collection.yaml.

Everything runs through the engine's one coercion path, so the same unit
parsing / validation that guards admin writes also guards this import.

Known V1 data hazards, all handled explicitly (see the review):
  * described_length_ft holds messy strings ("45 in", "24 inch", "36”",
    "3ft", bare numbers) -> unit parser; unknown suffix is a hard error.
  * rear_balance_pct stores fractions (0.8578, 1.0) -> multiplied by 100.
  * length_in vs length_cm disagree on rows 0049/0058 -> cm wins (the
    canonical stored unit), and the conflict is reported, not silent.
  * fall/handle are "true"/NULL text -> booleans.
  * materials is a comma string -> tags.
  * the single image filename -> a row in the images table (processed
    through the real pipeline so derivatives + OG crops exist).

Usage:
    python scripts/migrate_v1.py \
        --v1-db ../toy-db/toys.db \
        --v1-images ../toy-db/images \
        --instance /path/to/instance [--force]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from curio_cabinet import images as image_pipeline  # noqa: E402
from curio_cabinet.coerce import CoercionError, coerce_value  # noqa: E402
from curio_cabinet.db import connect, ensure_engine_tables, utcnow  # noqa: E402
from curio_cabinet.instance import resolve_instance  # noqa: E402
from curio_cabinet.schema import detect_drift, rebuild  # noqa: E402

# V1 column -> V2 field key. Columns absent here are intentionally dropped
# (length_in is redundant with length_cm; both feed the single length field).
COLUMN_MAP = {
    "toy_id": "id",
    "maker": "maker",
    "maker_web_link": "maker_web_link",
    "type": "type",
    "description": "description",
    "materials": "materials",
    "length_cm": "length",             # canonical; length_in reconciled below
    "described_length_ft": "described_length",
    "weight_g": "weight",
    "weight_g_per_m": "weight_per_m",
    "balance_point_cm": "balance_point",
    "fall": "fall",
    "handle": "handle",
    "whip_type": "whip_type",
    "plait_count": "plait_count",
    "fall_count": "fall_count",
    "cracker_attachment": "cracker_attachment",
    # V1 had two fall-length columns; in practice only _2 was ever filled and
    # they never coexist, so they merge into one field (primary wins if both).
    "fall_length_cm": "fall_length",
    "fall_length_cm_2": "fall_length",
    "fall_notes": "fall_notes",
    "heal_knot_d_mm": "heel_knot_diameter",
    "max_diam_mm": "max_diameter",
    "min_diam_mm": "min_diameter",
    "rear_balance_pct": "rear_balance",
    "notes": "notes",
    "usage_comments": "usage_comments",
}

WHIP_TYPE_FIXES = {"Hybird Signal": "Hybrid Signal", "Bull Whip": "Bull Whip"}


def build_row(v1: sqlite3.Row, registry, reports: list[str]) -> tuple[str, dict]:
    by_key = registry.by_key
    out: dict = {}
    item_id = v1["toy_id"]

    for v1col, key in COLUMN_MAP.items():
        if key == "id":
            continue
        raw = v1[v1col]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        if key in out:
            continue  # a merged target already filled by an earlier column

        if key == "rear_balance":
            raw = float(raw) * 100.0  # fraction -> percent
        elif key == "whip_type":
            raw = WHIP_TYPE_FIXES.get(raw, raw)

        field = by_key[key]
        try:
            out[key] = coerce_value(field, raw)
        except CoercionError as exc:
            reports.append(f"  {item_id}: {key}: {exc.reason} (got {raw!r}) -> skipped")

    # length reconciliation: cm is canonical; report disagreements with inches
    if v1["length_in"] is not None and v1["length_cm"] is not None:
        implied = round(float(v1["length_in"]) * 2.54, 1)
        if abs(implied - float(v1["length_cm"])) > 1.0:
            reports.append(
                f"  {item_id}: length conflict — {v1['length_in']}in implies "
                f"{implied}cm but stored cm={v1['length_cm']}; kept cm"
            )
    return item_id, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1-db", required=True, type=Path)
    ap.add_argument("--v1-images", required=True, type=Path)
    ap.add_argument("--instance", required=True)
    ap.add_argument("--force", action="store_true",
                    help="import even if the items table is non-empty")
    args = ap.parse_args()

    inst = resolve_instance(args.instance)
    registry = inst.registry
    conn = connect(inst.db_path, journal_mode=inst.journal_mode)
    ensure_engine_tables(conn)
    if detect_drift(conn, registry).kind != "match":
        rebuild(conn, registry)

    (existing,) = conn.execute(f'SELECT COUNT(*) FROM "{registry.table}"').fetchone()
    if existing and not args.force:
        print(f"items table already has {existing} rows; use --force to add anyway")
        return 1

    v1 = sqlite3.connect(args.v1_db)
    v1.row_factory = sqlite3.Row
    rows = v1.execute("SELECT * FROM toys ORDER BY toy_id").fetchall()

    reports: list[str] = []
    imported = 0
    max_seen = 0
    for v1row in rows:
        item_id, values = build_row(v1row, registry, reports)
        cols = ["id", *values.keys(), "created_at", "updated_at"]
        quoted = ", ".join(f'"{c}"' for c in cols)
        marks = ", ".join("?" for _ in cols)
        now = utcnow()
        conn.execute(
            f'INSERT OR REPLACE INTO "{registry.table}" ({quoted}) VALUES ({marks})',
            [item_id, *values.values(), now, now],
        )
        imported += 1
        try:
            max_seen = max(max_seen, int(item_id))
        except ValueError:
            pass

        # image: process the single V1 filename through the real pipeline
        fname = v1row["image"]
        if fname:
            src = _find_image(args.v1_images, fname)
            if src is None:
                reports.append(f"  {item_id}: image {fname!r} not found on disk")
            elif not image_pipeline.images_for_item(conn, item_id):
                try:
                    stored = image_pipeline.process_upload(
                        src.read_bytes(), inst.images_dir
                    )
                    image_pipeline.add_image(conn, item_id, stored)
                except image_pipeline.UploadError as exc:
                    reports.append(f"  {item_id}: image {fname!r}: {exc}")
    conn.commit()

    print(f"imported {imported} items (ids up to {max_seen:04d})")
    if reports:
        print(f"\n{len(reports)} notes:")
        print("\n".join(reports))
    else:
        print("no data issues")
    return 0


def _find_image(images_dir: Path, filename: str) -> Path | None:
    """V1 filenames drift in case/spacing; match forgivingly (per V1's
    own match_images.py behavior)."""
    direct = images_dir / filename
    if direct.is_file():
        return direct
    target = filename.lower().strip()
    for candidate in images_dir.iterdir():
        if candidate.name.lower().strip() == target:
            return candidate
    stem = Path(target).stem
    for candidate in images_dir.iterdir():
        if candidate.stem.lower().strip() == stem:
            return candidate
    return None


if __name__ == "__main__":
    raise SystemExit(main())
