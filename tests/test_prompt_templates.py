"""orchestrator.llm.prompts.FilePromptRepository + the Explorer prompt
templates (docs/specs/P6_P8_explorer_llm_design.md §3.10, §6, §8.11). Fully
offline -- no endpoint, no workspace."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from string import Template

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
    assert rules.rstrip().endswith("unless those also appear in PROFILE.")


# ── TASK_PROFILES (§3.4) ────────────────────────────────────────────────────


_NARRATION_TASKS = {
    "profile", "find", "find_synthesis", "find_candidates",
    "prioritise", "act", "export_summary", "export_caption",
}


def test_task_profiles_cover_the_explorer_and_narration_tasks():
    # P6 WP N5 (docs/specs/P6_narration_design.md §4.1) added the eight
    # narration tasks alongside the two pre-existing Explorer ones.
    assert set(TASK_PROFILES) == {"plan_explorer", "plan_repair"} | _NARRATION_TASKS


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


# ── narration TASK_PROFILES (P6 WP N5, §4.1) ────────────────────────────────


def test_narration_task_profiles_fallback_agrees_with_fallback_role():
    # FALLBACK_ROLE (orchestrator.llm.tasks) is the registry LLMGateway.call()
    # actually reads; TaskProfile.fallback must say the same thing for every
    # narration task, never a second, driftable copy of the same fact.
    from orchestrator.llm.tasks import FALLBACK_ROLE

    for task in _NARRATION_TASKS:
        assert TASK_PROFILES[task].fallback == FALLBACK_ROLE.get(task), task


def test_narration_task_profiles_desired_params_have_temperature_zero():
    # §4.1's table: every narration task is deterministic prose generation
    # (temperature 0), never sampled.
    for task in _NARRATION_TASKS:
        assert TASK_PROFILES[task].desired_params.get("temperature") == 0, task


def test_export_caption_uses_gpt_oss_with_low_reasoning_effort():
    profile = TASK_PROFILES["export_caption"]
    assert profile.role == "model_gpt_oss"
    assert profile.desired_params.get("reasoning_effort") == "low"


# ── narration prompt templates (P6 WP N5, docs/specs/P6_narration_design.md
# §4.3): orchestrator/prompts/narration/ -- one system_common.md shared by
# every task, plus one <task>_user.md per task, plus a shared repair_user.md
# (§3.5's repair round reuses the SAME task and schema, so one repair
# template serves every task, never a per-task copy).
#
# FilePromptRepository (orchestrator.llm.prompts) is NOT reused here: its
# pairing assumes one <id>_system.md PER template id, which is not this
# shape -- wiring a production loader around "one shared system prompt,
# nine per-task user templates" is WP N6/N7's job (the `narrate` node
# runner, which also supplies `$generation_line`/`$payload_json` at call
# time). This file only proves the raw template FILES this WP delivers
# satisfy §4.3/§11 T-PT on their own, with the same plain
# `string.Template.substitute` mechanism §4.3 names -- no production code
# added for it. ─────────────────────────────────────────────────────────

NARRATION_PROMPTS_ROOT = Path(__file__).parent.parent / "orchestrator" / "prompts" / "narration"

# task key (orchestrator.config.NODE_MODELS / TASK_PROFILES) -> its own
# <task>_user.md filename (§4.3's table; `find` is `finding_user.md`, not
# `find_user.md` -- the template is named after what it writes, not the
# task key).
_NARRATION_USER_TEMPLATES = {
    "profile": "profile_user.md",
    "find": "finding_user.md",
    "find_synthesis": "synthesis_user.md",
    "find_candidates": "candidates_user.md",
    "prioritise": "priority_user.md",
    "act": "remediation_user.md",
    "export_summary": "exec_summary_user.md",
    "export_caption": "captions_user.md",
}


def _narration_files():
    return sorted(NARRATION_PROMPTS_ROOT.glob("*.md"))


def test_narration_prompt_files_exist_exactly_as_designed():
    names = {p.name for p in _narration_files()}
    assert names == {"system_common.md", "repair_user.md"} | set(_NARRATION_USER_TEMPLATES.values())


def test_narration_task_profiles_agree_with_the_template_filename_map():
    # Every TASK_PROFILES narration task has exactly one user template, and
    # vice versa -- no orphaned template, no task without one.
    assert set(_NARRATION_USER_TEMPLATES) == _NARRATION_TASKS


def test_narration_prompt_templates_contain_no_organisation_or_endpoint_names():
    # Reuses the same _FORBIDDEN pattern the Explorer templates are checked
    # against above (§6: "no organisation, workspace or endpoint names").
    offenders = []
    for path in _narration_files():
        text = path.read_text()
        for match in _FORBIDDEN.finditer(text):
            offenders.append(f"{path.name}: {match.group(0)!r}")
    assert not offenders, "forbidden patterns found in narration prompt templates:\n" + "\n".join(offenders)


def _render_narration_user(user_filename: str, **params) -> str:
    template = Template((NARRATION_PROMPTS_ROOT / user_filename).read_text())
    return template.substitute(**params)


def _narration_system_text() -> str:
    return (NARRATION_PROMPTS_ROOT / "system_common.md").read_text()


_NARRATION_BASE_PARAMS = {"generation_line": "", "payload_json": '{"placeholder":true}'}


def _params_for(user_filename: str) -> dict:
    params = dict(_NARRATION_BASE_PARAMS)
    if user_filename == "candidates_user.md":
        params["max_candidates"] = "3"
    return params


@pytest.mark.parametrize("user_filename", sorted(set(_NARRATION_USER_TEMPLATES.values())))
def test_narration_user_template_renders_with_every_placeholder_supplied(user_filename):
    text = _render_narration_user(user_filename, **_params_for(user_filename))
    assert "$" not in text  # no stray $placeholder left over (PromptTemplateError's own check)


def test_repair_user_template_renders_with_every_placeholder_supplied():
    text = _render_narration_user("repair_user.md", violations_json="[]", previous_output="{}")
    assert "$" not in text


@pytest.mark.parametrize("user_filename", sorted(set(_NARRATION_USER_TEMPLATES.values())))
def test_narration_user_template_missing_placeholder_raises(user_filename):
    params = _params_for(user_filename)
    del params["payload_json"]
    with pytest.raises(KeyError):
        _render_narration_user(user_filename, **params)


def test_repair_user_template_missing_placeholder_raises():
    with pytest.raises(KeyError):
        _render_narration_user("repair_user.md", violations_json="[]")


def test_narration_system_common_is_shared_verbatim_by_every_task():
    # There is exactly one system_common.md -- not a per-task copy -- so a
    # rule change there (e.g. a lexicon addition) touches every task's
    # prompt_sha256 identically, never nine independently-drifting copies.
    text = _narration_system_text()
    assert "PLACEHOLDERS" in text and "IDENTIFIERS" in text and "PAYLOAD" in text
    assert "$" not in text  # no placeholder of its own -- it never varies per call


def test_narration_user_templates_end_with_the_payload_section():
    # §4.3: "Each ends with PAYLOAD:\n$payload_json (canonical JSON)."
    for user_filename in set(_NARRATION_USER_TEMPLATES.values()):
        text = (NARRATION_PROMPTS_ROOT / user_filename).read_text()
        assert text.rstrip("\n").endswith("PAYLOAD:\n$payload_json"), user_filename


# ── T-PT (§11, §12 WP N5): the same data gives prompt bytes that are
# identical, and a golden prompt_sha256 per task. ───────────────────────────


def _hash_prompt(system_text: str, user_text: str) -> str:
    return hashlib.sha256((system_text + "\x00" + user_text).encode("utf-8")).hexdigest()


# One representative, hand-built payload per task -- not the real
# orchestrator.narration.payloads builder output (that shape is asserted by
# test_narration_payloads.py already); this only needs to be realistic
# enough to render every template branch and stay fixed, so the hash below
# is a genuine regression check on the TEMPLATE FILES' bytes, not on
# whatever payloads.py happens to produce today.
_GOLDEN_PAYLOADS: dict[str, dict] = {
    "profile": {
        "placeholders": [
            {"placeholder": "{count:rows_expense_report}", "rendered": "1,234",
             "meaning": "row count of source expense_report"},
            {"placeholder": "{count:nulls_expense_report_employee_id}", "rendered": "3",
             "meaning": "null count of column 'Employee ID' in source expense_report"},
        ]
    },
    "find": {
        "finding_key": "T4_1", "title": "Missing Receipt Documentation", "test_id": "T4.1",
        "test_name": "Missing Receipt Documentation",
        "control_objective": "Ensure claims carry supporting receipts before reimbursement.",
        "severity": "High", "severity_rule": "missing_receipt_pct > 10", "analyst_set_severity": True,
        "placeholders": [
            {"placeholder": "{count:missing_receipt_count}", "rendered": "12",
             "meaning": "count metric of test T4.1 (Missing Receipt Documentation), unit count"},
            {"placeholder": "{pct:missing_receipt_pct}", "rendered": "8.5%",
             "meaning": "pct metric of test T4.1 (Missing Receipt Documentation), unit %"},
        ],
        "template_observation": "12 claims (8.5% of total) are missing receipt documentation.",
        "template_recommendation": "Enforce mandatory receipt attachment before expense report submission.",
        "template_management_questions": ["What is the current policy for handling claims without receipts?"],
    },
    "find_synthesis": {
        "findings": [
            {"key": "T4_1", "title": "Missing Receipt Documentation", "test_id": "T4.1", "severity": "High",
             "severity_rule": "missing_receipt_pct > 10", "monetary_basis": "spend",
             "placeholders": [{"placeholder": "{count:missing_receipt_count}", "rendered": "12",
                                "meaning": "count metric of test T4.1"}]},
            {"key": "T5_1", "title": "Split Claims", "test_id": "T5.1", "severity": "Medium",
             "severity_rule": "split_claims_count > 0", "monetary_basis": "excess",
             "placeholders": [{"placeholder": "{count:split_claims_count}", "rendered": "3",
                                "meaning": "count metric of test T5.1"}]},
        ]
    },
    "find_candidates": {
        "tests": [
            {"test_id": "T6.1d", "name": "Daily Spend Over Limit", "control_objective": "Limit daily spend.",
             "status": "exception", "exception_units": 4,
             "metrics": [{"placeholder": "{money:daily_over_amount}", "rendered": "$450.00", "kind": "excess",
                           "covering_finding_keys": []}]},
        ],
        "rule_findings": [{"key": "T4_1", "title": "Missing Receipt Documentation",
                            "metrics_cited": ["missing_receipt_count"]}],
        "decided_candidates": [],
    },
    "prioritise": {
        "items": [
            {"key": "T4_1", "severity": "High", "title": "Missing Receipt Documentation",
             "placeholders": [{"placeholder": "{money:exposure_amount}", "rendered": "$6,000.00",
                                "meaning": "amount at risk"}]},
        ]
    },
    "act": {
        "items": [
            {"key": "T4_1", "title": "Missing Receipt Documentation",
             "effective_recommendation": "Enforce mandatory receipt attachment.",
             "placeholders": [{"placeholder": "{count:missing_receipt_count}", "rendered": "12",
                                "meaning": "count metric"}]},
        ]
    },
    "export_summary": {
        "placeholders": [
            {"placeholder": "{count:run_finding_count}", "rendered": "6",
             "meaning": "count of this run's findings"},
            {"placeholder": "{money:run_exposure_headline}", "rendered": "$12,340.00",
             "meaning": "the run's amount-at-risk headline"},
        ],
        "theme_titles": ["Documentation gaps", "Spend over limit"],
        "top_findings": [{"title": "Missing Receipt Documentation", "severity": "High"}],
    },
    "export_caption": {
        "charts": [
            {"chart_id": "chart_high", "what_it_plots": "High-severity findings by test",
             "placeholders": [{"placeholder": "{count:chart_high}", "rendered": "3",
                                "meaning": "count of High-severity findings"}]},
        ]
    },
}

# Recorded once by running the golden payloads above through the templates
# committed alongside this test (see the module docstring above this
# section) -- a deliberate template edit changes these, and the diff makes
# that visible rather than silent (§12 T-PT: "golden prompt_sha256 per task").
_GOLDEN_PROMPT_SHA256: dict[str, str] = {
    "profile": "69a093a88ada84a5c2244a06701698ea8b154e3dba38012d2203f0bac32ca3da",
    "find": "74ce514d5e7a2c21438c1e7d58d873aa7f16f55311da53c2cdf8409ecbf3f536",
    # Updated 2026-09-25 (quality review, category (c) prompt fix): synthesis_user.md's
    # IDENTIFIERS line now spells out that a test_id must be copied in full including any
    # suffix, and that a finding's `key` is never a valid identifier -- see
    # tests/test_narration_validate_quality_regression.py for the real-data regression this
    # closes (a bare "T6.1d" used instead of the run's actual "T6.1d_dom").
    "find_synthesis": "2234f45cc967cb3c2c58d311b895412ba48b9b4804aaac02deae6c7f7bfed98b",
    "find_candidates": "fd0c9c52b79eb138d98bcf25b5892f4d3eae15f1065cacd995a76e5bbb9c84ea",
    "prioritise": "328d869d1804fb1e4224150318f97a4c4472e3cb9fe0076d8dffe218248b6fce",
    "act": "e03ad52c8d57fcc357de8ff803f4b631b8da3e047705d3d96137aea9812e4589",
    "export_summary": "06c7450a34278ad46de168f8f33d0941b266d4bb30c31fc3f5b708543a931034",
    "export_caption": "b28ef06e79b429aac58720cc9d6358bd3b0d76f50f53f7d2ae50480c88771643",
}


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize("task", sorted(_NARRATION_USER_TEMPLATES))
def test_narration_prompt_same_data_gives_byte_identical_prompts(task):
    user_filename = _NARRATION_USER_TEMPLATES[task]
    params = {"generation_line": "", "payload_json": _canonical(_GOLDEN_PAYLOADS[task])}
    if task == "find_candidates":
        params["max_candidates"] = "3"
    text_a = _render_narration_user(user_filename, **params)
    text_b = _render_narration_user(user_filename, **params)
    assert text_a == text_b


@pytest.mark.parametrize("task", sorted(_NARRATION_USER_TEMPLATES))
def test_narration_prompt_matches_its_golden_sha256(task):
    user_filename = _NARRATION_USER_TEMPLATES[task]
    params = {"generation_line": "", "payload_json": _canonical(_GOLDEN_PAYLOADS[task])}
    if task == "find_candidates":
        params["max_candidates"] = "3"
    user_text = _render_narration_user(user_filename, **params)
    digest = _hash_prompt(_narration_system_text(), user_text)
    assert digest == _GOLDEN_PROMPT_SHA256[task], (
        f"{task}: prompt bytes changed (digest={digest!r}) -- update the golden hash only for a "
        f"deliberate template edit"
    )


def test_narration_prompt_generation_line_changes_bytes_but_stays_deterministic():
    # §1 #8 / §4.3: absent (empty) at generation 0 so first-generation
    # prompts are cache-stable; present at generation > 0, and itself
    # deterministic for a fixed generation number (regenerate's own cache
    # key, §6.3).
    payload_json = _canonical(_GOLDEN_PAYLOADS["find"])
    gen0 = _render_narration_user("finding_user.md", generation_line="", payload_json=payload_json)
    line = "Regeneration request: write a fresh version. Generation 1."
    gen1_a = _render_narration_user("finding_user.md", generation_line=line, payload_json=payload_json)
    gen1_b = _render_narration_user("finding_user.md", generation_line=line, payload_json=payload_json)
    assert gen0 != gen1_a
    assert gen1_a == gen1_b
