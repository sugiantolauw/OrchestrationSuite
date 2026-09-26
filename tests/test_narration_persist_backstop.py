"""BUG-R5-1 backstop (independent review 2026-09-26): `_persist` must never
store `origin='model'/'model_repaired'` text that does not actually render
against the `table` it is persisting alongside -- regardless of what
`validate_fn` decided moments earlier. This is defence in depth for the
root cause fixed in `build_profile_payload` (tests/test_narration_payloads.
py's `test_build_profile_payload_slug_disambiguation_is_independent_of_
dict_order`): any future path where the validated table and the persisted
table diverge must degrade to the labelled fallback, never crash and never
store an unfilled `{class:name}` placeholder."""

from __future__ import annotations

from orchestrator.narration.placeholders import PlaceholderEntry
from orchestrator.narration.runner import RunnerContext, _persist


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
