from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.contract import LocalFileDataSource
from orchestrator.engine import execute_skill
from orchestrator.findings import _format_template
from orchestrator.skills import load_skill

SKILL_DIR = Path(__file__).parent.parent / "skills" / "tne_exco"
SYNTHETIC_DATA_DIR = Path(__file__).parent.parent / "synthetic_data"
SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "tne_real_metrics.json"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark = pytest.mark.skipif(
    not SYNTHETIC_DATA_DIR.is_dir(), reason="synthetic_data/ not present in this checkout"
)


@pytest.fixture(scope="module")
def tne_skill():
    skill = load_skill(SKILL_DIR)
    skill.validate()
    return skill


@pytest.fixture(scope="module")
def tne_result(tne_skill):
    ds = LocalFileDataSource(root_dir=SYNTHETIC_DATA_DIR, sources=tne_skill.contract["sources"])
    return execute_skill(
        tne_skill,
        data_source=ds,
        audit_period=AUDIT_PERIOD,
        run_context={"run_id": "test-skill-tne"},
    )


def test_skill_loads_and_validates(tne_skill):
    assert tne_skill.skill_id == "SKILL-001"
    assert len(tne_skill.plan["tests"]) == 21  # 14 catalogue tests, 7 split into sub-tests
    assert len(tne_skill.findings["findings"]) == 13  # every finding except T4.3 (not_testable)


def test_runs_end_to_end_on_real_synthetic_data_no_contract_violation(tne_result):
    # execute_skill() completing at all (the fixture) is itself the assertion
    # that no ContractViolation was raised reading the real files.
    assert tne_result.source_versions
    assert len(tne_result.source_versions) == 8


def test_every_test_result_has_a_valid_status(tne_result):
    for t in tne_result.test_results:
        assert t["status"] in ("exception", "pass", "not_testable")


def test_not_testable_only_where_the_spec_says(tne_result):
    not_testable = {t["test_id"]: t["reason"] for t in tne_result.test_results if t["status"] == "not_testable"}
    assert set(not_testable) == {"T3.2a_accom", "T4.3"}
    assert "hotel" in not_testable["T3.2a_accom"]
    assert "classification endpoint" in not_testable["T4.3"]


def test_p_exp_population_counts(tne_result):
    # See the P2b-1 report for why these differ from the spec's stated headline
    # numbers (3,738/3,654/1,444): the spec's own stated default excludes 50040
    # (a likely namesake of 52472), and these are the counts that default
    # actually produces on the real synthetic_data/ files.
    pops = tne_result.populations
    assert pops["p_exp_prepared_all"]["rows"] == 1417
    assert pops["p_exp_approved_all"]["rows"] == 2317
    assert pops["p_exp_both_all"]["rows"] == 23
    assert pops["p_exp"]["rows"] == 3628  # union, in period


def test_metrics_snapshot_matches_committed_file(tne_result):
    assert SNAPSHOT_PATH.is_file(), "run this file's generator and commit the snapshot first"
    committed = json.loads(SNAPSHOT_PATH.read_text())

    actual_metrics = {name: m["value"] for name, m in tne_result.metrics.items()}
    actual_statuses = {t["test_id"]: t["status"] for t in tne_result.test_results}
    actual_populations = {
        name: {"rows": p["rows"], "amount": p["amount"]} for name, p in tne_result.populations.items()
    }

    assert actual_metrics == committed["metrics"], (
        "metric values changed since the committed snapshot -- if this is a "
        "deliberate Skill or engine change, regenerate and re-commit the snapshot"
    )
    assert actual_statuses == committed["test_statuses"]
    assert actual_populations == committed["populations"]


def test_findings_build_without_error(tne_result):
    for f in tne_result.findings:
        assert f["severity"] in ("High", "Medium", "Low")
        assert f["metrics_cited"]


def test_all_finding_templates_render_cleanly_against_real_metrics(tne_skill, tne_result):
    # Renders every SKILL-001 finding template (not just the ones that trigger
    # on this run's population) against this run's real computed metrics --
    # catches a mismatched unit/template convention (e.g. a metric whose
    # format_metric_value already appends "%" and a template that also has a
    # literal "%" after the placeholder, rendering "84.6%%") that a spot check
    # on only the triggered findings could miss.
    metrics = tne_result.metrics
    committed = json.loads(SNAPSHOT_PATH.read_text())["metrics"]
    for name, expected in committed.items():
        assert metrics[name]["value"] == expected, f"{name}: not the committed real-metrics snapshot value"

    for rule in tne_skill.findings.get("findings", []):
        cited_names = rule.get("metrics_cited", [])
        missing = [n for n in cited_names if n not in metrics]
        assert not missing, f"{rule['id']}: metrics_cited references unknown metric(s) {missing}"
        cited = {n: metrics[n] for n in cited_names}
        for field in ("observation", "recommendation"):
            text = rule.get(field) or ""
            if not text.strip():
                continue
            rendered = _format_template(text, cited)
            assert "%%" not in rendered, f"{rule['id']}.{field}: double percent in rendered text: {rendered!r}"
            assert "{" not in rendered and "}" not in rendered, (
                f"{rule['id']}.{field}: unrendered template braces: {rendered!r}"
            )
