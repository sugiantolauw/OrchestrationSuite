from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Any


class ExpressionError(Exception):
    pass


_ALLOWED_BOOL_OPS = (ast.And, ast.Or)
_ALLOWED_CMP_OPS: dict[type, Any] = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}

@dataclass(frozen=True)
class CompiledExpr:
    """A findings.yaml `trigger` or `severity[].when` expression, parsed once and
    validated against the restricted grammar (CLAUDE.md §4.6). `source` is kept for
    error messages; `tree` is the validated ast.Expression."""

    source: str
    tree: ast.Expression
    metric_names: frozenset[str]
    threshold_ids: frozenset[str]


def _is_allowed_bare_constant(value: Any) -> bool:
    # A bare boolean is allowed OUTSIDE a comparison (e.g. a standalone
    # `trigger: True` for an always-fire rule) -- N3 only forbids True/False
    # as a COMPARISON operand, where Python's bool-is-an-int would silently
    # compare a metric or threshold against 1 or 0.
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value == 0
    return False


def _validate(
    node: ast.AST, metric_names: set[str], threshold_ids: set[str], *, in_compare: bool = False
) -> None:
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, _ALLOWED_BOOL_OPS):
            raise ExpressionError(f"disallowed boolean operator: {type(node.op).__name__}")
        for value in node.values:
            _validate(value, metric_names, threshold_ids)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.Not):
            raise ExpressionError(f"disallowed unary operator: {type(node.op).__name__}")
        _validate(node.operand, metric_names, threshold_ids, in_compare=in_compare)
        return
    if isinstance(node, ast.Compare):
        _validate(node.left, metric_names, threshold_ids, in_compare=True)
        for op in node.ops:
            if type(op) not in _ALLOWED_CMP_OPS:
                raise ExpressionError(f"disallowed comparison operator: {type(op).__name__}")
        for comparator in node.comparators:
            _validate(comparator, metric_names, threshold_ids, in_compare=True)
        return
    if isinstance(node, ast.Name):
        metric_names.add(node.id)
        return
    if isinstance(node, ast.Attribute):
        if not (isinstance(node.value, ast.Name) and node.value.id == "thresholds"):
            raise ExpressionError(
                "attribute access is only allowed as thresholds.<id> "
                f"(got {ast.dump(node)})"
            )
        threshold_ids.add(node.attr)
        return
    if isinstance(node, ast.Constant):
        # N3: True/False are rejected as a COMPARISON operand -- Python's
        # bool-subclasses-int means `x == True` silently becomes `x == 1`, a
        # non-zero numeric literal smuggled past the "no non-zero constants"
        # rule under a boolean spelling. Outside a comparison a bare boolean
        # is still a legitimate constant expression.
        if in_compare:
            if isinstance(node.value, bool):
                raise ExpressionError(
                    f"boolean constant not allowed as a comparison operand (smuggles "
                    f"1/0): {node.value!r}"
                )
            if not (isinstance(node.value, (int, float)) and node.value == 0):
                raise ExpressionError(f"disallowed constant: {node.value!r}")
        elif not _is_allowed_bare_constant(node.value):
            raise ExpressionError(f"disallowed constant: {node.value!r}")
        return
    raise ExpressionError(f"disallowed expression node: {type(node).__name__}")


def compile_expr(
    source: str,
    *,
    known_metrics: set[str] | None = None,
    known_thresholds: set[str] | None = None,
) -> CompiledExpr:
    """Parses and validates `source` under the restricted grammar: BoolOp(And/Or),
    UnaryOp(Not), Compare with Gt/GtE/Lt/LtE/Eq/NotEq (chaining allowed), Name (a
    metric reference), Attribute of the form thresholds.<id>, and the constants
    0, 0.0, True, False. Anything else -- calls, subscripts, other attributes,
    lambdas, comprehensions, arithmetic, strings, non-zero numbers -- is rejected
    here, at load time, never at eval(). Never uses eval/exec."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"could not parse expression {source!r}: {exc}") from exc

    metric_names: set[str] = set()
    threshold_ids: set[str] = set()
    _validate(tree.body, metric_names, threshold_ids)

    if known_metrics is not None:
        unknown = metric_names - known_metrics
        if unknown:
            raise ExpressionError(f"unknown metric name(s) in {source!r}: {sorted(unknown)}")
    if known_thresholds is not None:
        unknown_t = threshold_ids - known_thresholds
        if unknown_t:
            raise ExpressionError(f"unknown threshold id(s) in {source!r}: {sorted(unknown_t)}")

    return CompiledExpr(
        source=source,
        tree=tree,
        metric_names=frozenset(metric_names),
        threshold_ids=frozenset(threshold_ids),
    )


def _eval_operand(node: ast.AST, metrics: dict[str, Any], thresholds: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return metrics.get(node.id)
    if isinstance(node, ast.Attribute):
        return thresholds.get(node.attr)
    if isinstance(node, ast.Constant):
        return node.value
    raise ExpressionError(f"cannot evaluate node: {type(node).__name__}")


def _eval(node: ast.AST, metrics: dict[str, Any], thresholds: dict[str, Any]) -> bool | None:
    """Kleene three-valued logic (N3): a comparison against a metric whose value
    is None (a not_testable test) is UNKNOWN, not False -- `not Unknown` is
    Unknown, and And/Or follow the standard Kleene tables (a known False
    dominates And; a known True dominates Or; otherwise Unknown propagates).
    Only the top-level `evaluate()` collapses a remaining Unknown to False."""
    if isinstance(node, ast.BoolOp):
        results = [_eval(v, metrics, thresholds) for v in node.values]
        if isinstance(node.op, ast.And):
            if any(r is False for r in results):
                return False
            if any(r is None for r in results):
                return None
            return True
        if any(r is True for r in results):
            return True
        if any(r is None for r in results):
            return None
        return False
    if isinstance(node, ast.UnaryOp):
        r = _eval(node.operand, metrics, thresholds)
        return None if r is None else (not r)
    if isinstance(node, ast.Compare):
        current = _eval_operand(node.left, metrics, thresholds)
        if current is None:
            return None
        for op, comparator in zip(node.ops, node.comparators):
            other = _eval_operand(comparator, metrics, thresholds)
            if other is None:
                return None
            if not _ALLOWED_CMP_OPS[type(op)](current, other):
                return False
            current = other
        return True
    value = _eval_operand(node, metrics, thresholds)
    return None if value is None else bool(value)


def evaluate(compiled: CompiledExpr, metrics: dict[str, Any], thresholds: dict[str, Any]) -> bool:
    """Evaluates under Kleene logic (`_eval`) and collapses a top-level Unknown
    to False -- CLAUDE.md §4.6: a not_testable test's missing metric must never
    make a finding fire or a severity rule match by accident, but `not
    (x > 0)` with x None must itself read as Unknown while nested inside a
    larger expression, not silently become True."""
    result = _eval(compiled.tree.body, metrics, thresholds)
    return bool(result) if result is not None else False
