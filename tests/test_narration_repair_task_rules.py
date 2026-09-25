"""Round-5 narration-content review, item 1: `orchestrator/prompts/narration/
repair_user.md` was one generic template shared by every narration task --
it listed the violations and the previous output, but carried none of the
task's OWN instructions. A live run's `find_synthesis` repair round showed
the consequence: the generate round correctly wrote dotted test ids in
prose ("T4.2"), the repair round -- answering some OTHER, unrelated
violation -- silently rewrote those same ids to the schema's bare `key`
form ("T4_2"), tripping N-D1 fourteen times and discarding the whole call
(RUN-05B9661A8D1A, 2026-09-25; see scratchpad/narration_review/
before_after.md's "Round 4" section, item under "Themes ... fell back
again"). The rewrite happened because `synthesis_user.md`'s own IDENTIFIERS
section -- the ONLY place that distinguishes the schema's `key` field from
the prose `test_id` -- was never shown to the model on the repair call.

Fix: `orchestrator.narration.prompts.NarrationPromptRepository.
task_rules_text` extracts the task's own `<task>_user.md` instructions
verbatim (its IDENTIFIERS section included) and `render_repair` fills a new
`$task_rules` placeholder in `repair_user.md` with them, for every task that
uses repair -- so the repair call is governed by the SAME rules as the call
being repaired, not a generic stand-in. This file proves both halves: the
repair PROMPT itself now carries the missing rule (`test_render_repair_*`
below, parametrized over every narration task), and a repair-round response
that follows the "change only what the violations require" instruction now
explicit in the prompt keeps the display ids through the full generate ->
validate -> repair -> validate loop (`test_a_fake_model_repair_...`)."""

from __future__ import annotations

import json

import pytest

from orchestrator.llm.gateway import LLMResult
from orchestrator.narration import runner
from orchestrator.narration.prompts import NarrationPromptRepository, TASK_USER_FILES
from orchestrator.narration.schemas import finding_synthesis_schema
from orchestrator.narration.validate import validate_prose, validate_themes


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── task_rules_text: the extraction itself ──────────────────────────────────


def test_task_rules_text_for_find_synthesis_carries_the_key_vs_test_ids_distinction():
    repo = NarrationPromptRepository()
    rules = repo.task_rules_text("find_synthesis")
    assert "never edit it, never add a suffix to it, and never write it in prose." in rules
    assert "test_ids" in rules and "IDENTIFIERS" in rules
    # It is the raw, un-rendered template -- never the trailing PAYLOAD
    # section, which `render_repair` supplies separately via
    # `previous_output`/`violations_json`, not a second copy of the payload.
    assert "PAYLOAD:" not in rules
    assert "$payload_json" not in rules
    # The per-call regeneration hint is not a rule; it is never left as a
    # dangling, un-substitutable literal inside the repair prompt.
    assert "$generation_line" not in rules


@pytest.mark.parametrize("task", sorted(TASK_USER_FILES))
def test_task_rules_text_is_never_empty_and_has_no_stray_placeholder(task):
    repo = NarrationPromptRepository()
    rules = repo.task_rules_text(task)
    assert rules.strip()
    if task != "find_candidates":
        assert "$" not in rules  # only candidates_user.md's own $max_candidates is expected


def test_task_rules_text_missing_marker_raises(tmp_path):
    (tmp_path / "system_common.md").write_text("sys")
    (tmp_path / "repair_user.md").write_text("$task_rules $violations_json $previous_output")
    (tmp_path / "profile_user.md").write_text("TASK: no payload marker here at all.\n")
    repo = NarrationPromptRepository(root=tmp_path)
    with pytest.raises(Exception):
        repo.task_rules_text("profile")


# ── render_repair: every task's repair prompt carries ITS OWN rules ────────


@pytest.mark.parametrize("task", sorted(TASK_USER_FILES))
def test_render_repair_includes_this_tasks_own_rules(task):
    repo = NarrationPromptRepository()
    extra = {"max_candidates": 3} if task == "find_candidates" else {}
    messages = repo.render_repair(
        task, violations_json="[]", previous_output="{}", **extra,
    )
    user_text = messages[1]["content"]
    first_line = repo.task_rules_text(task).splitlines()[0]
    assert first_line in user_text, f"{task}: repair prompt is missing its own task rules"
    assert "$task_rules" not in user_text
    assert "$" not in user_text


def test_render_repair_find_synthesis_prompt_carries_the_exact_rule_that_was_missing():
    # The precise sentence a model needed to see on the REPAIR call to avoid
    # the live regression -- this is what was absent before this fix.
    repo = NarrationPromptRepository()
    messages = repo.render_repair(
        "find_synthesis", violations_json='[{"rule_id":"N-Q1"}]', previous_output="{}",
    )
    user_text = messages[1]["content"]
    assert "never edit it, never add a suffix to it, and never write it in prose." in user_text
    assert 'goes ONLY in this schema\'s own JSON fields' in user_text


def test_render_repair_candidates_task_rules_substitutes_max_candidates_no_stray_dollar():
    repo = NarrationPromptRepository()
    messages = repo.render_repair(
        "find_candidates", violations_json="[]", previous_output="{}", max_candidates=5,
    )
    user_text = messages[1]["content"]
    assert "Propose at most 5 additional findings" in user_text
    assert "$" not in user_text


def test_render_repair_candidates_task_without_max_candidates_raises():
    repo = NarrationPromptRepository()
    with pytest.raises(Exception):
        repo.render_repair("find_candidates", violations_json="[]", previous_output="{}")


# ── end-to-end: a repair-round response that follows the now-visible rule
# keeps the display ids through the real generate/validate/repair loop ────


class _ScriptedGateway:
    """Returns each entry of `responses` in order, one per `.call()` --
    the deterministic stand-in for round-4's live model: its FIRST
    (generate) response is exactly the shape recorded live (dotted test ids
    correctly used in prose, but one unrelated vague-magnitude word that
    still needs a repair round); its SECOND (repair) response is what a
    model that had actually been shown `synthesis_user.md`'s IDENTIFIERS
    rule on the repair call -- this fix's own contribution -- would write:
    the flagged word fixed, every identifier left exactly as it was."""

    def __init__(self, responses: list[LLMResult]):
        self._responses = list(responses)
        self.messages_seen: list[list[dict]] = []

    def call(self, *, messages, **kwargs):
        self.messages_seen.append(messages)
        return self._responses.pop(0)


def _synthesis_ok_result(call_id: str, *, root_cause: str, summary: str) -> LLMResult:
    parsed = {
        "schema_version": "finding-synthesis/1",
        "themes": [
            {
                "title": "Entertainment and attendee gaps",
                "summary": summary,
                "root_cause_hypothesis": root_cause,
                "finding_keys": ["T4_2", "T6_1c"],
                "review_observations": [],
            }
        ],
        "severity_proposals": [],
    }
    return LLMResult("ok", _canonical(parsed), parsed, call_id, "live", "test-model-v1", None)


def _rc(gateway) -> runner.RunnerContext:
    return runner.RunnerContext(
        gateway=gateway, prompts=NarrationPromptRepository(), persistence=None,
        clock=lambda: "2026-01-01T00:00:00Z", run_id="RUN-X", engagement_id=None,
        actor="tester", generation=0, pii_columns_masked=[],
    )


_VALID_KEYS = ["T4_2", "T6_1c"]
_ALLOWED_IDENTIFIERS = frozenset({"T4.2", "T6.1c"})


def _validate_fn(parsed: dict) -> tuple[bool, list[dict]]:
    violations: list[dict] = []
    struct = validate_themes(parsed.get("themes", []), valid_finding_keys=_VALID_KEYS)
    violations += [{"rule_id": v.rule_id, "field": "themes", "excerpt": v.message[:80]} for v in struct]
    for theme in parsed.get("themes", []):
        for field_name in ("summary", "root_cause_hypothesis"):
            result = validate_prose(
                theme.get(field_name, ""), {}, field="theme_summary" if field_name == "summary" else "root_cause",
                allowed_identifiers=_ALLOWED_IDENTIFIERS,
            )
            violations += [
                {"rule_id": v.rule_id, "field": field_name, "excerpt": (v.text or v.message)[:80]}
                for v in result.violations
            ]
    return not violations, violations


_GOOD_ROOT_CAUSE = (
    "The test T4.2 reported a proportion of entertainment expense lines with no matching "
    "attendee record, and the test T6.1c identified attendee hierarchy validity exceptions; "
    "together these may indicate a gap in entertainment claim review."
)


def test_a_fake_model_repair_of_the_live_synthesis_case_keeps_display_ids():
    # Generate round: dotted ids correct in prose (as round-4's live
    # generate call actually was -- see before_after.md), but a vague
    # magnitude word forces a repair round for an UNRELATED reason.
    generate_result = _synthesis_ok_result(
        "cid-generate", root_cause=_GOOD_ROOT_CAUSE,
        summary="Widespread gaps exist in entertainment claim review across the tested population.",
    )
    # Repair round: the word is fixed; every identifier is left exactly as
    # it was, matching what repair_user.md now explicitly asks for.
    repair_result = _synthesis_ok_result(
        "cid-repair", root_cause=_GOOD_ROOT_CAUSE,
        summary="Entertainment claim review shows gaps across the tested population.",
    )
    gateway = _ScriptedGateway([generate_result, repair_result])
    rc = _rc(gateway)

    outcome = runner._generate_item(
        rc, task="find_synthesis", payload={"themes": "placeholder"},
        schema=finding_synthesis_schema(_VALID_KEYS), extra_params={}, validate_fn=_validate_fn,
    )

    assert outcome.origin == "model_repaired"
    root_cause = outcome.parsed["themes"][0]["root_cause_hypothesis"]
    assert "T4.2" in root_cause and "T6.1c" in root_cause
    assert "T4_2" not in root_cause and "T6_1c" not in root_cause

    # The repair call this outcome came from really did carry the rule that
    # was missing live -- not a coincidence of the fake model's own script.
    repair_messages = gateway.messages_seen[1]
    repair_user_text = repair_messages[1]["content"]
    assert "never edit it, never add a suffix to it, and never write it in prose." in repair_user_text
    assert "T4.2" in repair_user_text  # the previous (generate-round) output is threaded through verbatim


def test_without_the_fix_a_repair_response_that_rewrites_ids_to_the_bare_key_form_still_trips_n_d1():
    # Defence in depth / documents the failure shape this fix targets: even
    # if a repair round DID regress the ids (the live symptom), the
    # validator -- unrelated to this fix, already correct -- still catches
    # it. This is what actually happened live before the prompt carried the
    # rule; it is not what a compliant model should now do.
    bad_root_cause = _GOOD_ROOT_CAUSE.replace("T4.2", "T4_2").replace("T6.1c", "T6_1c")
    parsed = {
        "schema_version": "finding-synthesis/1",
        "themes": [
            {
                "title": "Entertainment and attendee gaps",
                "summary": "Entertainment claim review shows gaps across the tested population.",
                "root_cause_hypothesis": bad_root_cause,
                "finding_keys": ["T4_2", "T6_1c"],
                "review_observations": [],
            }
        ],
        "severity_proposals": [],
    }
    is_valid, violations = _validate_fn(parsed)
    assert not is_valid
    assert any(v["rule_id"] == "N-D1" for v in violations)
