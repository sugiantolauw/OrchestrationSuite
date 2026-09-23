from __future__ import annotations

from pathlib import Path

from orchestrator.findings import build_findings, format_metric_value
from orchestrator.skills import Skill

MANIFEST = {"id": "SKILL-T", "name": "Test", "domain": "test", "version": "0.1.0", "owner": "test", "status": "draft"}
CONTRACT = {"timezone": "Australia/Sydney", "sources": {}}
PLAN = {
    "populations": {},
    "tests": [
        {
            "test_id": "T1",
            "control_id": "CTL-1",
            "risk_id": "RSK-1",
            "assertion": "operating",
        }
    ],
}
THRESHOLDS = {
    "high_threshold": {
        "value": 10,
        "unit": "count",
        "description": "high severity threshold",
        "used_by": ["T1"],
        "effective_date": "2025-01-01",
        "provenance": {"type": "analyst-set", "pending_policy_confirmation": True},
    },
    "policy_threshold": {
        "value": 5,
        "unit": "count",
        "description": "a threshold with a real policy source",
        "used_by": ["T1"],
        "effective_date": "2025-01-01",
        "provenance": {"type": "policy", "reference": "T&E Policy §4.2", "pending_policy_confirmation": False},
    },
}


def _findings_yaml(**overrides) -> dict:
    finding = {
        "id": "T1",
        "test_id": "T1",
        "title": "High Value Claims",
        "trigger": "hv_count > 0",
        "severity": [
            {"when": "hv_count > thresholds.high_threshold", "then": "High"},
            {"when": "hv_count > thresholds.policy_threshold", "then": "Medium"},
            {"else": "Low"},
        ],
        "metrics_cited": ["hv_count", "hv_amount"],
        "observation": "{hv_count} claims totalling {hv_amount}.",
        "recommendation": "Review these claims.",
        "management_questions": ["Why so many?"],
    }
    finding.update(overrides)
    return {"findings": [finding]}


def _skill(findings=None) -> Skill:
    return Skill(
        skill_dir=Path("."),
        manifest=MANIFEST,
        contract=CONTRACT,
        plan=PLAN,
        findings=findings or _findings_yaml(),
        thresholds=THRESHOLDS,
    )


def _metric(value, unit, source="claims"):
    return {"value": value, "unit": unit, "source_ref": {"sources": [{"name": source, "version": "v1"}], "columns": [], "grain": "row", "population": "pop"}}


def test_not_triggered_when_metric_zero():
    skill = _skill()
    metrics = {"hv_count": _metric(0, "count"), "hv_amount": _metric(0.0, "AUD")}
    findings = build_findings(skill, run_id="run-1", metrics=metrics)
    assert findings == []


def test_not_testable_metric_missing_means_trigger_false():
    skill = _skill()
    # T4.3-shaped scenario: the metric this finding's trigger needs was never
    # produced because the test is not_testable -- the finding must not fire.
    findings = build_findings(skill, run_id="run-1", metrics={})
    assert findings == []


def test_severity_selects_high_and_reports_threshold_refs():
    skill = _skill()
    metrics = {"hv_count": _metric(15, "count"), "hv_amount": _metric(1234.5, "AUD")}
    findings = build_findings(skill, run_id="run-1", metrics=metrics)
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "High"
    assert f["severity_rule"] == "hv_count > thresholds.high_threshold"
    threshold_ids = {r["id"] for r in f["threshold_refs"]}
    assert threshold_ids == {"high_threshold"}
    assert f["analyst_set_severity"] is True


def test_severity_selects_medium_with_policy_threshold_not_analyst_set():
    skill = _skill()
    metrics = {"hv_count": _metric(7, "count"), "hv_amount": _metric(500.0, "AUD")}
    findings = build_findings(skill, run_id="run-1", metrics=metrics)
    f = findings[0]
    assert f["severity"] == "Medium"
    threshold_ids = {r["id"] for r in f["threshold_refs"]}
    assert threshold_ids == {"policy_threshold"}
    assert f["analyst_set_severity"] is False


def test_severity_falls_to_else_low():
    skill = _skill()
    metrics = {"hv_count": _metric(1, "count"), "hv_amount": _metric(50.0, "AUD")}
    findings = build_findings(skill, run_id="run-1", metrics=metrics)
    f = findings[0]
    assert f["severity"] == "Low"
    assert f["severity_rule"] == "else"
    assert f["threshold_refs"] == []
    assert f["analyst_set_severity"] is False


def test_template_formatting_by_unit():
    assert format_metric_value(1234, "count") == "1,234"
    assert format_metric_value(1234.5, "AUD") == "$1,234.50"
    assert format_metric_value(12.3, "%") == "12.3%"
    assert format_metric_value(None, "count") == "n/a"


def test_observation_and_recommendation_use_formatted_values():
    skill = _skill()
    metrics = {"hv_count": _metric(3, "count"), "hv_amount": _metric(1500.5, "AUD")}
    findings = build_findings(skill, run_id="run-42", metrics=metrics)
    f = findings[0]
    assert f["observation"] == "3 claims totalling $1,500.50."
    assert f["recommendation"] == "Review these claims."
    assert f["management_questions"] == ["Why so many?"]


def test_finding_and_rule_ids_and_control_risk_assertion_inherited():
    skill = _skill()
    metrics = {"hv_count": _metric(1, "count"), "hv_amount": _metric(1.0, "AUD")}
    findings = build_findings(skill, run_id="run-42", metrics=metrics)
    f = findings[0]
    assert f["finding_id"] == "run-42:T1"
    assert f["rule_id"] == "SKILL-T.T1"
    assert f["control_id"] == "CTL-1"
    assert f["risk_id"] == "RSK-1"
    assert f["assertion"] == "operating"
    assert f["exposure_amount"] is None
    assert f["exposure_basis"] == "pending P3 de-duplicated exposure"
    assert f["review_state"] == "draft"


def test_findings_ordered_by_findings_yaml_order():
    findings_yaml = {
        "findings": [
            {
                "id": "A",
                "test_id": "T1",
                "title": "A",
                "trigger": "hv_count > 0",
                "severity": [{"else": "Low"}],
                "metrics_cited": ["hv_count"],
                "observation": "{hv_count}",
                "recommendation": "x",
            },
            {
                "id": "B",
                "test_id": "T1",
                "title": "B",
                "trigger": "hv_count > 0",
                "severity": [{"else": "Low"}],
                "metrics_cited": ["hv_count"],
                "observation": "{hv_count}",
                "recommendation": "x",
            },
        ]
    }
    skill = _skill(findings=findings_yaml)
    metrics = {"hv_count": _metric(1, "count")}
    findings = build_findings(skill, run_id="r", metrics=metrics)
    assert [f["rule_id"] for f in findings] == ["SKILL-T.A", "SKILL-T.B"]
