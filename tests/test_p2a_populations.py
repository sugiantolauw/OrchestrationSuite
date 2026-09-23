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


def test_between_filter_includes_full_last_day_for_datetime_columns():
    # Regression: a datetime column (not just a date) compared against a bare
    # date upper bound must include the whole last day, not just its midnight.
    df = pd.DataFrame(
        {"Approved At": pd.to_datetime(["2025-01-31 09:00:00", "2025-01-31 23:59:00", "2025-02-01 00:00:01"])}
    )
    ctx = _ctx({"src": _src(df)}, audit_period=("2025-01-01", "2025-01-31"))
    pop_cfg = {
        "source": "src",
        "filters": [{"column": "Approved At", "op": "between", "value": {"ref": "audit_period"}}],
    }
    result = build_population("pop", pop_cfg, ctx)
    assert result.rows == 2  # both 2025-01-31 rows included, the 2025-02-01 row excluded


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


def test_pre_filter_derive_lets_a_filter_key_on_a_cleaned_up_value():
    # Cross Change Approver-shaped scenario: a column mixes numeric ids with a
    # non-numeric sentinel ("Concur System") -- coerce to numeric BEFORE the `in`
    # filter runs, so the sentinel value fails to match rather than crashing or
    # being silently included.
    df = pd.DataFrame({"Approver": ["52725", "Concur System", "52394", None]})
    ctx = _ctx({"src": _src(df)}, references={"exco": [52725, 52394]})

    def to_numeric_approver(frame, params, ctx):
        numeric = pd.to_numeric(frame["Approver"], errors="coerce")
        non_numeric = numeric.isna() & frame["Approver"].notna()
        return numeric, {"approver_non_numeric_rows": int(non_numeric.sum())}

    ctx.custom_derivations["to_numeric_approver"] = to_numeric_approver
    pop_cfg = {
        "source": "src",
        "pre_filter_derive": [{"op": "custom", "name": "to_numeric_approver", "as": "approver_id"}],
        "filters": [{"column": "approver_id", "op": "in", "value": {"ref": "exco"}}],
    }
    result = build_population("pop", pop_cfg, ctx)
    assert result.rows == 2
    assert result.derivation_counters["approver_non_numeric_rows"] == 1


def test_post_filter_runs_after_derive_and_scopes_unmapped_counter_to_filtered_population():
    # Two-phase build (P2b-1 fix): `filters` narrows the population on raw columns
    # FIRST, `derive` then only has to run over that narrowed set (so its unmapped
    # counter is scoped correctly), and `post_filter` -- which needs the DERIVED
    # column -- runs last. A row excluded by `filters` (Bob) must never count
    # towards the unmapped-lookup counter, even though "Nowhereville" would also
    # fail to map.
    df = pd.DataFrame(
        {
            "Employee": ["Alice", "Alice", "Bob"],
            "City/Location": ["Sydney", "Nowhereville", "Nowhereville"],
        }
    )
    ref = pd.DataFrame({"city": ["Sydney"], "country": ["Australia"]})
    ctx = _ctx({"src": _src(df)}, references={"city_country": ref})
    pop_cfg = {
        "source": "src",
        "filters": [{"column": "Employee", "op": "eq", "value": "Alice"}],
        "derive": [
            {
                "op": "lookup",
                "table": "city_country",
                "on": ["City/Location"],
                "ref_on": ["city"],
                "value_column": "country",
                "as": "Country",
                "unmapped_counter": "country_unmapped_rows",
            }
        ],
        "post_filter": [{"column": "Country", "op": "eq", "value": "Australia"}],
    }
    result = build_population("pop", pop_cfg, ctx)
    assert result.rows == 1  # only Alice's Sydney row survives post_filter
    assert result.df["Employee"].tolist() == ["Alice"]
    # Bob's row was excluded by `filters` before `derive` ever ran -- the
    # unmapped counter is scoped to Alice's 2 rows (1 unmapped), not all 3.
    assert result.derivation_counters["country_unmapped_rows"] == 1
    assert result.excluded_counts["post_filter[0]:Country:eq"] == 1


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
