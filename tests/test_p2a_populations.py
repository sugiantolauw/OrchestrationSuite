from __future__ import annotations

import pandas as pd
import pytest

from orchestrator.populations import PopulationContext, build_population


def _ctx(sources, **overrides):
    base = dict(
        sources=sources,
        references={},
        audit_period=("2025-01-01", "2025-01-31"),
        thresholds={},
    )
    base.update(overrides)
    return PopulationContext(**base)


def _src(df: pd.DataFrame, version: str = "v1") -> dict:
    df = df.copy()
    if "__source" not in df.columns:
        df.insert(0, "__row_key", [f"k:{i + 1}" for i in range(len(df))])
        df.insert(0, "__source", "src")
    return {"df": df, "version": version}


def test_filters_in_and_between_audit_period_ref():
    df = pd.DataFrame(
        {
            "Employee ID": [1, 2, 3],
            "Transaction Date": pd.to_datetime(["2025-01-05", "2025-02-05", "2025-02-20"]),
        }
    )
    ctx = _ctx({"claims": _src(df)}, references={"exco": [1, 3]})
    pop_cfg = {
        "source": "claims",
        "filters": [
            {"column": "Employee ID", "op": "in", "value": {"ref": "exco"}},
            {"column": "Transaction Date", "op": "between", "value": {"ref": "audit_period"}},
        ],
        "date_column": "Transaction Date",
    }
    result = build_population("pop", pop_cfg, ctx)
    assert result.rows == 1
    assert result.df["Employee ID"].tolist() == [1]
    assert result.excluded_counts["filter[0]:Employee ID:in"] == 1  # employee 2 excluded
    assert result.excluded_counts["filter[1]:Transaction Date:between"] == 1  # employee 3, Feb date


def test_any_of_and_all_of_filters():
    df = pd.DataFrame({"A": [1, 1, 2], "B": [1, 2, 2]})
    ctx = _ctx({"src": _src(df)})
    pop_cfg = {
        "source": "src",
        "filters": [{"any_of": [{"column": "A", "op": "eq", "value": 1}, {"column": "B", "op": "eq", "value": 2}]}],
    }
    result = build_population("pop", pop_cfg, ctx)
    assert result.rows == 3  # every row matches A==1 or B==2

    pop_cfg_all = {
        "source": "src",
        "filters": [{"all_of": [{"column": "A", "op": "eq", "value": 1}, {"column": "B", "op": "eq", "value": 2}]}],
    }
    result_all = build_population("pop", pop_cfg_all, ctx)
    assert result_all.rows == 1


def test_derive_map_counts_unmapped_never_defaults():
    df = pd.DataFrame({"Booking Type": ["Domestic", "International", "Unknown Type"]})
    ctx = _ctx({"src": _src(df)})
    pop_cfg = {
        "source": "src",
        "derive": [
            {
                "op": "map",
                "column": "Booking Type",
                "mapping": {"Domestic": "Dom", "International": "Int"},
                "as": "Class",
            }
        ],
    }
    result = build_population("pop", pop_cfg, ctx)
    assert result.df["Class"].tolist()[:2] == ["Dom", "Int"]
    assert pd.isna(result.df["Class"].iloc[2])  # unmapped -> null, never defaulted
    assert result.derivation_counters["Class_unmapped_rows"] == 1


def test_derive_lookup_counts_unmapped_never_defaults():
    df = pd.DataFrame({"City/Location": ["Sydney", "Nowhereville"]})
    ref = pd.DataFrame({"city": ["Sydney"], "country": ["Australia"]})
    ctx = _ctx({"src": _src(df)}, references={"city_country": ref})
    pop_cfg = {
        "source": "src",
        "derive": [
            {"op": "lookup", "table": "city_country", "on": ["City/Location"], "ref_on": ["city"], "value_column": "country", "as": "Country"}
        ],
    }
    result = build_population("pop", pop_cfg, ctx)
    assert result.df["Country"].iloc[0] == "Australia"
    assert pd.isna(result.df["Country"].iloc[1])  # unmapped -> null, never defaulted
    assert result.derivation_counters["Country_unmapped_rows"] == 1


def test_derive_fx_rate_and_multiply():
    df = pd.DataFrame({"Amount SGD": [100.0], "Transaction Month": ["2025-01"]})
    rates = pd.DataFrame({"month": ["2025-01"], "rate": [1.15]})
    ctx = _ctx({"src": _src(df)}, references={"fx": rates})
    pop_cfg = {
        "source": "src",
        "derive": [
            {"op": "fx_rate", "table": "fx", "month_column": "Transaction Month", "key_column": "month", "value_column": "rate", "as": "FX Rate"},
            {"op": "multiply", "left": {"column": "Amount SGD"}, "right": {"column": "FX Rate"}, "as": "Amount AUD"},
        ],
    }
    result = build_population("pop", pop_cfg, ctx)
    assert result.df["Amount AUD"].iloc[0] == pytest.approx(115.0)


def test_amount_and_date_reconciliation_summary():
    df = pd.DataFrame({"Amount": [100.0, 200.0], "Transaction Date": pd.to_datetime(["2025-01-01", "2025-01-15"])})
    ctx = _ctx({"src": _src(df)})
    pop_cfg = {"source": "src", "amount_column": "Amount", "date_column": "Transaction Date"}
    result = build_population("pop", pop_cfg, ctx)
    assert result.amount == 300.0
    assert result.min_date == "2025-01-01"
    assert result.max_date == "2025-01-15"
    summary = result.reconciliation_summary()
    assert summary["rows"] == 2
    assert summary["amount"] == 300.0
