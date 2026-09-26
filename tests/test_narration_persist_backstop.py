"""BUG-R5-1/BUG-P2-3 backstop (independent review 2026-09-26): `_persist`
must never store `origin='model'/'model_repaired'` text that does not
actually render against the `table` it is persisting alongside, OR that
fails any field-scoped rule (the length cap chief among them) `validate_fn`
already checked moments ago -- regardless of what that earlier check
decided. This is defence in depth for the root cause fixed in
`build_profile_payload` (tests/test_narration_payloads.py's
`test_build_profile_payload_slug_disambiguation_is_independent_of_dict_
order`): any future path where the validated table/text and the persisted
one diverge must degrade to the labelled fallback, never crash and never
store an unfilled `{class:name}` placeholder or an over-length paragraph
(live test 2.4: a stored `question` at 332 characters, over the
250-character cap). `test_persist_downgrades_every_validator_field_when_
over_its_length_cap` below exercises every (target_kind, field) pair
`VALIDATOR_FIELD_FOR` maps -- every narration task and field, not only the
one BUG-R5-1 happened to surface on."""

from __future__ import annotations

import pytest

from orchestrator.narration.lexicon import FIELD_LENGTH_CAPS
from orchestrator.narration.placeholders import PlaceholderEntry
from orchestrator.narration.runner import VALIDATOR_FIELD_FOR, RunnerContext, _persist


class _FakePersistence:
    def __init__(self):
        self.rows: list[dict] = []

    def upsert_narrative(self, row: dict) -> None:
        self.rows.append(row)


def _rc(persistence: _FakePersistence) -> RunnerContext:
    return RunnerContext(
        gateway=None, prompts=None, persistence=persistence, clock=lambda: "2026-09-26T00:00:00Z",
        run_id="RUN-TEST", engagement_id=None, actor="tester", generation=0, pii_columns_masked=[],
    )


def test_persist_downgrades_to_fallback_invalid_when_table_does_not_cover_the_text():
    persistence = _FakePersistence()
    rc = _rc(persistence)
    table = {"rows_expense_report": PlaceholderEntry(name="rows_expense_report", unit="count", value=10)}
    text = "There are {count:nulls_expense_report_vendor} nulls."

    nid = _persist(
        rc, target_kind="profile", target_id="run", field="profile", origin="model_repaired", table=table,
        text=text, call_ids=["call-1"], served_model_version="v1", violations_payload=None,
    )

    assert len(persistence.rows) == 1
    row = persistence.rows[0]
    assert row["narrative_id"] == nid
    assert row["origin"] == "fallback_invalid"
    assert row["template_text"] is None
    assert row["sources"] == []
    assert row["updated_by"] == "system"
    assert any(v["rule_id"] == "N-G3" for v in row["violations"])
    assert rc.origin_counts.get("fallback_invalid") == 1


def test_persist_downgrades_a_valid_list_item_when_one_paragraph_does_not_render():
    persistence = _FakePersistence()
    rc = _rc(persistence)
    table = {"rows_expense_report": PlaceholderEntry(name="rows_expense_report", unit="count", value=10)}
    paragraphs = [
        "There are {count:rows_expense_report} rows.",
        "There are {count:nulls_expense_report_vendor} nulls.",
    ]

    _persist(
        rc, target_kind="profile", target_id="run", field="profile", origin="model_repaired", table=table,
        list_text=paragraphs, call_ids=["call-1"], served_model_version="v1", violations_payload=None,
    )

    row = persistence.rows[0]
    assert row["origin"] == "fallback_invalid"
    assert row["template_text"] is None


def test_persist_still_stores_valid_model_text_normally():
    persistence = _FakePersistence()
    rc = _rc(persistence)
    table = {"rows_expense_report": PlaceholderEntry(name="rows_expense_report", unit="count", value=10)}
    text = "There are {count:rows_expense_report} rows."

    _persist(
        rc, target_kind="profile", target_id="run", field="profile", origin="model", table=table,
        text=text, call_ids=["call-1"], served_model_version="v1", violations_payload=None,
    )

    row = persistence.rows[0]
    assert row["origin"] == "model"
    assert row["template_text"] == text
    assert row["sources"]
    assert rc.origin_counts.get("model") == 1


@pytest.mark.parametrize(("target_kind", "field"), sorted(VALIDATOR_FIELD_FOR))
def test_persist_downgrades_every_validator_field_when_over_its_length_cap(target_kind, field):
    """A field's own N-L1 cap is the exact shape live test 2.4 hit
    ('question' at 332 characters, over the 250-character cap, stored as
    model output): confirms `_persist`'s backstop catches it for EVERY
    (target_kind, field) `VALIDATOR_FIELD_FOR` maps, list-shaped fields
    included, not only the one bug happened to surface on."""
    persistence = _FakePersistence()
    rc = _rc(persistence)
    validator_field = VALIDATOR_FIELD_FOR[(target_kind, field)]
    cap = FIELD_LENGTH_CAPS[validator_field]
    over_length_text = "word " * (cap // len("word ") + 5)
    assert len(over_length_text) > cap
    is_list = field in ("management_questions", "review_observations", "profile", "exec_summary")

    _persist(
        rc, target_kind=target_kind, target_id="TARGET-1", field=field, origin="model_repaired", table={},
        text=None if is_list else over_length_text, list_text=[over_length_text] if is_list else None,
        call_ids=["call-1"], served_model_version="v1", violations_payload=None,
    )

    row = persistence.rows[0]
    assert row["origin"] == "fallback_invalid", (target_kind, field, row.get("violations"))
    assert row["template_text"] is None
    assert any(v["rule_id"] == "N-L1" for v in row["violations"])
