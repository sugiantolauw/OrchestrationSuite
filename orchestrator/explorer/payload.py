"""build_planner_payload (docs/specs/P6_P8_explorer_llm_design.md §4.4):
assembles the values the Explorer planner_user.md template's `$name`
placeholders are rendered with. `profile_json` is built here from an
ALREADY PII-masked `profile_result` (orchestrator.explorer.profile.
profile_source, §4.3/§4.3.1) -- this module never re-derives or re-checks
PII status, it only serialises what it is given, so the G15 guarantee lives
in one place (profile_source), not two that could drift.

`primitives_json` and `reference_skills_json` are accepted as ALREADY
RENDERED strings (defaulting to `"{}"`) rather than built here: describing
the 8 primitives' wire param schemas and digesting reference Skills is
docs/specs/P6_P8_explorer_llm_design.md §4.4's later part, which needs the
Explorer wire schema (§4.5, a later work package) this step does not build.
A caller with nothing to say for either yet gets a well-formed, empty
payload rather than this function guessing at a shape."""

from __future__ import annotations

import json


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_planner_payload(
    *,
    objective: str,
    audit_period: tuple[str, str],
    audit_timezone: str,
    business_unit: str | None,
    materiality: float | None,
    profile_result: dict,
    primitives_json: str = "{}",
    reference_skills_json: str = "{}",
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
        "primitives_json": primitives_json,
        "reference_skills_json": reference_skills_json,
    }
