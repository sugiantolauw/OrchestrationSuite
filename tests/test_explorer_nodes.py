"""Explorer's discover/profile node branches
(docs/specs/P6_P8_explorer_llm_design.md §4.3) wired end to end through
orchestrator.nodes.fieldwork.discover/profile -- not just the
orchestrator.explorer.profile module functions tests/test_explorer_profile.py
already covers directly. ctx.skill is None throughout, matching §4.11
resolve_run_skill's "Explorer before confirmation: None"."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import runs as runs_module
from orchestrator.config import DEFAULT_PPTX_TEMPLATE_PATH
from orchestrator.contract import ContractViolation, LocalFileDataSource
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import discover, profile
from tests.conftest import canonical_ts

AUDIT_PERIOD = ("2026-01-01", "2026-02-28")


def _fingerprint(fp_id: str) -> dict:
    return dict(
        fingerprint_id=fp_id, source_table_versions="{}", uploaded_file_hashes="{}",
        reference_data_hashes="{}", skill_content_hash=None, code_revision="rev1",
        dependency_lock_hash="dep1", runtime_config_hash="rc1", endpoint_config="{}",
        prompt_template_version="none", created_at=canonical_ts(0),
    )


def _settings(**overrides):
    defaults = dict(
        catalog=None, schema=None, pptx_template_path=DEFAULT_PPTX_TEMPLATE_PATH,
        explorer_category_max_distinct=30, explorer_category_min_count=1,
        explorer_max_columns=200, pii_tag_names=(), audit_timezone="Australia/Sydney",
    )
    defaults.update(overrides)
    return type("S", (), defaults)()


def _make_explorer_harness(local_persistence, tmp_path, *, settings=None):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    df = pd.DataFrame({
        "Employee": ["Alice Smith", "Alice Smith", "Bob Jones", "Carol Wu"],
        "Amount": [100, 200, 300, 400],
        "Category": ["Travel", "Travel", "Meals", "Travel"],
    })
    df.to_csv(data_dir / "claims.csv", index=False)

    sources = {"claims": {"format": "csv", "file": "claims.csv"}}
    data_source = LocalFileDataSource(root_dir=data_dir, sources=sources)
    version = data_source.resolve_version("claims")

    fp = _fingerprint(f"FP-{tmp_path.name}")
    now = canonical_ts(1)
    state = runs_module.create_run(
        local_persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="explorer", audit_period=AUDIT_PERIOD, objective="Assess claims",
        run_owner="alice", options={}, fingerprint=fp, now=now,
    )
    data_assets = [{"source": "claims", "table_fqn": "claims", "version": version}]
    state = local_persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    ctx = NodeContext(
        settings=settings or _settings(), persistence=local_persistence, data_source=data_source,
        skill=None, clock=lambda: canonical_ts(2),
    )
    return ctx, state, version


def test_explorer_discover_validates_bindings(local_persistence, tmp_path):
    ctx, state, _ = _make_explorer_harness(local_persistence, tmp_path)
    result = discover(ctx, state)
    assert any(e["node"] == "discover" for e in result.events)
    assert "claims" in result.events[-1]["message"]


def test_explorer_discover_fails_when_source_changed_since_pinning(local_persistence, tmp_path):
    ctx, state, _ = _make_explorer_harness(local_persistence, tmp_path)
    tampered = [dict(b) for b in state.data_assets]
    tampered[0]["version"] = "not-the-real-hash"
    stale = dataclasses.replace(state, data_assets=tampered)
    with pytest.raises(ContractViolation):
        discover(ctx, stale)


def test_explorer_profile_masks_pii_and_keeps_non_pii(local_persistence, tmp_path):
    ctx, state, _ = _make_explorer_harness(local_persistence, tmp_path)
    state = discover(ctx, state)
    result = profile(ctx, state)

    assert result.profile_result["kind"] == "explorer"
    claims = result.profile_result["sources"]["claims"]
    assert claims["row_count"] == 4
    columns = {c["name"]: c for c in claims["columns"]}
    # "Employee" matches skills/tne_exco/contract.yaml's expense_report.Employee
    # PII flag -- but this source is named "claims", so rule 1 does NOT apply;
    # it is masked purely by the heuristic name-token rule instead.
    assert columns["Employee"]["pii"] is True
    assert "values" not in columns["Employee"]
    assert columns["Amount"]["pii"] is False
    assert columns["Category"]["pii"] is False
    assert columns["Category"]["values"]

    assert any(e["node"] == "profile" for e in result.events)
    assert "PII column(s) masked" in result.events[-1]["message"]


def test_explorer_profile_raises_when_over_the_column_cap(local_persistence, tmp_path):
    ctx, state, _ = _make_explorer_harness(
        local_persistence, tmp_path, settings=_settings(explorer_max_columns=2),
    )
    state = discover(ctx, state)
    with pytest.raises(ContractViolation, match="too many columns for Explorer"):
        profile(ctx, state)


def test_explorer_profile_computes_in_period_count_from_settings_audit_timezone(local_persistence, tmp_path):
    # xlsx, not csv: pandas.read_csv never parses a text column as a date on
    # its own (orchestrator.explorer.profile.infer_column_type -- conservative,
    # never guessed), so a CSV-sourced date column profiles as "string" with
    # no min/max/in_period_count. An Excel date CELL round-trips as a real
    # datetime64 dtype, which is what this test needs to exercise.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    df = pd.DataFrame({
        "Transaction Date": pd.to_datetime(["2026-01-05", "2026-01-10", "2026-03-01"]),
        "Amount": [10, 20, 30],
    })
    df.to_excel(data_dir / "claims.xlsx", index=False, sheet_name="data")
    sources = {"claims": {"format": "xlsx", "sheet": "data", "file": "claims.xlsx"}}
    data_source = LocalFileDataSource(root_dir=data_dir, sources=sources)
    version = data_source.resolve_version("claims")

    fp = _fingerprint(f"FP-{tmp_path.name}-2")
    now = canonical_ts(1)
    state = runs_module.create_run(
        local_persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=None,
        skill_version=None, mode="explorer", audit_period=AUDIT_PERIOD, objective="t",
        run_owner="alice", options={}, fingerprint=fp, now=now,
    )
    state = local_persistence.save_state(dataclasses.replace(
        state, data_assets=[{"source": "claims", "table_fqn": "claims", "version": version}]
    ))
    ctx = NodeContext(
        settings=_settings(), persistence=local_persistence, data_source=data_source,
        skill=None, clock=lambda: canonical_ts(2),
    )
    result = profile(ctx, discover(ctx, state))
    date_col = next(
        c for c in result.profile_result["sources"]["claims"]["columns"] if c["name"] == "Transaction Date"
    )
    assert date_col["in_period_count"] == 2  # AUDIT_PERIOD is 2026-01-01..2026-02-28
