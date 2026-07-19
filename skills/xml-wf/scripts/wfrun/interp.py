"""Variable store, {var} interpolation, and the safe expression evaluator.

Interpolation: `{name}` where name is a Python identifier. `{{` / `}}` escape
to literal braces, so JSON snippets inside <task> bodies survive untouched
(`{"key": 1}` contains no identifier-shaped reference and is left as-is).

Expressions (set expr= / if test= / while test=) are interpolated first, then
evaluated on an ast allowlist: literals, arithmetic, comparison, boolean ops,
and a handful of pure builtins. No attribute access, no subscripts beyond
constants, no imports — evaluation is total or it raises InterpError.
"""
from __future__ import annotations

import ast
import re

_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class InterpError(Exception):
    pass


def interpolate(text: str, variables: dict) -> str:
    # Protect escaped braces before substitution.
    text = text.replace("{{", "\x00").replace("}}", "\x01")

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in variables:
            raise InterpError(f"undefined variable '{name}'")
        return str(variables[name])

    return _VAR_RE.sub(repl, text).replace("\x00", "{").replace("\x01", "}")


def find_refs(text: str) -> set[str]:
    """Identifier-shaped {var} references in text (escapes excluded)."""
    return set(_VAR_RE.findall(text.replace("{{", "\x00").replace("}}", "\x01")))


_ALLOWED_FUNCS = {"len": len, "int": int, "float": float, "str": str,
                  "abs": abs, "min": min, "max": max, "round": round}

_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.List, ast.Tuple,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.UnaryOp, ast.USub, ast.UAdd, ast.Not,
    ast.BoolOp, ast.And, ast.Or,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn,
    ast.Call, ast.Name, ast.Load,
)


def safe_eval(expr: str, variables: dict):
    """Interpolate {var} then evaluate on the allowlist. Returns the value."""
    resolved = interpolate(expr, variables)
    try:
        tree = ast.parse(resolved, mode="eval")
    except SyntaxError as e:
        raise InterpError(f"invalid expression '{resolved}': {e}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise InterpError(
                f"expression '{resolved}': {type(node).__name__} is not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise InterpError(f"expression '{resolved}': only "
                                  f"{sorted(_ALLOWED_FUNCS)} may be called")
        elif isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCS:
            raise InterpError(
                f"expression '{resolved}': bare name '{node.id}' is not allowed "
                f"(interpolate it as a quoted {{var}} instead)")
    try:
        return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, dict(_ALLOWED_FUNCS))
    except Exception as e:
        raise InterpError(f"expression '{resolved}' failed: {e}") from e


def check_expr_syntax(expr: str) -> str | None:
    """Static (lint-time) check: substitute dummy values and try the allowlist.

    Returns an error message or None. Dummy value '0' keeps arithmetic legal;
    quoted string comparisons ('{x}' == 'ok') work because the braces sit
    inside string literals after substitution.
    """
    dummies = {name: "0" for name in find_refs(expr)}
    try:
        safe_eval(expr, dummies)
    except InterpError as e:
        msg = str(e)
        # Runtime value errors on dummy data are fine; structural errors are not.
        if "is not allowed" in msg or "invalid expression" in msg or "may be called" in msg:
            return msg
    return None
