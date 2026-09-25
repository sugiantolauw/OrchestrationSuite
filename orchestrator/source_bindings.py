"""Per-environment source-binding configuration (independent review
2026-09-24 item 1 -- CLAUDE.md §11 "Corporate workspace assessment": at the
corporate workspace, T&E sources are Excel files in a UC Volume, not Delta
tables). `SOURCE_BINDINGS` (env, `orchestrator.config.Settings.
source_bindings_path`) names a config file OUTSIDE version control -- a
gitignored path, whether inside the repo or not -- that maps each Skill's
contract sources to an exact, pre-declared location:

    {
      "<skill_id>": {
        "<contract_source_name>": {
          "kind": "volume_file",
          "path": "/Volumes/<catalog>/<schema>/<volume>/tne/expense_report.xlsx",
          "sheet": "Sheet1",
          "columns": {"<contract column>": "<physical column>"}
        },
        "<other_source_name>": {
          "kind": "uc_table",
          "fqn": "<catalog>.<schema>.approval_aging"
        },
        "<optional_source_name>": {
          "kind": "not_supplied",
          "reason": "Travel agency data not held by this business unit"
        },
        "parameters": {
          "population_of_interest": {
            "path": "/Volumes/<catalog>/<schema>/<volume>/tne/exco_ids.csv",
            "format": "csv",
            "provenance": {"owner": "<name>", "as_of": "2026-07-01", "source": "HR extract ref <x>"}
          }
        }
      }
    }

Every `path`/`fqn` is an EXACT, explicitly declared value -- this module
never infers or guesses a filename (CLAUDE.md NN14, the `_find_col`
prohibition). YAML or JSON, chosen by the file's extension. No corporate
workspace name, catalog, volume or path may ever be committed (CLAUDE.md
NN16) -- this file lives only in the gitignored corporate `.env`/config
directory; `.env.example` documents the variable with a placeholder only.

Independent review 2026-09-25 item 1 (docs/specs/P7_mapping_authoring_design.md
D-P7-1, "run inputs"): a source binding may also carry `columns` -- an exact,
declared {contract column: physical column} mapping, never fuzzy -- or be
`kind: not_supplied` with a `reason`. `parameters` is a reserved key (no
contract source may be named "parameters") holding overrides of a Skill's
declared `parameters:` block (contract.yaml), each pinned by file hash and
provenance. All three are validated against the Skill's contract by
`orchestrator.run_inputs.resolve_run_inputs`, not here -- this module only
parses the file's own shape."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from orchestrator.errors import ConfigError

_VALID_KINDS = frozenset({"volume_file", "uc_table", "not_supplied"})
PARAMETERS_KEY = "parameters"


def load_source_bindings(path: str | Path | None) -> dict[str, dict[str, dict]]:
    """Returns `{skill_id: {source_name: binding}}` (source_name may be the
    reserved `"parameters"` key, whose value is `{param_name: entry}` rather
    than a single binding -- see `_validate_parameters_block`). `path` unset
    -> `{}` (no configured bindings at all -- every source falls back to the
    existing governed-table auto-bind / manual selection). `path` set but
    unreadable, or its content malformed, is a loud ConfigError -- an
    operator who names a bindings file and gets it wrong must find out
    immediately, not have it silently ignored (CLAUDE.md NN14)."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"SOURCE_BINDINGS={path!r} does not point at a file")
    text = p.read_text()
    try:
        if p.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            raw = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"SOURCE_BINDINGS={path!r} could not be parsed: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"SOURCE_BINDINGS={path!r} must be a mapping of skill_id -> sources, got {type(raw).__name__}")

    result: dict[str, dict[str, dict]] = {}
    for skill_id, sources in raw.items():
        if not isinstance(sources, dict):
            raise ConfigError(f"SOURCE_BINDINGS={path!r}: skill {skill_id!r} must map source names to bindings")
        by_source: dict[str, dict] = {}
        for source_name, binding in sources.items():
            if source_name == PARAMETERS_KEY:
                by_source[source_name] = _validate_parameters_block(path, skill_id, binding)
            else:
                by_source[source_name] = _validate_binding(path, skill_id, source_name, binding)
        result[skill_id] = by_source
    return result


def _validate_binding(path, skill_id: str, source_name: str, binding: object) -> dict:
    if not isinstance(binding, dict):
        raise ConfigError(
            f"SOURCE_BINDINGS={path!r}: {skill_id}.{source_name} must be a mapping, got {type(binding).__name__}"
        )
    kind = binding.get("kind")
    if kind not in _VALID_KINDS:
        raise ConfigError(
            f"SOURCE_BINDINGS={path!r}: {skill_id}.{source_name}.kind must be one of "
            f"{sorted(_VALID_KINDS)}, got {kind!r}"
        )
    if kind == "volume_file":
        if not binding.get("path"):
            raise ConfigError(
                f"SOURCE_BINDINGS={path!r}: {skill_id}.{source_name} (kind=volume_file) requires an exact 'path'"
            )
    elif kind == "uc_table":
        if not binding.get("fqn"):
            raise ConfigError(
                f"SOURCE_BINDINGS={path!r}: {skill_id}.{source_name} (kind=uc_table) requires an exact 'fqn'"
            )
    else:  # not_supplied
        if not binding.get("reason") or not str(binding["reason"]).strip():
            raise ConfigError(
                f"SOURCE_BINDINGS={path!r}: {skill_id}.{source_name} (kind=not_supplied) requires a non-empty 'reason'"
            )
        extra = set(binding) - {"kind", "reason"}
        if extra:
            raise ConfigError(
                f"SOURCE_BINDINGS={path!r}: {skill_id}.{source_name} (kind=not_supplied) "
                f"has no use for {sorted(extra)} -- an unsupplied source has no location and no columns"
            )
    columns = binding.get("columns")
    if columns is not None and not isinstance(columns, dict):
        raise ConfigError(
            f"SOURCE_BINDINGS={path!r}: {skill_id}.{source_name}.columns must be a mapping of "
            f"{{contract column: physical column}}, got {type(columns).__name__}"
        )
    return dict(binding)


def _validate_parameters_block(path, skill_id: str, block: object) -> dict:
    if not isinstance(block, dict):
        raise ConfigError(
            f"SOURCE_BINDINGS={path!r}: {skill_id}.parameters must be a mapping of "
            f"parameter name -> entry, got {type(block).__name__}"
        )
    result: dict[str, dict] = {}
    for name, entry in block.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                f"SOURCE_BINDINGS={path!r}: {skill_id}.parameters.{name} must be a mapping"
            )
        if not entry.get("path"):
            raise ConfigError(
                f"SOURCE_BINDINGS={path!r}: {skill_id}.parameters.{name} requires an exact 'path'"
            )
        if entry.get("format") not in ("csv", "yaml"):
            raise ConfigError(
                f"SOURCE_BINDINGS={path!r}: {skill_id}.parameters.{name}.format must be 'csv' or "
                f"'yaml', got {entry.get('format')!r}"
            )
        provenance = entry.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get("owner") or not provenance.get("as_of"):
            raise ConfigError(
                f"SOURCE_BINDINGS={path!r}: {skill_id}.parameters.{name}.provenance requires "
                f"'owner' and 'as_of' -- there are no defaults"
            )
        result[name] = dict(entry)
    return result


def bindings_for_skill(config: dict[str, dict[str, dict]], skill_id: str) -> dict[str, dict]:
    """`{source_name: binding}` for one Skill (may include the reserved
    `"parameters"` key); `{}` when the config has nothing configured for it
    (the common case for a Skill with no corporate-Volume sources, or in a
    workspace with no SOURCE_BINDINGS at all)."""
    return dict(config.get(skill_id) or {})


def suggested_values(bindings: dict[str, dict]) -> dict[str, str]:
    """`{source_name: exact bound value}` -- the Volume path for a
    volume_file entry, the FQN for a uc_table entry -- suitable for feeding
    straight into the existing auto-bind path (orchestrator.service.
    suggest_bindings) with no new UI (independent review item 1: "the
    landing page needs no new UI"). A `not_supplied` source, and the
    reserved `"parameters"` entry, have no physical value and are omitted --
    `orchestrator.run_inputs.resolve_run_inputs` is what a caller consults
    for those."""
    values: dict[str, str] = {}
    for name, binding in bindings.items():
        if name == PARAMETERS_KEY:
            continue
        kind = binding.get("kind")
        if kind == "volume_file":
            values[name] = binding["path"]
        elif kind == "uc_table":
            values[name] = binding["fqn"]
    return values


def not_supplied_reasons(bindings: dict[str, dict]) -> dict[str, str]:
    """`{source_name: reason}` for this Skill's configured `kind:
    not_supplied` entries -- the set `app/src/run_setup.py`'s `_auto_bind`
    treats as bound with no physical value required."""
    return {
        name: binding["reason"]
        for name, binding in bindings.items()
        if name != PARAMETERS_KEY and binding.get("kind") == "not_supplied"
    }


def volume_file_paths(bindings: dict[str, dict]) -> dict[str, dict]:
    """`{exact volume path: binding}` for this Skill's `kind: volume_file`
    entries only -- the shape orchestrator.adapters.datasource_volume_upload.
    VolumeUploadAwareDataSource's `configured_volume_paths` takes, so a
    source bound to one of these exact paths is read via the Files API
    instead of being sent through the UC table SQL path."""
    return {
        b["path"]: b
        for name, b in bindings.items()
        if name != PARAMETERS_KEY and b.get("kind") == "volume_file"
    }
