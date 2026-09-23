from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import pandas as pd

from orchestrator.populations import PopulationResult


class PrimitiveParamsError(Exception):
    pass


# The metrics-config fragment shared by every primitive's PARAMS_SCHEMA (CLAUDE.md
# §4.3): a Skill names its metrics and picks a standard "kind" -- never inlines a
# bare threshold or computed value.
METRICS_PROPERTY_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [
                    "count",
                    "rows",
                    "groups",
                    "sum",
                    "distinct",
                    "max",
                    "pct_of_population",
                    "excess",
                    "match_rate",
                    "value",
                    "count_where",
                    "sum_where",
                ],
            },
            "column": {"type": "string"},
            "key": {"type": "string"},
            "unit": {"type": "string"},
            "where": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "op": {
                        "type": "string",
                        "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"],
                    },
                    "value": {},
                },
                "required": ["column", "op"],
                "additionalProperties": False,
            },
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
}

THRESHOLD_ONLY_SCHEMA: dict = {
    "type": "object",
    "properties": {"threshold": {"type": "string"}},
    "required": ["threshold"],
    "additionalProperties": False,
}

LIMIT_SCHEMA: dict = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "properties": {"threshold": {"type": "string"}},
            "required": ["threshold"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"column": {"type": "string"}},
            "required": ["column"],
            "additionalProperties": False,
        },
    ],
}


@dataclass(frozen=True)
class Metric:
    value: Any
    unit: str
    source_ref: dict

    def to_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit, "source_ref": self.source_ref}


@dataclass
class PrimitiveResult:
    metrics: dict[str, dict]
    flags: pd.DataFrame
    scored_units: list[str]


@dataclass
class PrimitiveContext:
    populations: dict[str, PopulationResult]
    thresholds: dict[str, dict]
    references: dict[str, Any] = field(default_factory=dict)

    def population(self, name: str) -> PopulationResult:
        if name not in self.populations:
            raise PrimitiveParamsError(f"unknown population: {name!r}")
        return self.populations[name]


def resolve_limit_spec(ctx: PrimitiveContext, spec: dict, *, name: str = "limit") -> dict:
    if not isinstance(spec, dict) or len(spec) != 1 or next(iter(spec)) not in ("threshold", "column"):
        raise PrimitiveParamsError(
            f"{name} must be exactly one of {{'threshold': <id>}} or {{'column': <name>}}, "
            f"got {spec!r}"
        )
    if "threshold" in spec:
        tid = spec["threshold"]
        if tid not in ctx.thresholds:
            raise PrimitiveParamsError(f"unknown threshold id: {tid!r}")
        return {"kind": "threshold", "id": tid, "value": ctx.thresholds[tid]["value"]}
    return {"kind": "column", "column": spec["column"]}


def resolve_scalar_threshold(ctx: PrimitiveContext, spec: dict, *, name: str = "threshold") -> Any:
    resolved = resolve_limit_spec(ctx, spec, name=name)
    if resolved["kind"] != "threshold":
        raise PrimitiveParamsError(f"{name} must be a threshold reference: {{'threshold': <id>}}")
    return resolved["value"]


def limit_series(resolved: dict, df: pd.DataFrame) -> pd.Series:
    if resolved["kind"] == "threshold":
        return pd.Series(resolved["value"], index=df.index)
    return df[resolved["column"]]


_DIRECTION_OPS = {
    "above": lambda values, limits: values > limits,
    "below": lambda values, limits: values < limits,
    "at_or_above": lambda values, limits: values >= limits,
    "at_or_below": lambda values, limits: values <= limits,
}


def direction_mask(values: pd.Series, limits, direction: str) -> pd.Series:
    fn = _DIRECTION_OPS.get(direction)
    if fn is None:
        raise PrimitiveParamsError(
            f"unknown direction: {direction!r} (expected one of {sorted(_DIRECTION_OPS)})"
        )
    return fn(values, limits)


_ROW_OPS = {
    "eq": lambda s, v: s == v,
    "ne": lambda s, v: s != v,
    "gt": lambda s, v: s > v,
    "gte": lambda s, v: s >= v,
    "lt": lambda s, v: s < v,
    "lte": lambda s, v: s <= v,
    "in": lambda s, v: s.isin(v),
    "not_in": lambda s, v: ~s.isin(v),
}


def row_condition_mask(df: pd.DataFrame, spec: dict) -> pd.Series:
    col = spec["column"]
    op = spec["op"]
    if op not in _ROW_OPS:
        raise PrimitiveParamsError(f"unknown row condition op: {op!r}")
    return _ROW_OPS[op](df[col], spec.get("value"))


def flags_from_rows(
    df: pd.DataFrame, *, flag: str, group_ids: pd.Series | None = None
) -> pd.DataFrame:
    if len(df) == 0:
        return pd.DataFrame(columns=["__source", "__row_key", "flag", "group_id"])
    out = pd.DataFrame(
        {
            "__source": df["__source"].to_numpy(),
            "__row_key": df["__row_key"].to_numpy(),
            "flag": flag,
            "group_id": group_ids.to_numpy() if group_ids is not None else None,
        }
    )
    return out


def _format_key_component(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    return str(v)


def group_id_from_key(key_values: tuple) -> str:
    return "|".join(_format_key_component(v) for v in key_values)


def _normalise_scalar(v: Any) -> Any:
    """Normalises one key-tuple value for canonical_json (CLAUDE.md build brief
    N1): a numeric value is rendered via Decimal(repr(v)) so 810 and 810.0 --
    the int-in-YAML vs. engine-coerced-float difference that made the old
    delimiter-joined group_id_from_key fragile -- hash identically, and a date
    normalises to its ISO form regardless of Timestamp vs. str input."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        try:
            return format(Decimal(repr(v)).normalize(), "f")
        except InvalidOperation:
            return repr(v)
    return str(v)


def canonical_json(value: Any) -> str:
    """A deterministic JSON encoding: sorted keys, fixed separators, used as the
    hash input for both keyed-unit and group-unit ids (N1) so the id never
    depends on float/Decimal repr drift or an unescaped delimiter in a value."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def keyed_unit_id(kind: str, key_values: tuple) -> str:
    """A scoring-unit id for a unit identified by a natural key tuple (e.g. T6.1d's
    employee-day-country, T3.3b's entertainment entry, T5.2's duplicate key) --
    N1: f"{kind}:{sha256(canonical_json(normalised key tuple))[:24]}"."""
    normalised = [_normalise_scalar(v) for v in key_values]
    digest = hashlib.sha256(canonical_json(normalised).encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{digest}"


def member_group_id(kind: str, row_keys: Iterable[str]) -> str:
    """A scoring-unit id for a unit identified by its ROW MEMBERSHIP rather than a
    key tuple (N1) -- e.g. a split_detection claim group, where two detections
    (same-day and window) that cover the identical set of rows are the same
    claim regardless of how each was found. f"{kind}:{sha256(canonical_json(sorted
    member row keys))[:24]}"."""
    ordered = sorted(str(k) for k in row_keys)
    digest = hashlib.sha256(canonical_json(ordered).encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{digest}"


def _base_source_ref(population: PopulationResult, columns: list[str], grain: str) -> dict:
    return {
        "sources": [{"name": population.source, "version": population.source_version}],
        "columns": list(columns),
        "grain": grain,
        "population": population.name,
    }


def build_metrics(
    metrics_spec: dict[str, dict],
    *,
    population: PopulationResult,
    default_columns: list[str],
    grain: str,
    row_df: pd.DataFrame | None = None,
    group_df: pd.DataFrame | None = None,
    scored_units: list[str] | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, dict]:
    """Turns a primitive's `metrics:` config into the run's Metric dict. Each entry
    names a standard `kind` (CLAUDE.md §4.3); the primitive supplies the row-grain
    exceptions (`row_df`), the group-grain exceptions if any (`group_df`), the
    scoring-unit ids (`scored_units`) and any named scalar it computed itself
    (`values`, e.g. an "excess" total or a "match_rate")."""
    values = values or {}
    out: dict[str, dict] = {}
    for name, spec in (metrics_spec or {}).items():
        kind = spec["kind"]
        unit = spec.get("unit")
        columns = default_columns
        if kind == "count":
            n = len(scored_units) if scored_units is not None else (len(row_df) if row_df is not None else 0)
            metric = Metric(value=int(n), unit=unit or "count", source_ref=_base_source_ref(population, columns, grain))
        elif kind == "rows":
            n = len(row_df) if row_df is not None else 0
            metric = Metric(value=int(n), unit=unit or "count", source_ref=_base_source_ref(population, columns, grain))
        elif kind == "groups":
            n = len(group_df) if group_df is not None else 0
            metric = Metric(value=int(n), unit=unit or "count", source_ref=_base_source_ref(population, columns, grain))
        elif kind == "sum":
            col = spec["column"]
            frame = group_df if (group_df is not None and col in group_df.columns) else row_df
            v = float(frame[col].sum()) if frame is not None and len(frame) else 0.0
            metric = Metric(value=v, unit=unit or "AUD", source_ref=_base_source_ref(population, [col], grain))
        elif kind == "distinct":
            col = spec["column"]
            frame = row_df if (row_df is not None and col in row_df.columns) else group_df
            v = int(frame[col].nunique()) if frame is not None and len(frame) else 0
            metric = Metric(value=v, unit=unit or "count", source_ref=_base_source_ref(population, [col], grain))
        elif kind == "max":
            col = spec["column"]
            frame = group_df if (group_df is not None and col in group_df.columns) else row_df
            v = float(frame[col].max()) if frame is not None and len(frame) else 0.0
            metric = Metric(value=v, unit=unit or "AUD", source_ref=_base_source_ref(population, [col], grain))
        elif kind == "pct_of_population":
            n = len(scored_units) if scored_units is not None else (len(row_df) if row_df is not None else 0)
            denom = values.get("population_size", population.rows)
            v = round(n / denom * 100, 1) if denom else 0.0
            metric = Metric(value=v, unit=unit or "%", source_ref=_base_source_ref(population, columns, grain))
        elif kind == "excess":
            v = float(values.get("excess", 0.0))
            metric = Metric(value=v, unit=unit or "AUD", source_ref=_base_source_ref(population, columns, grain))
        elif kind == "match_rate":
            v = values.get("match_rate")
            metric = Metric(value=v, unit=unit or "%", source_ref=_base_source_ref(population, columns, grain))
        elif kind == "value":
            key = spec["key"]
            if key not in values:
                raise PrimitiveParamsError(f"metric {name!r}: no computed value named {key!r}")
            metric = Metric(value=values[key], unit=unit or "count", source_ref=_base_source_ref(population, columns, grain))
        elif kind == "count_where":
            frame = row_df
            where = spec["where"]
            n = int(row_condition_mask(frame, where).sum()) if frame is not None and len(frame) else 0
            metric = Metric(
                value=n, unit=unit or "count", source_ref=_base_source_ref(population, [where["column"]], grain)
            )
        elif kind == "sum_where":
            col = spec["column"]
            where = spec["where"]
            frame = row_df
            if frame is not None and len(frame):
                mask = row_condition_mask(frame, where)
                v = float(frame.loc[mask, col].sum())
            else:
                v = 0.0
            metric = Metric(
                value=v, unit=unit or "AUD", source_ref=_base_source_ref(population, [col, where["column"]], grain)
            )
        else:
            raise PrimitiveParamsError(f"unknown metric kind: {kind!r}")
        out[name] = metric.to_dict()
    return out
