"""Converts a parsed Explorer wire `PlanProposal` (orchestrator.explorer.
wire_schema.PLAN_PROPOSAL_SCHEMA) into the CANONICAL shape the rest of the
pipeline speaks -- the same severity-ladder/metrics-map/filter shapes
`orchestrator.skills`/`orchestrator.populations`/`orchestrator.expr` already
use for a repo Skill's `plan.yaml`/`findings.yaml` (CLAUDE.md §4.5, §4.6;
docs/specs/P6_P8_explorer_llm_design.md §4.6).

`to_canonical` is pure and deterministic: no profile lookup, no I/O, no
raising. It never resolves the `unit: "currency"` placeholder -- that is
`orchestrator.explorer.currency.resolve_currency_unit`, called by the
VALIDATOR (V-C1) and by `materialise.py`, deliberately kept out of this
"structural conversion only" step so the two concerns (shape vs. profile-
dependent meaning) cannot leak into each other.

A structurally malformed severity ladder (the wire schema's own
`jsonschema` pass cannot express "the null-when rule is last, and only
last" -- that is a cross-item constraint) is still converted best-effort
here -- every rule becomes SOME canonical rule, so downstream code never
sees a missing key -- and the violation is recorded in the returned dict's
`_canonicalization_errors` list (never part of the real proposal shape; a
caller that never checks it just ignores an always-optional key). The
validator folds these into `proposal_errors` verbatim (§4.7 V-S1's
'jsonschema against the wire schema' catches most malformed input before
this ever runs; this is the one cross-field rule that schema validation
structurally cannot express)."""

from __future__ import annotations

from typing import Any


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _canonical_condition(cond: dict) -> dict:
    return _drop_none({"column": cond.get("column"), "op": cond.get("op"), "value": cond.get("value")})


def _canonical_filter(flt: dict) -> dict:
    between = flt.get("between_audit_period")
    if between is not None:
        return {"column": between["column"], "op": "between", "value": {"ref": "audit_period"}}
    any_of = flt.get("any_of")
    if any_of is not None:
        return {"any_of": [_canonical_filter(f) for f in any_of]}
    all_of = flt.get("all_of")
    if all_of is not None:
        return {"all_of": [_canonical_filter(f) for f in all_of]}
    return _canonical_condition(flt)


def _canonical_source(s: dict) -> dict:
    return _drop_none({
        "source": s["source"],
        "amount_column": s.get("amount_column"),
        "date_column": s.get("date_column"),
        "entry_key": list(s["entry_key"]) if s.get("entry_key") is not None else None,
    })


def _canonical_population(p: dict) -> dict:
    return {
        "key": p["key"],
        "source": p["source"],
        "description": p["description"],
        "filters": [_canonical_filter(f) for f in (p.get("filters") or [])],
    }


def _canonical_metrics(metrics: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in metrics or []:
        entry = _drop_none({
            "kind": m["kind"],
            "column": m.get("column"),
            "key": m.get("key"),
            "unit": m["unit"],
            "where": _canonical_condition(m["where"]) if m.get("where") is not None else None,
        })
        out[m["name"]] = entry
    return out


def _canonical_params(params: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key == "kind" or value is None:
            continue
        if key == "metrics":
            out["metrics"] = _canonical_metrics(value)
        elif key == "exclude":
            out["exclude"] = _canonical_condition(value)
        else:
            out[key] = value
    return out


def _canonical_test(t: dict) -> dict:
    return {
        "key": t["key"],
        "name": t["name"],
        "primitive": t["primitive"],
        "params": _canonical_params(t.get("params") or {}),
        "control_key": t["control_key"],
        "risk_key": t["risk_key"],
        "assertion": t["assertion"],
        "control_objective": t["control_objective"],
        "risk_hypothesis": t["risk_hypothesis"],
        "rationale": t["rationale"],
    }


def _canonical_severity(rules: list[dict]) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    out: list[dict] = []
    if not rules:
        return out, ["severity must have at least one rule"]
    n = len(rules)
    for i, rule in enumerate(rules):
        when = rule.get("when")
        then = rule["then"]
        is_last = i == n - 1
        if when is None:
            if not is_last:
                errors.append(f"severity[{i}] has when: null but is not the last rule")
            out.append({"else": then})
        else:
            if is_last:
                errors.append("severity's last rule must have when: null (the default)")
            out.append({"when": when, "then": then})
    return out, errors


def _canonical_finding(f: dict) -> tuple[dict, list[str]]:
    severity, errors = _canonical_severity(f.get("severity") or [])
    out = {
        "key": f["key"],
        "test_key": f["test_key"],
        "title": f["title"],
        "trigger": f["trigger"],
        "severity": severity,
        "metrics_cited": list(f.get("metrics_cited") or []),
        "thresholds_cited": list(f.get("thresholds_cited") or []),
        "monetary_basis": f["monetary_basis"],
        "observation": f["observation"],
        "recommendation": f["recommendation"],
        "management_questions": list(f.get("management_questions") or []),
    }
    return out, [f"findings.{f.get('key', '?')}: {e}" for e in errors]


def to_canonical(wire: dict) -> dict:
    errors: list[str] = []

    out: dict[str, Any] = {
        "schema_version": wire.get("schema_version"),
        "skill_name": wire.get("skill_name"),
        "domain": wire.get("domain"),
        "summary": wire.get("summary"),
        "sources": [_canonical_source(s) for s in (wire.get("sources") or [])],
        "populations": [_canonical_population(p) for p in (wire.get("populations") or [])],
        "risks": [dict(r) for r in (wire.get("risks") or [])],
        "controls": [dict(c) for c in (wire.get("controls") or [])],
        "thresholds": [dict(th) for th in (wire.get("thresholds") or [])],
        "tests": [_canonical_test(t) for t in (wire.get("tests") or [])],
        "data_gaps": [dict(g) for g in (wire.get("data_gaps") or [])],
        "assumptions": list(wire.get("assumptions") or []),
    }

    findings_out = []
    for f in wire.get("findings") or []:
        cf, ferrs = _canonical_finding(f)
        findings_out.append(cf)
        errors.extend(ferrs)
    out["findings"] = findings_out

    if errors:
        out["_canonicalization_errors"] = errors
    return out
