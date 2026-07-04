import pytest

from curio_cabinet.units import UnitError, convert, format_measure, parse_measure


def test_convert_length():
    assert convert(1, "in", "cm", "length") == pytest.approx(2.54)
    assert convert(30.48, "cm", "ft", "length") == pytest.approx(1.0)
    assert convert(1000, "mm", "m", "length") == pytest.approx(1.0)


def test_convert_mass():
    assert convert(1, "kg", "g", "mass") == pytest.approx(1000)
    assert convert(1, "lb", "oz", "mass") == pytest.approx(16)


def test_parse_bare_number_is_store_unit():
    assert parse_measure(42, dimension="length", store="cm") == 42
    assert parse_measure("42", dimension="length", store="cm") == 42
    assert parse_measure("42.5", dimension="length", store="cm") == 42.5


def test_parse_with_units():
    assert parse_measure("6.5 ft", dimension="length", store="ft") == pytest.approx(6.5)
    assert parse_measure("198 cm", dimension="length", store="cm") == pytest.approx(198)
    assert parse_measure("6 ft", dimension="length", store="cm") == pytest.approx(182.88)


# The V1 hazard strings: these silently misparsed in the old codebase.
@pytest.mark.parametrize(
    "raw,expected_ft",
    [
        ("45 in", 3.75),
        ("24 inch", 2.0),
        ("36″", 3.0),  # unicode double-prime
        ('36"', 3.0),
        ("36”", 3.0),
        ("3ft", 3.0),
        ("3'", 3.0),
    ],
)
def test_parse_v1_hazard_strings(raw, expected_ft):
    assert parse_measure(raw, dimension="length", store="ft") == pytest.approx(expected_ft)


def test_unknown_suffix_is_hard_error():
    with pytest.raises(UnitError):
        parse_measure("36 bananas", dimension="length", store="ft")
    with pytest.raises(UnitError):
        parse_measure("5 oz", dimension="length", store="cm")  # wrong dimension


def test_empty_and_garbage_are_errors():
    with pytest.raises(UnitError):
        parse_measure("", dimension="length", store="cm")
    with pytest.raises(UnitError):
        parse_measure("about right", dimension="length", store="cm")
    with pytest.raises(UnitError):
        parse_measure(True, dimension="length", store="cm")


def test_format_measure():
    assert format_measure(2.54, store="cm", display="in", dimension="length") == "1 in"
    assert format_measure(61, store="cm", display="in", dimension="length") == "24 in"
