"""The ONE audit-period-timezone rule (CLAUDE.md §0.5, NN14, §5 G6/G9;
independent test-gap audit #13/H9): the contract's declared timezone is the
business calendar, a naive source datetime already represents local
wall-clock time in it, a timezone-aware one (a UC TIMESTAMP comes back UTC)
is converted to it, and the audit period [start, end] is the inclusive local
calendar days in that timezone -- applied identically on the UC-table,
Volume-file and local-file source paths.

Exercises orchestrator.timeutil.to_business_local directly, then the same
rule end to end through orchestrator.engine.execute_skill on a minimal
single-source Skill, comparing a LocalFileDataSource run (naive, already
local) against a UCTableDataSource run (tz-aware UTC, via a fake connection)
over the SAME real moments."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from orchestrator.adapters.datasource_uc import UCTableDataSource, _normalise_datetime_dtypes
from orchestrator.config import Settings
from orchestrator.contract import ContractViolation, LocalFileDataSource
from orchestrator.engine import execute_skill
from orchestrator.skills import Skill
from orchestrator.timeutil import to_business_local

TZ = "Australia/Sydney"
AUDIT_PERIOD = ("2026-04-01", "2026-04-30")

# The three moments every scenario below shares, in Sydney LOCAL wall-clock
# terms (April 30 2026 is well after Sydney's 2026 DST-end, so AEST/+10):
#   mid       -- comfortably inside the period
#   boundary_in  -- 23:30 local on the last day of the period: must be IN
#   boundary_out -- 00:30 local the day after: must be OUT
_LOCAL_MID = pd.Timestamp("2026-04-15 10:00:00")
_LOCAL_IN = pd.Timestamp("2026-04-30 23:30:00")
_LOCAL_OUT = pd.Timestamp("2026-05-01 00:30:00")


def _utc_equivalent(local_naive: pd.Timestamp) -> pd.Timestamp:
    """The real UTC instant a correct connector would return for this Sydney
    local wall-clock moment -- derived via zoneinfo, never hand-computed, so
    the fixture can't itself be wrong about the offset."""
    return local_naive.tz_localize(TZ).tz_convert("UTC")


# ── orchestrator.timeutil.to_business_local ─────────────────────────────────


def test_to_business_local_leaves_a_naive_scalar_unchanged():
    ts = pd.Timestamp("2026-04-30 23:30:00")
    assert to_business_local(ts, TZ) == ts


def test_to_business_local_converts_a_tz_aware_scalar_to_the_declared_zone():
    utc_ts = _utc_equivalent(_LOCAL_IN)
    assert utc_ts.tzinfo is not None
    out = to_business_local(utc_ts, TZ)
    assert out.tzinfo is None
    assert out == _LOCAL_IN


def test_to_business_local_leaves_a_naive_series_unchanged():
    s = pd.Series(pd.to_datetime(["2026-04-30 23:30:00", "2026-05-01 00:30:00"]))
    out = to_business_local(s, TZ)
    pd.testing.assert_series_equal(out, s)


def test_to_business_local_converts_a_tz_aware_series_to_the_declared_zone():
    s = pd.Series([_utc_equivalent(_LOCAL_IN), _utc_equivalent(_LOCAL_OUT)])
    out = to_business_local(s, TZ)
    assert out.dt.tz is None
    assert out.tolist() == [_LOCAL_IN, _LOCAL_OUT]


def test_to_business_local_a_non_datetime_series_passes_through():
    s = pd.Series([1, 2, 3])
    out = to_business_local(s, TZ)
    assert out.tolist() == [1, 2, 3]


def test_to_business_local_handles_a_dst_transition_correctly():
    # Sydney's 2026 DST (AEDT, +11) ends 2026-04-05 03:00 AEDT = 2026-04-04
    # 16:00 UTC. A FIXED offset (rather than a real IANA conversion) would
    # get one side of this wrong.
    before = to_business_local(pd.Timestamp("2026-04-04T13:00:00", tz="UTC"), TZ)
    after = to_business_local(pd.Timestamp("2026-04-04T19:00:00", tz="UTC"), TZ)
    assert before == pd.Timestamp("2026-04-05 00:00:00")  # +11h (AEDT, pre-transition)
    assert after == pd.Timestamp("2026-04-05 05:00:00")  # +10h (AEST, post-transition)


# ── UCTableDataSource: _normalise_datetime_dtypes / read_population ────────


def test_normalise_datetime_dtypes_converts_to_the_declared_timezone_not_utc():
    df = pd.DataFrame({"d": [_utc_equivalent(_LOCAL_IN), _utc_equivalent(_LOCAL_OUT)]})
    out = _normalise_datetime_dtypes(df.copy(), TZ)
    assert str(out["d"].dtype) == "datetime64[us]"
    assert out["d"].tolist() == [_LOCAL_IN, _LOCAL_OUT]


def test_normalise_datetime_dtypes_defaults_to_utc_when_no_timezone_given():
    # Backward-compatible default (legacy/pre-frame-snapshot callers that
    # have no contract in hand, e.g. orchestrator.service.
    # _get_run_frames_from_sources) -- unchanged from before this fix.
    df = pd.DataFrame({"d": pd.to_datetime(["2026-01-01T00:00:00Z"])})
    out = _normalise_datetime_dtypes(df.copy())
    assert out["d"].tolist() == [pd.Timestamp("2026-01-01 00:00:00")]


# ── FakeConnection harness for UCTableDataSource (mirrors
# tests/test_datasource_uc.py's own) ────────────────────────────────────────


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self._rows = []
        self._pos = 0
        self._arrow = None

    def execute(self, sql_text, params=None):
        self.conn.calls.append(sql_text)
        for key, handler in self.conn.handlers.items():
            if key in sql_text:
                columns, rows, arrow = handler(sql_text, params or {})
                self.description = [(c,) for c in columns]
                self._rows = list(rows)
                self._pos = 0
                self._arrow = arrow
                return self
        raise AssertionError(f"no fake handler for statement: {sql_text}")

    def fetchone(self):
        if self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def fetchall_arrow(self):
        if self._arrow is not None:
            return self._arrow
        columns = [d[0] for d in self.description]
        data = {c: [r[i] for r in self._rows] for i, c in enumerate(columns)}
        return pa.table(data) if columns else pa.table({})

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, handlers):
        self.handlers = handlers
        self.calls: list[str] = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass


def _uc_data_source(claims_df: pd.DataFrame, *, version: int = 5) -> UCTableDataSource:
    """A fake UCTableDataSource bound to one table ('cat.sch.claims') whose
    data query always returns `claims_df` -- a pandas DataFrame with a
    genuinely tz-aware ("Transaction Date") column, exactly what
    databricks-sql-connector hands back for a real TIMESTAMP column."""
    columns = list(claims_df.columns)
    with_source_row = claims_df.copy()
    with_source_row.insert(0, "_source_row", range(1, len(with_source_row) + 1))
    arrow = pa.Table.from_pandas(with_source_row, preserve_index=False)

    def describe_history(sql, params):
        return (["version"], [[version]], None)

    def describe_columns(sql, params):
        return (["_source_row"] + columns, [], None)

    def count_star(sql, params):
        return (["n"], [[len(claims_df)]], None)

    def data_query(sql, params):
        return (["_source_row"] + columns, [], arrow)

    handlers = {
        "DESCRIBE HISTORY": describe_history,
        "LIMIT 0": describe_columns,
        "SELECT count(*)": count_star,
        "ORDER BY `_source_row`": data_query,
    }
    conn = _FakeConnection(handlers)
    settings = Settings(host="https://x.cloud.databricks.com", warehouse_http_path="/sql/1.0/warehouses/abc")
    return UCTableDataSource(settings, {"claims": "cat.sch.claims"}, connection_factory=lambda: conn)


# ── minimal single-source Skill: same fixture, both paths ──────────────────


def _contract(timezone: str | None = TZ) -> dict:
    contract = {
        "sources": {
            "claims": {
                "format": "csv",
                "file": "claims.csv",
                "columns": {
                    "Employee ID": {"type": "integer", "nullable": False},
                    "Amount": {"type": "number", "nullable": False},
                    "Transaction Date": {"type": "datetime", "nullable": False},
                },
            },
        },
    }
    if timezone is not None:
        contract["timezone"] = timezone
    return contract


def _plan() -> dict:
    return {
        "populations": {
            "raw_claims": {"source": "claims", "amount_column": "Amount", "date_column": "Transaction Date"},
            "in_period": {
                "source": "claims",
                "filters": [{"column": "Transaction Date", "op": "between", "value": {"ref": "audit_period"}}],
                "amount_column": "Amount",
                "date_column": "Transaction Date",
            },
        },
        "tests": [
            {
                "test_id": "T-BOUNDARY",
                "primitive": "threshold_exceedance",
                "flag": "RF_OVER",
                "params": {
                    "population": "in_period", "column": "Amount",
                    "limit": {"threshold": "high_amount"}, "direction": "above",
                },
                "control_id": "CTL-1", "risk_id": "RSK-1", "assertion": "operating",
            }
        ],
    }


def _skill(skill_dir: Path, timezone: str | None = TZ) -> Skill:
    return Skill(
        skill_dir=skill_dir,
        manifest={"id": "SKILL-TZ-TEST", "version": "0.0.1"},
        contract=_contract(timezone),
        plan=_plan(),
        findings={"findings": []},
        thresholds={"high_amount": {"value": 999_999_999}},
    )


def _local_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Employee ID": [1, 1, 1],
            "Amount": [100.0, 200.0, 300.0],
            "Transaction Date": [_LOCAL_MID, _LOCAL_IN, _LOCAL_OUT],
        }
    )


def _write_claims_csv(tmp_path: Path) -> Path:
    df = _local_rows()
    path = tmp_path / "claims.csv"
    df.to_csv(path, index=False)
    return path


# ── boundary tests, file path ───────────────────────────────────────────────


def test_file_path_23_30_local_last_day_is_in_00_30_next_day_is_out(tmp_path: Path):
    _write_claims_csv(tmp_path)
    skill = _skill(tmp_path)
    ds = LocalFileDataSource(root_dir=tmp_path, sources=skill.contract["sources"])
    result = execute_skill(
        skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "file-boundary"},
    )
    pop = result.populations["in_period"]
    # mid + 23:30-last-day IN, 00:30-next-day OUT -> 2 of 3 rows survive.
    assert pop["rows"] == 2
    assert pop["amount"] == pytest.approx(300.0)  # 100 (mid) + 200 (23:30 last day)


# ── boundary tests, UC path (fake connection, tz-aware) ─────────────────────


def _uc_rows() -> pd.DataFrame:
    df = _local_rows()
    df["Transaction Date"] = [
        _utc_equivalent(_LOCAL_MID), _utc_equivalent(_LOCAL_IN), _utc_equivalent(_LOCAL_OUT),
    ]
    return df


def test_uc_path_23_30_local_last_day_is_in_00_30_next_day_is_out(tmp_path: Path):
    skill = _skill(tmp_path)
    ds = _uc_data_source(_uc_rows())
    result = execute_skill(
        skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "uc-boundary"},
    )
    pop = result.populations["in_period"]
    assert pop["rows"] == 2
    assert pop["amount"] == pytest.approx(300.0)


# ── same data, both paths: identical populations, metrics, findings ────────


def test_file_and_uc_paths_give_identical_populations_metrics_and_findings(tmp_path: Path):
    _write_claims_csv(tmp_path)
    skill = _skill(tmp_path)

    file_ds = LocalFileDataSource(root_dir=tmp_path, sources=skill.contract["sources"])
    file_result = execute_skill(
        skill, data_source=file_ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "cmp-file"},
    )

    uc_ds = _uc_data_source(_uc_rows())
    uc_result = execute_skill(
        skill, data_source=uc_ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "cmp-uc"},
    )

    # Population reconciliation-relevant figures (what G6 compares) agree.
    for name in ("raw_claims", "in_period"):
        file_pop, uc_pop = file_result.populations[name], uc_result.populations[name]
        assert file_pop["rows"] == uc_pop["rows"], name
        assert file_pop["amount"] == pytest.approx(uc_pop["amount"]), name
        assert file_pop["min_date"] == uc_pop["min_date"], name
        assert file_pop["max_date"] == uc_pop["max_date"], name

    # Test outcomes (findings are computed from these) agree.
    assert file_result.test_results == uc_result.test_results
    # Metric VALUES agree (source_ref differs only in irrelevant version
    # bookkeeping the two DataSourceAdapters resolve independently).
    file_values = {k: v["value"] for k, v in file_result.metrics.items()}
    uc_values = {k: v["value"] for k, v in uc_result.metrics.items()}
    assert file_values == uc_values
    assert file_result.findings == uc_result.findings == []


# ── missing timezone fails loudly (NN14) ────────────────────────────────────


def test_execute_skill_fails_loudly_when_contract_has_no_timezone(tmp_path: Path):
    _write_claims_csv(tmp_path)
    skill = _skill(tmp_path, timezone=None)
    assert "timezone" not in skill.contract
    ds = LocalFileDataSource(root_dir=tmp_path, sources=skill.contract["sources"])
    with pytest.raises(ContractViolation) as exc:
        execute_skill(skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "no-tz"})
    assert "timezone" in str(exc.value)


def test_execute_skill_fails_loudly_when_contract_timezone_is_empty_string(tmp_path: Path):
    _write_claims_csv(tmp_path)
    skill = _skill(tmp_path, timezone="")
    ds = LocalFileDataSource(root_dir=tmp_path, sources=skill.contract["sources"])
    with pytest.raises(ContractViolation):
        execute_skill(skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "empty-tz"})
