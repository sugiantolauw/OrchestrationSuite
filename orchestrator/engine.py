from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import jsonschema
import pandas as pd

from orchestrator.contract import validate_contract
from orchestrator.findings import build_findings
from orchestrator.populations import PopulationContext, build_populations
from orchestrator.primitives import PRIMITIVES, PrimitiveContext, PrimitiveParamsError, run_primitive
from orchestrator.skills import Skill

_FLAG_COLUMNS = ["__source", "__row_key", "flag", "group_id"]


@dataclass
class ExecutionResult:
    source_versions: dict[str, str]
    populations: dict[str, dict]
    data_quality: dict[str, Any]
    test_results: list[dict]
    metrics: dict[str, dict]
    flags: pd.DataFrame
    scored_units: dict[str, list[str]]
    findings: list[dict] = field(default_factory=list)


def _run_test_primitive(skill: Skill, name: str, ctx: PrimitiveContext, params: dict):
    if name in PRIMITIVES:
        return run_primitive(name, ctx, params)
    entry = skill.custom_primitives.get(name)
    if entry is None:
        raise PrimitiveParamsError(
            f"unknown primitive: {name!r} (not in the core registry and not in "
            f"{skill.skill_id}'s custom.py CUSTOM_PRIMITIVES)"
        )
    try:
        jsonschema.validate(params, entry["params_schema"])
    except jsonschema.ValidationError as exc:
        raise PrimitiveParamsError(f"{name}: invalid params: {exc.message}") from exc
    return entry["run"](ctx, params)


def _wide_flags(long_frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [f for f in long_frames if len(f)]
    if not frames:
        return pd.DataFrame(columns=["__source", "__row_key"])
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.assign(__present=1)
    wide = combined.pivot_table(
        index=["__source", "__row_key"],
        columns="flag",
        values="__present",
        aggfunc="max",
        fill_value=0,
    )
    wide.columns = [str(c) for c in wide.columns]
    return wide.reset_index()


def execute_skill(
    skill: Skill,
    *,
    data_source,
    audit_period: tuple[str, str],
    run_context: dict,
) -> ExecutionResult:
    """The `execute` node's logic (CLAUDE.md §4.2): resolves every source version
    first, reads with that pinned version (TOCTOU ordering, §4.1), validates every
    source's contract (a ContractViolation here propagates -- the caller fails the
    run, NN14/G7), runs every test in plan order, then builds findings.
    `execute` never calls an LLM (NN2)."""
    contract_sources = skill.contract.get("sources", {})

    source_versions: dict[str, str] = {
        name: data_source.resolve_version(name) for name in contract_sources
    }
    raw_sources: dict[str, dict] = {}
    for name in contract_sources:
        df = data_source.read_population(name, version=source_versions[name])
        validate_contract(df, contract_sources[name])
        raw_sources[name] = {"df": df, "version": source_versions[name]}

    pop_ctx = PopulationContext(
        sources=raw_sources,
        references=skill.references,
        audit_period=audit_period,
        thresholds=skill.thresholds,
        custom_derivations=skill.custom_derivations,
    )
    populations = build_populations(skill.plan.get("populations", {}), pop_ctx)

    prim_ctx = PrimitiveContext(
        populations=populations,
        thresholds=skill.thresholds,
        references=skill.references,
    )

    metrics: dict[str, dict] = {}
    test_results: list[dict] = []
    scored_units: dict[str, list[str]] = {}
    long_flag_frames: list[pd.DataFrame] = []

    for test in skill.plan.get("tests", []):
        test_id = test["test_id"]

        if "not_testable" in test:
            test_results.append(
                {
                    "test_id": test_id,
                    "status": "not_testable",
                    "reason": test["not_testable"]["reason"],
                    "metric_names": [],
                    "exception_units": 0,
                }
            )
            continue

        result = _run_test_primitive(skill, test["primitive"], prim_ctx, test["params"])

        for name, metric in result.metrics.items():
            if name in metrics:
                raise ValueError(
                    f"duplicate metric name across tests: {name!r} (re-emitted by {test_id})"
                )
            metrics[name] = metric

        scored_units[test_id] = result.scored_units
        long_flag_frames.append(result.flags)

        test_results.append(
            {
                "test_id": test_id,
                "status": "exception" if result.scored_units else "pass",
                "reason": None,
                "metric_names": list(result.metrics.keys()),
                "exception_units": len(result.scored_units),
            }
        )

    flags = _wide_flags(long_flag_frames)

    data_quality: dict[str, Any] = {}
    for name, pop in populations.items():
        for k, v in pop.excluded_counts.items():
            data_quality[f"{name}.excluded.{k}"] = v
        for k, v in pop.derivation_counters.items():
            data_quality[f"{name}.{k}"] = v

    findings = build_findings(skill, run_id=run_context["run_id"], metrics=metrics)

    return ExecutionResult(
        source_versions=source_versions,
        populations={name: pop.reconciliation_summary() for name, pop in populations.items()},
        data_quality=data_quality,
        test_results=test_results,
        metrics=metrics,
        flags=flags,
        scored_units=scored_units,
        findings=findings,
    )


def to_ui_payload(result: ExecutionResult, *, audit_period_label: str) -> dict:
    """Reshapes ExecutionResult into the {metrics: {name: {value, unit, source_file,
    source_ref}}} shape reference_app/app.py:compute_evidence_payload returns, so P4
    can feed the existing UI. Includes only keys this generic, domain-agnostic
    engine can actually compute -- it never invents Skill- or domain-specific keys
    (e.g. app.py's `exco_members`) that only a Skill's own workspace.py knows."""
    metrics_payload: dict[str, dict] = {}
    for name, metric in result.metrics.items():
        sources = metric.get("source_ref", {}).get("sources", [])
        source_file = sources[0]["name"] if sources else None
        metrics_payload[name] = {
            "value": metric["value"],
            "unit": metric["unit"],
            "source_file": source_file,
            "source_ref": metric["source_ref"],
        }
    return {
        "audit_period": audit_period_label,
        "metrics": metrics_payload,
        "populations": result.populations,
        "data_quality": result.data_quality,
        "test_results": result.test_results,
    }
