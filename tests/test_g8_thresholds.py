from __future__ import annotations

import string
from pathlib import Path

import pytest
import yaml

from orchestrator.expr import compile_expr
from orchestrator.skills import load_skill

SKILL_DIR = Path(__file__).parent.parent / "skills" / "tne_exco"


@pytest.fixture(scope="module")
def tne_skill():
    skill = load_skill(SKILL_DIR)
    skill.validate()
    return skill


@pytest.fixture(scope="module")
def catalogue() -> dict:
    return yaml.safe_load((SKILL_DIR / "catalogue.yaml").read_text())


def _collect_all(container, key: str) -> set[str]:
    out: set[str] = set()

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == key and isinstance(v, str):
                    out.add(v)
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(container)
    return out


def test_catalogue_threshold_text_matches_rendered_thresholds_yaml_values(tne_skill, catalogue):
    thr_values = {tid: spec["value"] for tid, spec in tne_skill.thresholds.items()}
    fmt = string.Formatter()
    for entry in catalogue["tests"]:
        placeholders = {fn for _, fn, _, _ in fmt.parse(entry["threshold"]) if fn}
        unknown = placeholders - set(thr_values)
        assert not unknown, f"{entry['test_id']}: catalogue threshold references unknown id(s) {unknown}"
        entry["threshold"].format(**thr_values)  # must render without KeyError


def test_every_threshold_referenced_by_plan_or_findings_exists(tne_skill):
    plan_refs = _collect_all(tne_skill.plan, "threshold")
    findings_refs: set[str] = set()
    for finding in tne_skill.findings["findings"]:
        findings_refs |= compile_expr(finding["trigger"]).threshold_ids
        for rule in finding["severity"]:
            if "when" in rule:
                findings_refs |= compile_expr(rule["when"]).threshold_ids

    all_refs = plan_refs | findings_refs
    unknown = all_refs - set(tne_skill.thresholds)
    assert not unknown, f"unknown threshold id(s) referenced: {unknown}"


def test_every_thresholds_yaml_entry_is_referenced_somewhere(tne_skill, catalogue):
    plan_refs = _collect_all(tne_skill.plan, "threshold")
    findings_refs: set[str] = set()
    for finding in tne_skill.findings["findings"]:
        findings_refs |= compile_expr(finding["trigger"]).threshold_ids
        for rule in finding["severity"]:
            if "when" in rule:
                findings_refs |= compile_expr(rule["when"]).threshold_ids

    catalogue_refs: set[str] = set()
    fmt = string.Formatter()
    for entry in catalogue["tests"]:
        catalogue_refs |= {fn for _, fn, _, _ in fmt.parse(entry["threshold"]) if fn}

    referenced = plan_refs | findings_refs | catalogue_refs
    unused = set(tne_skill.thresholds) - referenced
    assert not unused, f"thresholds.yaml entries never referenced: {unused}"


def test_every_analyst_set_threshold_has_pending_confirmation_true(tne_skill):
    for tid, spec in tne_skill.thresholds.items():
        provenance = spec["provenance"]
        if provenance["type"] == "analyst-set":
            assert provenance["pending_policy_confirmation"] is True, tid
        else:
            assert provenance.get("reference"), f"{tid}: provenance.type=policy requires a reference"


def test_findings_with_analyst_set_matched_severity_are_labelled(tne_skill):
    from orchestrator.findings import build_findings

    # Build a metrics dict with every metric present and non-zero/high so every
    # trigger fires and every "when" branch with the largest threshold matches --
    # forces every finding's High/Medium branch to be exercised at least once.
    metrics = {}
    for test in tne_skill.plan["tests"]:
        for name, spec in test.get("params", {}).get("metrics", {}).items():
            unit = spec.get("unit") or ("%" if "pct" in name else "count")
            metrics[name] = {"value": 99999, "unit": unit, "source_ref": {}}
    for name in ("approver_total_reports", "approver_count", "approver_no_receipt_pct", "approver_instant_pct", "approver_worst_case_n", "approver_worst_case_pct"):
        metrics.setdefault(name, {"value": 99999, "unit": "count", "source_ref": {}})

    findings = build_findings(tne_skill, run_id="g8-test", metrics=metrics)
    thresholds = tne_skill.thresholds
    assert findings, "expected every finding to trigger with all metrics maxed out"
    for f in findings:
        for ref in f["threshold_refs"]:
            assert thresholds[ref["id"]]["provenance"]["type"] == ref["provenance_type"]
        if f["analyst_set_severity"]:
            assert any(r["provenance_type"] == "analyst-set" for r in f["threshold_refs"])
