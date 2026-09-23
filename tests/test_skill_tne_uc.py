from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DELTA_TESTS") != "1",
    reason="RUN_DELTA_TESTS not set — live workspace is unavailable (CLAUDE.md §11)",
)

SKILL_DIR = Path(__file__).parent.parent / "skills" / "tne_exco"
SYNTHETIC_DATA_DIR = Path(__file__).parent.parent / "synthetic_data"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

# Compares the Skill run end to end with its sources bound to Unity Catalog against
# the same run against synthetic_data/ files, so that pointing the T&E Skill's data
# sources at UC tables in the UI (this task's WHY, CLAUDE.md §2.1) produces the same
# metrics as the file-based run. Compared directly against a second live file run
# rather than against tests/snapshots/tne_real_metrics.json: other agents are
# concurrently changing orchestrator/{engine,populations,primitives,findings}.py and
# skills/tne_exco/*.yaml in this same checkout, so the committed snapshot may not
# match the engine version this test runs against. A direct file-vs-UC comparison is
# correct regardless of which engine version is checked out.


def _bindings():
    catalog = os.environ["DBX_CATALOG"]
    schema = os.environ.get("DBX_TNE_SCHEMA", "tne_source")
    with open(SKILL_DIR / "contract.yaml") as f:
        contract = yaml.safe_load(f)
    return {name: f"{catalog}.{schema}.{name}" for name in contract["sources"]}


@pytest.fixture(scope="module")
def tne_skill():
    from orchestrator.skills import load_skill

    skill = load_skill(SKILL_DIR)
    skill.validate()
    return skill


@pytest.fixture(scope="module")
def file_result(tne_skill):
    from orchestrator.contract import LocalFileDataSource
    from orchestrator.engine import execute_skill

    ds = LocalFileDataSource(root_dir=SYNTHETIC_DATA_DIR, sources=tne_skill.contract["sources"])
    return execute_skill(
        tne_skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "test-skill-tne-uc-file"}
    )


@pytest.fixture(scope="module")
def uc_result(tne_skill):
    from orchestrator.adapters.datasource_uc import UCTableDataSource
    from orchestrator.config import load_settings
    from orchestrator.engine import execute_skill

    ds = UCTableDataSource(load_settings(), _bindings())
    try:
        return execute_skill(
            tne_skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "test-skill-tne-uc-uc"}
        )
    finally:
        ds.close()


def test_uc_run_completes_with_no_contract_violation(uc_result):
    assert uc_result.source_versions
    assert len(uc_result.source_versions) == 8


def test_uc_and_file_runs_agree_on_test_statuses(file_result, uc_result):
    file_statuses = {t["test_id"]: t["status"] for t in file_result.test_results}
    uc_statuses = {t["test_id"]: t["status"] for t in uc_result.test_results}
    assert uc_statuses == file_statuses


def test_uc_and_file_runs_agree_on_population_counts(file_result, uc_result):
    file_pops = {name: (p["rows"], p["amount"]) for name, p in file_result.populations.items()}
    uc_pops = {name: (p["rows"], p["amount"]) for name, p in uc_result.populations.items()}
    assert uc_pops == file_pops


def test_uc_and_file_runs_agree_on_every_metric_value(file_result, uc_result):
    file_metrics = {name: m["value"] for name, m in file_result.metrics.items()}
    uc_metrics = {name: m["value"] for name, m in uc_result.metrics.items()}
    assert set(uc_metrics) == set(file_metrics)
    mismatches = {
        name: (file_metrics[name], uc_metrics[name])
        for name in file_metrics
        if file_metrics[name] != uc_metrics[name]
    }
    assert not mismatches, f"metric values diverged between file and UC runs: {mismatches}"


def test_uc_and_file_runs_agree_on_findings(file_result, uc_result):
    # rule_id (skill_id + rule id), not finding_id, since finding_id embeds the
    # run_id and the two fixtures deliberately use different run_ids.
    file_findings = {f["rule_id"]: (f["severity"], tuple(sorted(f["metrics_cited"]))) for f in file_result.findings}
    uc_findings = {f["rule_id"]: (f["severity"], tuple(sorted(f["metrics_cited"]))) for f in uc_result.findings}
    assert uc_findings == file_findings
