"""BUG-EXPLORER-PLAN-2 (independent review round 5, RUN-99373993B1E0):
regression test built from the REAL planner response recorded in
`llm_calls` for that run (trimmed copy: tests/fixtures/explorer/
RUN-99373993B1E0_planner_response.json). Every one of the response's five
tests nested its own `findings[]` array -- a shape
orchestrator.explorer.reference_skills used to show the planner as the
worked example (fixed alongside this test) -- which PLAN_PROPOSAL_SCHEMA's
top-level, `test_key`-linked `findings[]` rejects outright as an
"Additional properties are not allowed" schema violation, leaving zero
valid tests. normalize_wire_proposal (wire_schema.py) hoists the nested
findings; fieldwork._recovered_proposal uses it to recover a proposal
LLMGateway's own (generic, schema-agnostic) client-side check would
otherwise have discarded. Fully offline: no endpoint, no workspace."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from orchestrator.explorer.validate import validate_wire_proposal
from orchestrator.explorer.wire_schema import PLAN_PROPOSAL_SCHEMA, normalize_wire_proposal
from orchestrator.nodes.fieldwork import _recovered_proposal

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "explorer" / "RUN-99373993B1E0_planner_response.json"


def _raw_proposal() -> dict:
    return json.loads(FIXTURE.read_text())


def _column(name, *, type="string", **overrides) -> dict:
    base = {
        "name": name, "type": type, "null_count": 0, "distinct_count": 10, "unique": False,
        "semantic_type": None, "pii": False, "pii_basis": None, "min": None, "max": None,
        "negative_count": 0, "zero_count": 0,
    }
    base.update(overrides)
    return base


def _profile() -> dict:
    return {
        "expense_report": {
            "row_count": 100,
            "null_counts": {},
            "columns": [
                _column("Employee ID", semantic_type="identifier"),
                _column("Transaction Date", type="date"),
                _column("Vendor", null_count=3),
                _column(
                    "Expense Amount (reimbursement currency)", type="number",
                    semantic_type="amount", min=-50, max=5000, negative_count=2,
                ),
                _column("Expense Type"),
                _column(
                    "Payment Type",
                    values=[
                        {"value": "Amex IBCP", "count": 40},
                        {"value": "Out of Pocket", "count": 30},
                        {"value": "ANZ Visa CBCP", "count": 30},
                    ],
                ),
                _column("Parent Key", unique=True),
            ],
        }
    }


def test_raw_planner_response_fails_wire_schema_before_the_fix():
    # Documents the bug: the RAW response, as the planner actually emitted
    # it, is not PLAN_PROPOSAL_SCHEMA-valid -- every test nests `findings`.
    raw = _raw_proposal()
    with __import__("pytest").raises(jsonschema.ValidationError):
        jsonschema.validate(raw, PLAN_PROPOSAL_SCHEMA)


def test_normalize_wire_proposal_hoists_nested_findings_and_validates():
    raw = _raw_proposal()
    normalized = normalize_wire_proposal(raw)
    jsonschema.validate(normalized, PLAN_PROPOSAL_SCHEMA)  # raises if still invalid
    assert normalized["findings"], "hoisted findings must not be dropped"
    for t in normalized["tests"]:
        assert "findings" not in t


def test_recovered_proposal_from_a_failed_llm_result():
    raw_text = FIXTURE.read_text()

    class _FakeResult:
        status = "invalid_output"
        parsed = None
        text = raw_text
        error = "schema validation failed: Additional properties are not allowed ('findings' was unexpected)"

    recovered = _recovered_proposal(_FakeResult())
    assert recovered is not None
    jsonschema.validate(recovered, PLAN_PROPOSAL_SCHEMA)


def test_recovered_proposal_produces_at_least_one_valid_test():
    raw = _raw_proposal()
    normalized = normalize_wire_proposal(raw)
    report = validate_wire_proposal(
        normalized, profile=_profile(), run_sources=["expense_report"],
        data_source=None, pinned_versions={"expense_report": "v1"},
    )
    valid_tests = [key for key, t in report["tests"].items() if t["valid"]]
    assert len(valid_tests) >= 1, report
