"""orchestrator.llm.prompts.FilePromptRepository + the Explorer prompt
templates (docs/specs/P6_P8_explorer_llm_design.md §3.10, §6, §8.11). Fully
offline -- no endpoint, no workspace."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.config import NODE_MODELS
from orchestrator.llm.prompts import DEFAULT_PROMPTS_ROOT, FilePromptRepository, PromptTemplateError
from orchestrator.llm.tasks import TASK_PROFILES
from test_portability import PATTERN as _ORG_PATTERN

_TEMPLATE_IDS = ["explorer/planner", "explorer/repair"]

_PLANNER_PARAMS = dict(
    objective="Assess T&E compliance for FY26",
    audit_period="2026-01-01 to 2026-06-30 (Australia/Sydney)",
    business_unit="not specified",
    materiality="not specified",
    profile_json="{}",
    primitives_json="{}",
    reference_skills_json="{}",
)

_REPAIR_PARAMS = dict(
    objective="Assess T&E compliance for FY26",
    audit_period="2026-01-01 to 2026-06-30 (Australia/Sydney)",
    profile_json="{}",
    primitives_json="{}",
    previous_output="{}",
    violations_json="[]",
)

# Reuses test_portability.py's own PATTERN (never re-spelled here -- this
# file lives under tests/, which that scanner also walks) plus "databricks-"
# (§6: "They contain no organisation, workspace or endpoint names").
_FORBIDDEN = re.compile(_ORG_PATTERN.pattern + r"|databricks-", re.IGNORECASE)


def _template_files():
    return sorted(DEFAULT_PROMPTS_ROOT.glob("explorer/*.md"))


def test_all_four_template_files_exist():
    names = {p.name for p in _template_files()}
    assert names == {
        "planner_system.md", "planner_user.md", "repair_system.md", "repair_user.md",
    }


def test_templates_contain_no_organisation_or_endpoint_names():
    offenders = []
    for path in _template_files():
        text = path.read_text()
        for match in _FORBIDDEN.finditer(text):
            offenders.append(f"{path.name}: {match.group(0)!r}")
    assert not offenders, "forbidden patterns found in prompt templates:\n" + "\n".join(offenders)


def test_get_template_loads_system_and_user_text():
    repo = FilePromptRepository()
    planner = repo.get_template("explorer/planner")
    assert "PlanProposal" in planner.system
    assert planner.template_id == "explorer/planner"

    repair = repo.get_template("explorer/repair")
    assert "repair" in repair.system.lower()
    assert repair.version != planner.version


def test_get_template_version_changes_when_a_file_byte_changes(tmp_path):
    root = tmp_path / "prompts"
    (root / "explorer").mkdir(parents=True)
    (root / "explorer" / "planner_system.md").write_text("A")
    (root / "explorer" / "planner_user.md").write_text("B")
    repo = FilePromptRepository(root=root)
    v1 = repo.get_template("explorer/planner").version

    (root / "explorer" / "planner_system.md").write_text("A!")
    v2 = repo.get_template("explorer/planner").version
    assert v1 != v2


def test_get_template_missing_file_raises_named_error(tmp_path):
    repo = FilePromptRepository(root=tmp_path)
    with pytest.raises(PromptTemplateError):
        repo.get_template("explorer/planner")


def test_template_set_version_hashes_the_union_and_changes_on_any_file(tmp_path):
    root = tmp_path / "prompts"
    (root / "explorer").mkdir(parents=True)
    for name in ("planner_system", "planner_user", "repair_system", "repair_user"):
        (root / "explorer" / f"{name}.md").write_text(name)
    repo = FilePromptRepository(root=root)
    v1 = repo.template_set_version(_TEMPLATE_IDS)

    (root / "explorer" / "repair_user.md").write_text("repair_user!")
    v2 = repo.template_set_version(_TEMPLATE_IDS)
    assert v1 != v2

    # order-independent: same union, same hash
    assert repo.template_set_version(list(reversed(_TEMPLATE_IDS))) == v2


def test_render_planner_substitutes_every_placeholder():
    repo = FilePromptRepository()
    messages = repo.render("explorer/planner", **_PLANNER_PARAMS)
    assert [m["role"] for m in messages] == ["system", "user"]
    user_text = messages[1]["content"]
    for value in _PLANNER_PARAMS.values():
        assert value in user_text
    # no stray $placeholder left in either part
    assert "$" not in messages[0]["content"]
    assert "$" not in user_text


def test_render_raises_on_a_missing_placeholder():
    repo = FilePromptRepository()
    params = dict(_PLANNER_PARAMS)
    del params["materiality"]
    with pytest.raises(PromptTemplateError, match="materiality"):
        repo.render("explorer/planner", **params)


def test_render_repair_auto_fills_planner_rules_when_not_supplied():
    repo = FilePromptRepository()
    messages = repo.render("explorer/repair", **_REPAIR_PARAMS)
    system_text = messages[0]["content"]
    assert "Compose tests only from the primitives" in system_text
    assert "$planner_rules" not in system_text


def test_render_repair_respects_an_explicit_planner_rules_override():
    repo = FilePromptRepository()
    params = {**_REPAIR_PARAMS, "planner_rules": "OVERRIDDEN RULES TEXT"}
    messages = repo.render("explorer/repair", **params)
    assert "OVERRIDDEN RULES TEXT" in messages[0]["content"]
    assert "Compose tests only from the primitives" not in messages[0]["content"]


def test_planner_rules_text_is_the_numbered_rules_block():
    repo = FilePromptRepository()
    rules = repo.planner_rules_text()
    assert rules.startswith("1. Compose tests only from the primitives")
    assert rules.rstrip().endswith("At most fifteen tests.")


# ── TASK_PROFILES (§3.4) ────────────────────────────────────────────────────


def test_task_profiles_cover_the_two_explorer_tasks():
    assert set(TASK_PROFILES) == {"plan_explorer", "plan_repair"}


def test_task_profiles_role_agrees_with_node_models():
    for task, profile in TASK_PROFILES.items():
        assert profile.role == NODE_MODELS[task], (
            f"TASK_PROFILES[{task!r}].role={profile.role!r} disagrees with "
            f"NODE_MODELS[{task!r}]={NODE_MODELS[task]!r}"
        )


def test_plan_explorer_has_no_fallback():
    # CLAUDE.md §6: "never degrade plan" -- unavailable becomes the labelled
    # degraded-mode output, not a GPT-OSS call.
    assert TASK_PROFILES["plan_explorer"].fallback is None


def test_plan_repair_has_no_fallback():
    # §3.8: "no repair call" on planner unavailability, and no further
    # fallback when repair itself is unavailable -- the planner's proposal
    # stands as-is.
    assert TASK_PROFILES["plan_repair"].fallback is None


def test_task_profiles_are_structured_output():
    for task, profile in TASK_PROFILES.items():
        assert profile.structured_output is True, task
