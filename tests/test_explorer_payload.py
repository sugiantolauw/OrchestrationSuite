"""orchestrator.explorer.payload's build_primitives_json/
build_reference_skills_json (docs/specs/P6_P8_explorer_llm_design.md §4.4).
Fully offline: no endpoint, no workspace."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.explorer.payload import (
    build_planner_payload,
    build_primitives_json,
    build_reference_skills_json,
)
from orchestrator.explorer.reference_skills import build_reference_skill_digest
from orchestrator.primitives import PRIMITIVES
from orchestrator.skills import load_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
TNE_SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"


def test_build_primitives_json_covers_every_primitive():
    entries = json.loads(build_primitives_json())
    assert {e["name"] for e in entries} == set(PRIMITIVES)


def test_build_primitives_json_has_purpose_and_params_schema_and_metric_kinds():
    entries = json.loads(build_primitives_json())
    for e in entries:
        assert isinstance(e["purpose"], str) and e["purpose"]
        assert e["params_schema"]["properties"]["kind"]["const"] == e["name"]
        assert set(e["metric_kinds"]) >= {"count", "sum", "excess", "pct_of_population"}


def test_build_primitives_json_is_deterministic():
    assert build_primitives_json() == build_primitives_json()


def test_build_reference_skills_json_empty_by_default():
    assert build_reference_skills_json() == "{}"
    assert build_reference_skills_json([]) == "{}"


def test_build_reference_skills_json_digests_a_real_repo_skill():
    skill = load_skill(TNE_SKILL_DIR)
    skill.validate()
    payload = json.loads(build_reference_skills_json([skill]))
    assert skill.skill_id in payload
    digest = payload[skill.skill_id]
    assert digest["id"] == skill.skill_id
    assert digest["content_hash"] == skill.content_hash
    assert digest["tests"], "digest should include at least one test"
    seen_primitives = {t["primitive"] for t in digest["tests"]}
    assert len(seen_primitives) == len(digest["tests"]), "one test per distinct primitive, first in plan order"


def test_reference_skill_digest_capped_at_30000_chars():
    skill = load_skill(TNE_SKILL_DIR)
    skill.validate()
    digest = build_reference_skill_digest(skill)
    canon = json.dumps(digest, sort_keys=True, separators=(",", ":"))
    assert len(canon) <= 30_000


def test_reference_skill_digest_never_leaks_reference_table_values():
    # §4.4: "{ref: ...} names only, never reference data values" -- a
    # reference table's own contents (e.g. the ExCo employee id list) must
    # never appear in the digest, only the {"ref": "<name>"} pointer.
    skill = load_skill(TNE_SKILL_DIR)
    skill.validate()
    digest = build_reference_skill_digest(skill)
    canon = json.dumps(digest)
    for ref_name, ref_value in skill.references.items():
        if isinstance(ref_value, list) and ref_value and isinstance(ref_value[0], (int, str)):
            for v in ref_value[:3]:
                if isinstance(v, str) and len(v) > 3:
                    assert v not in canon, f"reference {ref_name} value {v!r} leaked into the digest"


def test_build_planner_payload_uses_the_new_builders():
    skill = load_skill(TNE_SKILL_DIR)
    skill.validate()
    payload = build_planner_payload(
        objective="Assess T&E compliance", audit_period=("2026-01-01", "2026-06-30"),
        audit_timezone="Australia/Sydney", business_unit=None, materiality=None,
        profile_result={}, reference_skills=[skill],
    )
    primitives = json.loads(payload["primitives_json"])
    assert {e["name"] for e in primitives} == set(PRIMITIVES)
    reference_skills = json.loads(payload["reference_skills_json"])
    assert skill.skill_id in reference_skills
