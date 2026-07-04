"""FieldRegistry: precomputed, read-only lookups over a validated config.

Views, query building, forms, CSV mapping, and migration all consume the
registry — never the raw YAML and never request input. Any field name that
does not come out of this object must not reach SQL.
"""

from __future__ import annotations

from functools import cached_property

from .config import ENGINE_COLS, CollectionConfig, FieldSpec, GroupSpec

__all__ = ["FieldRegistry"]


class FieldRegistry:
    def __init__(self, config: CollectionConfig):
        self.config = config
        self.collection = config.collection

    @cached_property
    def fields(self) -> tuple[FieldSpec, ...]:
        return self.config.fields

    @cached_property
    def by_key(self) -> dict[str, FieldSpec]:
        return {f.key: f for f in self.fields}

    def get(self, key: str) -> FieldSpec | None:
        return self.by_key.get(key)

    @cached_property
    def groups(self) -> tuple[GroupSpec, ...]:
        return self.config.groups

    # View-facing subsets -------------------------------------------------

    @cached_property
    def table_default_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields if f.in_table)

    @cached_property
    def detail_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields if f.in_detail)

    @cached_property
    def card_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.card_slot != "hidden")

    @cached_property
    def sortable_keys(self) -> frozenset[str]:
        return frozenset(f.key for f in self.fields if f.sortable)

    @cached_property
    def searchable_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.fields if f.searchable)

    @cached_property
    def multi_filter_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.filter_kind == "multi")

    @cached_property
    def range_filter_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.filter_kind == "range")

    @cached_property
    def pivot_group_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if "group" in f.pivot_ops)

    @cached_property
    def pivot_agg_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(
            f
            for f in self.fields
            if any(op in ("avg", "min", "max", "sum") for op in f.pivot_ops)
        )

    # SQL identifiers ------------------------------------------------------

    @property
    def table(self) -> str:
        return self.collection.slug

    @cached_property
    def column_list(self) -> tuple[str, ...]:
        """All data columns in config order (engine columns excluded)."""
        return tuple(f.key for f in self.fields)

    def quoted(self, key: str) -> str:
        """Double-quoted identifier for a registered key. Raises on strangers."""
        if key not in self.by_key and key not in ENGINE_COLS:
            raise KeyError(f"unregistered field: {key!r}")
        return f'"{key}"'
