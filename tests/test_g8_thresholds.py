from __future__ import annotations

import string
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestrator.expr import compile_expr
from orchestrator.findings import build_findings
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
        # Item 7 (CLAUDE.md P2/P3 gate review): thresholds_cited is a prose
        # reference (a finding quoting a threshold's own value), as real a
        # reference as trigger/severity's.
        findings_refs |= set(finding.get("thresholds_cited", []))

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
        findings_refs |= set(finding.get("thresholds_cited", []))

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
        assert f["severity_basis"] in ("fixed", "threshold")
        # B4: analyst_set_severity holds either because the severity ladder
        # consulted no threshold at all (severity_basis == 'fixed' -- a bare
        # else/fixed severity is analyst-set by definition, exercised directly
        # against a minimal fixture below) or because a threshold consulted
        # matches the not-all-policy rule (also exercised directly below).
        # threshold_refs here mixes the trigger's own thresholds in, so it is
        # not itself sufficient evidence either way against real Skill data.
        if f["severity_basis"] == "fixed":
            assert f["analyst_set_severity"] is True


def _fake_skill(findings: list[dict]) -> SimpleNamespace:
    thresholds = {
        "policy_thr": {"value": 10, "unit": "count", "provenance": {"type": "policy", "reference": "Policy X §1"}},
        "analyst_thr": {
            "value": 5,
            "unit": "count",
            "provenance": {"type": "analyst-set", "pending_policy_confirmation": True},
        },
    }
    return SimpleNamespace(
        skill_id="SKILL-TEST",
        thresholds=thresholds,
        plan={"tests": []},
        findings={"findings": findings},
    )


def test_severity_basis_fixed_for_a_bare_else_with_no_when_evaluated(tne_skill):
    # B4: a severity ladder with no `when` at all never consults a threshold,
    # so it is 'fixed' -- analyst-set by definition -- REGARDLESS of a policy
    # threshold the finding's trigger happens to reference. Trigger thresholds
    # must never leak into the severity_basis/analyst_set_severity decision.
    skill = _fake_skill(
        [
            {
                "id": "F_BARE_ELSE",
                "test_id": "T_TEST",
                "title": "Bare else",
                "trigger": "m2 > thresholds.policy_thr",
                "severity": [{"else": "Medium"}],
                "metrics_cited": ["m2"],
                "monetary_basis": "none",
                "observation": "{m2}",
            }
        ]
    )
    metrics = {"m2": {"value": 20, "unit": "count", "source_ref": {}}}
    findings = build_findings(skill, run_id="r1", metrics=metrics)
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "Medium"
    assert f["severity_basis"] == "fixed"
    # analyst-set by definition, even though the trigger DID consult a
    # policy-provenance threshold -- that consultation is the trigger's, not
    # the severity ladder's, and threshold_refs (trigger ∪ severity) still
    # carries it for display.
    assert f["analyst_set_severity"] is True
    assert [r["id"] for r in f["threshold_refs"]] == ["policy_thr"]


def test_severity_basis_threshold_via_else_reached_after_a_false_when(tne_skill):
    # B4: an `else` reached only after a `when` evaluated false still counted
    # that `when`'s threshold as consulted -- severity_basis is 'threshold',
    # and since the sole consulted threshold is policy-provenance,
    # analyst_set_severity is False.
    skill = _fake_skill(
        [
            {
                "id": "F_ELSE_CONSULTED",
                "test_id": "T_TEST",
                "title": "Else after a false when",
                "trigger": "m1 > 0",
                "severity": [
                    {"when": "m1 > thresholds.policy_thr", "then": "High"},
                    {"else": "Low"},
                ],
                "metrics_cited": ["m1"],
                "monetary_basis": "none",
                "observation": "{m1}",
            }
        ]
    )
    metrics = {"m1": {"value": 1, "unit": "count", "source_ref": {}}}  # below policy_thr (10) -> else
    findings = build_findings(skill, run_id="r1", metrics=metrics)
    f = findings[0]
    assert f["severity"] == "Low"
    assert f["severity_basis"] == "threshold"
    assert [r["id"] for r in f["threshold_refs"]] == ["policy_thr"]
    assert f["analyst_set_severity"] is False


def test_severity_basis_threshold_analyst_set_when_matched(tne_skill):
    # An analyst-set threshold consulted by the rule that actually matched
    # marks the severity analyst-set, same as the matched-rule case already
    # covered by test_findings_with_analyst_set_matched_severity_are_labelled,
    # but pinned here against the fake skill for a minimal repro.
    skill = _fake_skill(
        [
            {
                "id": "F_ANALYST_MATCHED",
                "test_id": "T_TEST",
                "title": "Analyst-set matched",
                "trigger": "m3 > 0",
                "severity": [
                    {"when": "m3 > thresholds.analyst_thr", "then": "High"},
                    {"else": "Low"},
                ],
                "metrics_cited": ["m3"],
                "monetary_basis": "none",
                "observation": "{m3}",
            }
        ]
    )
    metrics = {"m3": {"value": 99, "unit": "count", "source_ref": {}}}  # above analyst_thr (5) -> High
    findings = build_findings(skill, run_id="r1", metrics=metrics)
    f = findings[0]
    assert f["severity"] == "High"
    assert f["severity_basis"] == "threshold"
    assert f["analyst_set_severity"] is True
