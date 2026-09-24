"""orchestrator.explorer.wire_schema -- the planner-cannot-emit-code gate
(CLAUDE.md §8 P8 DoD; docs/specs/P6_P8_explorer_llm_design.md §8 item 4).
Fully offline: no endpoint, no workspace."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import jsonschema
import pytest

from orchestrator.explorer.canonical import to_canonical
from orchestrator.explorer.validate import check_wire_schema
from orchestrator.explorer.wire_schema import (
    EXPLORER_PARAM_ALLOWLIST,
    EXPRESSION_FIELDS,
    PLAN_PROPOSAL_SCHEMA,
    PROSE_FIELDS,
    STRING_FIELD_CLASSIFICATION,
    walk_string_leaves,
)
from orchestrator.primitives import PRIMITIVES

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── (a) strict-compatible ────────────────────────────────────────────────


def _walk_objects(schema, seen=None):
    seen = seen if seen is not None else set()
    sid = id(schema)
    if sid in seen or not isinstance(schema, dict):
        return
    seen.add(sid)
    if schema.get("type") == "object" or "properties" in schema:
        assert schema.get("additionalProperties") is False, schema
        props = set(schema.get("properties", {}))
        assert set(schema.get("required", [])) == props, (
            f"every property must be required (nullable instead): {schema.get('required')} != {sorted(props)}"
        )
    assert "pattern" not in schema, f"no 'pattern' allowed in the wire schema: {schema}"
    assert "oneOf" not in schema, f"no 'oneOf' allowed (rewrite as anyOf): {schema}"
    if schema.get("type") == "object" and not schema.get("properties"):
        assert schema is not PLAN_PROPOSAL_SCHEMA, "no bare {'type': 'object'} property allowed"
    for v in schema.values():
        if isinstance(v, dict):
            _walk_objects(v, seen)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _walk_objects(item, seen)


def test_schema_is_valid_jsonschema():
    jsonschema.Draft202012Validator.check_schema(PLAN_PROPOSAL_SCHEMA)


def test_schema_every_object_closed_and_fully_required():
    _walk_objects(PLAN_PROPOSAL_SCHEMA)


def test_schema_has_no_bare_object_property():
    def _find_bare(schema, path=""):
        if not isinstance(schema, dict):
            return
        if schema.get("type") == "object" and "properties" not in schema and "$ref" not in schema:
            pytest.fail(f"bare {{'type': 'object'}} property at {path}")
        for k, v in schema.items():
            if isinstance(v, dict):
                _find_bare(v, f"{path}.{k}")
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, dict):
                        _find_bare(item, f"{path}.{k}[{i}]")

    _find_bare(PLAN_PROPOSAL_SCHEMA)


# ── (b) every string leaf is classified ─────────────────────────────────


def test_every_string_leaf_is_classified():
    found = set(walk_string_leaves())
    known = set(STRING_FIELD_CLASSIFICATION)
    assert found == known, f"unclassified: {found - known}; stale: {known - found}"


def test_prose_fields_match_classification():
    prose = {p for p, k in STRING_FIELD_CLASSIFICATION.items() if k == "prose"}
    assert prose == PROSE_FIELDS


def test_identifier_and_literal_fields_are_never_treated_as_prose():
    for path, kind in STRING_FIELD_CLASSIFICATION.items():
        assert kind in ("prose", "identifier", "literal", "expression", "enum"), (path, kind)


# ── (c) the only expression fields are trigger and when ─────────────────


def test_only_trigger_and_when_are_expression_fields():
    expr_fields = {p for p, k in STRING_FIELD_CLASSIFICATION.items() if k == "expression"}
    assert expr_fields == EXPRESSION_FIELDS == {"findings.trigger", "findings.severity.when"}


def test_expression_fields_compile_only_under_orchestrator_expr():
    from orchestrator.expr import compile_expr

    compile_expr("m1 > 0", known_metrics={"m1"}, known_thresholds=set())
    with pytest.raises(Exception):
        compile_expr("__import__('os')", known_metrics=set(), known_thresholds=set())


# ── (d) allowlist subset of each primitive's real PARAMS_SCHEMA ─────────


def test_allowlist_is_subset_of_primitive_params_schema():
    for name, allowed in EXPLORER_PARAM_ALLOWLIST.items():
        real = set(PRIMITIVES[name].PARAMS_SCHEMA["properties"])
        assert set(allowed) <= real, (name, set(allowed) - real)


def test_allowlist_covers_every_primitive():
    assert set(EXPLORER_PARAM_ALLOWLIST) == set(PRIMITIVES)


# ── (e) adversarial corpus, each rejected with the expected rule id ─────


def _base_wire() -> dict:
    return {
        "schema_version": "explorer-plan/1", "skill_name": "s", "domain": "d", "summary": "sum",
        "sources": [{"source": "src", "amount_column": None, "date_column": None, "entry_key": None}],
        "populations": [{"key": "p1", "source": "src", "description": "d", "filters": []}],
        "risks": [{"key": "r1", "title": "t", "description": "d"}],
        "controls": [{"key": "c1", "risk_key": "r1", "title": "t", "description": "d", "type": "preventive"}],
        "thresholds": [{"id": "th1", "value": 5, "unit": "count", "description": "d"}],
        "tests": [{
            "key": "t1", "name": "n", "primitive": "threshold_exceedance",
            "params": {
                "kind": "threshold_exceedance", "population": "p1", "column": "Amount",
                "limit": {"threshold": "th1"}, "direction": "above",
                "group_by": None, "aggregate": None, "exclude": None,
                "metrics": [{"name": "m1", "kind": "count", "column": None, "key": None, "unit": "count", "where": None}],
            },
            "control_key": "c1", "risk_key": "r1", "assertion": "operating",
            "control_objective": "o", "risk_hypothesis": "h", "rationale": "r",
        }],
        "findings": [{
            "key": "f1", "test_key": "t1", "title": "t", "trigger": "m1 > 0",
            "severity": [{"when": None, "then": "Low"}],
            "metrics_cited": ["m1"], "thresholds_cited": [], "monetary_basis": "none",
            "observation": "o", "recommendation": "r", "management_questions": [],
        }],
        "data_gaps": [], "assumptions": [],
    }


def _profile() -> dict:
    return {"src": {"row_count": 1, "null_counts": {}, "columns": [
        {"name": "Amount", "type": "number", "null_count": 0, "distinct_count": 1, "unique": True,
         "semantic_type": "amount", "pii": False, "pii_basis": None, "min": 1, "max": 1,
         "negative_count": 0, "zero_count": 0},
    ]}}


def _errs_for(wire: dict) -> str:
    from orchestrator.explorer.validate import validate_wire_proposal

    report = validate_wire_proposal(
        wire, profile=_profile(), run_sources=["src"], data_source=None, pinned_versions={"src": "v1"},
    )
    msgs = [e["rule"] for e in report["proposal_errors"]]
    for t in report["tests"].values():
        msgs += [r["rule"] for r in t["reasons"]]
    for f in report["findings"].values():
        msgs += [r["rule"] for r in f["reasons"]]
    return " ".join(msgs)


def test_adversarial_column_with_sql_injection_text():
    wire = _base_wire()
    wire["tests"][0]["params"]["column"] = "x; DROP TABLE"
    assert "V-T3" in _errs_for(wire)


def test_adversarial_trigger_import():
    wire = _base_wire()
    wire["findings"][0]["trigger"] = "__import__('os')"
    assert "V-N2" in _errs_for(wire)


def test_adversarial_trigger_nonzero_constant():
    wire = _base_wire()
    wire["findings"][0]["trigger"] = "m1 > 5"
    assert "V-N2" in _errs_for(wire)


def test_adversarial_observation_dunder_attribute():
    wire = _base_wire()
    wire["findings"][0]["observation"] = "{m1.__class__}"
    assert "V-P2" in _errs_for(wire)


def test_adversarial_observation_conversion_spec():
    wire = _base_wire()
    wire["findings"][0]["observation"] = "{m1!r}"
    assert "V-P2" in _errs_for(wire)


def test_adversarial_dollar_amount_in_prose():
    wire = _base_wire()
    wire["tests"][0]["rationale"] = "over $5,000"
    assert "V-P1" in _errs_for(wire)


def test_adversarial_select_in_rationale():
    wire = _base_wire()
    wire["tests"][0]["rationale"] = "run a SELECT statement"
    assert "V-P3" in _errs_for(wire)


def test_adversarial_custom_primitive_name():
    s1_errors = check_wire_schema(_wire_with(primitive="my_custom_primitive"))
    assert s1_errors


def _wire_with(**overrides) -> dict:
    wire = _base_wire()
    if "primitive" in overrides:
        wire["tests"][0]["primitive"] = overrides["primitive"]
    return wire


def test_adversarial_allowed_values_ref_object():
    wire = _base_wire()
    wire["tests"][0]["primitive"] = "list_membership"
    wire["tests"][0]["params"] = {
        "kind": "list_membership", "population": "p1", "column": "Amount",
        "allowed_values": {"ref": "x"}, "negate": False, "match": "exact",
        "metrics": [{"name": "m1", "kind": "count", "column": None, "key": None, "unit": "count", "where": None}],
    }
    errors = check_wire_schema(wire)
    assert errors, "allowed_values as an object should fail V-S1 (jsonschema)"


def test_adversarial_filter_literal_value_not_profiled():
    wire = _base_wire()
    wire["populations"][0]["filters"] = [{"column": "Amount", "op": "eq", "value": 999}]
    assert "V-F2" in _errs_for(wire)


def test_adversarial_unprofiled_string_filter_value():
    wire = _base_wire()
    wire["sources"][0]["amount_column"] = None
    prof = _profile()
    prof["src"]["columns"].append({
        "name": "Category", "type": "string", "null_count": 0, "distinct_count": 1, "unique": False,
        "semantic_type": "category", "pii": False, "pii_basis": None,
        "values": [{"value": "Travel", "count": 1}], "suppressed_values": 0,
    })
    wire["populations"][0]["filters"] = [{"column": "Category", "op": "eq", "value": "NotProfiled"}]
    from orchestrator.explorer.validate import validate_wire_proposal
    report = validate_wire_proposal(
        wire, profile=prof, run_sources=["src"], data_source=None, pinned_versions={"src": "v1"},
    )
    all_msgs = " ".join(r["rule"] for t in report["tests"].values() for r in t["reasons"])
    assert "V-F2" in all_msgs


def test_adversarial_assertion_design():
    wire = _base_wire()
    wire["tests"][0]["assertion"] = "design"
    s1_errors = check_wire_schema(wire)
    assert s1_errors, "assertion: design should fail V-S1 (only 'operating' is in the wire enum)"


def test_adversarial_severity_no_trailing_null_when():
    wire = _base_wire()
    wire["findings"][0]["severity"] = [{"when": "m1 > 0", "then": "High"}]
    canonical = to_canonical(wire)
    assert canonical["_canonicalization_errors"]


def test_adversarial_extra_top_level_key():
    wire = _base_wire()
    wire["unexpected_key"] = "x"
    errors = check_wire_schema(wire)
    assert errors


# ── (f) static check: no eval/exec/subprocess/importlib/SQL/str.format ──


def test_no_dangerous_calls_in_explorer_or_llm_packages():
    # AST-based, not a substring scan: a dangerous BUILTIN CALL (`eval(`,
    # `exec(`, bare `compile(`) is what this forbids, not the word
    # appearing in a string literal -- e.g. validate.py's own
    # `_FORBIDDEN_SUBSTRINGS` tuple legitimately contains the text
    # "exec(" as DATA (what V-P3 itself screens prose for), which a naive
    # text scan would misreport as a violation of the very rule it
    # implements.
    import_forbidden = re.compile(r"^\s*(import subprocess|from subprocess|import importlib(?!\.util)|from importlib(?!\.util))", re.MULTILINE)
    token_forbidden = re.compile(r"\bsubprocess\.|\bimportlib(?!\.util)\.|sql_connector")
    dangerous_builtins = {"eval", "exec", "compile"}
    for base in ("orchestrator/explorer", "orchestrator/llm"):
        base_dir = REPO_ROOT / base
        if not base_dir.is_dir():
            continue
        for path in base_dir.rglob("*.py"):
            text = path.read_text()
            for m in import_forbidden.finditer(text):
                pytest.fail(f"{path}: forbidden import {m.group(0)!r}")
            for m in token_forbidden.finditer(text):
                pytest.fail(f"{path}: forbidden pattern {m.group(0)!r}")
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in dangerous_builtins:
                    pytest.fail(f"{path}:{node.lineno}: bare {node.func.id}() call")


def test_no_str_format_on_model_text_in_explorer_package():
    # A model-authored template is rendered only via the restricted
    # placeholder check in validate.py -- never Python's str.format(), which
    # permits attribute/index access ({0.__class__}) a JSON-safe placeholder
    # walk does not.
    explorer_dir = REPO_ROOT / "orchestrator" / "explorer"
    for path in explorer_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                pytest.fail(f"{path}:{node.lineno}: str.format() call -- render templates via the placeholder walk only")
