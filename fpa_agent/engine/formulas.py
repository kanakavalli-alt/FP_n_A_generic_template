"""Safe Decimal evaluator for derived-metric formulas stored in the semantic
layer, e.g. "{net_revenue} / {active_students}" or
"({gross_sales} - {returns}) / {gross_sales} * 100".

This is what turns "client-specific math in code" into catalog rows: the
formula is data, evaluation is generic, reviewed code. Only arithmetic on
metric references and numeric literals is accepted — no attribute access, no
names other than declared metrics, no calls other than abs/min/max.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, DivisionByZero, InvalidOperation

from fpa_agent.engine.results import D

_REF_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_FUNCS = {"abs": abs, "min": min, "max": max}


class FormulaError(ValueError):
    pass


def references(formula: str) -> list[str]:
    return list(dict.fromkeys(_REF_RE.findall(formula or "")))


def parse(formula: str) -> ast.Expression:
    if not formula or not formula.strip():
        raise FormulaError("empty formula")
    py = _REF_RE.sub(lambda m: f"__m_{m.group(1)}", formula)
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"formula is not valid arithmetic: {formula!r}") from e
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub,
                             ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Load)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            continue
        if isinstance(node, ast.Name) and (node.id.startswith("__m_") or node.id in _FUNCS):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _FUNCS and not node.keywords:
            continue
        raise FormulaError(f"unsupported element {type(node).__name__} in formula {formula!r}")
    return tree


def evaluate(formula: str, values: dict[str, Decimal | None]) -> Decimal | None:
    """Evaluate in Decimal. A missing (None) input yields None (no number is
    better than a wrong one); division by zero raises FormulaError."""
    tree = parse(formula)

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return D(node.value)
        if isinstance(node, ast.Name):
            key = node.id[4:]
            if key not in values:
                raise FormulaError(f"formula references unknown metric {key!r}")
            return values[key]
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            return None if v is None else (-v if isinstance(node.op, ast.USub) else v)
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if a is None or b is None:
                return None
            try:
                if isinstance(node.op, ast.Add):
                    return a + b
                if isinstance(node.op, ast.Sub):
                    return a - b
                if isinstance(node.op, ast.Mult):
                    return a * b
                if b == 0:
                    raise FormulaError("division by zero")
                return a / b
            except (DivisionByZero, InvalidOperation) as e:
                raise FormulaError(str(e)) from e
        if isinstance(node, ast.Call):
            args = [ev(a) for a in node.args]
            if any(a is None for a in args):
                return None
            return _FUNCS[node.func.id](*args)
        raise FormulaError(f"unsupported node {type(node).__name__}")

    return ev(tree)
