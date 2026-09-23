from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import jsonschema
import pandas as pd

from orchestrator.contract import validate_contract
from orchestrator.findings import build_findings
from orchestrator.populations import PopulationContext, PopulationResult, build_populations
from orchestrator.primitives import PRIMITIVES, PrimitiveContext, PrimitiveParamsError, run_primitive
from orchestrator.skills import Skill

_FLAG_COLUMNS = ["__source", "__row_key", "flag", "group_id"]


def _run_metric_source_ref(pops: list[PopulationResult], **extra: Any) -> dict:
    seen: set[tuple[str, str]] = set()
    sources = []
    for p in pops:
        key = (p.source, p.source_version)
        if key in seen:
            continue
        seen.add(key)
        sources.append({"name": p.source, "version": p.source_version})
    return {"sources": sources, "populations": [p.name for p in pops], **extra}


def _months_in_period(audit_period: tuple[str, str]) -> int:
    start = date.fromisoformat(audit_period[0])
    end = date.fromisoformat(audit_period[1])
    if end < start:
        raise ValueError(f"run_metrics months_in_period: audit_period end {end} precedes start {start}")
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _population_union(pops: list[PopulationResult]) -> tuple[int, float | None, str | None]:
    frames = [p.df for p in pops if len(p.df)]
    amount_col = next((p.amount_column for p in pops if p.amount_column), None)
    if not frames:
        return 0, (0.0 if amount_col else None), amount_col
    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset="__row_key")
    rows = int(len(combined))
    amount = float(combined[amount_col].sum()) if amount_col and amount_col in combined.columns else None
    return rows, amount, amount_col


def _evaluate_run_metrics(
    spec: dict[str, dict], populations: dict[str, PopulationResult], audit_period: tuple[str, str]
) -> dict[str, dict]:
    """Run-level reporting metrics declared in plan.yaml's `run_metrics:`
    (CLAUDE.md build brief P3 §2, N6) -- total_records, total_files,
    months_covered, claims_prepared/approved/combined and similar for other
    Skills. Domain-agnostic by design: the engine only knows five generic
    aggregation kinds over already-built populations; which populations feed
    which run metric, and what each one is called, is entirely the Skill's
    plan.yaml, never a name this module hardcodes."""
    metrics: dict[str, dict] = {}
    for name, cfg in spec.items():
        kind = cfg["kind"]
        if kind == "sum_rows":
            pops = [populations[p] for p in cfg["populations"]]
            metrics[name] = {
                "value": sum(p.rows for p in pops),
                "unit": cfg.get("unit", "count"),
                "source_ref": _run_metric_source_ref(pops, aggregation="sum of row counts"),
            }
        elif kind == "count_populations":
            pops = [populations[p] for p in cfg["populations"]]
            metrics[name] = {
                "value": len(pops),
                "unit": cfg.get("unit", "count"),
                "source_ref": _run_metric_source_ref(pops, aggregation="population count"),
            }
        elif kind == "months_in_period":
            metrics[name] = {
                "value": _months_in_period(audit_period),
                "unit": cfg.get("unit", "count"),
                "source_ref": {"basis": "distinct calendar months spanned by audit_period, inclusive"},
            }
        elif kind == "population":
            pop = populations[cfg["population"]]
            metrics[f"{name}_rows"] = {
                "value": pop.rows, "unit": "count",
                "source_ref": _run_metric_source_ref([pop], aggregation="row count"),
            }
            metrics[f"{name}_amount"] = {
                "value": pop.amount, "unit": cfg.get("unit", "AUD"),
                "source_ref": _run_metric_source_ref([pop], aggregation=f"sum({pop.amount_column})"),
            }
        elif kind == "population_union":
            pops = [populations[p] for p in cfg["populations"]]
            rows, amount, amount_col = _population_union(pops)
            metrics[f"{name}_rows"] = {
                "value": rows, "unit": "count",
                "source_ref": _run_metric_source_ref(pops, aggregation="distinct __row_key union"),
            }
            metrics[f"{name}_amount"] = {
                "value": amount, "unit": cfg.get("unit", "AUD"),
                "source_ref": _run_metric_source_ref(
                    pops, aggregation=f"sum({amount_col}) over distinct __row_key union"
                ),
            }
        else:  # pragma: no cover - the plan.yaml schema already restricts `kind`
            raise ValueError(f"run_metrics {name!r}: unknown kind {kind!r}")
    return metrics


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
    # Long-format flags (__source, __row_key, flag, group_id) -- one row per
    # exception instance, group_id intact -- exactly what each primitive
    # returned before `flags` pivoted it wide for the RF_* column shape
    # ExecutionResult.flags/get_run_frames need. The `execute` node persists
    # flagged_rows from THIS, never by un-pivoting `flags` back to long
    # (which cannot recover group_id -- the pivot never carried it).
    flags_long: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=_FLAG_COLUMNS)
    )
    # The two ingredients orchestrator.frames.build_row_snapshots needs to build
    # the `execute` node's per-run row snapshot (CLAUDE.md build brief P4 perf
    # fix) without re-reading source data: raw_frames is each contract source's
    # own validated/typed dataframe (source -> df, never mutated by population
    # building -- build_population() copies before deriving/filtering, so this
    # is exactly what validate_contract produced), and population_objects is
    # EVERY built population (not just reconciliation_summary()) so a Skill's
    # tested-population row keys and frame_tags source populations can both be
    # read back without rebuilding them.
    raw_frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    population_objects: dict[str, "PopulationResult"] = field(default_factory=dict)


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
    pinned_versions: dict[str, str] | None = None,
) -> ExecutionResult:
    """The `execute` node's logic (CLAUDE.md §4.2): resolves every source version
    first, reads with that pinned version (TOCTOU ordering, §4.1), validates every
    source's contract (a ContractViolation here propagates -- the caller fails the
    run, NN14/G7), runs every test in plan order, then builds findings.
    `execute` never calls an LLM (NN2).

    `pinned_versions`, when given, is used INSTEAD of calling
    `data_source.resolve_version()` here -- the version each contract source was
    already pinned to at run creation (`state.data_assets`), so a source that
    changes between `start_audit_run` and this node running does not change what
    gets read (CLAUDE.md §4.1 TOCTOU ordering, closing the exact gap this
    function's docstring used to name as a known one). When absent, the
    resolve-first behaviour is unchanged -- existing callers (Surface 2, the
    engine's own tests) that never pinned a version keep working."""
    contract_sources = skill.contract.get("sources", {})

    if pinned_versions is not None:
        missing = sorted(set(contract_sources) - set(pinned_versions))
        if missing:
            raise ValueError(f"pinned_versions is missing contract source(s): {missing}")
        source_versions: dict[str, str] = {name: pinned_versions[name] for name in contract_sources}
    else:
        source_versions = {name: data_source.resolve_version(name) for name in contract_sources}
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

    metrics: dict[str, dict] = _evaluate_run_metrics(
        skill.plan.get("run_metrics", {}), populations, audit_period
    )
    test_results: list[dict] = []
    scored_units: dict[str, list[str]] = {}
    long_flag_frames: list[pd.DataFrame] = []
    not_testable_flag_columns: set[str] = set()

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
            # N11: a not_testable test's RF_* column(s) (declared in plan.yaml,
            # never inferred) still exist in the flags frame, filled with a
            # true null -- never 0 -- so a downstream renderer can tell "not
            # tested" apart from "tested and no breach".
            not_testable_flag_columns.update(test["not_testable"].get("flags", []))
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
    for col in sorted(not_testable_flag_columns):
        if col not in flags.columns:
            flags[col] = pd.array([pd.NA] * len(flags), dtype="Int8")

    non_empty_long_frames = [f for f in long_flag_frames if len(f)]
    flags_long = (
        pd.concat(non_empty_long_frames, ignore_index=True)
        if non_empty_long_frames
        else pd.DataFrame(columns=_FLAG_COLUMNS)
    )

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
        flags_long=flags_long,
        raw_frames={name: raw_sources[name]["df"] for name in raw_sources},
        population_objects=populations,
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
