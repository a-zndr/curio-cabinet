"""Instance resolution: where a deployment's config, data, and images live.

The engine repo never contains data. An *instance directory* holds:

    collection.yaml   the schema config
    .env              secrets and settings (never committed)
    data/catalog.db   SQLite database
    data/backups/
    images/           processed image derivatives

Resolved from $CABINET_INSTANCE, falling back to ./instance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import CollectionConfig, ConfigError, load_config
from .registry import FieldRegistry

__all__ = ["Instance", "resolve_instance", "load_dotenv"]


def load_dotenv(path: Path) -> dict[str, str]:
    """Tiny .env reader (KEY=VALUE lines, # comments). No dependency."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # allow inline comments: KEY=VALUE  # explanation
        value, _, _ = value.partition(" #")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True)
class Instance:
    root: Path
    config: CollectionConfig
    registry: FieldRegistry
    env: dict[str, str]

    @property
    def db_path(self) -> Path:
        return self.root / "data" / "catalog.db"

    @property
    def backups_dir(self) -> Path:
        return self.root / "data" / "backups"

    @property
    def images_dir(self) -> Path:
        return self.root / "images"

    def setting(self, key: str, default: str = "") -> str:
        return os.environ.get(key) or self.env.get(key, default)

    @property
    def journal_mode(self) -> str:
        return self.setting("CABINET_JOURNAL_MODE", "WAL")


def resolve_instance(root: str | Path | None = None) -> Instance:
    root_path = Path(root or os.environ.get("CABINET_INSTANCE") or "instance").resolve()
    config_path = root_path / "collection.yaml"
    if not config_path.exists():
        raise ConfigError(
            f"no instance at {root_path} (missing collection.yaml). "
            "Run `curio-cabinet init` or set CABINET_INSTANCE."
        )
    config = load_config(config_path)
    env = load_dotenv(root_path / ".env")
    (root_path / "data").mkdir(exist_ok=True)
    return Instance(
        root=root_path, config=config, registry=FieldRegistry(config), env=env
    )
