from __future__ import annotations

import pandas as pd
import pytest

from orchestrator.populations import PopulationResult
from orchestrator.primitives import PRIMITIVES, run_primitive
from orchestrator.primitives.common import PrimitiveContext, PrimitiveParamsError


def _rows(n: int) -> list[str]:
    return [f"k:{i + 1}" for i in range(n)]


def _pop(name: str, df: pd.DataFrame, *, source: str = "src", version: str = "v1") -> PopulationResult:
    df = df.copy()
    if "__source" not in df.columns:
        df.insert(0, "__row_key", _rows(len(df)))
        df.insert(0, "__source", source)
    return PopulationResult(
        name=name,
        source=source,
        source_version=version,
        df=df,
        rows=len(df),
        amount=None,
        min_date=None,
        max_date=None,
    )


def _ctx(populations: dict, *, thresholds: dict | None = None, references: dict | None = None) -> PrimitiveContext:
    return PrimitiveContext(populations=populations, thresholds=thresholds or {}, references=references or {})


# ============================== threshold_exceedance =========================


def test_threshold_exceedance_happy_path_row_grain():
    df = pd.DataFrame({"Amount": [100, 6000, 5001]})
    ctx = _ctx({"pop": _pop("pop", df)}, thresholds={"hv": {"value": 5000, "unit": "AUD"}})
    params = {
        "population": "pop",
        "column": "Amount",
        "limit": {"threshold": "hv"},
        "direction": "above",
        "metrics": {"hv_count": {"kind": "count"}, "hv_amount": {"kind": "sum", "column": "Amount"}},
    }
    res = run_primitive("threshold_exceedance", ctx, params)
    assert res.metrics["hv_count"]["value"] == 2
    assert res.metrics["hv_amount"]["value"] == 11001.0
    assert set(res.scored_units) == {"k:2", "k:3"}


def test_threshold_exceedance_negative_and_zero_amounts_excluded_and_counted():
    df = pd.DataFrame({"Amount": [-50, 0, 100, 6000]})
    ctx = _ctx({"pop": _pop("pop", df)}, thresholds={"hv": {"value": 5000, "unit": "AUD"}})
    params = {
        "population": "pop",
        "column": "Amount",
        "limit": {"threshold": "hv"},
        "direction": "above",
        "exclude": {"column": "Amount", "op": "lte", "value": 0},
        "metrics": {
            "hv_count": {"kind": "count"},
            "excluded": {"kind": "value", "key": "excluded_count"},
        },
    }
    res = run_primitive("threshold_exceedance", ctx, params)
    assert res.metrics["hv_count"]["value"] == 1
    assert res.metrics["excluded"]["value"] == 2


def test_threshold_exceedance_group_by_aggregate_and_column_limit():
    df = pd.DataFrame(
        {
            "Employee ID": [1, 1, 2, 2],
            "Date": ["2025-01-01", "2025-01-01", "2025-01-02", "2025-01-02"],
            "Amount": [600, 600, 400, 300],
            "Limit": [1000, 1000, 1000, 1000],
        }
    )
    ctx = _ctx({"pop": _pop("pop", df)})
    params = {
        "population": "pop",
        "column": "Amount",
        "limit": {"column": "Limit"},
        "direction": "above",
        "group_by": ["Employee ID", "Date"],
        "aggregate": "sum",
        "metrics": {"over_count": {"kind": "count"}, "over_amount": {"kind": "excess"}},
    }
    res = run_primitive("threshold_exceedance", ctx, params)
    assert res.metrics["over_count"]["value"] == 1
    assert res.metrics["over_amount"]["value"] == 200.0
    assert res.scored_units == ["1|2025-01-01"]
    assert set(res.flags["__row_key"]) == {"k:1", "k:2"}


def test_threshold_exceedance_params_schema_rejects_bare_numeric_limit():
    with pytest.raises(PrimitiveParamsError):
        run_primitive(
            "threshold_exceedance",
            _ctx({}),
            {"population": "pop", "column": "Amount", "limit": 5000, "direction": "above"},
        )


def test_threshold_exceedance_params_schema_rejects_unknown_keys():
    with pytest.raises(PrimitiveParamsError):
        run_primitive(
            "threshold_exceedance",
            _ctx({}),
            {
                "population": "pop",
                "column": "Amount",
                "limit": {"threshold": "hv"},
                "direction": "above",
                "unexpected_key": True,
            },
        )


# ============================== duplicate_detection ===========================


def test_duplicate_detection_happy_path_and_amount():
    df = pd.DataFrame(
        {
            "Employee ID": [1, 1, 2],
            "Date": ["2025-01-01"] * 3,
            "Vendor": ["V", "V", "W"],
            "Amount": [100.0, 100.0, 50.0],
        }
    )
    ctx = _ctx({"pop": _pop("pop", df)})
    params = {
        "population": "pop",
        "key_columns": ["Employee ID", "Date", "Vendor", "Amount"],
        "amount_column": "Amount",
        "metrics": {
            "dup_groups": {"kind": "groups"},
            "dup_lines": {"kind": "rows"},
            "dup_amount": {"kind": "value", "key": "duplicate_amount", "unit": "AUD"},
        },
    }
    res = run_primitive("duplicate_detection", ctx, params)
    assert res.metrics["dup_groups"]["value"] == 1
    assert res.metrics["dup_lines"]["value"] == 2
    assert res.metrics["dup_amount"]["value"] == 100.0
    assert res.scored_units == ["1|2025-01-01|V|100.0"]


def test_duplicate_detection_exclude_within_parent_key_itemisation():
    df = pd.DataFrame(
        {
            "Employee ID": [1, 1, 1, 2, 2],
            "Date": ["2025-01-01"] * 5,
            "Vendor": ["V", "V", "V", "W", "W"],
            "Amount": [100.0, 100.0, 100.0, 50.0, 50.0],
            "Parent Key": ["P1", "P1", "P1", "PA", "PB"],
        }
    )
    ctx = _ctx({"pop": _pop("pop", df)})
    params = {
        "population": "pop",
        "key_columns": ["Employee ID", "Date", "Vendor", "Amount"],
        "amount_column": "Amount",
        "exclude_within": ["Parent Key"],
        "metrics": {"dup_groups": {"kind": "groups"}, "dup_lines": {"kind": "rows"}},
    }
    res = run_primitive("duplicate_detection", ctx, params)
    # Employee 1's 3 lines all share Parent Key P1 -- one itemised entry, not a
    # duplicate. Employee 2's 2 lines have different Parent Keys -- a real duplicate.
    assert res.metrics["dup_groups"]["value"] == 1
    assert res.metrics["dup_lines"]["value"] == 2
    assert res.scored_units == ["2|2025-01-01|W|50.0"]


def test_duplicate_detection_oop_vs_card_subtest():
    df = pd.DataFrame(
        {
            "Employee ID": [1, 1, 2, 2],
            "Date": ["2025-01-01"] * 4,
            "Vendor": ["V", "V", "W", "W"],
            "Amount": [100.0, 100.0, 75.0, 75.0],
            "Payment Method": ["Out of Pocket", "Amex Corporate", "Out of Pocket", "Out of Pocket"],
        }
    )
    ctx = _ctx({"pop": _pop("pop", df)})
    params = {
        "population": "pop",
        "key_columns": ["Employee ID", "Date", "Vendor", "Amount"],
        "amount_column": "Amount",
        "payment_column": "Payment Method",
        "oop_values": ["out of pocket"],
        "card_values": ["amex corporate"],
        "metrics": {"oop_card_groups": {"kind": "value", "key": "oop_card_groups"}},
    }
    res = run_primitive("duplicate_detection", ctx, params)
    assert res.metrics["oop_card_groups"]["value"] == 1  # employee 1's group only


def test_duplicate_detection_params_schema_rejects_unknown_keys():
    with pytest.raises(PrimitiveParamsError):
        run_primitive("duplicate_detection", _ctx({}), {"population": "pop", "key_columns": ["A"], "bogus": 1})


# ============================== split_detection ================================


def _split_pop() -> PopulationResult:
    df = pd.DataFrame(
        {
            "Employee ID": [1, 1, 1, 2, 2, 3, 3, 3],
            "Vendor": ["A", "A", "A", "B", "B", "C", "C", "C"],
            "Date": [
                "2025-01-01",
                "2025-01-01",
                "2025-01-02",
                "2025-02-01",
                "2025-02-05",
                "2025-03-01",
                "2025-03-01",
                "2025-03-01",
            ],
            "Type": ["T"] * 8,
            "Amount": [3000, 3000, 100, 6000, 10, 1000, 1000, 1000],
        }
    )
    df["Date"] = pd.to_datetime(df["Date"])
    return _pop("pop", df)


def _split_ctx() -> PrimitiveContext:
    return _ctx(
        {"pop": _split_pop()},
        thresholds={
            "hv": {"value": 5000, "unit": "AUD"},
            "window_days": {"value": 3, "unit": "days"},
            "split_amt": {"value": 5000, "unit": "AUD"},
        },
    )


def _split_params(**overrides) -> dict:
    params = {
        "population": "pop",
        "group_keys": ["Employee ID", "Vendor", "Type"],
        "date_column": "Date",
        "amount_column": "Amount",
        "window_days": {"threshold": "window_days"},
        "aggregate_threshold": {"threshold": "split_amt"},
        "max_line": {"threshold": "hv"},
        "metrics": {
            "same_day_groups": {"kind": "value", "key": "same_day_groups"},
            "same_day_lines": {"kind": "value", "key": "same_day_lines"},
            "window_groups": {"kind": "value", "key": "window_groups"},
            "window_lines": {"kind": "value", "key": "window_lines"},
            "split_amount": {"kind": "value", "key": "split_amount", "unit": "AUD"},
        },
    }
    params.update(overrides)
    return params


def test_split_detection_same_day_and_window_merge():
    res = run_primitive("split_detection", _split_ctx(), _split_params())
    # Employee 1: same-day group on 01-01 (2 lines, 6000 > 5000) plus a window
    # merge across 01-01..01-02 (3 lines, 6100 > 5000): the window is the overlap
    # of two anchor windows and must be reported as ONE merged group.
    assert res.metrics["same_day_groups"]["value"] == 1
    assert res.metrics["same_day_lines"]["value"] == 2
    assert res.metrics["window_groups"]["value"] == 1
    assert res.metrics["window_lines"]["value"] == 3
    # Employee 2: 6000+10 four days apart (> window_days) and 6000 alone breaches
    # max_line -- no split flagged either way.
    # Employee 3: 3x1000 = 3000, under the aggregate threshold -- no split.
    assert res.metrics["split_amount"]["value"] == 6100.0  # union of flagged rows, no double count


def test_split_detection_max_line_excludes_group():
    df = pd.DataFrame(
        {
            "Employee ID": [9, 9],
            "Vendor": ["Z", "Z"],
            "Type": ["T", "T"],
            "Date": pd.to_datetime(["2025-01-01", "2025-01-01"]),
            "Amount": [6000, 100],  # sum > 5000, but one line itself exceeds max_line
        }
    )
    ctx = _ctx(
        {"pop": _pop("pop", df)},
        thresholds={"hv": {"value": 5000}, "window_days": {"value": 3}, "split_amt": {"value": 5000}},
    )
    res = run_primitive("split_detection", ctx, _split_params())
    assert res.metrics["same_day_groups"]["value"] == 0
    assert res.metrics["window_groups"]["value"] == 0


def test_split_detection_params_schema_rejects_bare_numeric_window_days():
    with pytest.raises(PrimitiveParamsError):
        run_primitive(
            "split_detection",
            _split_ctx(),
            _split_params(window_days=3),
        )


# ============================== anti_join_gap ===================================


def _anti_join_pops():
    left = pd.DataFrame({"Name": ["Alice", "Bob", "Carol"], "Class": ["Dom", "Dom", "Int"]})
    right = pd.DataFrame({"EmpName": ["Alice", "Carol"], "ReqClass": ["Dom", "Int"]})
    return {"left": _pop("left", left), "right": _pop("right", right)}


def test_anti_join_gap_anti_mode_composite_differently_named_keys():
    ctx = _ctx(_anti_join_pops())
    params = {
        "left_population": "left",
        "right_population": "right",
        "left_keys": ["Name", "Class"],
        "right_keys": ["EmpName", "ReqClass"],
        "mode": "anti",
        "metrics": {"gap_count": {"kind": "count"}, "match_rate": {"kind": "match_rate"}},
    }
    res = run_primitive("anti_join_gap", ctx, params)
    assert res.scored_units == ["k:2"]  # Bob has no match
    assert res.metrics["gap_count"]["value"] == 1
    assert res.metrics["match_rate"]["value"] == pytest.approx(66.7, abs=0.1)


def test_anti_join_gap_semi_mode_flags_matched_rows():
    ctx = _ctx(_anti_join_pops())
    params = {
        "left_population": "left",
        "right_population": "right",
        "left_keys": ["Name", "Class"],
        "right_keys": ["EmpName", "ReqClass"],
        "mode": "semi",
        "metrics": {"match_count": {"kind": "count"}},
    }
    res = run_primitive("anti_join_gap", ctx, params)
    assert sorted(res.scored_units) == ["k:1", "k:3"]
    assert res.metrics["match_count"]["value"] == 2


def test_anti_join_gap_params_schema_rejects_unknown_mode():
    with pytest.raises(PrimitiveParamsError):
        run_primitive(
            "anti_join_gap",
            _ctx(_anti_join_pops()),
            {
                "left_population": "left",
                "right_population": "right",
                "left_keys": ["Name"],
                "right_keys": ["EmpName"],
                "mode": "outer",
            },
        )


# ============================== list_membership =================================


def test_list_membership_whole_word_upper_match_negate():
    df = pd.DataFrame(
        {"Vendor": ["QANTAS AIRWAYS (I)", "QANTASLINKX PTY LTD", "VIRGIN AUSTRALIA - AU - AGENCY"]}
    )
    ctx = _ctx({"pop": _pop("pop", df)})
    params = {
        "population": "pop",
        "column": "Vendor",
        "allowed_values": ["QANTAS", "VIRGIN AUSTRALIA"],
        "negate": True,
        "match": "whole_word_upper",
        "metrics": {"non_pref": {"kind": "count"}},
    }
    res = run_primitive("list_membership", ctx, params)
    # QANTAS is a whole word inside "QANTAS AIRWAYS (I)" -> preferred, not flagged.
    # QANTAS is NOT a whole word inside "QANTASLINKX" -> non-preferred, flagged.
    assert res.scored_units == ["k:2"]
    assert res.metrics["non_pref"]["value"] == 1


def test_list_membership_exact_match_not_negated():
    df = pd.DataFrame({"Status": ["Approved", "Approved - Pending Booking", "Rejected"]})
    ctx = _ctx({"pop": _pop("pop", df)})
    params = {
        "population": "pop",
        "column": "Status",
        "allowed_values": ["Approved", "Approved - Pending Booking"],
        "negate": False,
        "match": "exact",
        "metrics": {"n": {"kind": "count"}},
    }
    res = run_primitive("list_membership", ctx, params)
    assert res.metrics["n"]["value"] == 2


def test_list_membership_allowed_values_from_reference():
    df = pd.DataFrame({"Approver ID": [52725, 999]})
    ctx = _ctx({"pop": _pop("pop", df)}, references={"exco_ids": [52725, 52394]})
    params = {
        "population": "pop",
        "column": "Approver ID",
        "allowed_values": {"ref": "exco_ids"},
        "negate": False,
        "match": "exact",
        "metrics": {"n": {"kind": "count"}},
    }
    res = run_primitive("list_membership", ctx, params)
    assert res.metrics["n"]["value"] == 1


def test_list_membership_params_schema_rejects_unknown_keys():
    with pytest.raises(PrimitiveParamsError):
        run_primitive(
            "list_membership",
            _ctx({}),
            {"population": "pop", "column": "X", "allowed_values": ["A"], "negate": False, "match": "exact", "bogus": 1},
        )


# ============================== date_lag ========================================


def test_date_lag_happy_path():
    df = pd.DataFrame(
        {
            "Booking Date": pd.to_datetime(["2025-01-01", "2025-01-01"]),
            "Depart Date": pd.to_datetime(["2025-01-03", "2025-02-01"]),
        }
    )
    ctx = _ctx({"pop": _pop("pop", df)}, thresholds={"late": {"value": 7, "unit": "days"}})
    params = {
        "population": "pop",
        "start_column": "Booking Date",
        "end_column": "Depart Date",
        "threshold": {"threshold": "late"},
        "direction": "below",
        "metrics": {"late_count": {"kind": "count"}},
    }
    res = run_primitive("date_lag", ctx, params)
    assert res.metrics["late_count"]["value"] == 1
    assert res.scored_units == ["k:1"]


# ============================== ratio_per_group ==================================


def test_ratio_per_group_per_row_limit_selection():
    df = pd.DataFrame(
        {
            "Entry Amount": [120.0, 200.0, 90.0],
            "Number of Attendees": [3, 2, 3],
            "External": ["internal", "external", "internal"],
        }
    )
    ctx = _ctx(
        {"pop": _pop("pop", df)},
        thresholds={"int40": {"value": 40, "unit": "AUD"}, "ext80": {"value": 80, "unit": "AUD"}},
    )
    params = {
        "population": "pop",
        "numerator_column": "Entry Amount",
        "denominator_column": "Number of Attendees",
        "direction": "above",
        "limit_selector_column": "External",
        "limit_cases": {"internal": {"threshold": "int40"}, "external": {"threshold": "ext80"}},
        "metrics": {
            "over_internal": {"kind": "value", "key": "count_internal"},
            "over_external": {"kind": "value", "key": "count_external"},
        },
    }
    res = run_primitive("ratio_per_group", ctx, params)
    assert res.metrics["over_internal"]["value"] == 0
    assert res.metrics["over_external"]["value"] == 1
    assert res.scored_units == ["k:2"]


def test_ratio_per_group_requires_limit_or_selector():
    with pytest.raises(PrimitiveParamsError):
        run_primitive(
            "ratio_per_group",
            _ctx({}),
            {
                "population": "pop",
                "numerator_column": "A",
                "denominator_column": "B",
                "direction": "above",
            },
        )


# ============================== attribute_missing =================================


def test_attribute_missing_is_null():
    df = pd.DataFrame({"Tax Invoice": ["INV1", None, "INV3"]})
    ctx = _ctx({"pop": _pop("pop", df)})
    params = {"population": "pop", "column": "Tax Invoice", "condition": "is_null", "metrics": {"n": {"kind": "count"}}}
    res = run_primitive("attribute_missing", ctx, params)
    assert res.metrics["n"]["value"] == 1
    assert res.scored_units == ["k:2"]


def test_attribute_missing_is_blank():
    df = pd.DataFrame({"Notes": ["ok", "  ", None]})
    ctx = _ctx({"pop": _pop("pop", df)})
    params = {"population": "pop", "column": "Notes", "condition": "is_blank", "metrics": {"n": {"kind": "count"}}}
    res = run_primitive("attribute_missing", ctx, params)
    assert res.metrics["n"]["value"] == 2


# ============================== registry / run_primitive =========================


def test_registry_has_all_eight_primitives():
    assert set(PRIMITIVES) == {
        "threshold_exceedance",
        "duplicate_detection",
        "split_detection",
        "anti_join_gap",
        "list_membership",
        "date_lag",
        "ratio_per_group",
        "attribute_missing",
    }


def test_run_primitive_rejects_unknown_primitive_name():
    with pytest.raises(PrimitiveParamsError):
        run_primitive("not_a_real_primitive", _ctx({}), {})
