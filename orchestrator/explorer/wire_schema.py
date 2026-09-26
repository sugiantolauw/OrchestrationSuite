"""The Explorer `PlanProposal` wire JSON Schema (CLAUDE.md §4.5;
docs/specs/P6_P8_explorer_llm_design.md §4.5). Generated from `PRIMITIVES`
at import so it can never drift from the primitives the pipeline actually
runs -- there is no second, hand-maintained copy of "what a test's params
may contain".

Strict-mode rules (§4.5, driven by the recorded GPT-OSS matrix, CLAUDE.md
§6 §1.2): every object has `additionalProperties: false` and lists every
property in `required`; an optional value is a `["<type>", "null"]` union
(`_nullable`, below); there is no `pattern` (identifier formats are
validated in Python, V-S2), no bare `{"type": "object"}` property, and
`oneOf` is written as `anyOf`.

Two things live here besides the schema:

1. `EXPLORER_PARAM_ALLOWLIST` -- which of a primitive's own `PARAMS_SCHEMA`
   parameters Explorer v1 exposes at all (§4.5's table). A unit test
   (tests/test_explorer_wire_schema.py) asserts this is always a subset of
   the primitive's real schema, so drift there fails CI, not a run.
2. `STRING_FIELD_CLASSIFICATION` -- every wire string leaf's kind (prose /
   identifier / literal / expression), keyed by a stable dotted path
   `walk_string_leaves()` also produces. This is what makes "every string
   leaf is deliberately accounted for" a checkable fact (CLAUDE.md §8 P8
   DoD "planner cannot emit code") rather than a claim: a new wire field
   that is not added to this table fails the gate test, so nobody can
   quietly widen the planner's free-text surface.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from orchestrator.primitives import PRIMITIVES

# ── generic JSON-Schema leaf builders (strict-mode compatible) ─────────────

STR: dict = {"type": "string"}
NUM: dict = {"type": "number"}
BOOL: dict = {"type": "boolean"}
ANY_SCALAR: dict = {"type": ["string", "number", "boolean", "null"]}
# A population-filter / row-condition "value": a literal scalar, or (for
# in/not_in) a list of literal strings. Never a bare {"type": "object"}, and
# no `pattern` -- format checks (V-S2 key patterns, V-F2 profiled-value
# membership) happen in Python, not in the wire schema.
FILTER_VALUE: dict = {"type": ["string", "number", "array", "null"], "items": {"type": "string"}}


def _nullable(schema: dict) -> dict:
    """Widens a leaf/object schema's `type` (or `enum`) to also accept
    `null` -- the wire schema's spelling of "this property is optional"
    (§4.5: every object still lists it in `required`, so the planner must
    always emit the key, just possibly as `null`)."""
    widened = dict(schema)
    t = widened.get("type")
    if isinstance(t, list):
        widened["type"] = list(t) if "null" in t else [*t, "null"]
    elif isinstance(t, str):
        widened["type"] = [t, "null"]
    if "enum" in widened and None not in widened["enum"]:
        widened["enum"] = [*widened["enum"], None]
    return widened


def _obj(properties: dict, *, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def _array(items: dict, *, min_items: int | None = None) -> dict:
    out: dict = {"type": "array", "items": items}
    if min_items is not None:
        out["minItems"] = min_items
    return out


def _array_str_leaf() -> dict:
    return _array(STR)


def _enum(values: list[str]) -> dict:
    return {"type": "string", "enum": list(values)}


def _const(value: str) -> dict:
    return {"type": "string", "const": value}


# ── the condition/filter fragments shared by population filters, primitive
# `exclude`, and metric `where` (all three are the CLAUDE.md §4.3
# METRICS_PROPERTY_SCHEMA / orchestrator.populations row-condition shape,
# restated on the wire) ──────────────────────────────────────────────────

_CONDITION_OP_ENUM = ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"]
_FILTER_OP_ENUM = [*_CONDITION_OP_ENUM, "is_null", "not_null"]


def _condition_obj() -> dict:
    """{column, op, value} -- a primitive's `exclude` param or a metric's
    `where` (no is_null/not_null: those primitives' row_condition_mask
    never receives them, common.py _ROW_OPS)."""
    return _obj({"column": STR, "op": _enum(_CONDITION_OP_ENUM), "value": FILTER_VALUE})


# ── the recursive population Filter (§4.5) ─────────────────────────────────

_FILTER_REF = {"$ref": "#/$defs/filter"}


def _filter_def() -> dict:
    leaf = _obj({"column": STR, "op": _enum(_FILTER_OP_ENUM), "value": FILTER_VALUE})
    between = _obj({"between_audit_period": _obj({"column": STR})})
    any_of = _obj({"any_of": _array(_FILTER_REF, min_items=1)})
    all_of = _obj({"all_of": _array(_FILTER_REF, min_items=1)})
    return {"anyOf": [leaf, between, any_of, all_of]}


# ── metrics[] (§4.5: "metrics is converted from a map to an array") ────────

_METRIC_KIND_ENUM = [
    "count", "rows", "groups", "sum", "distinct", "max", "pct_of_population",
    "excess", "match_rate", "value", "count_where", "sum_where",
]
_METRIC_UNIT_ENUM = ["count", "%", "days", "ratio", "currency"]


def _metric_item() -> dict:
    return _obj({
        "name": STR,
        "kind": _enum(_METRIC_KIND_ENUM),
        "column": _nullable(STR),
        "key": _nullable(STR),
        "unit": _enum(_METRIC_UNIT_ENUM),
        "where": _nullable(_condition_obj()),
    })


def _metrics_array() -> dict:
    return _array(_metric_item())


# ── the {threshold: id} limit reference. Explorer only ever proposes a
# literal threshold, never a column-based limit (that would be an
# arithmetic-shaped relationship no finding rule could describe in prose
# without a number, CLAUDE.md §4.6) -- the internal primitives' oneOf(
# threshold|column) LIMIT_SCHEMA is deliberately narrowed to one branch
# here, which is also why it needs no oneOf/anyOf at all. ──────────────────

def _threshold_ref() -> dict:
    return _obj({"threshold": STR})


# ── per-primitive wire param allowlist (§4.5) ───────────────────────────────

EXPLORER_PARAM_ALLOWLIST: dict[str, list[str]] = {
    "threshold_exceedance": [
        "population", "column", "limit", "direction", "group_by", "aggregate", "exclude", "metrics",
    ],
    "duplicate_detection": [
        "population", "key_columns", "amount_column", "exclude_within", "metrics",
    ],
    "split_detection": [
        "population", "group_keys", "date_column", "amount_column", "window_days",
        "aggregate_threshold", "max_line", "metrics",
    ],
    "anti_join_gap": [
        "left_population", "right_population", "left_keys", "right_keys", "mode",
        "carry_right_columns", "metrics",
    ],
    "list_membership": [
        "population", "column", "allowed_values", "negate", "match", "metrics",
    ],
    "date_lag": [
        "population", "start_column", "end_column", "threshold", "direction", "metrics",
    ],
    "ratio_per_group": [
        "population", "numerator_column", "denominator_column", "group_by",
        "numerator_aggregate", "direction", "limit", "metrics",
    ],
    "attribute_missing": [
        "population", "column", "condition", "value", "metrics",
    ],
}

# Required (non-nullable) wire params, per primitive -- everything else in
# EXPLORER_PARAM_ALLOWLIST is present-but-nullable. `metrics` is always
# required (a test with none is caught by V-T5, not by the wire schema).
_REQUIRED_PARAMS: dict[str, list[str]] = {
    "threshold_exceedance": ["population", "column", "limit", "direction", "metrics"],
    "duplicate_detection": ["population", "key_columns", "metrics"],
    "split_detection": [
        "population", "group_keys", "date_column", "amount_column",
        "window_days", "aggregate_threshold", "metrics",
    ],
    "anti_join_gap": [
        "left_population", "right_population", "left_keys", "right_keys", "mode", "metrics",
    ],
    "list_membership": ["population", "column", "allowed_values", "negate", "match", "metrics"],
    "date_lag": ["population", "start_column", "end_column", "threshold", "direction", "metrics"],
    "ratio_per_group": [
        "population", "numerator_column", "denominator_column", "direction", "limit", "metrics",
    ],
    "attribute_missing": ["population", "column", "condition", "metrics"],
}

# Every allow-listed param's wire type, keyed by param name -- reused
# identically across every primitive that allows it (a "population" always
# means the same thing, wherever it appears). Object-valued and array-
# valued params build their own nested schema below.
_STR_PARAMS = {
    "population", "left_population", "right_population",
    "column", "amount_column", "date_column", "start_column", "end_column",
    "numerator_column", "denominator_column",
}
_ARRAY_STR_PARAMS = {
    "key_columns", "group_by", "group_keys", "left_keys", "right_keys",
    "carry_right_columns", "exclude_within", "allowed_values",
}
_ENUM_PARAMS: dict[str, list[str]] = {
    "direction": ["above", "below", "at_or_above", "at_or_below"],
    "aggregate": ["sum"],
    "mode": ["anti", "semi"],
    "match": ["exact", "whole_word_upper"],
    "numerator_aggregate": ["sum", "first"],
    "condition": ["is_null", "is_blank", "not_equals"],
}
_THRESHOLD_REF_PARAMS = {"limit", "threshold", "window_days", "aggregate_threshold", "max_line"}


def _param_schema(param: str) -> dict:
    if param == "metrics":
        return _metrics_array()
    if param == "exclude":
        return _condition_obj()
    if param == "negate":
        return BOOL
    if param == "value":
        return ANY_SCALAR
    if param in _THRESHOLD_REF_PARAMS:
        return _threshold_ref()
    if param in _ENUM_PARAMS:
        return _enum(_ENUM_PARAMS[param])
    if param in _ARRAY_STR_PARAMS:
        return _array_str_leaf()
    if param in _STR_PARAMS:
        return STR
    raise AssertionError(f"wire_schema: no wire type registered for allow-listed param {param!r}")


def _primitive_params_schema(name: str) -> dict:
    allowed = EXPLORER_PARAM_ALLOWLIST[name]
    required = set(_REQUIRED_PARAMS[name])
    properties: dict = {"kind": _const(name)}
    for param in allowed:
        schema = _param_schema(param)
        properties[param] = schema if param in required else _nullable(schema)
    return _obj(properties, required=["kind", *allowed])


# Every primitive's own wire params object schema -- also what
# orchestrator.explorer.payload's primitives_json exposes as `params_schema`.
PRIMITIVE_PARAM_SCHEMAS: dict[str, dict] = {
    name: _primitive_params_schema(name) for name in sorted(PRIMITIVES)
}


def _test_params_anyof() -> dict:
    return {"anyOf": [PRIMITIVE_PARAM_SCHEMAS[name] for name in sorted(PRIMITIVES)]}


# ── top-level blocks (§4.5) ─────────────────────────────────────────────────

def _source_item() -> dict:
    return _obj({
        "source": STR,
        "amount_column": _nullable(STR),
        "date_column": _nullable(STR),
        "entry_key": _nullable(_array_str_leaf()),
    })


def _population_item() -> dict:
    return _obj({
        "key": STR,
        "source": STR,
        "description": STR,
        "filters": _array(_FILTER_REF),
    })


def _risk_item() -> dict:
    return _obj({"key": STR, "title": STR, "description": STR})


def _control_item() -> dict:
    return _obj({
        "key": STR, "risk_key": STR, "title": STR, "description": STR,
        "type": _enum(["preventive", "detective"]),
    })


def _threshold_item() -> dict:
    return _obj({
        "id": STR, "value": NUM, "unit": _enum(_METRIC_UNIT_ENUM), "description": STR,
    })


def _test_item() -> dict:
    return _obj({
        "key": STR, "name": STR, "primitive": _enum(sorted(PRIMITIVES)),
        "params": _test_params_anyof(),
        "control_key": STR, "risk_key": STR, "assertion": _enum(["operating"]),
        "control_objective": STR, "risk_hypothesis": STR, "rationale": STR,
    })


def _severity_rule() -> dict:
    return _obj(
        {"when": _nullable(STR), "then": _enum(["High", "Medium", "Low"])},
        required=["when", "then"],
    )


def _finding_item() -> dict:
    return _obj({
        "key": STR, "test_key": STR, "title": STR, "trigger": STR,
        "severity": _array(_severity_rule(), min_items=1),
        "metrics_cited": _array_str_leaf(),
        "thresholds_cited": _array_str_leaf(),
        "monetary_basis": _enum(["spend", "excess", "approved_not_spent", "none"]),
        "observation": STR, "recommendation": STR,
        "management_questions": _array_str_leaf(),
    })


def _data_gap_item() -> dict:
    return _obj({"description": STR, "affects_test_keys": _array_str_leaf()})


def _build_plan_proposal_schema() -> dict:
    return {
        "$defs": {"filter": _filter_def()},
        "type": "object",
        "properties": {
            "schema_version": _const("explorer-plan/1"),
            "skill_name": STR,
            "domain": STR,
            "summary": STR,
            "sources": _array(_source_item()),
            "populations": _array(_population_item()),
            "risks": _array(_risk_item()),
            "controls": _array(_control_item()),
            "thresholds": _array(_threshold_item()),
            "tests": _array(_test_item()),
            "findings": _array(_finding_item()),
            "data_gaps": _array(_data_gap_item()),
            "assumptions": _array_str_leaf(),
        },
        "required": [
            "schema_version", "skill_name", "domain", "summary", "sources", "populations",
            "risks", "controls", "thresholds", "tests", "findings", "data_gaps", "assumptions",
        ],
        "additionalProperties": False,
    }


PLAN_PROPOSAL_SCHEMA: dict = _build_plan_proposal_schema()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


WIRE_SCHEMA_SHA256: str = hashlib.sha256(
    _canonical_json(PLAN_PROPOSAL_SCHEMA).encode("utf-8")
).hexdigest()


# ── string-leaf classification (CLAUDE.md §8 P8 DoD: "planner cannot emit
# code"; docs/specs/... §8 gate test 4b) ────────────────────────────────────
#
# Every string-typed leaf reachable from PLAN_PROPOSAL_SCHEMA is exactly one
# of:
#   "enum"        -- schema has "enum" or "const" (closed set, no free text)
#   "expression"  -- compiles only under orchestrator.expr (trigger/when)
#   "identifier"  -- a name/key/column reference, checked against a real
#                    known set by the VALIDATOR (columns, population/test/
#                    risk/control/threshold keys, metric names) -- never a
#                    JSON Schema `pattern` (unsupported by GPT-OSS strict
#                    mode, §1.2), always a Python-level check (V-S2 etc.)
#   "literal"     -- a data VALUE (a filter/condition's `value`), checked
#                    against profiled values by the validator (V-F2), never
#                    interpreted as code
#   "prose"       -- free text the planner writes, subject to V-P1 (no
#                    digits)/V-P2 (template placeholders only)/V-P3 (no
#                    code-like substrings)
#
# `walk_string_leaves()` enumerates every reachable leaf path from the real
# generated schema; the gate test asserts its result's keys are exactly
# this dict's keys (so a new field that is not deliberately classified here
# fails CI, not a silent widening of the planner's free-text surface).

PROSE_FIELDS: frozenset[str] = frozenset({
    "skill_name", "domain", "summary",
    "populations.description",
    "risks.title", "risks.description",
    "controls.title", "controls.description",
    "thresholds.description",
    "tests.name", "tests.control_objective", "tests.risk_hypothesis", "tests.rationale",
    "findings.title", "findings.observation", "findings.recommendation",
    "findings.management_questions",
    "data_gaps.description",
    "assumptions",
})

EXPRESSION_FIELDS: frozenset[str] = frozenset({"findings.trigger", "findings.severity.when"})

# Every other string leaf's classification, keyed by the same dotted path
# `walk_string_leaves()` produces. Filter/condition/metric leaves recur
# under several parents (population filters, a primitive's `exclude`, a
# metric's `where`) with identical meaning wherever they appear, so they are
# named once here and the walker below folds every occurrence onto the same
# path key rather than inventing a new one per parent.
_OTHER_FIELD_CLASSIFICATION: dict[str, str] = {
    "sources.source": "identifier",
    "sources.amount_column": "identifier",
    "sources.date_column": "identifier",
    "sources.entry_key": "identifier",
    "populations.key": "identifier",
    "populations.source": "identifier",
    "populations.filters.column": "identifier",
    "populations.filters.op": "enum",
    "populations.filters.value": "literal",
    "populations.filters.between_audit_period.column": "identifier",
    "risks.key": "identifier",
    "controls.key": "identifier",
    "controls.risk_key": "identifier",
    "controls.type": "enum",
    "thresholds.id": "identifier",
    "thresholds.unit": "enum",
    "tests.key": "identifier",
    "tests.primitive": "enum",
    "tests.control_key": "identifier",
    "tests.risk_key": "identifier",
    "tests.assertion": "enum",
    # per-primitive params (identical meaning under every primitive branch)
    "tests.params.kind": "enum",
    "tests.params.population": "identifier",
    "tests.params.left_population": "identifier",
    "tests.params.right_population": "identifier",
    "tests.params.column": "identifier",
    "tests.params.amount_column": "identifier",
    "tests.params.date_column": "identifier",
    "tests.params.start_column": "identifier",
    "tests.params.end_column": "identifier",
    "tests.params.numerator_column": "identifier",
    "tests.params.denominator_column": "identifier",
    "tests.params.key_columns": "identifier",
    "tests.params.group_by": "identifier",
    "tests.params.group_keys": "identifier",
    "tests.params.left_keys": "identifier",
    "tests.params.right_keys": "identifier",
    "tests.params.carry_right_columns": "identifier",
    "tests.params.exclude_within": "identifier",
    "tests.params.allowed_values": "literal",
    "tests.params.direction": "enum",
    "tests.params.aggregate": "enum",
    "tests.params.mode": "enum",
    "tests.params.match": "enum",
    "tests.params.numerator_aggregate": "enum",
    "tests.params.condition": "enum",
    "tests.params.value": "literal",
    "tests.params.limit.threshold": "identifier",
    "tests.params.threshold.threshold": "identifier",
    "tests.params.window_days.threshold": "identifier",
    "tests.params.aggregate_threshold.threshold": "identifier",
    "tests.params.max_line.threshold": "identifier",
    "tests.params.exclude.column": "identifier",
    "tests.params.exclude.op": "enum",
    "tests.params.exclude.value": "literal",
    "tests.params.metrics.name": "identifier",
    "tests.params.metrics.kind": "enum",
    "tests.params.metrics.column": "identifier",
    "tests.params.metrics.key": "identifier",
    "tests.params.metrics.unit": "enum",
    "tests.params.metrics.where.column": "identifier",
    "tests.params.metrics.where.op": "enum",
    "tests.params.metrics.where.value": "literal",
    "findings.key": "identifier",
    "findings.test_key": "identifier",
    "findings.severity.then": "enum",
    "findings.metrics_cited": "identifier",
    "findings.thresholds_cited": "identifier",
    "findings.monetary_basis": "enum",
    "data_gaps.affects_test_keys": "identifier",
    "schema_version": "enum",
}

STRING_FIELD_CLASSIFICATION: dict[str, str] = {
    **{p: "prose" for p in PROSE_FIELDS},
    **{p: "expression" for p in EXPRESSION_FIELDS},
    **_OTHER_FIELD_CLASSIFICATION,
}


def _unwrap(schema: dict) -> dict:
    """Strips a `_nullable()` wrapper (a `type` list containing "null", or
    an `enum` list containing None) back to the underlying leaf/object
    schema, so the walker sees one canonical shape per field regardless of
    whether it was optional."""
    if not isinstance(schema, dict):
        return schema
    t = schema.get("type")
    if isinstance(t, list) and "null" in t:
        rest = [x for x in t if x != "null"]
        schema = {**schema, "type": rest[0] if len(rest) == 1 else rest}
    if "enum" in schema and None in schema["enum"]:
        schema = {**schema, "enum": [v for v in schema["enum"] if v is not None]}
    return schema


def walk_string_leaves(schema: dict = PLAN_PROPOSAL_SCHEMA, *, _defs: dict | None = None,
                        _path: str = "", _depth: int = 0, _seen: set | None = None) -> dict[str, dict]:
    """Every string-typed leaf reachable from `schema`, as {dotted_path:
    the_leaf_schema_dict} -- "string-typed" includes an array whose items
    are themselves string leaves (e.g. `assumptions`, `metrics_cited`),
    which are leaves in their own right for classification purposes, not a
    container to recurse past. `$ref`/anyOf/oneOf are resolved
    transparently; recursion depth is capped (the population Filter $def is
    the only genuinely recursive shape, and its leaf field set does not
    change with depth) so a pathological schema cannot hang this walk."""
    defs = _defs if _defs is not None else schema.get("$defs", {})
    seen = _seen if _seen is not None else set()
    out: dict[str, dict] = {}
    if _depth > 25:
        return out

    node = schema
    if "$ref" in node:
        ref = node["$ref"]
        name = ref.rsplit("/", 1)[-1]
        # The population Filter is genuinely self-referential (any_of/
        # all_of nest more Filters). "any_of"/"all_of" are structural
        # wrapper property names, path-transparent below, so a repeat visit
        # to the SAME $ref at the SAME path is exactly the boolean-nesting
        # cycle closing, not a new set of leaf fields -- dedup on (ref,
        # path) rather than depth alone, so recursion terminates as soon as
        # every reachable leaf shape has been seen once.
        key = (name, _path)
        if key in seen:
            return out
        seen.add(key)
        node = defs[name]

    node = _unwrap(node)

    if "anyOf" in node:
        for branch in node["anyOf"]:
            out.update(walk_string_leaves(branch, _defs=defs, _path=_path, _depth=_depth + 1, _seen=seen))
        return out

    if node.get("type") == "object" and "properties" in node:
        for name, sub in node["properties"].items():
            # "any_of"/"all_of" are structural wrappers around more Filters
            # (the SAME leaf field set: column/op/value/between_audit_
            # period.column), not a distinct semantic path segment -- kept
            # path-transparent so the walk converges instead of growing a
            # new path for every level of boolean nesting the schema
            # itself allows.
            child_path = _path if name in ("any_of", "all_of") else (f"{_path}.{name}" if _path else name)
            out.update(walk_string_leaves(sub, _defs=defs, _path=child_path, _depth=_depth + 1, _seen=seen))
        return out

    node_type = node.get("type")
    type_list = node_type if isinstance(node_type, list) else ([node_type] if node_type else [])

    if node_type == "array":
        items = node.get("items", {})
        item_node = _unwrap(items)
        item_type = item_node.get("type")
        item_type_list = item_type if isinstance(item_type, list) else ([item_type] if item_type else [])
        if "$ref" in item_node or "anyOf" in item_node or (
            item_type == "object" and "properties" in item_node
        ):
            out.update(walk_string_leaves(items, _defs=defs, _path=_path, _depth=_depth + 1, _seen=seen))
            return out
        # array of scalar leaves (e.g. `assumptions: [string]`) -- a leaf in
        # its own right, at the array's own path.
        if "string" in item_type_list:
            if _path:
                out[_path] = node
            return out
        return out

    # A leaf whose `type` includes "string" -- whether it is exactly
    # "string" or a multi-type union (e.g. FILTER_VALUE's
    # ["string","number","array","null"], ANY_SCALAR) -- is still a string
    # leaf for classification purposes: the planner CAN put free text there,
    # so it must be accounted for the same way a plain string property is.
    if "string" in type_list:
        if _path:
            out[_path] = node
        return out

    return out


def normalize_wire_proposal(wire: dict) -> dict:
    """BUG-EXPLORER-PLAN-2 (independent review round 5, RUN-99373993B1E0):
    hoists a `findings` array nested inside a `tests[]` item up to the
    top-level `findings[]` PLAN_PROPOSAL_SCHEMA actually requires, setting
    each hoisted finding's `test_key` to its owning test's `key` when not
    already present. This is a tolerant RECOVERY for a proposal a planner
    call already produced with the wrong (but self-evidently intentional
    -- every test carried its own findings) nesting; it never invents a
    finding, only relocates one the model already wrote in full. Returns a
    NEW dict (`wire` and its nested objects are never mutated); returns
    `wire` unchanged when no test carries a nested `findings` key so a
    proposal that never needed recovery is never copied for nothing."""
    tests = wire.get("tests") if isinstance(wire, dict) else None
    if not isinstance(tests, list) or not any(isinstance(t, dict) and "findings" in t for t in tests):
        return wire
    new_tests = []
    hoisted: list[dict] = []
    for t in tests:
        if isinstance(t, dict) and "findings" in t:
            t = dict(t)
            nested = t.pop("findings")
            if isinstance(nested, list):
                for f in nested:
                    if isinstance(f, dict):
                        f = dict(f)
                        f.setdefault("test_key", t.get("key"))
                        hoisted.append(f)
        new_tests.append(t)
    out = dict(wire)
    out["tests"] = new_tests
    out["findings"] = [*(wire.get("findings") or []), *hoisted]
    return out


__all__ = [
    "PLAN_PROPOSAL_SCHEMA", "WIRE_SCHEMA_SHA256", "PRIMITIVE_PARAM_SCHEMAS",
    "EXPLORER_PARAM_ALLOWLIST", "PROSE_FIELDS", "EXPRESSION_FIELDS",
    "STRING_FIELD_CLASSIFICATION", "walk_string_leaves", "normalize_wire_proposal",
]
