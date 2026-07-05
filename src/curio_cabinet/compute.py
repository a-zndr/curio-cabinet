"""Computed fields: a tiny, safe arithmetic evaluator over other fields.

A number/integer field may declare ``computed: "weight / (length / 100)"``.
The expression references other field keys and uses only + - * / and
parentheses — no names beyond field keys, no calls, no attributes. It is
parsed with ``ast`` and walked against an allowlist, so nothing executes;
this is deliberately not ``eval``.

Missing operands and division by zero yield ``None`` (a blank cell), never
an error — a half-filled item shouldn't fail to save.
"""

from __future__ import annotations

import ast
import operator

__all__ = ["ComputeError", "validate_expr", "field_refs", "evaluate"]

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
# every ast node type the grammar permits
_ALLOWED = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
)


class ComputeError(ValueError):
    """A formula is syntactically invalid or uses disallowed constructs."""


def validate_expr(expr: str) -> set[str]:
    """Parse and allowlist-check a formula at config-load time. Returns the
    set of field keys it references. Raises ComputeError on anything unsafe."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ComputeError(f"invalid formula {expr!r}: {exc.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ComputeError(
                f"formula {expr!r} uses unsupported syntax "
                f"({type(node).__name__}); only + - * / and field names allowed"
            )
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ComputeError(f"formula {expr!r}: only numeric constants allowed")
    refs = field_refs(tree)
    if not refs:
        raise ComputeError(f"formula {expr!r} references no fields")
    return refs


def field_refs(expr: str | ast.AST) -> set[str]:
    tree = ast.parse(expr, mode="eval") if isinstance(expr, str) else expr
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def evaluate(expr: str, values: dict) -> float | None:
    """Evaluate ``expr`` against ``values`` (field key -> stored value).
    Returns None if any referenced value is missing/non-numeric or a
    division by zero occurs."""
    return _eval(ast.parse(expr, mode="eval").body, values)


def _eval(node: ast.AST, values: dict) -> float | None:
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        v = values.get(node.id)
        if v is None or isinstance(v, bool):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, values)
        if v is None:
            return None
        return +v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, values)
        right = _eval(node.right, values)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Div) and right == 0:
            return None
        return _BINOPS[type(node.op)](left, right)
    # unreachable for validated expressions
    raise ComputeError("unsupported expression node")
