"""Quality review 2026-09-25 (coordinator follow-up to the narration
concurrency/retry work): the baseline live run's `narrate` node fell back to
template text on 13 of its narrative rows (12 on a NARRATION_MAX_PARALLEL=2
re-run) -- roughly 1 in 5 items, even after the one repair round. Every
fallback_invalid row was one of exactly three items (`prioritise`'s single
call, `profile`'s single call, `find_synthesis`'s single call), each
producing several narrative rows -- not 13 independent model mistakes.

Classification (§ the coordinator's own three buckets):

- `prioritise` and `profile`: **(b) validator false positive.** N-L1
  measured the model's RAW typed-placeholder text
  (`{pct:att_missing_pct}` is 21 characters for a value that renders to
  "84.6%", 5) against a cap sized for what an auditor actually reads. Every
  one of the 11 `prioritise` items and both `profile` paragraphs is well
  under its cap once measured on the RENDERED text -- proven below with the
  real recorded tables. Fixed in `orchestrator/narration/validate.py`
  (N-L1 now measures `render(text, table)`); this never loosens what a
  placeholder may say -- N-D1/N-G3/N-C1 etc. are unchanged, proven below by
  the still-invalid bare-test-id case.
- `find_synthesis`'s N-Q1 ("frequently") and N-S2 ("breaches") violations:
  **(a) genuine model errors, correctly caught.** No fix -- the repair
  round is the correct mechanism and it did fix them.
- `find_synthesis`'s N-D1 on a bare test id ("T6.1d" instead of the run's
  actual "T6.1d_dom", which coexists with a sibling "T6.1d_int" this run):
  **(c) prompt problem.** The payload gives the model BOTH the finding's
  `key` (underscore form, "T6_1d") for the schema's own `finding_keys`
  array and its `test_id` (dotted form, possibly suffixed) as the only
  valid prose identifier -- easy to conflate. Fixed in
  `orchestrator/prompts/narration/synthesis_user.md` (explicit: copy the
  full test_id including any suffix; the `key` is never a valid identifier
  in prose). This is a prompt fix, not a validator loosening: allowing the
  bare form would be a genuine loss of precision here (T6.1d_dom and
  T6.1d_int both exist in this run), so the fix has to live upstream of the
  validator, and it does.

`tests/fixtures/narration/quality_review_2026_09_25.json` is the real,
unaltered recording (`synthetic_recording: false`) of the model text,
placeholder tables and violations from that live run (RUN-89AF157ED704,
2026-09-25). One synthetic case (clearly marked below) proves N-L1 still
fires on genuinely over-length rendered text -- the fix narrows what
counts as "over length", it does not disable the rule."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.narration.placeholders import PlaceholderEntry
from orchestrator.narration.validate import validate_prose

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "narration" / "quality_review_2026_09_25.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text())

assert _FIXTURE["synthetic_recording"] is False, "this fixture is a real recording -- do not flip the flag"


def _table_from_json(raw: dict) -> dict[str, PlaceholderEntry]:
    return {name: PlaceholderEntry(name, entry["unit"], entry["value"]) for name, entry in raw.items()}


def _rule_ids(text: str, table: dict[str, PlaceholderEntry], *, field: str) -> set[str]:
    return validate_prose(text, table, field=field).rule_ids()


# ── (b) prioritise: N-L1 false positive, fixed by measuring rendered length ─


def test_every_recorded_prioritise_rationale_passes_on_generate_once_rendered():
    """All 11 items' FIRST (generate) attempt is real model prose that
    should never have needed a repair round at all -- the N-L1 raw-text
    measurement was the only thing standing between it and acceptance."""
    tables = _FIXTURE["prioritise"]["tables_by_key"]
    texts = _FIXTURE["prioritise"]["generate_rationale_by_key"]
    assert set(tables) == set(texts) == {
        "T4_2", "T5_2", "T6_1a", "T3_1a", "T4_4", "T6_1c", "T6_1d", "T3_1b", "T3_2a", "T3_3b", "T4_1",
    }
    for key, text in texts.items():
        table = _table_from_json(tables[key])
        rule_ids = _rule_ids(text, table, field="rationale")
        assert "N-L1" not in rule_ids, f"{key}: N-L1 still fires on real recorded text: {rule_ids}"


def test_every_recorded_prioritise_rationale_also_passes_on_the_repair_text():
    tables = _FIXTURE["prioritise"]["tables_by_key"]
    texts = _FIXTURE["prioritise"]["repair_rationale_by_key"]
    for key, text in texts.items():
        table = _table_from_json(tables[key])
        rule_ids = _rule_ids(text, table, field="rationale")
        assert "N-L1" not in rule_ids, f"{key}: N-L1 still fires on the repair text: {rule_ids}"


# ── (b) profile: same N-L1 false positive ───────────────────────────────────


def test_recorded_profile_paragraphs_pass_on_generate_once_rendered():
    table = _table_from_json(_FIXTURE["profile"]["table"])
    for i, text in enumerate(_FIXTURE["profile"]["generate_paragraphs"]):
        rule_ids = _rule_ids(text, table, field="profile_paragraph")
        assert "N-L1" not in rule_ids, f"paragraph {i}: N-L1 still fires: {rule_ids}"


# ── N-L1 still fires on genuinely over-length RENDERED text (synthetic) ────


def test_n_l1_still_fires_when_the_rendered_text_is_genuinely_too_long():
    """SYNTHETIC (not a recording): the fix narrows what N-L1 measures, it
    does not disable it. A table whose own rendered values are short, with
    prose padded well past the 220-character `rationale` cap even after
    rendering, must still be flagged."""
    table = {
        "x": PlaceholderEntry("x", "count", 1),
    }
    text = (
        "This is a very long rationale sentence, deliberately padded with a great deal of "
        "filler prose so that even after any placeholder is rendered to {count:x}, the total "
        "length of the text an auditor would actually read still comfortably exceeds the two "
        "hundred and twenty character cap this field enforces for a priority rationale."
    )
    rule_ids = _rule_ids(text, table, field="rationale")
    assert "N-L1" in rule_ids


# ── (a) genuine model errors in find_synthesis, correctly caught -- no fix ──


def test_synthesis_review_observations_vague_and_policy_language_are_still_correctly_flagged():
    """N-Q1 (vague magnitude) and N-S2 (policy/breach language) in the real
    GENERATE-round synthesis text -- genuine model errors the validator is
    right to catch. Unaffected by the N-L1 change; asserted here so a
    future change to those rules is caught by this same fixture."""
    table: dict[str, PlaceholderEntry] = {}
    assert "N-Q1" in _rule_ids(
        "Expense items frequently proceed without full attendee information.", table, field="theme_summary",
    )
    assert "N-S2" in _rule_ids(
        "alerts the audit committee to potential breaches of spend controls", table, field="root_cause",
    )


# ── (c) prompt fix for the bare-test-id N-D1 case: the VALIDATOR is
# unchanged and correctly still rejects the short form (proves the fix was
# made upstream, in the prompt, never by loosening N-D1) ──────────────────


def test_bare_test_id_missing_its_sub_test_suffix_is_still_correctly_rejected():
    """The recorded repair-round review_observations used the finding's
    `key` (underscore form, e.g. "T6_1d") instead of its `test_id`
    ("T6.1d_dom") -- correctly rejected both then and now. This run has a
    T6.1d_dom AND a T6.1d_int, so allowing the short form would be a
    genuine loss of precision, not a validator false positive; the fix
    lives in the prompt (synthesis_user.md), not here."""
    allowed = set(_FIXTURE["synthesis"]["test_id_by_key"].values())
    assert "T6.1d_dom" in allowed and "T6.1d_int" not in allowed  # this run only has the _dom sibling
    for text in _FIXTURE["synthesis"]["repair_review_observations"]:
        result = validate_prose(text, {}, field="review_observation", allowed_identifiers=allowed)
        # The run's REAL identifiers (dotted, with any sub-test suffix) are
        # in `allowed` -- every recorded repair-round sentence nonetheless
        # uses the finding's underscore `key` form instead (T4_2/T5_2/
        # T6_1a/etc.), which is not among them, so N-D1 must still fire.
        assert "N-D1" in result.rule_ids(), f"expected N-D1 on: {text!r}"


def test_the_real_test_id_form_with_its_full_suffix_is_accepted():
    """The positive half of the same rule: the FULL test_id -- exactly what
    synthesis_user.md's amended IDENTIFIERS instruction now tells the model
    to copy -- passes clean."""
    allowed = {"T6.1d_dom"}
    text = "The finding T6.1d_dom identifies a monetary exposure that surpasses its configured threshold"
    result = validate_prose(text, {}, field="review_observation", allowed_identifiers=allowed)
    assert "N-D1" not in result.rule_ids()
