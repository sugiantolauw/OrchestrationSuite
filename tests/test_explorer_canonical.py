"""orchestrator.explorer.canonical.to_canonical (docs/specs/
P6_P8_explorer_llm_design.md §4.6). Fully offline, pure-function tests."""

from __future__ import annotations

from orchestrator.explorer.canonical import to_canonical


def _wire_finding(severity):
    return {
        "key": "f1", "test_key": "t1", "title": "t", "trigger": "m1 > 0",
        "severity": severity, "metrics_cited": ["m1"], "thresholds_cited": [],
        "monetary_basis": "none", "observation": "o", "recommendation": "r",
        "management_questions": [],
    }


def _minimal_wire(**overrides):
    wire = {
        "schema_version": "explorer-plan/1", "skill_name": "s", "domain": "d", "summary": "sum",
        "sources": [{"source": "src", "amount_column": None, "date_column": None, "entry_key": None}],
        "populations": [{"key": "p1", "source": "src", "description": "d", "filters": []}],
        "risks": [], "controls": [], "thresholds": [], "tests": [], "findings": [],
        "data_gaps": [], "assumptions": [],
    }
    wire.update(overrides)
    return wire


def test_severity_last_null_when_becomes_else():
    wire = _minimal_wire(findings=[_wire_finding([{"when": "m1 > 0", "then": "High"}, {"when": None, "then": "Low"}])])
    canonical = to_canonical(wire)
    assert canonical["findings"][0]["severity"] == [{"when": "m1 > 0", "then": "High"}, {"else": "Low"}]
    assert "_canonicalization_errors" not in canonical


def test_severity_null_when_not_last_records_error():
    wire = _minimal_wire(findings=[_wire_finding([{"when": None, "then": "Low"}, {"when": "m1 > 0", "then": "High"}])])
    canonical = to_canonical(wire)
    assert canonical["_canonicalization_errors"]
    assert any("t is not the last" in e for e in canonical["_canonicalization_errors"])


def test_severity_last_not_null_records_error():
    wire = _minimal_wire(findings=[_wire_finding([{"when": "m1 > 0", "then": "High"}])])
    canonical = to_canonical(wire)
    assert canonical["_canonicalization_errors"]
    assert any("last rule must have when: null" in e for e in canonical["_canonicalization_errors"])


def test_metrics_array_becomes_map_dropping_nulls():
    wire = _minimal_wire(tests=[{
        "key": "t1", "name": "n", "primitive": "threshold_exceedance",
        "params": {
            "kind": "threshold_exceedance", "population": "p1", "column": "Amount",
            "limit": {"threshold": "hv"}, "direction": "above",
            "group_by": None, "aggregate": None, "exclude": None,
            "metrics": [
                {"name": "m1", "kind": "count", "column": None, "key": None, "unit": "count", "where": None},
                {"name": "m2", "kind": "sum", "column": "Amount", "key": None, "unit": "currency",
                 "where": {"column": "Amount", "op": "gt", "value": 0}},
            ],
        },
        "control_key": "c1", "risk_key": "r1", "assertion": "operating",
        "control_objective": "o", "risk_hypothesis": "h", "rationale": "r",
    }])
    canonical = to_canonical(wire)
    params = canonical["tests"][0]["params"]
    assert "kind" not in params
    metrics = params["metrics"]
    assert metrics["m1"] == {"kind": "count", "unit": "count"}
    assert metrics["m2"] == {"kind": "sum", "column": "Amount", "unit": "currency",
                              "where": {"column": "Amount", "op": "gt", "value": 0}}


def test_between_audit_period_filter_conversion():
    wire = _minimal_wire(populations=[{
        "key": "p1", "source": "src", "description": "d",
        "filters": [{"column": None, "op": None, "value": None,
                     "between_audit_period": {"column": "Transaction Date"},
                     "any_of": None, "all_of": None}],
    }])
    canonical = to_canonical(wire)
    assert canonical["populations"][0]["filters"] == [
        {"column": "Transaction Date", "op": "between", "value": {"ref": "audit_period"}}
    ]


def test_any_of_and_all_of_recurse():
    wire = _minimal_wire(populations=[{
        "key": "p1", "source": "src", "description": "d",
        "filters": [{
            "column": None, "op": None, "value": None, "between_audit_period": None,
            "any_of": [
                {"column": "A", "op": "eq", "value": "1", "between_audit_period": None, "any_of": None, "all_of": None},
                {"column": "B", "op": "eq", "value": "2", "between_audit_period": None, "any_of": None, "all_of": None},
            ],
            "all_of": None,
        }],
    }])
    canonical = to_canonical(wire)
    assert canonical["populations"][0]["filters"] == [
        {"any_of": [{"column": "A", "op": "eq", "value": "1"}, {"column": "B", "op": "eq", "value": "2"}]}
    ]


def test_sources_drop_null_optional_fields():
    wire = _minimal_wire(sources=[{"source": "src", "amount_column": None, "date_column": None, "entry_key": None}])
    canonical = to_canonical(wire)
    assert canonical["sources"] == [{"source": "src"}]


def test_is_deterministic():
    wire = _minimal_wire(findings=[_wire_finding([{"when": None, "then": "Low"}])])
    assert to_canonical(wire) == to_canonical(wire)


def test_upper_case_keys_are_lowercased_consistently():
    """BUG-EXPLORER-B-ZERO-TESTS (independent review round 5, RUN-20E8643022BB):
    the planner proposed a schema-valid, otherwise-correct test/finding
    using upper-case keys ("R1", "C1", "T1_high_value", "F1_high_value").
    V-S2's ^[a-z][a-z0-9_]{1,31}$ pattern rejected all four, and the
    repair round that followed dropped the test/finding entirely rather
    than fixing the casing -- a real proposal ended with zero tests.
    to_canonical must lower-case every key/cross-reference the SAME way,
    so a proposal like this canonicalizes straight into something V-S2
    accepts and cross-references still resolve."""
    wire = _minimal_wire(
        risks=[{"key": "R1", "title": "t", "description": "d"}],
        controls=[{"key": "C1", "risk_key": "R1", "title": "t", "description": "d", "type": "detective"}],
        tests=[{
            "key": "T1_high_value", "name": "n", "primitive": "threshold_exceedance", "params": {},
            "control_key": "C1", "risk_key": "R1", "assertion": "operating",
            "control_objective": "o", "risk_hypothesis": "h", "rationale": "r",
        }],
        findings=[_wire_finding([{"when": None, "then": "Low"}])],
    )
    wire["findings"][0]["key"] = "F1_high_value"
    wire["findings"][0]["test_key"] = "T1_high_value"
    canonical = to_canonical(wire)

    assert canonical["risks"][0]["key"] == "r1"
    assert canonical["controls"][0]["key"] == "c1"
    assert canonical["controls"][0]["risk_key"] == "r1"
    assert canonical["tests"][0]["key"] == "t1_high_value"
    assert canonical["tests"][0]["control_key"] == "c1"
    assert canonical["tests"][0]["risk_key"] == "r1"
    assert canonical["findings"][0]["key"] == "f1_high_value"
    assert canonical["findings"][0]["test_key"] == "t1_high_value"
