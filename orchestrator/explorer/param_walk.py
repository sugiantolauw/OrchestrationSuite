"""The column-valued-param walk (docs/specs/P6_P8_explorer_llm_design.md
§4.7 V-T3: "Column-valued params are the keys column, *_column,
key_columns, group_by, group_keys, left_keys, right_keys,
carry_right_columns, exclude_within, metrics[].column, where.column,
exclude.column and limit.column. The walk is driven by the allowlist
table and is not fuzzy.").

One implementation, shared by `validate.py`'s V-T3 (a canonical Explorer
test's params) and `reference_skills.py`'s "contract columns the test
uses" digest (a repo Skill's plan.yaml params) -- both already speak the
same INTERNAL params shape (`{"limit": {"threshold": id} | {"column":
name}, "metrics": {name: {kind, column?, ...}}, ...}`), the one
`to_canonical` produces and a repo Skill's plan.yaml already uses, so one
walker serves both without knowing which produced its input."""

from __future__ import annotations

from typing import Iterator

# Params whose value is a LIST of column names (every other column-valued
# param is a single string, matched by name/suffix below).
COLUMN_LIST_PARAM_KEYS: frozenset[str] = frozenset({
    "key_columns", "group_by", "group_keys", "left_keys", "right_keys",
    "carry_right_columns", "exclude_within",
})

# Params whose value is a {"threshold": id} | {"column": name} object (the
# internal LIMIT_SCHEMA/THRESHOLD_ONLY_SCHEMA shape) -- only the column
# variant is a column reference.
_LIMIT_SHAPED_PARAM_KEYS: frozenset[str] = frozenset({
    "limit", "threshold", "window_days", "aggregate_threshold", "max_line",
})


def iter_column_params(params: dict) -> Iterator[tuple[str, str]]:
    """Yields `(location, column_name)` for every column this primitive's
    `params` references -- `location` is a dotted path for error messages
    (e.g. `"metrics.hv_count.where.column"`), never used as a lookup key."""
    for key, value in (params or {}).items():
        if value is None:
            continue
        if key == "column" or key.endswith("_column"):
            if isinstance(value, str):
                yield key, value
            continue
        if key in COLUMN_LIST_PARAM_KEYS:
            for v in value or []:
                if isinstance(v, str):
                    yield key, v
            continue
        if key in _LIMIT_SHAPED_PARAM_KEYS:
            if isinstance(value, dict) and isinstance(value.get("column"), str):
                yield f"{key}.column", value["column"]
            continue
        if key == "exclude":
            if isinstance(value, dict) and isinstance(value.get("column"), str):
                yield "exclude.column", value["column"]
            continue
        if key == "metrics" and isinstance(value, dict):
            for metric_name, spec in value.items():
                if not isinstance(spec, dict):
                    continue
                if isinstance(spec.get("column"), str):
                    yield f"metrics.{metric_name}.column", spec["column"]
                where = spec.get("where")
                if isinstance(where, dict) and isinstance(where.get("column"), str):
                    yield f"metrics.{metric_name}.where.column", where["column"]
