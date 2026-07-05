"""Live config editing from the admin UI (the Customize page).

Presentation edits — names, accent color, per-field view flags, enum values,
group labels, presets — apply instantly with no schema change. Adding a field
runs the same additive rebuild-migration the CLI uses. Destructive changes
(removing/renaming/retyping a field, unit changes) are REFUSED here; those stay
a deliberate CLI operation so a stray click can't drop column data.
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import yaml

from .config import CollectionConfig, ConfigError
from .db import connect, ensure_engine_tables
from .instance import Instance, resolve_instance
from .registry import FieldRegistry
from .schema import backup_database, detect_drift, rebuild

__all__ = ["ConfigEditError", "load_raw", "apply_config"]


class ConfigEditError(ValueError):
    """A rejected config edit; message is safe to show the admin."""


def load_raw(instance: Instance) -> dict:
    """The current collection.yaml as a plain dict (round-tripped, so edits
    touch only the keys they mean to)."""
    text = (instance.root / "collection.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def _backup_yaml(instance: Instance) -> Path:
    src = instance.root / "collection.yaml"
    bdir = instance.root / "data" / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = bdir / f"collection-{stamp}.yaml"
    n = 0
    while dst.exists():
        n += 1
        dst = bdir / f"collection-{stamp}-{n}.yaml"
    shutil.copy2(src, dst)
    return dst


def apply_config(instance: Instance, new_raw: dict) -> Instance:
    """Validate and apply an edited config; return the reloaded Instance.

    Raises ConfigEditError if the config is invalid or the change would alter
    the database destructively.
    """
    try:
        new_config = CollectionConfig.from_raw(new_raw)
    except (ConfigError, ValueError) as exc:
        raise ConfigEditError(str(exc)) from None

    new_registry = FieldRegistry(new_config)
    conn = connect(instance.db_path, journal_mode=instance.journal_mode)
    try:
        ensure_engine_tables(conn)
        drift = detect_drift(conn, new_registry)
        if drift.kind == "destructive":
            raise ConfigEditError(
                f"that edit would change the database ({drift.describe()}) — "
                "removing, renaming, or retyping a field must be done with the "
                "CLI so its data can be handled deliberately."
            )
        # write the new config first (backing up the old); additive drift is
        # then applied to the DB. If a later boot ever finds config ahead of
        # the DB, the boot path re-applies the additive migration safely.
        _backup_yaml(instance)
        (instance.root / "collection.yaml").write_text(
            yaml.safe_dump(new_raw, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        if drift.kind in ("additive", "fresh"):
            backup_database(instance.db_path, instance.backups_dir)
            rebuild(conn, new_registry)
    finally:
        conn.close()

    return resolve_instance(instance.root)
