"""Wire JSON Schemas for every run-time narration model call (P6 WP N5,
docs/specs/P6_narration_design.md §4.2).

Same strict-mode rules `orchestrator.explorer.wire_schema` follows, for the
same reason -- these are sent to GPT-OSS strict mode (CLAUDE.md §6's
recorded parameter matrix) whenever a role resolves there, and must pass
Sonnet's ordinary JSON Schema validation too:

  * every object is CLOSED (`additionalProperties: false`) with every
    property listed in `required` -- there is no optional property in any
    schema below that would need a `["<type>", "null"]` nullable union
    (`orchestrator.explorer.wire_schema._nullable`'s approach): every field
    the design lists is always present, even as an empty array (an empty
    `themes`/`candidates`/`severity_proposals` list is itself a legitimate,
    fully-typed answer -- "no themes" or "zero candidates" -- never an
    omitted key);
  * no `pattern` (GPT-OSS strict mode rejects it) and no bare
    `{"type": "object"}` property (comes back empty under strict mode);
  * no `oneOf` (rewritten as `anyOf` where a schema below needs a choice --
    none currently do);
  * every id a model must name back (a finding key, a chart id, a metric
    name) is an `enum` built FRESH per call from this run's own real key
    set, or a `const` for a single expected value -- never a free string a
    model could invent (the wire-schema half of CLAUDE.md non-negotiable 2:
    the model can only ever refer to something Python already knows about).

Each `schema_version` `const` matches docs/specs/P6_narration_design.md
§4.2's own naming (`finding-narration/1`, ...) so a stored `narratives` row
and its originating schema can always be matched up by eye.

These are schema BUILDERS, not fixed module-level constants, because most of
them close over this call's own key set (which findings/candidates/charts
exist this run) -- exactly the "ids are enums or consts built per call"
rule above. `orchestrator.narration.payloads` calls these when it builds
each task's `LLMGateway.call(schema=...)` argument; this module has no
dependency on `payloads`, `llm.gateway` or anything run-specific -- it only
turns key lists it is given into schema dicts."""

from __future__ import annotations

__all__ = [
    "finding_narration_schema",
    "finding_synthesis_schema",
    "finding_candidates_schema",
    "priority_rationale_schema",
    "remediation_schema",
    "exec_summary_schema",
    "chart_captions_schema",
    "profile_narrative_schema",
]

STR: dict = {"type": "string"}
_SEVERITY_ENUM = ["High", "Medium", "Low"]


def _obj(properties: dict) -> dict:
    # Every property is always required here -- see the module docstring:
    # nothing below needs `_nullable`'s ["type", "null"] widening, because
    # no field in any of these schemas is genuinely optional.
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _array(items: dict, *, min_items: int | None = None, max_items: int | None = None) -> dict:
    out: dict = {"type": "array", "items": items}
    if min_items is not None:
        out["minItems"] = min_items
    if max_items is not None:
        out["maxItems"] = max_items
    return out


def _enum(values: list[str]) -> dict:
    return {"type": "string", "enum": list(values)}


def _const(value: str) -> dict:
    return {"type": "string", "const": value}


# ── finding-narration/1 (task `find`: one call per rule finding) ───────────


def finding_narration_schema(finding_key: str) -> dict:
    return _obj({
        "schema_version": _const("finding-narration/1"),
        "finding_key": _const(finding_key),
        "observation": STR,
        "recommendation": STR,
        "management_questions": _array(STR, min_items=1, max_items=4),
    })


# ── finding-synthesis/1 (task `find_synthesis`: one call, themes across
# this run's findings) ──────────────────────────────────────────────────────


def _theme_item(finding_keys: list[str]) -> dict:
    return _obj({
        "title": STR,
        "summary": STR,
        "root_cause_hypothesis": STR,
        "finding_keys": _array(_enum(finding_keys), min_items=1),
        "review_observations": _array(STR, max_items=3),
    })


def _severity_proposal_item(finding_keys: list[str]) -> dict:
    return _obj({
        "finding_key": _enum(finding_keys),
        "proposed_severity": _enum(_SEVERITY_ENUM),
        "reason": STR,
    })


def finding_synthesis_schema(finding_keys: list[str]) -> dict:
    return _obj({
        "schema_version": _const("finding-synthesis/1"),
        "themes": _array(_theme_item(finding_keys), max_items=6),
        "severity_proposals": _array(_severity_proposal_item(finding_keys)),
    })


# ── finding-candidates/1 (task `find_candidates`: one call, at most
# NARRATION_MAX_CANDIDATES proposals) ───────────────────────────────────────


def _candidate_item(metric_names: list[str]) -> dict:
    return _obj({
        "title": STR,
        "metrics_cited": _array(_enum(metric_names), min_items=1, max_items=8),
        "proposed_severity": _enum(_SEVERITY_ENUM),
        "severity_reason": STR,
        "rationale": STR,
        "observation": STR,
        "recommendation": STR,
        "management_questions": _array(STR, min_items=1, max_items=4),
    })


def finding_candidates_schema(metric_names: list[str], max_candidates: int) -> dict:
    return _obj({
        "schema_version": _const("finding-candidates/1"),
        "candidates": _array(_candidate_item(metric_names), max_items=max_candidates),
    })


# ── priority-rationale/1 and remediation/1 (tasks `prioritise`/`act`: one
# call, one item per ordered finding/candidate key -- exact count, so
# minItems == maxItems == len(keys)) ────────────────────────────────────────


def _keyed_text_item(field_name: str, keys: list[str]) -> dict:
    return _obj({"key": _enum(keys), field_name: STR})


def priority_rationale_schema(keys: list[str]) -> dict:
    n = len(keys)
    return _obj({
        "schema_version": _const("priority-rationale/1"),
        "items": _array(_keyed_text_item("rationale", keys), min_items=n, max_items=n),
    })


def remediation_schema(keys: list[str]) -> dict:
    n = len(keys)
    return _obj({
        "schema_version": _const("remediation/1"),
        "items": _array(_keyed_text_item("remediation", keys), min_items=n, max_items=n),
    })


# ── exec-summary/1 (task `export_summary`: one call, skipped on a clean run
# -- CLAUDE.md G10) ──────────────────────────────────────────────────────────


def exec_summary_schema() -> dict:
    return _obj({
        "schema_version": _const("exec-summary/1"),
        "paragraphs": _array(STR, min_items=2, max_items=3),
    })


# ── chart-captions/1 (task `export_caption`: one call, one caption per
# chart -- exact count) ─────────────────────────────────────────────────────


def _caption_item(chart_ids: list[str]) -> dict:
    return _obj({"chart_id": _enum(chart_ids), "caption": STR})


def chart_captions_schema(chart_ids: list[str]) -> dict:
    n = len(chart_ids)
    return _obj({
        "schema_version": _const("chart-captions/1"),
        "captions": _array(_caption_item(chart_ids), min_items=n, max_items=n),
    })


# ── profile-narrative/1 (task `profile`: one call) ──────────────────────────


def profile_narrative_schema() -> dict:
    return _obj({
        "schema_version": _const("profile-narrative/1"),
        "paragraphs": _array(STR, min_items=1, max_items=2),
    })
