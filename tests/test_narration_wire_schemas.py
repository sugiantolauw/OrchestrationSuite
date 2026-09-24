"""T-S1 (docs/specs/P6_narration_design.md §11, §12 WP N5): every narration
wire schema (orchestrator.narration.schemas) is strict-compatible -- valid
JSON Schema, every object closed and fully required, no `pattern`, no bare
`{"type": "object"}` property, no `oneOf` -- and every string leaf is
either a closed enum/const or one of the task's known free-text ("prose")
fields, never an unaccounted-for string a model could fill with anything.
Mirrors the linter `tests/test_explorer_wire_schema.py` already applies to
`orchestrator.explorer.wire_schema.PLAN_PROPOSAL_SCHEMA` (same rules,
re-stated here rather than imported, so this file has no dependency on the
Explorer package). Fully offline."""

from __future__ import annotations

import jsonschema
import pytest

from orchestrator.narration.schemas import (
    chart_captions_schema,
    exec_summary_schema,
    finding_candidates_schema,
    finding_narration_schema,
    finding_synthesis_schema,
    priority_rationale_schema,
    profile_narrative_schema,
    remediation_schema,
)

# ── (a) representative, per-call instances of every schema builder ─────────

_FINDING_KEYS = ["T4_1", "T5_1", "T6_1d"]
_METRIC_NAMES = ["missing_receipt_count", "missing_receipt_pct", "split_claims_count"]
_CHART_IDS = ["chart_high", "chart_medium", "chart_low"]

SCHEMAS: dict[str, dict] = {
    "finding-narration/1": finding_narration_schema("T4_1"),
    "finding-synthesis/1": finding_synthesis_schema(_FINDING_KEYS),
    "finding-candidates/1": finding_candidates_schema(_METRIC_NAMES, max_candidates=3),
    "priority-rationale/1": priority_rationale_schema(_FINDING_KEYS),
    "remediation/1": remediation_schema(_FINDING_KEYS),
    "exec-summary/1": exec_summary_schema(),
    "chart-captions/1": chart_captions_schema(_CHART_IDS),
    "profile-narrative/1": profile_narrative_schema(),
}

# Also cover the zero-key/zero-candidate degenerate shapes (a legitimate
# per-call instance: e.g. a run with no charts at all, or a
# find_candidates call the caller still built a schema for before
# discovering there is nothing to propose).
DEGENERATE_SCHEMAS: dict[str, dict] = {
    "finding-synthesis/1 (empty)": finding_synthesis_schema([]),
    "finding-candidates/1 (empty)": finding_candidates_schema([], max_candidates=0),
    "priority-rationale/1 (empty)": priority_rationale_schema([]),
    "chart-captions/1 (empty)": chart_captions_schema([]),
}

ALL_SCHEMAS = {**SCHEMAS, **DEGENERATE_SCHEMAS}


# ── (b) strict-mode structural linter (GPT-OSS limits, CLAUDE.md §6) ───────


def _lint_strict(schema, path="$"):
    if not isinstance(schema, dict):
        return
    assert "pattern" not in schema, f"{path}: 'pattern' is not allowed (GPT-OSS strict mode rejects it): {schema}"
    assert "oneOf" not in schema, f"{path}: 'oneOf' is not allowed (rewrite as anyOf): {schema}"

    if schema.get("type") == "object" or "properties" in schema:
        assert schema.get("additionalProperties") is False, f"{path}: object must be closed: {schema}"
        props = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        assert required == props, (
            f"{path}: every property must be required (no optional/nullable field in these "
            f"schemas): required={sorted(required)} != properties={sorted(props)}"
        )
        if not props:
            pytest.fail(f"{path}: bare {{'type': 'object'}} property with no properties at all")
        for name, sub in schema["properties"].items():
            _lint_strict(sub, f"{path}.{name}")
        return

    if schema.get("type") == "array":
        _lint_strict(schema.get("items", {}), f"{path}[]")


@pytest.mark.parametrize("name", sorted(ALL_SCHEMAS))
def test_schema_is_valid_jsonschema(name):
    jsonschema.Draft202012Validator.check_schema(ALL_SCHEMAS[name])


@pytest.mark.parametrize("name", sorted(ALL_SCHEMAS))
def test_schema_is_strict_compatible(name):
    _lint_strict(ALL_SCHEMAS[name])


# ── (c) every string leaf is an enum/const or a listed prose field ─────────
#
# Unlike the Explorer PlanProposal schema (many nested identifier/literal/
# expression kinds), every narration schema's string leaves are exactly one
# of two kinds: a closed set (enum/const -- schema_version, an item's own
# key, a severity, a chart id) or free prose the model writes (observation,
# recommendation, a theme's summary, ...). This walk asserts every string
# leaf is accounted for as one or the other -- a new field added to a
# schema without being added to PROSE_LEAVES fails here, not silently.

PROSE_LEAVES: dict[str, frozenset[str]] = {
    "finding-narration/1": frozenset({"observation", "recommendation", "management_questions"}),
    "finding-synthesis/1": frozenset({
        "themes.title", "themes.summary", "themes.root_cause_hypothesis", "themes.review_observations",
        "severity_proposals.reason",
    }),
    "finding-candidates/1": frozenset({
        "candidates.title", "candidates.severity_reason", "candidates.rationale",
        "candidates.observation", "candidates.recommendation", "candidates.management_questions",
    }),
    "priority-rationale/1": frozenset({"items.rationale"}),
    "remediation/1": frozenset({"items.remediation"}),
    "exec-summary/1": frozenset({"paragraphs"}),
    "chart-captions/1": frozenset({"captions.caption"}),
    "profile-narrative/1": frozenset({"paragraphs"}),
}


def _walk_string_leaves(schema, path="", out=None):
    out = {} if out is None else out
    if not isinstance(schema, dict):
        return out

    if "enum" in schema or "const" in schema:
        if path:
            out[path] = "closed"
        return out

    if schema.get("type") == "object" and "properties" in schema:
        for name, sub in schema["properties"].items():
            _walk_string_leaves(sub, f"{path}.{name}" if path else name, out)
        return out

    if schema.get("type") == "array":
        # A scalar-string array (e.g. management_questions, paragraphs) is a
        # leaf at the ARRAY's own path; an array of objects recurses into
        # its own properties at that same path; an array of ENUM strings
        # (metrics_cited, finding_keys) must be classified the same way a
        # bare enum property would be -- recursing uniformly through the
        # top-of-function enum/const check does that in one place, rather
        # than re-deciding "object vs string" here and silently mis-classing
        # an enum-of-strings array as free prose.
        _walk_string_leaves(schema.get("items", {}), path, out)
        return out

    if schema.get("type") == "string":
        if path:
            out[path] = "prose"
        return out

    return out


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_string_leaf_is_either_closed_or_a_listed_prose_field(name):
    leaves = _walk_string_leaves(SCHEMAS[name])
    prose_paths = {p for p, kind in leaves.items() if kind == "prose"}
    assert prose_paths == PROSE_LEAVES[name], (
        f"{name}: prose leaves {sorted(prose_paths)} != declared {sorted(PROSE_LEAVES[name])}"
    )
    # Every remaining leaf must be a closed enum/const -- true by
    # construction of _walk_string_leaves (a leaf is either "closed" or
    # "prose"), asserted here for readability rather than left implicit.
    assert set(leaves) - prose_paths == {p for p, kind in leaves.items() if kind == "closed"}


# ── (d) ids really are built fresh per call, never a fixed module-level set ─


def test_schema_ids_are_scoped_to_the_call_not_shared_across_calls():
    a = finding_synthesis_schema(["T4_1"])
    b = finding_synthesis_schema(["T9_9"])
    a_enum = a["properties"]["themes"]["items"]["properties"]["finding_keys"]["items"]["enum"]
    b_enum = b["properties"]["themes"]["items"]["properties"]["finding_keys"]["items"]["enum"]
    assert a_enum == ["T4_1"]
    assert b_enum == ["T9_9"]


def test_priority_rationale_min_items_equals_max_items_equals_key_count():
    schema = priority_rationale_schema(["A", "B", "C"])
    items_schema = schema["properties"]["items"]
    assert items_schema["minItems"] == items_schema["maxItems"] == 3


def test_chart_captions_key_enum_matches_the_given_chart_ids():
    schema = chart_captions_schema(["chart_a", "chart_b"])
    caption_item = schema["properties"]["captions"]["items"]
    assert caption_item["properties"]["chart_id"]["enum"] == ["chart_a", "chart_b"]
