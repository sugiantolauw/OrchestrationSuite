""""Run inputs" (docs/specs/P7_mapping_authoring_design.md §1.3, independent
review 2026-09-25 item 1): one concept covering column mappings, parameter
overrides and unsupplied sources for a single audit run, declared in the
existing `SOURCE_BINDINGS` file (D-P7-1) and validated here -- before the
run fingerprint is computed, so every problem is a loud, named
ContractViolation rather than a run that starts on a guess (CLAUDE.md NN14).

`resolve_run_inputs` is the sole entry point. It returns a JSON-safe dict
in exactly the shape stored in `RunState.options["run_inputs"]`:

    {
        "mappings": {source: {contract_column: physical_column}},
        "not_supplied": {source: {"reason": str, "affected_tests": [test_id, ...]}},
        "parameters": {
            name: {
                "path": str, "sha256": str, "provenance": {...},
                "kind": str, "value": <parsed contents>,
            }
        },
    }

Absent entirely, or every sub-dict empty, when the environment's
SOURCE_BINDINGS has nothing configured for this Skill beyond ordinary
volume_file/uc_table bindings -- the common case today."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from orchestrator.contract import ContractViolation
from orchestrator.fingerprint import sha256_file
from orchestrator.skills import Skill
from orchestrator.source_bindings import PARAMETERS_KEY


def resolve_run_inputs(
    skill: Skill,
    configured: dict[str, dict],
    data_source: Any,
    *,
    source_versions: dict[str, str] | None = None,
) -> dict:
    """`configured` is this Skill's own slice of SOURCE_BINDINGS
    (`orchestrator.source_bindings.bindings_for_skill`'s result) -- every
    key is either a contract source name or the reserved "parameters" key.
    `data_source` is the same resolve-only DataSourceAdapter start_audit_run
    already built to resolve source versions; used here, read-only, to
    validate a declared mapping's physical column names against the
    source's real header at the pinned version. `source_versions` is
    `{source: version}` for every source configured has already resolved a
    version for (never for a not_supplied one, which has none) -- omit it
    only when no `configured` entry declares a `columns` mapping (nothing
    to validate against real data)."""
    violations: list[str] = []
    contract_sources = skill.contract.get("sources", {})
    source_versions = source_versions or {}

    mappings: dict[str, dict[str, str]] = {}
    not_supplied: dict[str, dict] = {}

    for name, binding in configured.items():
        if name == PARAMETERS_KEY:
            continue
        if name not in contract_sources:
            violations.append(f"run inputs: {name!r} is not a contract source of {skill.skill_id}")
            continue

        if binding.get("kind") == "not_supplied":
            if not contract_sources[name].get("optional"):
                violations.append(
                    f"run inputs: source {name!r} is bound not_supplied but contract.yaml does "
                    f"not declare it optional -- add 'optional: true' to that source first"
                )
                continue
            reason = str(binding.get("reason") or "").strip()
            if not reason:
                violations.append(f"run inputs: source {name!r} not_supplied requires a non-empty reason")
                continue
            affected = sorted(t for t, srcs in skill.test_sources.items() if name in srcs)
            not_supplied[name] = {"reason": reason, "affected_tests": affected}
            continue

        columns = binding.get("columns")
        if not columns:
            continue
        mapped = _resolve_mapping(
            skill, name, columns, data_source, source_versions.get(name), violations,
        )
        if mapped:
            mappings[name] = mapped

    parameters = _resolve_parameters(skill, configured.get(PARAMETERS_KEY) or {}, violations)

    if violations:
        raise ContractViolation(violations)

    return {"mappings": mappings, "not_supplied": not_supplied, "parameters": parameters}


def _resolve_mapping(
    skill: Skill, source: str, columns: dict, data_source: Any, version: str | None,
    violations: list[str],
) -> dict[str, str]:
    if not isinstance(columns, dict):
        violations.append(f"run inputs: {source}.columns must be a mapping of contract column -> physical column")
        return {}
    if version is None:
        violations.append(f"run inputs: {source}: cannot validate a column mapping with no resolved source version")
        return {}
    try:
        df = data_source.read_population(source, version=version)
    except Exception as exc:  # noqa: BLE001 - surfaced as a named ContractViolation
        violations.append(f"run inputs: {source}: could not read the source to validate its column mapping: {exc!r}")
        return {}
    actual_columns = set(df.columns) - {"__source", "__row_key"}
    contract_columns = set(skill.contract["sources"][source].get("columns", {}))

    seen_physical: set[str] = set()
    mapped: dict[str, str] = {}
    for contract_col, physical_col in columns.items():
        if contract_col not in contract_columns:
            violations.append(f"run inputs: {source}.columns key {contract_col!r} is not a declared contract column")
            continue
        if not isinstance(physical_col, str) or physical_col not in actual_columns:
            violations.append(
                f"run inputs: {source}.columns[{contract_col!r}] = {physical_col!r} was not found in "
                f"the source's header at the pinned version (available: {sorted(actual_columns)})"
            )
            continue
        if physical_col in seen_physical:
            violations.append(
                f"run inputs: {source}.columns: physical column {physical_col!r} is the target of "
                f"more than one contract column -- a collision"
            )
            continue
        seen_physical.add(physical_col)
        mapped[contract_col] = physical_col

    # An unmapped contract column whose own name equals some OTHER column's
    # physical target is ambiguous -- after renaming, two columns would want
    # the same logical name (D-P7-1 / §1.3 "shadowing").
    for contract_col in contract_columns:
        if contract_col in mapped:
            continue
        if contract_col in seen_physical:
            violations.append(
                f"run inputs: {source}: contract column {contract_col!r} is unmapped, but it is also "
                f"the physical target of another mapped column -- ambiguous after renaming"
            )
    return mapped


def _load_parameter_file(name: str, path: str, fmt: str, violations: list[str]) -> tuple[bytes, str, Any] | None:
    p = Path(path)
    if not p.is_file():
        violations.append(f"run inputs: parameter {name!r} file not found: {path}")
        return None
    data = p.read_bytes()
    sha256 = sha256_file(p)
    try:
        if fmt == "csv":
            df = pd.read_csv(io.BytesIO(data))
            parsed: Any = df.to_dict("records")
        else:  # yaml, validated by source_bindings._validate_parameters_block
            parsed = yaml.safe_load(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a named ContractViolation
        violations.append(f"run inputs: parameter {name!r} file {path} could not be parsed as {fmt}: {exc!r}")
        return None
    return data, sha256, parsed


def _coerce_id_list(name: str, parsed: Any, item_type: str, violations: list[str]) -> list | None:
    if isinstance(parsed, dict) and len(parsed) == 0:
        parsed = []
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        # A CSV parses to one dict per row; an id_list uses the row's single column.
        keys = {k for row in parsed for k in row}
        if len(keys) != 1:
            violations.append(
                f"run inputs: parameter {name!r} (kind=id_list) expects a single-column file, "
                f"found columns {sorted(keys)}"
            )
            return None
        parsed = [row[next(iter(keys))] for row in parsed]
    if not isinstance(parsed, list):
        violations.append(f"run inputs: parameter {name!r} (kind=id_list) must parse to a list")
        return None
    try:
        if item_type == "integer":
            values = [int(v) for v in parsed]
        else:
            values = [str(v) for v in parsed]
    except (TypeError, ValueError) as exc:
        violations.append(f"run inputs: parameter {name!r}: value could not be coerced to {item_type}: {exc}")
        return None
    return values


def _resolve_parameters(skill: Skill, configured_params: dict, violations: list[str]) -> dict[str, dict]:
    param_specs: dict = skill.contract.get("parameters", {}) or {}
    if not configured_params:
        return {}

    unknown = sorted(set(configured_params) - set(param_specs))
    for name in unknown:
        violations.append(f"run inputs: unknown parameter {name!r} (not declared in contract.yaml parameters)")

    groups: dict[str, list[str]] = {}
    for name, spec in param_specs.items():
        groups.setdefault(spec["group"], []).append(name)
    touched_groups = {param_specs[n]["group"] for n in configured_params if n in param_specs}
    for group_name, members in groups.items():
        if group_name not in touched_groups:
            continue
        missing_members = sorted(m for m in members if m not in configured_params)
        if missing_members:
            violations.append(
                f"run inputs: parameter group {group_name!r} must be overridden together; "
                f"missing {missing_members}"
            )

    result: dict[str, dict] = {}
    for name, entry in configured_params.items():
        if name not in param_specs:
            continue
        spec = param_specs[name]
        provenance = entry.get("provenance") or {}
        if not provenance.get("owner") or not provenance.get("as_of"):
            violations.append(f"run inputs: parameter {name!r} provenance requires 'owner' and 'as_of'")
            continue
        loaded = _load_parameter_file(name, entry.get("path"), entry.get("format"), violations)
        if loaded is None:
            continue
        _data, sha256, parsed = loaded
        if parsed is None or (hasattr(parsed, "__len__") and len(parsed) == 0):
            violations.append(f"run inputs: parameter {name!r} file is empty: {entry.get('path')}")
            continue

        kind = spec.get("kind")
        if kind == "id_list":
            value = _coerce_id_list(name, parsed, spec.get("item_type", "string"), violations)
            if value is None:
                continue
        else:
            # table/mapping kinds: the parsed rows/mapping are stored as-is,
            # already validated as non-empty above. Column-shape checking
            # against spec["columns"] is authoring-time work for a Skill
            # that declares one; no SKILL-001 parameter uses these kinds
            # today (population_of_interest is id_list throughout).
            value = parsed

        result[name] = {
            "path": str(entry.get("path")),
            "sha256": sha256,
            "provenance": dict(provenance),
            "kind": kind,
            "value": value,
        }
    return result
