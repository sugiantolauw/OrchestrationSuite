"""CLAUDE.md P2/P3 gate review item 6 (G10): every primitive must return
zero exceptions and well-formed metrics on an empty population -- never
crash (the ratio_per_group bug this item names: `work.apply(..., axis=1)`
on an empty frame returns an empty DataFrame, not an empty Series, so the
later `values > limits` comparison raised "Operands are not aligned"), and
never silently report a percentage-of-population as 0.0 when the true
denominator is zero (build_metrics' `pct_of_population` now returns None
with a not_testable-style reason in source_ref instead).

Parametrized over every primitive in orchestrator.primitives.PRIMITIVES,
including both the grouped and ungrouped code paths for the two primitives
that branch on `group_by` (ratio_per_group, threshold_exceedance)."""

from __future__ import annotations

import pandas as pd
import pytest

from orchestrator.populations import PopulationResult
from orchestrator.primitives import PRIMITIVES, run_primitive
from orchestrator.primitives.common import PrimitiveContext


def _empty_pop(name: str, columns: dict[str, str], *, source: str = "src", version: str = "v1") -> PopulationResult:
    data = {"__source": pd.Series([], dtype=object), "__row_key": pd.Series([], dtype=object)}
    for col, dtype in columns.items():
        data[col] = pd.Series([], dtype=dtype)
    df = pd.DataFrame(data)
    return PopulationResult(
        name=name, source=source, source_version=version, df=df, rows=0,
        amount=None, min_date=None, max_date=None,
    )


def _ctx(populations: dict, *, thresholds: dict | None = None, references: dict | None = None) -> PrimitiveContext:
    return PrimitiveContext(populations=populations, thresholds=thresholds or {}, references=references or {})


def _case(primitive: str, params: dict, populations: dict, *, thresholds: dict | None = None, references: dict | None = None, label: str | None = None):
    return pytest.param(primitive, params, populations, thresholds or {}, references or {}, id=label or primitive)


CASES = [
    _case(
        "threshold_exceedance",
        {"population": "pop", "column": "Amount", "limit": {"threshold": "lim"}, "direction": "above",
         "metrics": {"n": {"kind": "count"}, "pct": {"kind": "pct_of_population"}, "amt": {"kind": "sum", "column": "Amount"}}},
        {"pop": _empty_pop("pop", {"Amount": "float64"})},
        thresholds={"lim": {"value": 100, "unit": "AUD"}},
        label="threshold_exceedance_row",
    ),
    _case(
        "threshold_exceedance",
        {"population": "pop", "column": "Amount", "limit": {"threshold": "lim"}, "direction": "above",
         "group_by": ["Employee ID", "Vendor"],
         "metrics": {"n": {"kind": "groups"}}},
        {"pop": _empty_pop("pop", {"Amount": "float64", "Employee ID": "int64", "Vendor": "object"})},
        thresholds={"lim": {"value": 100, "unit": "AUD"}},
        label="threshold_exceedance_grouped",
    ),
    _case(
        "duplicate_detection",
        {"population": "pop", "key_columns": ["Employee ID", "Vendor"], "amount_column": "Amount",
         "metrics": {"n": {"kind": "groups"}, "amt": {"kind": "value", "key": "duplicate_amount"}}},
        {"pop": _empty_pop("pop", {"Employee ID": "int64", "Vendor": "object", "Amount": "float64"})},
    ),
    _case(
        "split_detection",
        {"population": "pop", "group_keys": ["Employee ID", "Vendor"], "date_column": "Transaction Date",
         "amount_column": "Amount", "window_days": {"threshold": "win"}, "aggregate_threshold": {"threshold": "agg"},
         "metrics": {"n": {"kind": "value", "key": "split_groups"}}},
        {"pop": _empty_pop("pop", {"Employee ID": "int64", "Vendor": "object", "Amount": "float64", "Transaction Date": "datetime64[ns]"})},
        thresholds={"win": {"value": 3, "unit": "days"}, "agg": {"value": 5000, "unit": "AUD"}},
    ),
    _case(
        "anti_join_gap",
        {"left_population": "left", "right_population": "right", "left_keys": ["Employee ID"],
         "right_keys": ["Employee ID"], "mode": "anti",
         "metrics": {"n": {"kind": "count"}, "mr": {"kind": "match_rate"}}},
        {"left": _empty_pop("left", {"Employee ID": "int64"}), "right": _empty_pop("right", {"Employee ID": "int64"})},
    ),
    _case(
        "list_membership",
        {"population": "pop", "column": "Vendor", "allowed_values": ["A", "B"], "negate": True, "match": "exact",
         "metrics": {"n": {"kind": "count"}}},
        {"pop": _empty_pop("pop", {"Vendor": "object"})},
        label="list_membership_exact",
    ),
    _case(
        "list_membership",
        {"population": "pop", "column": "Vendor", "allowed_values": ["A", "B"], "negate": True, "match": "whole_word_upper",
         "metrics": {"n": {"kind": "count"}}},
        {"pop": _empty_pop("pop", {"Vendor": "object"})},
        label="list_membership_whole_word",
    ),
    _case(
        "date_lag",
        {"population": "pop", "start_column": "Invoice Date", "end_column": "Payment Date",
         "threshold": {"threshold": "lag"}, "direction": "below",
         "metrics": {"n": {"kind": "count"}}},
        {"pop": _empty_pop("pop", {"Invoice Date": "datetime64[ns]", "Payment Date": "datetime64[ns]"})},
        thresholds={"lag": {"value": 0, "unit": "days"}},
    ),
    _case(
        "ratio_per_group",
        {"population": "pop", "numerator_column": "Entry Amount", "denominator_column": "Number of Attendees",
         "direction": "above", "limit": {"threshold": "lim"},
         "metrics": {"n": {"kind": "count"}}},
        {"pop": _empty_pop("pop", {"Entry Amount": "float64", "Number of Attendees": "float64"})},
        thresholds={"lim": {"value": 40, "unit": "AUD"}},
        label="ratio_per_group_ungrouped",
    ),
    _case(
        "ratio_per_group",
        {"population": "pop", "numerator_column": "Entry Amount", "denominator_column": "Number of Attendees",
         "group_by": ["Employee ID", "Report Name"], "numerator_aggregate": "first", "direction": "above",
         "limit_selector_column": "External", "limit_selector_aggregate": "any",
         "limit_cases": {"internal": {"threshold": "int40"}, "external": {"threshold": "ext80"}},
         "metrics": {"n_int": {"kind": "value", "key": "count_internal"}, "n_ext": {"kind": "value", "key": "count_external"},
                     "assessed": {"kind": "value", "key": "assessed"}}},
        {"pop": _empty_pop("pop", {"Entry Amount": "float64", "Number of Attendees": "float64",
                                    "Employee ID": "int64", "Report Name": "object", "External": "object"})},
        thresholds={"int40": {"value": 40, "unit": "AUD"}, "ext80": {"value": 80, "unit": "AUD"}},
        label="ratio_per_group_grouped_with_selector",
    ),
    _case(
        "attribute_missing",
        {"population": "pop", "column": "Receipt", "condition": "is_null",
         "metrics": {"n": {"kind": "count"}, "pct": {"kind": "pct_of_population"}}},
        {"pop": _empty_pop("pop", {"Receipt": "object"})},
    ),
]


@pytest.mark.parametrize("primitive,params,populations,thresholds,references", CASES)
def test_primitive_on_empty_population_never_crashes(primitive, params, populations, thresholds, references):
    ctx = _ctx(populations, thresholds=thresholds, references=references)
    result = run_primitive(primitive, ctx, params)

    assert result.scored_units == []
    assert len(result.flags) == 0

    for name, metric in result.metrics.items():
        kind = params["metrics"][name]["kind"]
        if kind == "pct_of_population":
            # CLAUDE.md P2/P3 gate review item 6: an undefined percentage
            # (0/0) is None with a reason, never a fabricated 0.0.
            assert metric["value"] is None, f"{name}: pct_of_population on an empty population must be None, got {metric['value']!r}"
            assert "reason" in metric["source_ref"]
        elif kind in ("count", "rows", "groups"):
            assert metric["value"] == 0
        elif kind in ("sum", "excess"):
            assert metric["value"] == 0.0
        elif kind == "match_rate":
            assert metric["value"] is None
        elif kind == "value":
            assert metric["value"] == 0


def test_every_primitive_in_the_registry_is_covered_by_a_case():
    covered = {c.values[0] for c in CASES}
    assert covered == set(PRIMITIVES), f"missing empty-population coverage for: {set(PRIMITIVES) - covered}"
