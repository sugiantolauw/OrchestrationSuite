"""G15 (CLAUDE.md §5 Tier B): PII columns never reach the Explorer planner
payload (docs/specs/P6_P8_explorer_llm_design.md §4.4 build_planner_payload,
§8.7). This step's scope is the profiling + payload-building path only --
the full plan-node/LLMGateway wiring (llm_calls.messages_json etc.) is a
later work package; what is asserted here is that a PII column's real
values, once masked by orchestrator.explorer.profile.profile_source, can
never reappear once orchestrator.explorer.payload.build_planner_payload
serialises the (already-masked) profile into the payload sent to the
planner."""

from __future__ import annotations

import json

import pandas as pd

from orchestrator.explorer.payload import build_planner_payload
from orchestrator.explorer.profile import pandas_profile_columns, profile_source

PII_SENTINEL = "SENTINEL-PII-7731@example.test"
NON_PII_SENTINEL = "SENTINEL-FREETEXT-9142-not-pii"


class _FakeDataSource:
    """Stands in for a DataSourceAdapter: profile_columns is the only method
    profile_source() calls."""

    def __init__(self, df: pd.DataFrame):
        self.df = df

    def profile_columns(self, source, *, version, max_distinct, min_count,
                         audit_period=None, audit_timezone=None):
        return pandas_profile_columns(
            self.df, max_distinct=max_distinct, min_count=min_count,
            audit_period=audit_period, audit_timezone=audit_timezone,
        )


def _fixture_df() -> pd.DataFrame:
    # "Employee" is PII by contract flag (skills/tne_exco/contract.yaml
    # declares expense_report.Employee: pii: true) -- rule 1.
    # "Approver Notes" is PII only by the heuristic rule (name token
    # "approver" + free_text semantic type) -- rule 3, never declared
    # anywhere.
    # "Vendor Description" is a genuinely non-PII, high-cardinality free-text
    # column carrying its OWN sentinel, to prove masking is column-scoped,
    # never a blanket redaction of every long string.
    employees = [f"{PII_SENTINEL}-{i}" for i in range(3)] + [f"{PII_SENTINEL}-{i}" for i in range(3)]
    notes = [f"Approver comment {i}: {PII_SENTINEL}" for i in range(6)]
    # Each distinct description repeated twice, so it clears the min_count=2
    # small-cell suppression threshold used below (D6) -- a column whose
    # every value is singleton would be suppressed for that reason alone,
    # which is not what this fixture is testing.
    descriptions = [f"{NON_PII_SENTINEL}-{i}" for i in range(3) for _ in range(2)]
    return pd.DataFrame({
        "Employee": employees,
        "Approver Notes": notes,
        "Vendor Description": descriptions,
        "Amount": [10, 20, 30, 40, 50, 60],
        # A category column with one value seen only once -- below the
        # min_count=2 suppression threshold used below (D6).
        "Category": ["Travel", "Travel", "Travel", "Travel", "Travel", "OnlyOnce"],
    })


def _payload_text(profile_result: dict) -> str:
    payload = build_planner_payload(
        objective="Assess T&E compliance",
        audit_period=("2026-01-01", "2026-06-30"),
        audit_timezone="Australia/Sydney",
        business_unit=None,
        materiality=None,
        profile_result=profile_result,
    )
    # Round-trips exactly what a caller would send to the model: everything
    # in the payload dict, canonicalised.
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def test_g15_pii_sentinel_never_reaches_the_planner_payload():
    df = _fixture_df()
    result = profile_source(
        _FakeDataSource(df), "expense_report", version="v1", max_distinct=30, min_count=2,
        pii_tag_names=(),
    )
    text = _payload_text(result)

    assert PII_SENTINEL not in text, "a PII column's value leaked into the planner payload"
    assert NON_PII_SENTINEL in text, "masking must be column-scoped, not blanket redaction"


def test_g15_pii_columns_carry_only_name_type_and_counts():
    df = _fixture_df()
    result = profile_source(
        _FakeDataSource(df), "expense_report", version="v1", max_distinct=30, min_count=2,
        pii_tag_names=(),
    )
    columns_by_name = {c["name"]: c for c in result["columns"]}

    employee = columns_by_name["Employee"]
    assert employee["pii"] is True
    assert employee["pii_basis"] == "contract"
    assert set(employee) == {"name", "type", "null_count", "distinct_count", "unique", "semantic_type", "pii", "pii_basis"}

    notes = columns_by_name["Approver Notes"]
    assert notes["pii"] is True
    assert notes["pii_basis"] == "heuristic"
    assert "values" not in notes and "min" not in notes and "max" not in notes

    vendor = columns_by_name["Vendor Description"]
    assert vendor["pii"] is False


def test_g15_pii_masked_column_has_no_values_field_at_all():
    df = _fixture_df()
    result = profile_source(
        _FakeDataSource(df), "expense_report", version="v1", max_distinct=30, min_count=1,
        pii_tag_names=(),
    )
    employee = next(c for c in result["columns"] if c["name"] == "Employee")
    # Employee has only 2 distinct values in the fixture (well under
    # max_distinct=30), so an UNMASKED column of this shape would carry a
    # `values` list -- proving masking removed it, not that it was never
    # eligible.
    assert "values" not in employee


def test_g15_category_suppression_matches_the_payload():
    df = _fixture_df()
    result = profile_source(
        _FakeDataSource(df), "expense_report", version="v1", max_distinct=30, min_count=2,
        pii_tag_names=(),
    )
    category = next(c for c in result["columns"] if c["name"] == "Category")
    assert category["suppressed_values"] == 1
    text = _payload_text(result)
    assert "OnlyOnce" not in text


def test_g15_call_context_pii_columns_masked_would_list_exactly_the_masked_names():
    # §4.4's CallContext.pii_columns_masked bookkeeping is later work-package
    # scope (the plan node itself), but the input it will be built FROM --
    # which columns profile_source actually masked -- must already be
    # derivable purely from the profile_result this step produces.
    df = _fixture_df()
    result = profile_source(
        _FakeDataSource(df), "expense_report", version="v1", max_distinct=30, min_count=2,
        pii_tag_names=(),
    )
    masked = sorted(
        f"expense_report.{c['name']}" for c in result["columns"] if c.get("pii")
    )
    assert masked == ["expense_report.Approver Notes", "expense_report.Employee"]
