"""orchestrator/schemas/contract.schema.json's format enum extension
(docs/specs/P6_P8_explorer_llm_design.md §4.11 "Contract schema change"):
`format` gains `parquet`/`uc_table`/`upload`, and `file` is required only
for the three local-file formats. Fully offline."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "orchestrator" / "schemas" / "contract.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def test_schema_is_valid_jsonschema(schema):
    jsonschema.Draft202012Validator.check_schema(schema)


def test_format_enum_has_all_five_kinds(schema):
    enum = schema["properties"]["sources"]["additionalProperties"]["properties"]["format"]["enum"]
    assert set(enum) == {"xlsx", "csv", "parquet", "uc_table", "upload"}


def _source(**overrides) -> dict:
    base = {"format": "csv", "file": "a.csv", "columns": {}}
    base.update(overrides)
    return {"timezone": "Australia/Sydney", "sources": {"s1": base}}


@pytest.mark.parametrize("fmt", ["xlsx", "csv", "parquet"])
def test_local_file_formats_require_file(schema, fmt):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"timezone": "Australia/Sydney", "sources": {"s1": {"format": fmt, "columns": {}}}}, schema,
        )


@pytest.mark.parametrize("fmt", ["xlsx", "csv", "parquet"])
def test_local_file_formats_valid_with_file(schema, fmt):
    jsonschema.validate(_source(format=fmt), schema)


@pytest.mark.parametrize("fmt", ["uc_table", "upload"])
def test_non_local_formats_do_not_require_file(schema, fmt):
    jsonschema.validate({"timezone": "Australia/Sydney", "sources": {"s1": {"format": fmt, "columns": {}}}}, schema)


def test_repo_skill_contract_still_validates(schema):
    import yaml

    contract_path = Path(__file__).resolve().parent.parent / "skills" / "tne_exco" / "contract.yaml"
    data = yaml.safe_load(contract_path.read_text())
    jsonschema.validate(data, schema)
