"""Collection config: models, YAML loading, validation.

The parsed, validated config is the single source of truth for the DB
schema, write validation, query whitelists, and every view. Nothing else
in the engine may hard-code a field name.

Implemented with the standard library only (frozen dataclasses + explicit
validation) so the engine stays pure-Python and ``pip install``s cleanly on
constrained hosts — no Rust/compiled config dependency.

Per-type view defaults live here (``default_views``) and nowhere else;
the config reference docs are generated from them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .units import DIMENSIONS

__all__ = [
    "ConfigError",
    "CollectionConfig",
    "CollectionMeta",
    "FieldSpec",
    "FieldType",
    "GroupSpec",
    "PresetSpec",
    "WhenSpec",
    "UnitSpec",
    "ViewsSpec",
    "default_views",
    "load_config",
    "ENGINE_COLS",
    "RESERVED_KEYS",
    "RESERVED_TABLES",
    "SQL_TYPES",
]

_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")


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

CARD_SLOTS = ("primary", "secondary", "hidden")
FILTER_KINDS = ("none", "multi", "range")
PIVOT_OPS = ("group", "avg", "min", "max", "sum")


# -- small validation helpers -------------------------------------------------


def _mapping(raw: Any, ctx: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{ctx}: expected a mapping")
    return raw


def _reject_unknown(raw: dict, allowed: set[str], ctx: str) -> None:
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f"{ctx}: unknown key(s): {', '.join(sorted(extra))}")


def _str(raw: dict, key: str, ctx: str, *, required: bool = True,
         default: str | None = None) -> str | None:
    if key not in raw or raw[key] is None:
        if required:
            raise ValueError(f"{ctx}: {key!r} is required")
        return default
    v = raw[key]
    if not isinstance(v, str):
        raise ValueError(f"{ctx}: {key!r} must be text")
    return v


def _bool(raw: dict, key: str, ctx: str, default: bool = False) -> bool:
    if key not in raw or raw[key] is None:
        return default
    v = raw[key]
    if not isinstance(v, bool):
        raise ValueError(f"{ctx}: {key!r} must be true or false")
    return v


def _str_tuple(raw: dict, key: str, ctx: str) -> tuple[str, ...]:
    v = raw.get(key)
    if v is None:
        return ()
    if not isinstance(v, (list, tuple)):
        raise ValueError(f"{ctx}: {key!r} must be a list")
    return tuple(str(x) for x in v)


def _one_of(value: Any, options: tuple[str, ...], ctx: str) -> str:
    if value not in options:
        raise ValueError(f"{ctx}: must be one of {', '.join(options)} (got {value!r})")
    return value


# -- specs --------------------------------------------------------------------


@dataclass(frozen=True)
class UnitSpec:
    dimension: str | None = None
    store: str | None = None
    display: tuple[str, ...] = ()
    label: str | None = None  # display-only suffix, no conversion (e.g. "g/m", "%")

    @classmethod
    def from_raw(cls, raw: Any, ctx: str) -> "UnitSpec":
        raw = _mapping(raw, ctx)
        _reject_unknown(raw, {"dimension", "store", "display", "label"}, ctx)
        label = _str(raw, "label", ctx, required=False)
        if label is not None:
            if raw.get("dimension") or raw.get("store") or raw.get("display"):
                raise ValueError(f"{ctx}: use either label OR dimension/store/display")
            return cls(label=label)
        dimension = _str(raw, "dimension", ctx)
        store = _str(raw, "store", ctx)
        if dimension not in DIMENSIONS:
            raise ValueError(f"{ctx}: unknown dimension {dimension!r}")
        units = DIMENSIONS[dimension]
        display = _str_tuple(raw, "display", ctx) or (store,)
        for u in (store, *display):
            if u not in units:
                raise ValueError(f"{ctx}: {u!r} is not a {dimension} unit")
        return cls(dimension=dimension, store=store, display=tuple(display))


@dataclass(frozen=True)
class ViewsSpec:
    """Where a field appears. Unset (None) values fall back to per-type defaults."""

    table: bool | None = None
    card: str | None = None
    detail: bool | None = None
    filter: str | None = None
    sort: bool | None = None
    pivot: tuple[str, ...] | None = None

    @classmethod
    def from_raw(cls, raw: Any, ctx: str) -> "ViewsSpec":
        if raw is None:
            return cls()
        raw = _mapping(raw, ctx)
        _reject_unknown(raw, {"table", "card", "detail", "filter", "sort", "pivot"}, ctx)
        card = raw.get("card")
        if card is not None:
            _one_of(card, CARD_SLOTS, f"{ctx}.card")
        filt = raw.get("filter")
        if filt is not None:
            _one_of(filt, FILTER_KINDS, f"{ctx}.filter")
        pivot = raw.get("pivot")
        if pivot is not None:
            pivot = tuple(pivot)
            for op in pivot:
                _one_of(op, PIVOT_OPS, f"{ctx}.pivot")
        return cls(
            table=None if "table" not in raw else _bool(raw, "table", ctx),
            card=card,
            detail=None if "detail" not in raw else _bool(raw, "detail", ctx),
            filter=filt,
            sort=None if "sort" not in raw else _bool(raw, "sort", ctx),
            pivot=pivot,
        )


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


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    type: FieldType
    required: bool = False
    must_have: bool = False            # soft-required: tracked on the admin
                                       # to-finish list, never blocks a save
    private: bool = False              # admin-only: never rendered publicly
    every_days: int | None = None      # date fields: maintenance cadence; a
                                       # blank/stale date lands on the to-finish list
    default: Any = None
    searchable: bool = False
    suggest: bool = False              # text fields: admin form offers existing values
    unit: UnitSpec | None = None
    link: str | None = None            # text fields: render as link to this url field
    values: tuple[str, ...] = ()       # enum only
    strict: bool = False               # enum only: reject values outside `values`
    rename_from: str | None = None     # migration hint; used only if the old column exists
    views: ViewsSpec = dc_field(default_factory=ViewsSpec)

    @classmethod
    def from_raw(cls, raw: Any) -> "FieldSpec":
        raw = _mapping(raw, "field")
        _reject_unknown(
            raw,
            {"key", "label", "type", "required", "must_have", "private",
             "every_days", "default", "searchable", "suggest", "unit", "link",
             "values", "strict", "rename_from", "views"},
            "field",
        )
        key = _str(raw, "key", "field")
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"field key {key!r} must be snake_case ([a-z][a-z0-9_]*)")
        if key in RESERVED_KEYS:
            raise ValueError(f"field key {key!r} is reserved by the engine")
        if key in SQL_KEYWORDS:
            raise ValueError(f"field key {key!r} is a SQL keyword; pick another name")
        ctx = f"field {key!r}"

        type_raw = _str(raw, "type", ctx)
        try:
            ftype = FieldType(type_raw)
        except ValueError:
            raise ValueError(f"{ctx}: unknown type {type_raw!r}") from None

        unit = UnitSpec.from_raw(raw["unit"], f"{ctx}.unit") if raw.get("unit") else None
        values = _str_tuple(raw, "values", ctx)

        spec = cls(
            key=key,
            label=_str(raw, "label", ctx),
            type=ftype,
            required=_bool(raw, "required", ctx),
            must_have=_bool(raw, "must_have", ctx),
            private=_bool(raw, "private", ctx),
            every_days=raw.get("every_days"),
            default=raw.get("default"),
            searchable=_bool(raw, "searchable", ctx),
            suggest=_bool(raw, "suggest", ctx),
            unit=unit,
            link=_str(raw, "link", ctx, required=False),
            values=values,
            strict=_bool(raw, "strict", ctx),
            rename_from=_str(raw, "rename_from", ctx, required=False),
            views=ViewsSpec.from_raw(raw.get("views"), f"{ctx}.views"),
        )

        if spec.unit and spec.type not in (FieldType.number, FieldType.integer):
            raise ValueError(f"{ctx}: unit only applies to number/integer")
        if spec.values and spec.type is not FieldType.enum:
            raise ValueError(f"{ctx}: values only applies to enum")
        if spec.type is FieldType.enum and not spec.values:
            raise ValueError(f"{ctx}: enum needs values")
        if spec.searchable and spec.type not in (
            FieldType.text, FieldType.longtext, FieldType.tags
        ):
            raise ValueError(f"{ctx}: searchable only applies to text-like fields")
        if spec.suggest and spec.type is not FieldType.text:
            raise ValueError(f"{ctx}: suggest only applies to text fields")
        if spec.every_days is not None:
            if spec.type is not FieldType.date:
                raise ValueError(f"{ctx}: every_days only applies to date fields")
            if not isinstance(spec.every_days, int) or isinstance(spec.every_days, bool) \
                    or spec.every_days < 1:
                raise ValueError(f"{ctx}: every_days must be a positive integer")
        if spec.private:
            if spec.searchable:
                raise ValueError(f"{ctx}: a private field cannot be searchable")
            v = spec.views
            if (v.table or v.sort or (v.card and v.card != "hidden")
                    or (v.filter and v.filter != "none") or v.pivot):
                raise ValueError(
                    f"{ctx}: a private field cannot appear in public views "
                    "(table/card/filter/sort/pivot)"
                )
        return spec

    # Effective view settings (per-type defaults applied) -----------------

    # public-view settings a private field is never allowed to have,
    # regardless of explicit config or per-type defaults
    _PRIVATE_FORCED = {
        "table": False, "card": "hidden", "filter": "none",
        "sort": False, "pivot": (),
    }

    def _view(self, name: str) -> Any:
        if self.private and name in self._PRIVATE_FORCED:
            return self._PRIVATE_FORCED[name]
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


@dataclass(frozen=True)
class WhenSpec:
    """Structured condition for a group's visibility or a preset's filter:
    exactly one of eq / in."""

    field: str
    eq: Any = None
    in_: tuple[Any, ...] | None = None

    @classmethod
    def from_raw(cls, raw: Any, ctx: str) -> "WhenSpec":
        raw = _mapping(raw, ctx)
        _reject_unknown(raw, {"field", "eq", "in"}, ctx)
        field = _str(raw, "field", ctx)
        eq = raw.get("eq")
        in_ = tuple(raw["in"]) if raw.get("in") is not None else None
        if (eq is None) == (in_ is None):
            raise ValueError(f"{ctx}: exactly one of eq/in is required")
        return cls(field=field, eq=eq, in_=in_)

    def matches(self, item: dict[str, Any]) -> bool:
        value = item.get(self.field)
        if self.eq is not None:
            return value == self.eq
        return value in (self.in_ or ())


@dataclass(frozen=True)
class GroupSpec:
    key: str
    label: str
    fields: tuple[str, ...]
    when: WhenSpec | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "GroupSpec":
        raw = _mapping(raw, "group")
        _reject_unknown(raw, {"key", "label", "fields", "when"}, "group")
        key = _str(raw, "key", "group")
        ctx = f"group {key!r}"
        when = WhenSpec.from_raw(raw["when"], f"{ctx}.when") if raw.get("when") else None
        return cls(
            key=key,
            label=_str(raw, "label", ctx),
            fields=_str_tuple(raw, "fields", ctx),
            when=when,
        )


@dataclass(frozen=True)
class PresetSpec:
    """A named "specialty table": a row filter + a curated column set,
    surfaced as a tab above the table view (e.g. Primes, Zooms)."""

    key: str
    label: str
    filter: WhenSpec
    columns: tuple[str, ...]

    @classmethod
    def from_raw(cls, raw: Any) -> "PresetSpec":
        raw = _mapping(raw, "preset")
        _reject_unknown(raw, {"key", "label", "filter", "columns"}, "preset")
        key = _str(raw, "key", "preset")
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"preset key {key!r} must be snake_case")
        ctx = f"preset {key!r}"
        if not raw.get("filter"):
            raise ValueError(f"{ctx}: filter is required")
        return cls(
            key=key,
            label=_str(raw, "label", ctx),
            filter=WhenSpec.from_raw(raw["filter"], f"{ctx}.filter"),
            columns=_str_tuple(raw, "columns", ctx),
        )

    def filter_values(self) -> tuple[str, ...]:
        if self.filter.eq is not None:
            return (str(self.filter.eq),)
        return tuple(str(v) for v in (self.filter.in_ or ()))


@dataclass(frozen=True)
class IdSpec:
    strategy: str = "sequential"
    width: int = 4

    @classmethod
    def from_raw(cls, raw: Any) -> "IdSpec":
        if raw is None:
            return cls()
        raw = _mapping(raw, "collection.id")
        _reject_unknown(raw, {"strategy", "width"}, "collection.id")
        strategy = raw.get("strategy", "sequential")
        _one_of(strategy, ("sequential",), "collection.id.strategy")
        width = raw.get("width", 4)
        if not isinstance(width, int) or isinstance(width, bool) or width < 1:
            raise ValueError("collection.id.width must be a positive integer")
        return cls(strategy=strategy, width=width)


@dataclass(frozen=True)
class SortSpec:
    field: str
    order: str = "asc"

    @classmethod
    def from_raw(cls, raw: Any) -> "SortSpec":
        raw = _mapping(raw, "collection.default_sort")
        _reject_unknown(raw, {"field", "order"}, "collection.default_sort")
        order = raw.get("order", "asc")
        _one_of(order, ("asc", "desc"), "collection.default_sort.order")
        return cls(field=_str(raw, "field", "collection.default_sort"), order=order)


@dataclass(frozen=True)
class CollectionMeta:
    title: str
    slug: str
    title_field: str
    default_sort: SortSpec
    id: IdSpec = dc_field(default_factory=IdSpec)
    accent_hue: int | None = None
    accent: str | None = None  # full hex color; takes precedence over accent_hue
    monogram: str | None = None  # favicon letter(s); defaults to title initial
    must_have_photos: bool = False  # photoless items appear on the to-finish list

    @classmethod
    def from_raw(cls, raw: Any) -> "CollectionMeta":
        raw = _mapping(raw, "collection")
        _reject_unknown(
            raw,
            {"title", "slug", "id", "title_field", "default_sort",
             "accent_hue", "accent", "monogram", "must_have_photos"},
            "collection",
        )
        slug = _str(raw, "slug", "collection")
        if not _KEY_RE.fullmatch(slug):
            raise ValueError(f"slug {slug!r} must be [a-z][a-z0-9_]*")
        if slug in RESERVED_TABLES or slug.startswith("sqlite_"):
            raise ValueError(f"slug {slug!r} collides with an engine table")
        hue = raw.get("accent_hue")
        if hue is not None and (not isinstance(hue, int) or isinstance(hue, bool)):
            raise ValueError("collection.accent_hue must be an integer")
        accent = raw.get("accent")
        if accent is not None:
            from .colors import normalize_hex

            norm = normalize_hex(str(accent))
            if norm is None:
                raise ValueError("collection.accent must be a hex color like #7c5cff")
            accent = norm
        monogram = raw.get("monogram")
        if monogram is not None:
            monogram = str(monogram).strip()
            if not 1 <= len(monogram) <= 2:
                raise ValueError("collection.monogram must be 1-2 characters")
        if "default_sort" not in raw:
            raise ValueError("collection: 'default_sort' is required")
        return cls(
            title=_str(raw, "title", "collection"),
            slug=slug,
            title_field=_str(raw, "title_field", "collection"),
            default_sort=SortSpec.from_raw(raw["default_sort"]),
            id=IdSpec.from_raw(raw.get("id")),
            accent_hue=hue,
            accent=accent,
            monogram=monogram,
            must_have_photos=_bool(raw, "must_have_photos", "collection"),
        )


@dataclass(frozen=True)
class CollectionConfig:
    collection: CollectionMeta
    fields: tuple[FieldSpec, ...]
    groups: tuple[GroupSpec, ...] = ()
    presets: tuple[PresetSpec, ...] = ()

    @classmethod
    def from_raw(cls, raw: Any) -> "CollectionConfig":
        raw = _mapping(raw, "config")
        _reject_unknown(raw, {"collection", "fields", "groups", "presets"}, "config")
        if "collection" not in raw:
            raise ValueError("config: 'collection' section is required")
        collection = CollectionMeta.from_raw(raw["collection"])

        fields_raw = raw.get("fields")
        if not isinstance(fields_raw, list) or not fields_raw:
            raise ValueError("config: 'fields' must be a non-empty list")
        fields = tuple(FieldSpec.from_raw(f) for f in fields_raw)

        groups = tuple(GroupSpec.from_raw(g) for g in (raw.get("groups") or ()))
        presets = tuple(PresetSpec.from_raw(p) for p in (raw.get("presets") or ()))

        groups = _validate(collection, fields, groups, presets)
        return cls(collection=collection, fields=fields, groups=groups, presets=presets)

    def schema_snapshot(self) -> list[dict[str, Any]]:
        """Logical schema (type + unit identity) recorded in _meta and used
        for drift detection — SQLite affinity alone can't see longtext→tags
        or a unit.store change."""
        return [
            {
                "key": f.key,
                "type": f.type.value,
                "store": f.unit.store if f.unit else None,
                "dimension": f.unit.dimension if f.unit else None,
            }
            for f in self.fields
        ]

    def sha(self) -> str:
        """Stable hash of the schema-relevant parts, recorded in _meta."""
        payload = json.dumps(self.schema_snapshot(), separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def _validate(
    collection: CollectionMeta,
    fields: tuple[FieldSpec, ...],
    groups: tuple[GroupSpec, ...],
    presets: tuple[PresetSpec, ...],
) -> tuple[GroupSpec, ...]:
    """Cross-field validation. Returns the group list with an implicit
    "Other" group appended for any ungrouped fields."""
    keys = [f.key for f in fields]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise ValueError(f"duplicate field keys: {sorted(dupes)}")
    by_key = {f.key: f for f in fields}

    if collection.title_field not in by_key:
        raise ValueError("collection.title_field references unknown field")
    if by_key[collection.title_field].private:
        # the headline renders on every public surface (cards, table, OG,
        # share pages); a private title would be a contradiction that leaks
        raise ValueError("collection.title_field cannot be a private field")
    if collection.default_sort.field not in by_key:
        raise ValueError("collection.default_sort references unknown field")

    for f in fields:
        if f.link is not None:
            target = by_key.get(f.link)
            if target is None or target.type is not FieldType.url:
                raise ValueError(f"field {f.key!r}: link must name a url field")
            if target.private and not f.private:
                # the link URL is emitted into public HTML via the anchor
                raise ValueError(
                    f"field {f.key!r}: link target {f.link!r} is private"
                )

    grouped: dict[str, str] = {}
    for g in groups:
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

    preset_keys: set[str] = set()
    for p in presets:
        if p.key in preset_keys:
            raise ValueError(f"duplicate preset key: {p.key!r}")
        preset_keys.add(p.key)
        if p.filter.field not in by_key:
            raise ValueError(
                f"preset {p.key!r}: filter references unknown field {p.filter.field!r}"
            )
        if not p.columns:
            raise ValueError(f"preset {p.key!r}: needs at least one column")
        for col in p.columns:
            if col not in by_key:
                raise ValueError(f"preset {p.key!r}: unknown column {col!r}")
            if by_key[col].private:
                # preset columns render in the public table
                raise ValueError(f"preset {p.key!r}: column {col!r} is private")

    # labels double as CSV headers; ambiguous key/label mappings corrupt imports
    taken: dict[str, str] = {}
    for f in fields:
        for name in {f.key.lower(), f.label.lower()}:
            owner = taken.get(name)
            if owner is not None and owner != f.key:
                raise ValueError(
                    f"field {f.key!r}: key/label {name!r} collides with "
                    f"field {owner!r} (labels must be unambiguous)"
                )
            taken[name] = f.key

    # a default that can't pass the field's own coercion is a config bug
    from .coerce import CoercionError, coerce_value  # deferred: avoids import cycle

    for f in fields:
        if f.default is None:
            continue
        try:
            coerce_value(f, f.default)
        except CoercionError as exc:
            raise ValueError(f"field {f.key!r}: invalid default: {exc.reason}")

    # every field must render somewhere: ungrouped fields go to an implicit
    # trailing "Other" group so they can never be silently invisible
    leftover = tuple(k for k in keys if k not in grouped)
    if leftover:
        if any(g.key == "other" for g in groups):
            raise ValueError(
                f"fields {leftover} are not in any group, and the group key "
                "'other' is taken — assign them explicitly"
            )
        groups = (*groups, GroupSpec(key="other", label="Other", fields=leftover))
    return groups


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
        return CollectionConfig.from_raw(raw)
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from None
