"""Collection config: pydantic models, YAML loading, validation.

The parsed, validated config is the single source of truth for the DB
schema, write validation, query whitelists, and every view. Nothing else
in the engine may hard-code a field name.

Per-type view defaults live here (``default_views``) and nowhere else;
the config reference docs are generated from these models.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from .units import DIMENSIONS

__all__ = ["ConfigError", "CollectionConfig", "FieldSpec", "GroupSpec", "load_config"]


class ConfigError(ValueError):
    """collection.yaml is invalid. Message is meant for humans."""


class FieldType(str, Enum):
    text = "text"
    longtext = "longtext"
    number = "number"
    integer = "integer"
    boolean = "boolean"
    enum = "enum"
    tags = "tags"
    url = "url"
    date = "date"


# SQLite column affinity per field type. booleans are 0/1 INTEGER,
# tags are a JSON array in TEXT, dates are ISO-8601 TEXT.
SQL_TYPES: dict[FieldType, str] = {
    FieldType.text: "TEXT",
    FieldType.longtext: "TEXT",
    FieldType.number: "REAL",
    FieldType.integer: "INTEGER",
    FieldType.boolean: "INTEGER",
    FieldType.enum: "TEXT",
    FieldType.tags: "TEXT",
    FieldType.url: "TEXT",
    FieldType.date: "TEXT",
}

# Column names the engine owns on the items table (in DDL order around the
# data columns). Single source for every module that builds row tuples.
ENGINE_COLS = ("id", "created_at", "updated_at")
RESERVED_KEYS = set(ENGINE_COLS)

# Table names the engine owns in the database.
RESERVED_TABLES = {"users", "sessions", "login_attempts", "images", "_meta"}

# SQL keywords that would produce confusing DDL/queries as column names even
# quoted; rejected outright to keep generated SQL boring.
SQL_KEYWORDS = {
    "select", "from", "where", "group", "order", "by", "having", "limit",
    "offset", "index", "table", "default", "primary", "key", "references",
    "join", "union", "and", "or", "not", "null", "case", "when", "then",
    "else", "end", "as", "on", "in", "is", "between", "exists", "distinct",
}

CardSlot = Literal["primary", "secondary", "hidden"]
FilterKind = Literal["none", "multi", "range"]
PivotOp = Literal["group", "avg", "min", "max", "sum"]


class UnitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: str | None = None
    store: str | None = None
    display: tuple[str, ...] = ()
    label: str | None = None  # display-only suffix, no conversion (e.g. "g/m", "%")

    @model_validator(mode="after")
    def _check(self) -> "UnitSpec":
        if self.label is not None:
            if self.dimension or self.store or self.display:
                raise ValueError("unit: use either label OR dimension/store/display")
            return self
        if not self.dimension or not self.store:
            raise ValueError("unit: dimension and store are both required")
        if self.dimension not in DIMENSIONS:
            raise ValueError(f"unit: unknown dimension {self.dimension!r}")
        units = DIMENSIONS[self.dimension]
        for u in (self.store, *self.display):
            if u not in units:
                raise ValueError(f"unit: {u!r} is not a {self.dimension} unit")
        return self

    @model_validator(mode="after")
    def _default_display(self) -> "UnitSpec":
        if self.store and not self.display:
            object.__setattr__(self, "display", (self.store,))
        return self


class ViewsSpec(BaseModel):
    """Where a field appears. Unset values fall back to per-type defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: StrictBool | None = None       # default False: new fields never silently join the public table
    card: CardSlot | None = None
    detail: StrictBool | None = None
    filter: FilterKind | None = None
    sort: StrictBool | None = None
    pivot: tuple[PivotOp, ...] | None = None


def default_views(ftype: FieldType) -> dict[str, Any]:
    """Per-type view defaults — defined once, here."""
    numeric = ftype in (FieldType.number, FieldType.integer)
    return {
        "table": False,
        "card": "hidden",
        "detail": True,
        "filter": "range" if numeric else (
            "multi" if ftype in (FieldType.enum, FieldType.tags, FieldType.boolean) else "none"
        ),
        "sort": ftype not in (FieldType.longtext, FieldType.tags, FieldType.url),
        "pivot": ("avg",) if numeric else (
            ("group",) if ftype in (FieldType.enum, FieldType.tags, FieldType.boolean) else ()
        ),
    }


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    type: FieldType
    required: StrictBool = False
    default: Any = None
    searchable: StrictBool = False
    unit: UnitSpec | None = None
    link: str | None = None            # text fields: render as link to this url field
    values: tuple[str, ...] = ()       # enum only
    strict: StrictBool = False         # enum only: reject values outside `values`
    rename_from: str | None = None     # migration hint; consumed only if the old column exists
    views: ViewsSpec = ViewsSpec()

    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"field key {v!r} must be snake_case ([a-z][a-z0-9_]*)")
        if v in RESERVED_KEYS:
            raise ValueError(f"field key {v!r} is reserved by the engine")
        if v in SQL_KEYWORDS:
            raise ValueError(f"field key {v!r} is a SQL keyword; pick another name")
        return v

    @model_validator(mode="after")
    def _type_consistency(self) -> "FieldSpec":
        if self.unit and self.type not in (FieldType.number, FieldType.integer):
            raise ValueError(f"field {self.key!r}: unit only applies to number/integer")
        if self.values and self.type is not FieldType.enum:
            raise ValueError(f"field {self.key!r}: values only applies to enum")
        if self.type is FieldType.enum and not self.values:
            raise ValueError(f"field {self.key!r}: enum needs values")
        if self.searchable and self.type not in (
            FieldType.text, FieldType.longtext, FieldType.tags
        ):
            raise ValueError(f"field {self.key!r}: searchable only applies to text-like fields")
        return self

    # Effective view settings (per-type defaults applied) -----------------

    def _view(self, name: str) -> Any:
        explicit = getattr(self.views, name)
        if explicit is not None:
            return explicit
        return default_views(self.type)[name]

    @property
    def in_table(self) -> bool:
        return bool(self._view("table"))

    @property
    def card_slot(self) -> str:
        return self._view("card")

    @property
    def in_detail(self) -> bool:
        return bool(self._view("detail"))

    @property
    def filter_kind(self) -> str:
        return self._view("filter")

    @property
    def sortable(self) -> bool:
        return bool(self._view("sort"))

    @property
    def pivot_ops(self) -> tuple[str, ...]:
        return tuple(self._view("pivot"))

    @property
    def sql_type(self) -> str:
        return SQL_TYPES[self.type]


class WhenSpec(BaseModel):
    """Structured visibility condition for a group: eq or in only (v0)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    eq: Any = None
    in_: tuple[Any, ...] | None = Field(default=None, alias="in")

    @model_validator(mode="after")
    def _one_op(self) -> "WhenSpec":
        if (self.eq is None) == (self.in_ is None):
            raise ValueError("when: exactly one of eq/in is required")
        return self

    def matches(self, item: dict[str, Any]) -> bool:
        value = item.get(self.field)
        if self.eq is not None:
            return value == self.eq
        return value in (self.in_ or ())


class GroupSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    fields: tuple[str, ...]
    when: WhenSpec | None = None


class IdSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Literal["sequential"] = "sequential"
    width: int = 4


class SortSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    order: Literal["asc", "desc"] = "asc"


class CollectionMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    slug: str
    id: IdSpec = IdSpec()
    title_field: str
    default_sort: SortSpec
    accent_hue: int | None = None

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"slug {v!r} must be [a-z][a-z0-9_]*")
        if v in RESERVED_TABLES or v.startswith("sqlite_"):
            raise ValueError(f"slug {v!r} collides with an engine table")
        return v


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    collection: CollectionMeta
    fields: tuple[FieldSpec, ...]
    groups: tuple[GroupSpec, ...] = ()

    @model_validator(mode="after")
    def _cross_checks(self) -> "CollectionConfig":
        keys = [f.key for f in self.fields]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise ValueError(f"duplicate field keys: {sorted(dupes)}")
        by_key = {f.key: f for f in self.fields}

        for name in ("title_field",):
            if getattr(self.collection, name) not in by_key:
                raise ValueError(f"collection.{name} references unknown field")
        if self.collection.default_sort.field not in by_key:
            raise ValueError("collection.default_sort references unknown field")

        for f in self.fields:
            if f.link is not None:
                target = by_key.get(f.link)
                if target is None or target.type is not FieldType.url:
                    raise ValueError(f"field {f.key!r}: link must name a url field")

        grouped: dict[str, str] = {}
        for g in self.groups:
            for key in g.fields:
                if key not in by_key:
                    raise ValueError(f"group {g.key!r} references unknown field {key!r}")
                if key in grouped:
                    raise ValueError(
                        f"field {key!r} appears in groups {grouped[key]!r} and {g.key!r}"
                    )
                grouped[key] = g.key
            if g.when and g.when.field not in by_key:
                raise ValueError(f"group {g.key!r}: when references unknown field")

        # Every field must render somewhere: ungrouped fields go to an
        # implicit trailing "Other" group so they can never be invisible.
        leftover = tuple(k for k in keys if k not in grouped)
        if leftover:
            extra = GroupSpec(key="other", label="Other", fields=leftover)
            if any(g.key == "other" for g in self.groups):
                raise ValueError(
                    f"fields {leftover} are not in any group, and the group key "
                    "'other' is taken — assign them explicitly"
                )
            object.__setattr__(self, "groups", (*self.groups, extra))
        return self

    @model_validator(mode="after")
    def _label_collisions(self) -> "CollectionConfig":
        """Labels double as CSV headers; ambiguous mappings corrupt imports."""
        taken: dict[str, str] = {}
        for f in self.fields:
            for name in {f.key.lower(), f.label.lower()}:
                owner = taken.get(name)
                if owner is not None and owner != f.key:
                    raise ValueError(
                        f"field {f.key!r}: key/label {name!r} collides with "
                        f"field {owner!r} (labels must be unambiguous)"
                    )
                taken[name] = f.key
        return self

    @model_validator(mode="after")
    def _defaults_coerce(self) -> "CollectionConfig":
        """A default that can't pass the field's own coercion is a config bug."""
        from .coerce import CoercionError, coerce_value  # deferred: avoids cycle

        for f in self.fields:
            if f.default is None:
                continue
            try:
                coerce_value(f, f.default)
            except CoercionError as exc:
                raise ValueError(f"field {f.key!r}: invalid default: {exc.reason}")
        return self

    def schema_snapshot(self) -> list[dict[str, Any]]:
        """Logical schema (type + unit identity) recorded in _meta and used
        for drift detection — SQLite affinity alone can't see longtext→tags
        or a unit.store change."""
        snapshot = []
        for f in self.fields:
            snapshot.append(
                {
                    "key": f.key,
                    "type": f.type.value,
                    "store": f.unit.store if f.unit else None,
                    "dimension": f.unit.dimension if f.unit else None,
                }
            )
        return snapshot

    def sha(self) -> str:
        """Stable hash of the schema-relevant parts, recorded in _meta."""
        payload = json.dumps(self.schema_snapshot(), separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_config(path: str | Path) -> CollectionConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return CollectionConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError → human message
        raise ConfigError(f"{path}: {exc}") from None
