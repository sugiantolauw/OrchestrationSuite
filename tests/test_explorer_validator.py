"""orchestrator.explorer.validate -- one positive and one negative case per
rule id in docs/specs/P6_P8_explorer_llm_design.md §4.7 (CLAUDE.md §8 P8
DoD gate test item 5). Fully offline: no endpoint, no workspace.

`_base_proposal()`/`_base_profile()` build one small, entirely valid
canonical proposal + profile; every negative test mutates exactly the one
field the rule under test checks, so a failing assertion always points at
the rule that regressed."""

from __future__ import annotations

import pytest

from orchestrator.explorer.validate import validate_proposal


class _FakeDataSource:
    def __init__(self, distinct: int):
        self._distinct = distinct

    def distinct_count(self, source, *, version, columns):
        return self._distinct


def _col(name, type_, **kw):
    base = {"name": name, "type": type_, "null_count": 0, "distinct_count": 1, "unique": False,
            "semantic_type": kw.pop("semantic_type", "other"), "pii": kw.pop("pii", False), "pii_basis": None}
    base.update(kw)
    return base


def _base_profile() -> dict:
    return {
        "expense_report": {
            "row_count": 10, "null_counts": {},
            "columns": [
                _col("Amount", "number", semantic_type="amount", min=1.0, max=100.0,
                     negative_count=0, zero_count=0, distinct_count=10, unique=False),
                _col("Transaction Date", "date", semantic_type="date", min="2026-01-01", max="2026-01-31",
                     distinct_count=5),
                _col("Category", "string", semantic_type="category", distinct_count=2,
                     values=[{"value": "Travel", "count": 6}, {"value": "Meals", "count": 4}], suppressed_values=0),
                _col("Currency", "string", semantic_type="currency_code", distinct_count=1,
                     values=[{"value": "AUD", "count": 10}], suppressed_values=0),
                _col("Employee ID", "integer", semantic_type="identifier", pii=True, unique=True, distinct_count=10),
                _col("Employee ID 2", "integer", semantic_type="identifier", pii=True, unique=False, distinct_count=5),
            ],
        }
    }


def _base_canonical() -> dict:
    return {
        "schema_version": "explorer-plan/1", "skill_name": "Test Skill", "domain": "Travel",
        "summary": "A clean summary",
        "sources": [{"source": "expense_report", "amount_column": "Amount", "date_column": "Transaction Date",
                     "entry_key": ["Employee ID"]}],
        "populations": [{"key": "p1", "source": "expense_report", "description": "All claims",
                          "filters": [{"column": "Category", "op": "eq", "value": "Travel"}]}],
        "risks": [{"key": "r1", "title": "A risk", "description": "Some risk"}],
        "controls": [{"key": "c1", "risk_key": "r1", "title": "A control", "description": "Some control",
                      "type": "preventive"}],
        "thresholds": [{"id": "hv", "value": 50, "unit": "currency", "description": "High value limit"}],
        "tests": [{
            "key": "t1", "name": "High value test", "primitive": "threshold_exceedance",
            "params": {
                "population": "p1", "column": "Amount", "limit": {"threshold": "hv"}, "direction": "above",
                "metrics": {
                    "hv_count": {"kind": "count", "unit": "count"},
                    "hv_amount": {"kind": "sum", "column": "Amount", "unit": "currency"},
                },
            },
            "control_key": "c1", "risk_key": "r1", "assertion": "operating",
            "control_objective": "objective text", "risk_hypothesis": "hypothesis text", "rationale": "rationale text",
        }],
        "findings": [{
            "key": "f1", "test_key": "t1", "title": "High value claims", "trigger": "hv_count > 0",
            "severity": [{"when": "hv_count > 0", "then": "High"}, {"else": "Low"}],
            "metrics_cited": ["hv_count", "hv_amount"], "thresholds_cited": ["hv"], "monetary_basis": "spend",
            "observation": "{hv_count} claims over the limit, totalling {hv_amount}.",
            "recommendation": "Review high value claims.",
            "management_questions": ["What controls exist?"],
        }],
        "data_gaps": [], "assumptions": [],
    }


def _validate(canonical, profile=None, run_sources=("expense_report",), data_source=None):
    return validate_proposal(
        canonical, profile=profile or _base_profile(), run_sources=list(run_sources),
        data_source=data_source or _FakeDataSource(10), pinned_versions={"expense_report": "v1"},
    )


def _test_valid(report, key="t1") -> bool:
    return report["tests"].get(key, {}).get("valid", False)


def _finding_valid(report, key="f1") -> bool:
    return report["findings"].get(key, {}).get("valid", False)


def _rules(report, key="t1") -> set[str]:
    return {r["rule"] for r in report["tests"].get(key, {}).get("reasons", [])}


def _finding_rules(report, key="f1") -> set[str]:
    return {r["rule"] for r in report["findings"].get(key, {}).get("reasons", [])}


def _proposal_rules(report) -> set[str]:
    return {r["rule"] for r in report["proposal_errors"]}


# ── happy path ───────────────────────────────────────────────────────────


def test_base_proposal_is_fully_valid():
    report = _validate(_base_canonical())
    assert report["proposal_errors"] == []
    assert _test_valid(report)
    assert _finding_valid(report)


# ── V-S2 ─────────────────────────────────────────────────────────────────


def test_v_s2_negative_bad_key_format():
    c = _base_canonical()
    c["populations"][0]["key"] = "P1-bad"
    report = _validate(c)
    assert "V-S2" in _proposal_rules(report)


def test_v_s2_negative_duplicate_key():
    c = _base_canonical()
    c["risks"].append({"key": "r1", "title": "dup", "description": "d"})
    report = _validate(c)
    assert "V-S2" in _proposal_rules(report)


def test_v_s2_positive():
    report = _validate(_base_canonical())
    assert "V-S2" not in _proposal_rules(report)


# ── V-S3 ─────────────────────────────────────────────────────────────────


def test_v_s3_negative_too_many_tests():
    c = _base_canonical()
    template = c["tests"][0]
    c["tests"] = [dict(template, key=f"t{i}") for i in range(16)]
    report = _validate(c)
    assert "V-S3" in _proposal_rules(report)


def test_v_s3_positive():
    report = _validate(_base_canonical())
    assert "V-S3" not in _proposal_rules(report)


# ── V-S4 ─────────────────────────────────────────────────────────────────


def test_v_s4_negative_source_not_a_run_source():
    c = _base_canonical()
    report = _validate(c, run_sources=["other_source"])
    assert "V-S4" in _proposal_rules(report)


def test_v_s4_positive():
    report = _validate(_base_canonical())
    assert "V-S4" not in _proposal_rules(report)


# ── V-P1 ─────────────────────────────────────────────────────────────────


def test_v_p1_negative_digit_in_prose():
    c = _base_canonical()
    c["tests"][0]["rationale"] = "over $5,000"
    report = _validate(c)
    assert "V-P1/V-P3" in _rules(report)


def test_v_p1_positive_placeholder_and_column_exempted():
    c = _base_canonical()
    c["findings"][0]["observation"] = "{hv_count} claims, `Amount` column."
    report = _validate(c)
    assert "V-P1/V-P3" not in _finding_rules(report)


# ── V-P2 ─────────────────────────────────────────────────────────────────


def test_v_p2_negative_conversion_spec():
    c = _base_canonical()
    c["findings"][0]["observation"] = "{hv_count!r} claims."
    report = _validate(c)
    assert "V-P2" in _finding_rules(report)


def test_v_p2_negative_placeholder_not_cited():
    c = _base_canonical()
    c["findings"][0]["observation"] = "{not_a_real_metric} claims."
    report = _validate(c)
    assert "V-P2" in _finding_rules(report)


def test_v_p2_positive():
    report = _validate(_base_canonical())
    assert "V-P2" not in _finding_rules(report)


# ── V-P3 ─────────────────────────────────────────────────────────────────


def test_v_p3_negative_code_substring():
    c = _base_canonical()
    c["tests"][0]["rationale"] = "run a SELECT statement here"
    report = _validate(c)
    assert "V-P1/V-P3" in _rules(report)


def test_v_p3_positive():
    report = _validate(_base_canonical())
    assert "V-P1/V-P3" not in _rules(report)


# ── V-F1 ─────────────────────────────────────────────────────────────────


def test_v_f1_negative_unknown_column():
    c = _base_canonical()
    c["populations"][0]["filters"] = [{"column": "Nonexistent", "op": "eq", "value": "x"}]
    report = _validate(c)
    assert "V-F1" in _rules(report)


def test_v_f1_positive():
    report = _validate(_base_canonical())
    assert "V-F1" not in _rules(report)


# ── V-F2 ─────────────────────────────────────────────────────────────────


def test_v_f2_negative_unprofiled_value():
    c = _base_canonical()
    c["populations"][0]["filters"] = [{"column": "Category", "op": "eq", "value": "NotProfiled"}]
    report = _validate(c)
    assert "V-F2" in _rules(report)


def test_v_f2_positive_zero_is_always_allowed():
    c = _base_canonical()
    c["populations"][0]["filters"] = [{"column": "Amount", "op": "gt", "value": 0}]
    report = _validate(c)
    assert "V-F2" not in _rules(report)


# ── V-F3 ─────────────────────────────────────────────────────────────────


def test_v_f3_negative_between_not_audit_period():
    c = _base_canonical()
    c["populations"][0]["filters"] = [{"column": "Transaction Date", "op": "between", "value": ["2026-01-01", "2026-01-31"]}]
    report = _validate(c)
    assert "V-F3" in _rules(report)


def test_v_f3_positive_between_audit_period():
    c = _base_canonical()
    c["populations"][0]["filters"] = [{"column": "Transaction Date", "op": "between", "value": {"ref": "audit_period"}}]
    report = _validate(c)
    assert "V-F3" not in _rules(report)


# ── V-C1 ─────────────────────────────────────────────────────────────────


def test_v_c1_negative_no_currency_column():
    c = _base_canonical()
    profile = _base_profile()
    profile["expense_report"]["columns"] = [
        c2 for c2 in profile["expense_report"]["columns"] if c2["semantic_type"] != "currency_code"
    ]
    report = _validate(c, profile=profile)
    assert "V-C1" in _rules(report)


def test_v_c1_positive():
    report = _validate(_base_canonical())
    assert "V-C1" not in _rules(report)


# ── V-C2 ─────────────────────────────────────────────────────────────────


def test_v_c2_negative_amount_column_has_nulls():
    c = _base_canonical()
    profile = _base_profile()
    for col in profile["expense_report"]["columns"]:
        if col["name"] == "Amount":
            col["null_count"] = 3
    report = _validate(c, profile=profile)
    assert "V-C2" in _rules(report)


def test_v_c2_positive():
    report = _validate(_base_canonical())
    assert "V-C2" not in _rules(report)


# ── V-C3 ─────────────────────────────────────────────────────────────────


def test_v_c3_negative_composite_key_not_unique():
    c = _base_canonical()
    c["sources"][0]["entry_key"] = ["Employee ID", "Employee ID 2"]
    report = _validate(c, data_source=_FakeDataSource(distinct=5))  # 5 != row_count 10
    assert "V-C3" in _rules(report)


def test_v_c3_positive_composite_key_unique():
    c = _base_canonical()
    c["sources"][0]["entry_key"] = ["Employee ID", "Employee ID 2"]
    report = _validate(c, data_source=_FakeDataSource(distinct=10))  # == row_count
    assert "V-C3" not in _rules(report)


# ── V-T1 ─────────────────────────────────────────────────────────────────


def test_v_t1_negative_unknown_primitive():
    c = _base_canonical()
    c["tests"][0]["primitive"] = "not_a_real_primitive"
    report = _validate(c)
    assert "V-T1" in _rules(report)


def test_v_t1_positive():
    report = _validate(_base_canonical())
    assert "V-T1" not in _rules(report)


# ── V-T2 ─────────────────────────────────────────────────────────────────


def test_v_t2_negative_missing_required_param():
    c = _base_canonical()
    del c["tests"][0]["params"]["direction"]
    report = _validate(c)
    assert "V-T2" in _rules(report)


def test_v_t2_positive():
    report = _validate(_base_canonical())
    assert "V-T2" not in _rules(report)


# ── V-T3 ─────────────────────────────────────────────────────────────────


def test_v_t3_negative_column_not_in_profile():
    c = _base_canonical()
    c["tests"][0]["params"]["column"] = "NotAColumn"
    report = _validate(c)
    assert "V-T3" in _rules(report)


def test_v_t3_positive():
    report = _validate(_base_canonical())
    assert "V-T3" not in _rules(report)


# ── V-T4 ─────────────────────────────────────────────────────────────────


def test_v_t4_negative_unknown_threshold_ref():
    c = _base_canonical()
    c["tests"][0]["params"]["limit"] = {"threshold": "no_such_threshold"}
    report = _validate(c)
    assert "V-T4" in _rules(report)


def test_v_t4_positive():
    report = _validate(_base_canonical())
    assert "V-T4" not in _rules(report)


# ── V-T5 ─────────────────────────────────────────────────────────────────


def test_v_t5_negative_no_metrics():
    c = _base_canonical()
    c["tests"][0]["params"]["metrics"] = {}
    report = _validate(c)
    assert "V-T5" in _rules(report)


def test_v_t5_positive():
    report = _validate(_base_canonical())
    assert "V-T5" not in _rules(report)


# ── V-T6 ─────────────────────────────────────────────────────────────────


def test_v_t6_negative_control_risk_mismatch():
    c = _base_canonical()
    c["controls"][0]["risk_key"] = "some_other_risk"
    report = _validate(c)
    assert "V-T6" in _rules(report)


def test_v_t6_positive():
    report = _validate(_base_canonical())
    assert "V-T6" not in _rules(report)


# ── V-T7 ─────────────────────────────────────────────────────────────────


def test_v_t7_negative_dry_run_catches_incompatible_params():
    # A `kind: excess` metric is a JSON-Schema-legal `metrics[]` entry for
    # EVERY primitive (METRICS_PROPERTY_SCHEMA's kind enum is shared,
    # orchestrator.primitives.common) but `build_metrics` raises
    # `PrimitiveParamsError` for any primitive that never actually computes
    # an excess total (attribute_missing is one) -- exactly the shape of
    # bug V-T7 exists to catch: valid against the wire/params schema, only
    # caught by actually running the primitive.
    c = _base_canonical()
    c["tests"][0]["primitive"] = "attribute_missing"
    c["tests"][0]["params"] = {
        "population": "p1", "column": "Category", "condition": "is_null",
        "metrics": {"am_excess": {"kind": "excess", "unit": "currency"}},
    }
    report = _validate(c)
    assert "V-T7" in _rules(report)


def test_v_t7_positive():
    report = _validate(_base_canonical())
    assert "V-T7" not in _rules(report)


# ── V-T8 ─────────────────────────────────────────────────────────────────


def test_v_t8_negative_no_finding_for_test():
    c = _base_canonical()
    c["findings"] = []
    report = _validate(c)
    assert "V-T8" in _rules(report)
    assert not _test_valid(report)


def test_v_t8_positive():
    report = _validate(_base_canonical())
    assert "V-T8" not in _rules(report)


# ── V-N1 ─────────────────────────────────────────────────────────────────


def test_v_n1_negative_metric_not_this_tests_own():
    c = _base_canonical()
    c["findings"][0]["metrics_cited"] = ["hv_count", "some_other_metric"]
    report = _validate(c)
    assert "V-N1" in _finding_rules(report)


def test_v_n1_positive():
    report = _validate(_base_canonical())
    assert "V-N1" not in _finding_rules(report)


# ── V-N2 ─────────────────────────────────────────────────────────────────


def test_v_n2_negative_nonzero_literal():
    c = _base_canonical()
    c["findings"][0]["trigger"] = "hv_count > 5"
    report = _validate(c)
    assert "V-N2" in _finding_rules(report)


def test_v_n2_positive():
    report = _validate(_base_canonical())
    assert "V-N2" not in _finding_rules(report)


# ── V-N3 ─────────────────────────────────────────────────────────────────


def test_v_n3_negative_spend_without_entry_key():
    c = _base_canonical()
    c["sources"][0]["entry_key"] = None
    report = _validate(c)
    assert "V-N3" in _finding_rules(report)


def test_v_n3_positive():
    report = _validate(_base_canonical())
    assert "V-N3" not in _finding_rules(report)


# ── V-H1 ─────────────────────────────────────────────────────────────────


def test_v_h1_negative_percent_out_of_range():
    c = _base_canonical()
    c["thresholds"][0]["unit"] = "%"
    c["thresholds"][0]["value"] = 150
    report = _validate(c)
    assert "V-H1" in _rules(report)


def test_v_h1_positive_unused_threshold_is_a_warning_not_an_error():
    c = _base_canonical()
    c["thresholds"].append({"id": "unused_th", "value": 10, "unit": "count", "description": "d"})
    report = _validate(c)
    assert "V-H1" not in _rules(report)
    assert any("unused_th" in w for w in report["warnings"])
