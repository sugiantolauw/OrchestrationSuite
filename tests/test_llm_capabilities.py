"""orchestrator.llm.capabilities (independent review 2026-09-24 item 3):
never send an untested parameter -- only a param recorded exactly
"supported" for a role is ever sent; everything else is withheld with a
reason."""

from __future__ import annotations

from orchestrator.explorer.wire_schema import PLAN_PROPOSAL_SCHEMA
from orchestrator.llm.capabilities import (
    filter_params,
    find_rejected_schema_keywords,
    load_capabilities,
    role_config,
    schema_keyword_allowed,
    schema_property_count,
    schema_strict_block_reason,
    served_model_matches,
)
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

_CAPS = {
    "roles": {
        "model_gpt_oss": {
            "verified_on": "2026-09-23",
            "served_model_prefix": "gpt-oss-120b",
            "params": {
                "max_tokens": "supported",
                "temperature": "supported",
                "json_schema_strict": "supported",
                "seed": "rejected",
                "json_object": "requires_json_word",
            },
            "schema_keywords_rejected": ["pattern"],
        },
        "model_sonnet": {
            "verified_on": None,
            "served_model_prefix": None,
            "params": {},
        },
    }
}


def test_load_capabilities_reads_the_real_shipped_file():
    caps = load_capabilities()
    assert "model_gpt_oss" in caps["roles"]
    assert "model_sonnet" in caps["roles"]
    assert caps["roles"]["model_gpt_oss"]["params"]["max_tokens"] == "supported"
    assert caps["roles"]["model_sonnet"]["params"] == {}


def test_filter_params_sends_only_supported():
    sent, dropped = filter_params(_CAPS, "model_gpt_oss", {"max_tokens": 100, "temperature": 0})
    assert sent == {"max_tokens": 100, "temperature": 0}
    assert dropped == {}


def test_filter_params_withholds_rejected_with_its_reason():
    sent, dropped = filter_params(_CAPS, "model_gpt_oss", {"max_tokens": 100, "seed": 7})
    assert sent == {"max_tokens": 100}
    assert dropped == {"seed": "rejected"}


def test_filter_params_withholds_unlisted_as_untested():
    sent, dropped = filter_params(_CAPS, "model_gpt_oss", {"top_k": 5})
    assert sent == {}
    assert dropped == {"top_k": "untested"}


def test_filter_params_withholds_everything_for_an_unverified_role():
    sent, dropped = filter_params(_CAPS, "model_sonnet", {"max_tokens": 100, "temperature": 0})
    assert sent == {}
    assert dropped == {"max_tokens": "untested", "temperature": "untested"}


def test_filter_params_for_a_role_absent_from_the_matrix_entirely():
    sent, dropped = filter_params(_CAPS, "model_unknown", {"max_tokens": 100})
    assert sent == {}
    assert dropped == {"max_tokens": "untested"}


def test_role_config_returns_untested_stand_in_for_unknown_role():
    cfg = role_config(_CAPS, "model_unknown")
    assert cfg["params"] == {}
    assert cfg["verified_on"] is None


def test_schema_keyword_allowed():
    assert not schema_keyword_allowed(_CAPS, "model_gpt_oss", "pattern")
    assert schema_keyword_allowed(_CAPS, "model_gpt_oss", "enum")


def test_served_model_matches_prefix():
    assert served_model_matches(_CAPS, "model_gpt_oss", "gpt-oss-120b-080525")
    assert not served_model_matches(_CAPS, "model_gpt_oss", "claude-sonnet-5-somethingelse")


def test_served_model_matches_true_when_never_verified():
    # Nothing to mismatch against yet -- an unverified role's params are
    # already empty, so served_model_matches itself stays permissive.
    assert served_model_matches(_CAPS, "model_sonnet", "anything-at-all")


# ── schema_property_count / schema_strict_block_reason (2026-09-25 live bug:
# `plan_repair` sent PLAN_PROPOSAL_SCHEMA as a strict response_format and
# GPT-OSS rejected it with "schema has too many properties maximum allowed
# is 128" -- gate this offline for every schema this codebase can hand to a
# GPT-OSS-configured role's LLMGateway.call(schema=...), so a schema that
# grows past the endpoint's own limit fails a unit test, not a live run) ───


def test_schema_property_count_counts_every_properties_object_once_per_occurrence():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "$defs": {"x": {"type": "object", "properties": {"c": {"type": "string"}}}},
        "items": {"anyOf": [
            {"type": "object", "properties": {"d": {"type": "string"}, "e": {"type": "string"}}},
            {"$ref": "#/$defs/x"},
        ]},
    }
    # a,b (top) + c ($defs, once) + d,e (anyOf branch 1) = 5 -- the $ref
    # branch is not re-walked (it carries no "properties" key of its own),
    # matching "once per occurrence", not "once per $ref use".
    assert schema_property_count(schema) == 5


def test_schema_property_count_sums_anyof_branches_separately():
    # Each anyOf branch is its own object schema -- a planner "any one of
    # these 8 primitive param shapes" schema (orchestrator.explorer.
    # wire_schema._test_params_anyof) costs the sum of all 8, not the max
    # or the first.
    schema = {"anyOf": [
        {"type": "object", "properties": {"a": {}, "b": {}}},
        {"type": "object", "properties": {"c": {}}},
    ]}
    assert schema_property_count(schema) == 3


_GPT_OSS_MAX_SCHEMA_PROPERTIES = role_config(load_capabilities(), "model_gpt_oss")["max_schema_properties"]

# Every JSON Schema this codebase can pass as LLMGateway.call(schema=...)
# for a task whose role can resolve to a GPT-OSS-configured endpoint --
# `plan_explorer`/`plan_repair` (Explorer) and the eight P6 narration tasks
# (orchestrator.llm.tasks.TASK_PROFILES; the development-workspace override
# (CLAUDE.md §6) routes model_sonnet to the same endpoint as model_gpt_oss,
# and several narration tasks' FALLBACK_ROLE also lands on model_gpt_oss) --
# with representative arguments for the schema BUILDERS (array length/
# content never changes a schema's own property count, only its items'
# once). `known_over_budget=True` documents a schema this codebase already
# knows exceeds the endpoint's limit and therefore MUST NEVER be sent as a
# strict response_format -- LLMGateway falls back to schema-in-prompt +
# client-side validation for it (schema_strict_block_reason) instead.
_CANDIDATE_SCHEMAS: dict[str, tuple[dict, bool]] = {
    "PLAN_PROPOSAL_SCHEMA (plan_explorer/plan_repair)": (PLAN_PROPOSAL_SCHEMA, True),
    "finding_narration/1": (finding_narration_schema("T4_1"), False),
    "finding_synthesis/1": (finding_synthesis_schema(["T4_1", "T4_2"]), False),
    "finding_candidates/1": (finding_candidates_schema(["m1", "m2"], 5), False),
    "priority_rationale/1": (priority_rationale_schema(["T4_1", "T4_2"]), False),
    "remediation/1": (remediation_schema(["T4_1", "T4_2"]), False),
    "exec_summary/1": (exec_summary_schema(), False),
    "chart_captions/1": (chart_captions_schema(["chart_1", "chart_2"]), False),
    "profile_narrative/1": (profile_narrative_schema(), False),
}


def test_every_candidate_schema_is_classified_correctly_for_gpt_oss_strict_mode():
    caps = load_capabilities()
    for name, (schema, known_over_budget) in _CANDIDATE_SCHEMAS.items():
        count = schema_property_count(schema)
        blocked = schema_strict_block_reason(caps, "model_gpt_oss", schema) is not None
        if known_over_budget:
            assert count > _GPT_OSS_MAX_SCHEMA_PROPERTIES, (
                f"{name}: {count} properties no longer exceeds the recorded "
                f"limit of {_GPT_OSS_MAX_SCHEMA_PROPERTIES} -- drop it from "
                f"the known-over-budget list"
            )
            assert blocked, f"{name}: over budget but LLMGateway would still send it strict"
        else:
            assert count <= _GPT_OSS_MAX_SCHEMA_PROPERTIES, (
                f"{name}: {count} properties exceeds GPT-OSS's recorded limit of "
                f"{_GPT_OSS_MAX_SCHEMA_PROPERTIES} -- this schema can no longer be sent "
                f"as a strict response_format (the P6P8 explorer bug, 2026-09-25); mark it "
                f"known_over_budget or shrink it"
            )
            assert not blocked, f"{name}: under budget but LLMGateway would not send it strict"


def test_every_candidate_schema_uses_no_keyword_rejected_for_gpt_oss():
    caps = load_capabilities()
    for name, (schema, _known_over_budget) in _CANDIDATE_SCHEMAS.items():
        rejected = find_rejected_schema_keywords(caps, "model_gpt_oss", schema)
        assert rejected == [], f"{name}: uses GPT-OSS-rejected schema keyword(s) {rejected}"
