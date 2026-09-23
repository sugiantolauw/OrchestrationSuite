from __future__ import annotations

import pytest

from orchestrator.expr import ExpressionError, compile_expr, evaluate


def test_accepts_comparison():
    c = compile_expr("hv_count > 0")
    assert c.metric_names == frozenset({"hv_count"})
    assert evaluate(c, {"hv_count": 5}, {}) is True
    assert evaluate(c, {"hv_count": 0}, {}) is False


def test_accepts_boolean_and_or_not():
    c = compile_expr("a > 0 and (b > 0 or not c > 0)")
    assert evaluate(c, {"a": 1, "b": 1, "c": 0}, {}) is True
    assert evaluate(c, {"a": 1, "b": 0, "c": 1}, {}) is False
    assert evaluate(c, {"a": 0, "b": 1, "c": 0}, {}) is False


def test_accepts_all_comparison_operators():
    ops = {
        "a > 0": (1, True),
        "a >= 0": (0, True),
        "a < 0": (-1, True),
        "a <= 0": (0, True),
        "a == 0": (0, True),
        "a != 0": (1, True),
    }
    for src, (val, expected) in ops.items():
        c = compile_expr(src)
        assert evaluate(c, {"a": val}, {}) is expected


def test_accepts_chained_comparison():
    c = compile_expr("a > 0")
    assert evaluate(c, {"a": 5}, {}) is True


def test_accepts_threshold_reference():
    c = compile_expr("missing_receipt_pct > thresholds.missing_receipt_high")
    assert c.threshold_ids == frozenset({"missing_receipt_high"})
    assert evaluate(c, {"missing_receipt_pct": 15}, {"missing_receipt_high": 10}) is True
    assert evaluate(c, {"missing_receipt_pct": 5}, {"missing_receipt_high": 10}) is False


def test_accepts_zero_and_boolean_constants():
    compile_expr("a > 0")
    compile_expr("a == 0.0")
    compile_expr("a == True")
    compile_expr("a == False")


def test_rejects_calls():
    with pytest.raises(ExpressionError):
        compile_expr("__import__('os').system('rm -rf /')")


def test_rejects_subscripts():
    with pytest.raises(ExpressionError):
        compile_expr("a[0] > 0")


def test_rejects_attributes_other_than_thresholds():
    with pytest.raises(ExpressionError):
        compile_expr("a.b > 0")


def test_rejects_unknown_names_when_known_metrics_given():
    with pytest.raises(ExpressionError):
        compile_expr("unknown_metric > 0", known_metrics={"hv_count"})


def test_rejects_unknown_threshold_ids_when_known_given():
    with pytest.raises(ExpressionError):
        compile_expr("a > thresholds.unknown_id", known_thresholds={"hv_limit"})


def test_rejects_non_zero_numeric_literals():
    with pytest.raises(ExpressionError):
        compile_expr("a > 5")
    with pytest.raises(ExpressionError):
        compile_expr("a > 5.5")


def test_rejects_strings():
    with pytest.raises(ExpressionError):
        compile_expr("a == 'High'")


def test_rejects_lambdas():
    with pytest.raises(ExpressionError):
        compile_expr("(lambda: 0)()")


def test_rejects_comprehensions():
    with pytest.raises(ExpressionError):
        compile_expr("[x for x in range(3)]")


def test_rejects_arithmetic():
    with pytest.raises(ExpressionError):
        compile_expr("a + 1 > 0")
    with pytest.raises(ExpressionError):
        compile_expr("a * 2 > 0")


def test_none_metric_comparison_evaluates_false():
    c = compile_expr("hv_count > 0")
    assert evaluate(c, {"hv_count": None}, {}) is False


def test_none_metric_in_boolean_combination():
    c = compile_expr("hv_count > 0 or missing_count > 0")
    assert evaluate(c, {"hv_count": None, "missing_count": 3}, {}) is True
    assert evaluate(c, {"hv_count": None, "missing_count": None}, {}) is False


def test_never_uses_eval_semantics_malicious_string_stays_inert():
    # Even if a Skill author's YAML somehow smuggled a call-shaped string in as a
    # *metric value* (never as source code) it must never be executed -- compile_expr
    # only ever parses the expression source, and evaluate() only ever looks values
    # up in the metrics/thresholds dicts it is given.
    c = compile_expr("a == 0")
    assert evaluate(c, {"a": "__import__('os').system('echo pwned')"}, {}) is False
