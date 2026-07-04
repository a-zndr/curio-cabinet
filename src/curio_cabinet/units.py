"""Unit dimensions, conversions, and measurement-string parsing.

Every quantity field stores exactly one canonical value (the config's
``unit.store`` unit). This module is the only place conversion math lives;
admin forms, CSV import, range filters, and the V1 migrator all parse
through :func:`parse_measure`.

Unrecognized unit suffixes are hard errors, never silently treated as bare
numbers — "36″" must not become 36 feet because the suffix wasn't in a
lookup table.
"""

from __future__ import annotations

import re

__all__ = [
    "UnitError",
    "DIMENSIONS",
    "convert",
    "parse_measure",
    "format_measure",
]


class UnitError(ValueError):
    """A value could not be interpreted in the requested unit/dimension."""


# Factors express one unit in terms of the dimension's base unit.
DIMENSIONS: dict[str, dict[str, float]] = {
    "length": {  # base: cm
        "mm": 0.1,
        "cm": 1.0,
        "m": 100.0,
        "in": 2.54,
        "ft": 30.48,
    },
    "mass": {  # base: g
        "g": 1.0,
        "kg": 1000.0,
        "oz": 28.349523125,
        "lb": 453.59237,
    },
}

# Spelled-out and symbol synonyms, normalized to canonical unit names.
_SYNONYMS: dict[str, str] = {
    "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "inch": "in", "inches": "in", '"': "in", "”": "in", "″": "in", "''": "in",
    "foot": "ft", "feet": "ft", "'": "ft", "’": "ft", "′": "ft",
    "gram": "g", "grams": "g",
    "kilogram": "kg", "kilograms": "kg", "kgs": "kg",
    "ounce": "oz", "ounces": "oz",
    "lbs": "lb", "pound": "lb", "pounds": "lb",
}

_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)")


def _canonical_unit(token: str, dimension: str) -> str:
    unit = _SYNONYMS.get(token.lower(), token.lower())
    if unit not in DIMENSIONS[dimension]:
        raise UnitError(f"unknown {dimension} unit {token!r}")
    return unit


def convert(value: float, from_unit: str, to_unit: str, dimension: str) -> float:
    """Convert ``value`` between two units of the same dimension."""
    if dimension not in DIMENSIONS:
        raise UnitError(f"unknown dimension {dimension!r}")
    src = _canonical_unit(from_unit, dimension)
    dst = _canonical_unit(to_unit, dimension)
    table = DIMENSIONS[dimension]
    return value * table[src] / table[dst]


def parse_measure(raw: object, *, dimension: str, store: str) -> float:
    """Parse a measurement into the canonical ``store`` unit.

    Accepts numbers (assumed to already be in the store unit) and strings
    like ``"6.5 ft"``, ``"36in"``, ``'36"'``, ``"36″"``, ``"24 inch"``,
    ``"198 cm"``. Raises :class:`UnitError` for anything else, including
    strings whose suffix is not a known unit of ``dimension``.
    """
    if isinstance(raw, bool):
        raise UnitError(f"boolean is not a measurement: {raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if not text:
        raise UnitError("empty measurement")

    match = _NUMBER_RE.match(text)
    if not match:
        raise UnitError(f"no number found in {text!r}")
    value = float(match.group(0))
    suffix = text[match.end():].strip().rstrip(".").strip()

    if not suffix:
        return value  # bare number: already in the store unit
    return convert(value, suffix, store, dimension)


def format_measure(
    value: float, *, store: str, display: str, dimension: str, precision: int = 1
) -> str:
    """Render a stored value in a display unit, e.g. ``format_measure(61, ...) -> "24.0 in"``."""
    shown = convert(value, store, display, dimension)
    text = f"{shown:.{precision}f}".rstrip("0").rstrip(".")
    return f"{text} {display}"
