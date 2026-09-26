"""BUG-EXPLORER-PLAN-1 (independent review round 5, RUN-B68ACB9ED712): the
`plan` node crashed with TypeError("unhashable type: 'list'") -- silently,
with no reason ever reaching the run page (NN14) -- for the governed
source combination approval_aging + attendee_validity + booking_detail.

Root cause: PLAN_PROPOSAL_SCHEMA's FILTER_VALUE wire type allows an ARRAY
`value` on every filter op, but orchestrator.explorer.validate._validate_
filter only guards the list case for "in"/"not_in" -- a "gt"/"gte"/"lt"/
"lte"/"eq"/"ne" filter whose `value` is itself a list reached
`_is_profiled_or_zero`'s `value in profiled` unguarded and crashed the
whole node (validate.py). Fully offline: no endpoint, no workspace."""

from __future__ import annotations

import jsonschema
import pytest

from orchestrator.explorer.validate import validate_wire_proposal


def _base_wire() -> dict:
    return {
        "schema_version": "explorer-plan/1", "skill_name": "s", "domain": "d", "summary": "sum",
        "sources": [{"source": "approval_aging", "amount_column": None, "date_column": None, "entry_key": None}],
        "populations": [{
            "key": "p1", "source": "approval_aging", "description": "d",
            "filters": [{
                "column": "Days of Approval from Receipt View", "op": "gt",
                # BUG-EXPLORER-PLAN-1: a LIST value on a non-in/not_in op --
                # this is exactly the shape that crashed the plan node.
                # FILTER_VALUE's array branch is string-only (wire_schema.py),
                # so a schema-valid instance of the bug is a list of strings.
                "value": ["3"],
            }],
        }],
        "risks": [{"key": "r1", "title": "t", "description": "d"}],
        "controls": [{"key": "c1", "risk_key": "r1", "title": "t", "description": "d", "type": "preventive"}],
        "thresholds": [{"id": "th1", "value": 5, "unit": "count", "description": "d"}],
        "tests": [{
            "key": "t1", "name": "n", "primitive": "threshold_exceedance",
            "params": {
                "kind": "threshold_exceedance", "population": "p1",
                "column": "Days of Approval from Receipt View",
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


def _profile_with_categorical_numeric_column() -> dict:
    # Modelled on the real approval_aging source (contract.yaml) -- a
    # numeric column profiled with a small, categorical-shaped value set
    # (<= max_distinct), which is exactly what makes `_profiled_value_set`
    # return a real `set` rather than `None` and reach the `value in
    # profiled` line.
    return {"approval_aging": {"row_count": 20, "null_counts": {}, "columns": [
        {
            "name": "Days of Approval from Receipt View", "type": "integer", "null_count": 0,
            "distinct_count": 4, "unique": False, "semantic_type": "category", "pii": False,
            "pii_basis": None, "min": 0, "max": 3, "negative_count": 0, "zero_count": 5,
            "values": [
                {"value": 0, "count": 8}, {"value": 1, "count": 6},
                {"value": 2, "count": 4}, {"value": 3, "count": 2},
            ],
        },
    ]}}


def test_wire_schema_allows_a_list_value_on_a_gt_filter():
    # Documents WHY the crash was reachable at all: PLAN_PROPOSAL_SCHEMA's
    # generic FILTER_VALUE type does not itself reject this shape.
    from orchestrator.explorer.wire_schema import PLAN_PROPOSAL_SCHEMA
    jsonschema.validate(_base_wire(), PLAN_PROPOSAL_SCHEMA)  # raises if this stops being true


def test_list_valued_gt_filter_no_longer_crashes_and_is_reported():
    wire = _base_wire()
    profile = _profile_with_categorical_numeric_column()
    # Before the fix this call raised TypeError("unhashable type: 'list'").
    report = validate_wire_proposal(
        wire, profile=profile, run_sources=["approval_aging"], data_source=None,
        pinned_versions={"approval_aging": "v1"},
    )
    assert report["tests"]["t1"]["valid"] is False
    reasons = " ".join(r["message"] for r in report["tests"]["t1"]["reasons"])
    assert "V-F2" in reasons or "not a profiled value" in reasons
