from __future__ import annotations

from typing import Any

from orchestrator.expr import compile_expr, evaluate, evaluate_ternary
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


def _format_template(
    template: str, metrics_cited: dict[str, dict], threshold_kwargs: dict[str, str] | None = None
) -> str:
    kwargs = {name: format_metric_value(m["value"], m["unit"]) for name, m in metrics_cited.items()}
    kwargs.update(threshold_kwargs or {})
    return template.format(**kwargs)


def _select_severity(
    severity_rules: list[dict], metric_values: dict[str, Any], threshold_values: dict[str, Any]
) -> tuple[str, str, set[str], str | None]:
    """Walks the severity ladder, returning the rule that fires plus every
    threshold CONSULTED getting there (B4): every `when` evaluated -- including
    the ones that came back false before the rule that matched -- not just the
    threshold(s) named by the winning rule. A bare `else` with nothing tested
    before it consults none, which is exactly what marks it 'fixed' severity.

    Item 4 (CLAUDE.md NN14, P2/P3 gate review): a `when` whose metric(s) are
    missing is Kleene-UNKNOWN, not False (`evaluate_ternary`, never the
    trigger's own collapsing `evaluate()`) -- it must not silently fall
    through to a lower rung as if the condition had genuinely failed. That
    would let a rule intended to catch a High-severity case instead render
    Medium/Low with no sign anything was wrong. Returns ("Indeterminate",
    when_text, consulted_so_far, reason) instead, where `reason` names the
    missing metric(s); every other path returns reason=None."""
    consulted: set[str] = set()
    for rule in severity_rules:
        if "when" in rule:
            compiled = compile_expr(rule["when"])
            consulted |= set(compiled.threshold_ids)
            result = evaluate_ternary(compiled, metric_values, threshold_values)
            if result is None:
                missing = sorted(n for n in compiled.metric_names if metric_values.get(n) is None)
                reason = (
                    f"severity rule {rule['when']!r} could not be evaluated: "
                    f"metric(s) {missing} unavailable"
                )
                return "Indeterminate", reason, consulted, reason
            if result:
                return rule["then"], rule["when"], consulted, None
        elif "else" in rule:
            return rule["else"], "else", consulted, None
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

        severity, severity_rule_text, severity_threshold_ids, indeterminate_reason = _select_severity(
            rule["severity"], metric_values, threshold_values
        )

        metrics_cited = {name: metrics[name] for name in rule.get("metrics_cited", []) if name in metrics}

        # thresholds_cited (item 7, CLAUDE.md P2/P3 gate review): a finding's
        # prose may quote a threshold's own value (e.g. "$50 without
        # supporting documentation") -- rendered from thresholds.yaml, never
        # a bare number written into the template text, so the figure can
        # never silently drift from what the run actually enforces.
        thresholds_cited_ids = list(rule.get("thresholds_cited", []))
        threshold_kwargs = {
            tid: format_metric_value(thresholds[tid]["value"], thresholds[tid]["unit"])
            for tid in thresholds_cited_ids
        }

        ref_ids = sorted(set(trigger.threshold_ids) | severity_threshold_ids | set(thresholds_cited_ids))
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
        # B4: severity_basis records whether the ladder consulted any threshold
        # at all to reach its answer -- a bare `else` (or a `when` chain that
        # never referenced `thresholds.*`) is 'fixed': the severity did not
        # come from a number, so it is analyst-set BY DEFINITION, not by
        # provenance lookup. Where thresholds WERE consulted, analyst_set is
        # true unless every one of them carries provenance 'policy' -- one
        # analyst-set threshold anywhere in the path that was walked is enough
        # to mark the whole severity call analyst-set.
        if indeterminate_reason is not None:
            # Item 4: a distinct severity_basis enum value (DDL CHECK
            # constraint: 'fixed' | 'threshold' | 'indeterminate', migration
            # 005) -- the actual explanation of WHICH metric was missing is
            # in severity_rule (unconstrained text, already `reason` here).
            # An Indeterminate severity is never counted as Low, never
            # attributed to an analyst-set threshold it never actually
            # consulted.
            severity_basis = "indeterminate"
            analyst_set_severity = False
        else:
            severity_basis = "threshold" if severity_threshold_ids else "fixed"
            if severity_basis == "fixed":
                analyst_set_severity = True
            else:
                consulted_refs = [r for r in threshold_refs if r["id"] in severity_threshold_ids]
                analyst_set_severity = not all(r["provenance_type"] == "policy" for r in consulted_refs)

        test = test_lookup.get(rule["test_id"], {})

        findings.append(
            {
                "finding_id": f"{run_id}:{rule['id']}",
                "rule_id": f"{skill.skill_id}.{rule['id']}",
                "test_id": rule["test_id"],
                "title": rule["title"],
                "severity": severity,
                "severity_rule": severity_rule_text,
                "severity_basis": severity_basis,
                "threshold_refs": threshold_refs,
                "analyst_set_severity": analyst_set_severity,
                "metrics_cited": metrics_cited,
                "observation": _format_template(rule["observation"], metrics_cited, threshold_kwargs),
                "recommendation": _format_template(rule.get("recommendation", ""), metrics_cited, threshold_kwargs),
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
