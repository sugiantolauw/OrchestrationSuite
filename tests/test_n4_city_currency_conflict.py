"""CLAUDE.md P2/P3 gate review item 5 (N4): T6.1d's country_from_city_or_currency
(skills/tne_exco/custom.py) must report rows where the city->country lookup is
ambiguous or CONFLICTS with the currency->country mapping, as a real run
metric -- never silently accepted just because the declared rule (city wins)
has an unambiguous answer to give."""

from __future__ import annotations

import pandas as pd
import pytest

from orchestrator.populations import PopulationContext
from skills.tne_exco.custom import country_from_city_or_currency

CITY_TABLE = [
    {"city": "Sydney", "country": "Australia"},
    {"city": "Singapore", "country": "Singapore"},
]
CURRENCY_TABLE = [
    {"currency": "AUD", "country": "Australia"},
    {"currency": "SGD", "country": "Singapore"},
    {"currency": "USD", "country": "United States"},
]


def _ctx():
    return PopulationContext(
        sources={},
        references={"city_country": CITY_TABLE, "currency_country": CURRENCY_TABLE},
        audit_period=("2025-01-01", "2025-12-31"),
        thresholds={},
    )


def test_no_conflict_when_city_and_currency_agree():
    df = pd.DataFrame({"City/Location": ["Sydney"], "Transaction Currency": ["AUD"]})
    country, counters = country_from_city_or_currency(df, {}, _ctx())
    assert list(country) == ["Australia"]
    assert counters["t61d_city_currency_conflict_rows"] == 0
    assert counters["t61d_unmapped_rows"] == 0


def test_conflict_when_city_and_currency_disagree():
    # City says Sydney (Australia) but the transaction's own currency is USD
    # (United States) -- city still wins per the declared rule, but this is
    # exactly the kind of data inconsistency N4 asks to surface, never accept
    # silently.
    df = pd.DataFrame({"City/Location": ["Sydney"], "Transaction Currency": ["USD"]})
    country, counters = country_from_city_or_currency(df, {}, _ctx())
    assert list(country) == ["Australia"]  # the declared rule is unchanged
    assert counters["t61d_city_currency_conflict_rows"] == 1
    assert counters["t61d_unmapped_rows"] == 0


def test_no_conflict_check_when_currency_does_not_resolve():
    # City resolves, currency does not map to any country at all -- not a
    # CONFLICT (nothing to disagree with), so it must not be counted as one.
    df = pd.DataFrame({"City/Location": ["Sydney"], "Transaction Currency": ["XYZ"]})
    country, counters = country_from_city_or_currency(df, {}, _ctx())
    assert list(country) == ["Australia"]
    assert counters["t61d_city_currency_conflict_rows"] == 0


def test_currency_fallback_used_when_city_absent_never_flagged_as_conflict():
    # City absent -> currency fallback governs `country` directly; there is
    # no "other side" to disagree with, so this must never register as a
    # conflict even though it is the currency-driven path.
    df = pd.DataFrame({"City/Location": [None], "Transaction Currency": ["SGD"]})
    country, counters = country_from_city_or_currency(df, {}, _ctx())
    assert list(country) == ["Singapore"]
    assert counters["t61d_city_currency_conflict_rows"] == 0


def test_unmapped_when_neither_resolves():
    df = pd.DataFrame({"City/Location": [None], "Transaction Currency": ["XYZ"]})
    country, counters = country_from_city_or_currency(df, {}, _ctx())
    assert pd.isna(country.iloc[0])
    assert counters["t61d_unmapped_rows"] == 1
    assert counters["t61d_city_currency_conflict_rows"] == 0


def test_conflict_counter_is_wired_through_to_a_real_run_metric():
    # CLAUDE.md P2/P3 gate review item 5: the counter must be a real
    # RUN METRIC (orchestrator.primitives.common.build_metrics now merges
    # population.derivation_counters into `kind: value` lookups), not just
    # computed and discarded.
    from orchestrator.primitives.common import PopulationResult, build_metrics

    population = PopulationResult(
        name="t61d_pop_dom", source="expense_report", source_version="v1",
        df=pd.DataFrame({"__row_key": []}), rows=0, amount=None, min_date=None, max_date=None,
        derivation_counters={"t61d_city_currency_conflict_rows": 3, "t61d_unmapped_rows": 1},
    )
    metrics = build_metrics(
        {"t61d_dom_city_currency_conflict_rows": {"kind": "value", "key": "t61d_city_currency_conflict_rows"}},
        population=population, default_columns=[], grain="row",
    )
    assert metrics["t61d_dom_city_currency_conflict_rows"]["value"] == 3


def test_a_primitives_own_value_wins_over_a_population_counter_of_the_same_name():
    from orchestrator.primitives.common import PopulationResult, build_metrics

    population = PopulationResult(
        name="p", source="s", source_version="v1", df=pd.DataFrame({"__row_key": []}),
        rows=0, amount=None, min_date=None, max_date=None,
        derivation_counters={"population_size": 999},
    )
    metrics = build_metrics(
        {"n": {"kind": "value", "key": "population_size"}},
        population=population, default_columns=[], grain="row",
        values={"population_size": 5},
    )
    assert metrics["n"]["value"] == 5
