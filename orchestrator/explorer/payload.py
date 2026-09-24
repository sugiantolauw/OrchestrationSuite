"""build_planner_payload (docs/specs/P6_P8_explorer_llm_design.md §4.4):
assembles the values the Explorer planner_user.md template's `$name`
placeholders are rendered with. `profile_json` is built here from an
ALREADY PII-masked `profile_result` (orchestrator.explorer.profile.
profile_source, §4.3/§4.3.1) -- this module never re-derives or re-checks
PII status, it only serialises what it is given, so the G15 guarantee lives
in one place (profile_source), not two that could drift.

`primitives_json` is a pure function of `PRIMITIVES` (orchestrator.
primitives) -- it names, purpose and wire params schema never vary per run,
so it is built here rather than accepted as a caller-supplied string.

`reference_skills_json` digests the given, ALREADY-LOADED reference Skills
(orchestrator.explorer.reference_skills.build_reference_skill_digest) --
WHICH Skills to pass is a run-setup decision (EXPLORER_REFERENCE_SKILL_IDS,
CLAUDE.md §11) belonging to a later work package; this module only turns
whatever it is given into the payload's rendered string. An empty list (the
default) is a legitimate call -- the first Explorer run in a fresh
environment has no reference Skill yet."""

from __future__ import annotations

import json

from orchestrator.explorer.reference_skills import build_reference_skills_digests
from orchestrator.explorer.wire_schema import PRIMITIVE_PARAM_SCHEMAS
from orchestrator.primitives import PRIMITIVES
from orchestrator.primitives.common import METRIC_KIND_DESCRIPTIONS
from orchestrator.skills import Skill


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_primitives_json() -> str:
    """§4.4: "for each of the 8 primitives, {name, purpose, params_schema:
    <wire params schema §4.5>, metric_kinds: {kind: meaning}}". One shared
    `metric_kinds` dict for every primitive -- see
    orchestrator.primitives.common.METRIC_KIND_DESCRIPTIONS's own
    docstring for why this is not filtered per primitive."""
    entries = [
        {
            "name": name,
            "purpose": PRIMITIVES[name].DESCRIPTION,
            "params_schema": PRIMITIVE_PARAM_SCHEMAS[name],
            "metric_kinds": dict(METRIC_KIND_DESCRIPTIONS),
        }
        for name in sorted(PRIMITIVES)
    ]
    return _canonical_json(entries)


def build_reference_skills_json(reference_skills: list[Skill] = ()) -> str:
    return _canonical_json(build_reference_skills_digests(list(reference_skills)))


def build_planner_payload(
    *,
    objective: str,
    audit_period: tuple[str, str],
    audit_timezone: str,
    business_unit: str | None,
    materiality: float | None,
    profile_result: dict,
    reference_skills: list[Skill] = (),
) -> dict:
    start, end = audit_period
    return {
        "objective": objective,
        "audit_period": f"{start} to {end} ({audit_timezone})",
        "business_unit": business_unit or "not specified",
        "materiality": (
            "not specified" if materiality is None
            else f"{materiality} (auditor-stated; any threshold you base on it is analyst-set)"
        ),
        "profile_json": _canonical_json(profile_result),
        "primitives_json": build_primitives_json(),
        "reference_skills_json": build_reference_skills_json(reference_skills),
    }
