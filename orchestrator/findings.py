from __future__ import annotations

from typing import Any

from orchestrator.expr import compile_expr, evaluate
from orchestrator.skills import Skill


def format_metric_value(value: Any, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "count":
        return f"{int(round(value)):,}"
    if unit == "AUD":
        return f"${value:,.2f}"
    if unit == "%":
        return f"{value}%"
    return str(value)


def _format_template(template: str, metrics_cited: dict[str, dict]) -> str:
    kwargs = {name: format_metric_value(m["value"], m["unit"]) for name, m in metrics_cited.items()}
    return template.format(**kwargs)


def _select_severity(
    severity_rules: list[dict], metric_values: dict[str, Any], threshold_values: dict[str, Any]
) -> tuple[str, str, set[str]]:
    for rule in severity_rules:
        if "when" in rule:
            compiled = compile_expr(rule["when"])
            if evaluate(compiled, metric_values, threshold_values):
                return rule["then"], rule["when"], set(compiled.threshold_ids)
        elif "else" in rule:
            return rule["else"], "else", set()
    raise ValueError("no severity rule matched, and no 'else' entry was present")


def build_findings(
    skill: Skill,
    *,
    run_id: str,
    metrics: dict[str, dict],
) -> list[dict]:
    """Builds this run's findings from skill.findings.yaml + this run's metrics
    (CLAUDE.md §4.6). Never invents a finding, a number or a severity -- every
    value here traces to `metrics` (produced by orchestrator.engine, which is in
    turn produced by primitives evaluating this run's data) or to
    skill.thresholds (fixed at Skill-authoring time)."""
    thresholds = skill.thresholds
    threshold_values = {tid: spec["value"] for tid, spec in thresholds.items()}
    metric_values = {name: m["value"] for name, m in metrics.items()}
    test_lookup = {t["test_id"]: t for t in skill.plan.get("tests", [])}

    findings: list[dict] = []
    for rule in skill.findings.get("findings", []):
        trigger = compile_expr(rule["trigger"])
        if not evaluate(trigger, metric_values, threshold_values):
            continue

        severity, severity_rule_text, severity_threshold_ids = _select_severity(
            rule["severity"], metric_values, threshold_values
        )

        metrics_cited = {name: metrics[name] for name in rule.get("metrics_cited", []) if name in metrics}

        ref_ids = sorted(set(trigger.threshold_ids) | severity_threshold_ids)
        threshold_refs = []
        for tid in ref_ids:
            spec = thresholds[tid]
            provenance = spec.get("provenance", {})
            threshold_refs.append(
                {
                    "id": tid,
                    "value": spec.get("value"),
                    "unit": spec.get("unit"),
                    "provenance_type": provenance.get("type"),
                    "pending_policy_confirmation": bool(provenance.get("pending_policy_confirmation", False)),
                }
            )
        analyst_set_severity = any(
            r["id"] in severity_threshold_ids and r["provenance_type"] == "analyst-set"
            for r in threshold_refs
        )

        test = test_lookup.get(rule["test_id"], {})

        findings.append(
            {
                "finding_id": f"{run_id}:{rule['id']}",
                "rule_id": f"{skill.skill_id}.{rule['id']}",
                "test_id": rule["test_id"],
                "title": rule["title"],
                "severity": severity,
                "severity_rule": severity_rule_text,
                "threshold_refs": threshold_refs,
                "analyst_set_severity": analyst_set_severity,
                "metrics_cited": metrics_cited,
                "observation": _format_template(rule["observation"], metrics_cited),
                "recommendation": _format_template(rule.get("recommendation", ""), metrics_cited),
                "management_questions": list(rule.get("management_questions", [])),
                "control_id": test.get("control_id"),
                "risk_id": test.get("risk_id"),
                "assertion": test.get("assertion"),
                "exposure_amount": None,
                "exposure_basis": "pending P3 de-duplicated exposure",
                "review_state": "draft",
            }
        )

    return findings
