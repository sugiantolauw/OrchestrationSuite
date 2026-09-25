from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.authoring.checks import check_g8_thresholds
from orchestrator.findings import build_findings
from orchestrator.skills import load_skill

SKILL_DIR = Path(__file__).parent.parent / "skills" / "tne_exco"


@pytest.fixture(scope="module")
def tne_skill():
    skill = load_skill(SKILL_DIR)
    skill.validate()
    return skill


def test_g8_threshold_provenance(tne_skill):
    """CLAUDE.md §5 G8: catalogue thresholds == thresholds.yaml == code;
    every threshold carries a policy reference or is analyst-set and
    pending_policy_confirmation; every analyst-set threshold that drives a
    matched severity is labelled analyst_set_severity. Moved into
    `orchestrator.authoring.checks.check_g8_thresholds`
    (docs/specs/P7_mapping_authoring_design.md §2.1, §2.2 "Moved into
    orchestrator/") so any Skill's authoring-kit validator run gets the
    same check, not only SKILL-001's -- this test keeps the assertion,
    against real SKILL-001 data, that the moved function still passes."""
    violations = check_g8_thresholds(tne_skill)
    assert not violations, violations


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
