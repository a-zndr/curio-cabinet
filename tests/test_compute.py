"""The safe arithmetic evaluator behind computed fields."""

import pytest

from curio_cabinet.compute import ComputeError, evaluate, field_refs, validate_expr


def test_basic_arithmetic_and_refs():
    assert validate_expr("weight / (length / 100)") == {"weight", "length"}
    assert evaluate("weight / (length / 100)", {"weight": 343.9, "length": 60}) == pytest.approx(573.1667, abs=1e-3)
    assert evaluate("(a - b) / a * 100", {"a": 100, "b": 15}) == 85.0
    assert evaluate("-x + 2 * y", {"x": 3, "y": 4}) == 5.0


@pytest.mark.parametrize("expr", [
    "__import__('os')",          # calls
    "weight.__class__",          # attribute access
    "weight or 1",               # boolean op
    "weight ** 2",               # power (DoS vector) not allowed
    "weight % 3",                # modulo not allowed
    "lambda: 1",                 # lambda
    "weight if x else y",        # conditional
    "[weight]",                  # list
    "weight == 1",               # comparison
    "'str'",                     # string constant
    "weight +",                  # syntax error
    "42",                        # references no fields
])
def test_disallowed_expressions_rejected_at_config_load(expr):
    with pytest.raises(ComputeError):
        validate_expr(expr)


def test_everything_validate_accepts_evaluates_without_error():
    # the two grammars must agree: a validated formula never crashes evaluate
    for expr in ("a+b", "a-b*c", "(a+b)/(c+d)", "-a", "+a", "a/b/c/d"):
        validate_expr(expr)
        assert evaluate(expr, {"a": 2, "b": 3, "c": 4, "d": 5}) is not None


def test_missing_operand_and_divzero_are_blank_not_errors():
    assert evaluate("a / b", {"a": 10, "b": 0}) is None
    assert evaluate("a / b", {"a": 10}) is None          # b missing
    assert evaluate("a + b", {"a": 10, "b": "x"}) is None  # non-numeric
    assert evaluate("a + b", {"a": 10, "b": True}) is None  # bool excluded


def test_numeric_looking_strings_coerce():
    assert evaluate("a * 2", {"a": "5.5"}) == 11.0


def test_apply_computed_guards_non_finite():
    # inf/nan must become blank, never a stored inf or an int() crash
    from curio_cabinet.coerce import apply_computed
    from curio_cabinet.config import CollectionConfig

    def build(ftype):
        return CollectionConfig.from_raw({
            "collection": {"title": "T", "slug": "things", "title_field": "name",
                           "default_sort": {"field": "name", "order": "asc"}},
            "fields": [
                {"key": "name", "label": "N", "type": "text", "required": True},
                {"key": "a", "label": "A", "type": "number"},
                {"key": "b", "label": "B", "type": "number"},
                {"key": "c", "label": "C", "type": ftype, "computed": "a * b"},
            ],
            "groups": [{"key": "g", "label": "G", "fields": ["name", "a", "b", "c"]}],
        }).fields

    for ftype in ("number", "integer"):
        fields = build(ftype)
        vals = {"a": 1e200, "b": 1e200}  # overflows to inf
        apply_computed(fields, vals)     # must not raise
        assert vals["c"] is None


def test_half_up_rounding_matches_migrated_data():
    from curio_cabinet.coerce import _round_half_up

    # 243/(96/100) == 253.125 exactly; half-up -> 253.13 (banker's would give .12)
    assert _round_half_up(253.125, 2) == 253.13
    assert _round_half_up(2.5, 0) == 3.0
