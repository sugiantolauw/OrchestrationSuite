"""Round-4 narration-content fix, task item 1: a live find_synthesis
generate call (RUN-2B50AE494DE7, 2026-09-25) returned `finding_keys`
entries `'T6_1d_dom'` and `'T3_2a_air_dom'` -- neither in the schema's own
key enum -- because the prompt's round-3 wording ("copy the test_id suffix
verbatim") was ambiguous about which field it applied to, and the model
applied it to the schema's `key` field instead of to a `test_id` used in
prose. `orchestrator.llm.gateway._parse_and_validate` correctly rejected
both attempts (that machinery was never the bug); the fix is
`orchestrator/prompts/narration/synthesis_user.md`'s IDENTIFIERS section
plus `build_synthesis_payload`'s new `identifiers` field
(`orchestrator/narration/payloads.py`), which gives the model an explicit
`{key, test_ids}` pair per finding instead of two same-shaped fields with
only prose to distinguish them.

This test replays the exact recorded schema-validation errors against
`tests/fixtures/narration/live_round4_synthesis_key_confusion.json`'s
`failing_response` (proving the fixture reproduces the real failure, not a
different one), then proves a response with only those two positions
corrected validates cleanly -- both through the wire schema
(`orchestrator.llm.gateway._parse_and_validate`) and through the semantic
membership check (`orchestrator.narration.validate.validate_themes`)."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.llm.gateway import _parse_and_validate
from orchestrator.narration.schemas import finding_synthesis_schema
from orchestrator.narration.validate import validate_themes

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "narration" / "live_round4_synthesis_key_confusion.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text())

assert _FIXTURE["synthetic_recording"] is True, (
    "this fixture reconstructs a real recorded error around invented filler content -- "
    "see its own 'note' field; it is deliberately not flagged as a verbatim recording"
)


def _schema():
    return finding_synthesis_schema(_FIXTURE["valid_keys"])


def test_the_fixtures_failing_response_reproduces_both_recorded_schema_errors():
    """The literal error TEXT is what was recorded live (see the fixture's
    own docstring/note); this proves `failing_response` trips the schema at
    exactly those two, and only those two, points -- not some other shape
    of invalid document."""
    text = json.dumps(_FIXTURE["failing_response"], separators=(",", ":"))
    parsed, error = _parse_and_validate(text, _schema())
    assert parsed is None
    # jsonschema stops at the first error under `Draft*Validator.validate`;
    # re-run with `iter_errors` to confirm this is genuinely ONE of the two
    # recorded messages, not some third, unrelated failure.
    assert any(
        error == f"schema validation failed: {recorded}"
        for recorded in _FIXTURE["recorded_schema_errors"]
    ), f"unexpected schema error: {error!r}"


def test_every_invalid_key_position_individually_matches_a_recorded_error():
    """Belt-and-braces on the above: iterate every jsonschema error the
    failing document raises (not just the first) and confirm the set of
    messages is exactly the two recorded ones -- no more, no fewer."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(_schema())
    from orchestrator.llm.gateway import _describe_schema_error

    messages = {f"schema validation failed: {_describe_schema_error(e)}" for e in validator.iter_errors(_FIXTURE["failing_response"])}
    expected = {f"schema validation failed: {recorded}" for recorded in _FIXTURE["recorded_schema_errors"]}
    assert messages == expected


def test_the_corrected_response_passes_schema_validation():
    text = json.dumps(_FIXTURE["corrected_response"], separators=(",", ":"))
    parsed, error = _parse_and_validate(text, _schema())
    assert error is None
    assert parsed == _FIXTURE["corrected_response"]


def test_the_corrected_response_also_passes_the_semantic_membership_check():
    violations = validate_themes(
        _FIXTURE["corrected_response"]["themes"], valid_finding_keys=_FIXTURE["valid_keys"],
    )
    unknown_key_violations = [v for v in violations if "unknown finding key" in v.message]
    assert not unknown_key_violations, unknown_key_violations


def test_the_failing_response_also_trips_the_semantic_membership_check():
    """Defence in depth: even if a future schema change ever let an
    invented key like this through the wire schema, the semantic check
    (`orchestrator.narration.runner.narrate_synthesis`'s `validate_fn`)
    still catches it -- proving the fix is not relying on a single layer."""
    violations = validate_themes(
        _FIXTURE["failing_response"]["themes"], valid_finding_keys=_FIXTURE["valid_keys"],
    )
    unknown_key_violations = [v for v in violations if "unknown finding key" in v.message]
    assert len(unknown_key_violations) == 2
    joined = " ".join(v.message for v in unknown_key_violations)
    assert "T6_1d_dom" in joined
    assert "T3_2a_air_dom" in joined
