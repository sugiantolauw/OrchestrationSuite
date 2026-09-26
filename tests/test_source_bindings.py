from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from orchestrator.errors import ConfigError
from orchestrator.source_bindings import (
    bindings_for_skill,
    load_source_bindings,
    not_supplied_reasons,
    suggested_values,
    volume_file_paths,
)


def _write(tmp_path: Path, data: dict, name: str = "bindings.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return p


def test_no_path_returns_empty():
    assert load_source_bindings(None) == {}
    assert load_source_bindings("") == {}


def test_missing_file_raises_config_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="does not point at a file"):
        load_source_bindings(str(tmp_path / "nope.yaml"))


def test_volume_file_and_uc_table_still_parse(tmp_path: Path):
    p = _write(tmp_path, {
        "SKILL-001": {
            "expense_report": {"kind": "volume_file", "path": "/Volumes/x/y/z/e.xlsx", "sheet": "data"},
            "approval_aging": {"kind": "uc_table", "fqn": "cat.sch.approval_aging"},
        }
    })
    config = load_source_bindings(str(p))
    bindings = bindings_for_skill(config, "SKILL-001")
    assert suggested_values(bindings) == {
        "expense_report": "/Volumes/x/y/z/e.xlsx",
        "approval_aging": "cat.sch.approval_aging",
    }


def test_volume_file_with_columns_mapping(tmp_path: Path):
    p = _write(tmp_path, {
        "SKILL-001": {
            "expense_report": {
                "kind": "volume_file", "path": "/v/e.xlsx",
                "columns": {"Employee ID": "Emp No"},
            },
        }
    })
    bindings = bindings_for_skill(load_source_bindings(str(p)), "SKILL-001")
    assert bindings["expense_report"]["columns"] == {"Employee ID": "Emp No"}


def test_columns_must_be_a_mapping(tmp_path: Path):
    p = _write(tmp_path, {
        "SKILL-001": {"expense_report": {"kind": "volume_file", "path": "/v/e.xlsx", "columns": ["not", "a", "dict"]}}
    })
    with pytest.raises(ConfigError, match="columns must be a mapping"):
        load_source_bindings(str(p))


def test_not_supplied_requires_reason(tmp_path: Path):
    p = _write(tmp_path, {"SKILL-001": {"booking_detail": {"kind": "not_supplied"}}})
    with pytest.raises(ConfigError, match="requires a non-empty 'reason'"):
        load_source_bindings(str(p))


def test_not_supplied_rejects_columns(tmp_path: Path):
    p = _write(tmp_path, {
        "SKILL-001": {"booking_detail": {"kind": "not_supplied", "reason": "x", "columns": {"a": "b"}}}
    })
    with pytest.raises(ConfigError, match="has no use for"):
        load_source_bindings(str(p))


def test_not_supplied_rejects_extra_keys(tmp_path: Path):
    p = _write(tmp_path, {
        "SKILL-001": {"booking_detail": {"kind": "not_supplied", "reason": "x", "path": "/v/x.csv"}}
    })
    with pytest.raises(ConfigError, match="has no use for"):
        load_source_bindings(str(p))


def test_not_supplied_parses_and_reports_via_not_supplied_reasons(tmp_path: Path):
    p = _write(tmp_path, {"SKILL-001": {"booking_detail": {"kind": "not_supplied", "reason": "no travel agency data"}}})
    bindings = bindings_for_skill(load_source_bindings(str(p)), "SKILL-001")
    assert not_supplied_reasons(bindings) == {"booking_detail": "no travel agency data"}
    assert suggested_values(bindings) == {}
    assert volume_file_paths(bindings) == {}


def test_invalid_kind_raises(tmp_path: Path):
    p = _write(tmp_path, {"SKILL-001": {"expense_report": {"kind": "something_else"}}})
    with pytest.raises(ConfigError, match="kind must be one of"):
        load_source_bindings(str(p))


def test_parameters_block_parses(tmp_path: Path):
    p = _write(tmp_path, {
        "SKILL-001": {
            "parameters": {
                "population_of_interest": {
                    "path": "/v/ids.csv", "format": "csv",
                    "provenance": {"owner": "Jane", "as_of": "2026-07-01"},
                },
            },
        }
    })
    config = load_source_bindings(str(p))
    bindings = bindings_for_skill(config, "SKILL-001")
    assert bindings["parameters"]["population_of_interest"]["path"] == "/v/ids.csv"
    # The reserved "parameters" entry is never treated as a physical binding.
    assert suggested_values(bindings) == {}
    assert volume_file_paths(bindings) == {}


def test_parameters_block_requires_provenance_owner_and_as_of(tmp_path: Path):
    p = _write(tmp_path, {
        "SKILL-001": {
            "parameters": {"population_of_interest": {"path": "/v/ids.csv", "format": "csv", "provenance": {"owner": "Jane"}}},
        }
    })
    with pytest.raises(ConfigError, match="requires 'owner' and 'as_of'"):
        load_source_bindings(str(p))


def test_parameters_block_requires_format(tmp_path: Path):
    p = _write(tmp_path, {
        "SKILL-001": {
            "parameters": {"population_of_interest": {"path": "/v/ids.csv", "provenance": {"owner": "a", "as_of": "b"}}},
        }
    })
    with pytest.raises(ConfigError, match="format must be 'csv' or 'yaml'"):
        load_source_bindings(str(p))


def test_json_format_also_parses(tmp_path: Path):
    p = tmp_path / "bindings.json"
    p.write_text(json.dumps({"SKILL-001": {"approval_aging": {"kind": "uc_table", "fqn": "c.s.t"}}}))
    config = load_source_bindings(str(p))
    assert bindings_for_skill(config, "SKILL-001")["approval_aging"]["fqn"] == "c.s.t"
