"""The one coercion codepath.

Admin form posts, CSV import, and the V1 migrator all convert raw values
to stored values through :func:`coerce_value`. There is deliberately no
second, laxer parser anywhere in the engine — if a value can't be
interpreted safely it is a :class:`CoercionError`, never a guess.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from .config import FieldSpec, FieldType
from .units import UnitError, parse_measure

__all__ = ["CoercionError", "coerce_value", "coerce_row", "display_value"]

_TRUE = {"true", "yes", "y", "1", "on", "x", "✓"}
_FALSE = {"false", "no", "n", "0", "off", ""}


class CoercionError(ValueError):
    def __init__(self, field: str, raw: Any, reason: str):
        self.field = field
        self.raw = raw
        self.reason = reason
        super().__init__(f"{field}: {reason} (got {raw!r})")


def coerce_value(field: FieldSpec, raw: Any) -> Any:
    """Convert a raw value to its stored form, or raise CoercionError.

    Returns None for empty input on non-required fields; requiredness is
    enforced here so every write path shares it.
    """
    if isinstance(raw, str):
        raw = raw.strip()
    if raw is None or raw == "":
        if field.default is not None:
            raw = field.default
        elif field.required:
            raise CoercionError(field.key, raw, "value is required")
        else:
            return None

    ftype = field.type
    try:
        if ftype in (FieldType.number, FieldType.integer):
            return _coerce_numeric(field, raw)
        if ftype is FieldType.boolean:
            return _coerce_bool(field, raw)
        if ftype is FieldType.tags:
            return _coerce_tags(field, raw)
        if ftype is FieldType.enum:
            return _coerce_enum(field, raw)
        if ftype is FieldType.url:
            return _coerce_url(field, raw)
        if ftype is FieldType.date:
            return _coerce_date(field, raw)
        return str(raw)  # text / longtext
    except CoercionError:
        raise
    except (ValueError, TypeError) as exc:
        raise CoercionError(field.key, raw, str(exc)) from None


def _coerce_numeric(field: FieldSpec, raw: Any) -> float | int:
    unit = field.unit
    if unit and unit.dimension and unit.store:
        try:
            value = parse_measure(raw, dimension=unit.dimension, store=unit.store)
        except UnitError as exc:
            raise CoercionError(field.key, raw, str(exc)) from None
    elif unit and unit.label and isinstance(raw, str):
        # display-only suffix ("%", "g/m"): tolerate it in input
        text = raw.removesuffix(unit.label).strip()
        value = float(text)
    else:
        value = float(raw)

    if field.type is FieldType.integer:
        if abs(value - round(value)) > 1e-9:
            raise CoercionError(field.key, raw, "expected a whole number")
        return int(round(value))
    return float(value)


def _coerce_bool(field: FieldSpec, raw: Any) -> int:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return int(raw)
    text = str(raw).strip().lower()
    if text in _TRUE:
        return 1
    if text in _FALSE:
        return 0
    raise CoercionError(field.key, raw, "expected true/false")


def _coerce_tags(field: FieldSpec, raw: Any) -> str:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            items = parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            items = None
        if items is None:
            items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise CoercionError(field.key, raw, "expected a comma-separated list")
    tags = [str(t).strip() for t in items if str(t).strip()]
    seen: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.append(tag)
    return json.dumps(seen, ensure_ascii=False)


def _coerce_enum(field: FieldSpec, raw: Any) -> str:
    text = str(raw).strip()
    for known in field.values:
        if text.lower() == known.lower():
            return known  # normalize case to the declared value
    if field.strict:
        raise CoercionError(
            field.key, raw, f"must be one of {', '.join(field.values)}"
        )
    return text


def _coerce_url(field: FieldSpec, raw: Any) -> str:
    text = str(raw).strip()
    if not text.startswith(("http://", "https://")):
        raise CoercionError(field.key, raw, "URL must start with http:// or https://")
    return text


def _coerce_date(field: FieldSpec, raw: Any) -> str:
    if isinstance(raw, _dt.datetime):
        return raw.date().isoformat()
    if isinstance(raw, _dt.date):
        return raw.isoformat()
    text = str(raw).strip()
    try:
        return _dt.date.fromisoformat(text).isoformat()
    except ValueError:
        raise CoercionError(field.key, raw, "expected an ISO date (YYYY-MM-DD)") from None


def coerce_row(
    fields: tuple[FieldSpec, ...], raw: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Coerce a whole row. Returns (values, errors-by-field-key).

    Only keys present in ``raw`` are coerced (PATCH semantics); callers
    building full rows pass every field key.
    """
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field in fields:
        if field.key not in raw:
            continue
        try:
            values[field.key] = coerce_value(field, raw[field.key])
        except CoercionError as exc:
            errors[field.key] = exc.reason
    return values, errors


def display_value(field: FieldSpec, stored: Any) -> str:
    """Human-readable rendering of a stored value (no HTML)."""
    if stored is None:
        return ""
    if field.type is FieldType.boolean:
        return "Yes" if stored else "No"
    if field.type is FieldType.tags:
        try:
            return ", ".join(json.loads(stored))
        except (json.JSONDecodeError, TypeError):
            return str(stored)
    if field.type in (FieldType.number, FieldType.integer) and field.unit:
        unit = field.unit
        if unit.label:
            return f"{stored:g} {unit.label}" if unit.label != "%" else f"{stored:g}%"
        from .units import format_measure

        return format_measure(
            float(stored),
            store=unit.store or "",
            display=unit.display[0],
            dimension=unit.dimension or "",
        )
    if field.type is FieldType.number:
        return f"{stored:g}"
    return str(stored)
