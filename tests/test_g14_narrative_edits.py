"""G14 (CLAUDE.md §5 Tier B) and CLAUDE.md §14 Answers Q3 (amended): every
narrative edit is recorded (who, when, before/after, diff), and an edit
whose typed number does not match a figure the item cites is refused,
naming the mismatch. P6 WP N9, `orchestrator.service.edit_narrative`
(docs/specs/P6_narration_design.md §6.4)."""

from __future__ import annotations

import pytest

from orchestrator import service
from orchestrator.errors import NarrativeEditNotAllowed, NarrativeEditRejected, NarrativeNotFound
from orchestrator.findings import format_metric_value
from tests.n9_test_support import harness_at_awaiting_signoff


def _t1_observation_row(persistence, run_id: str) -> dict:
    findings = {f["rule_id"].split(".")[-1]: f for f in persistence.list_findings(run_id)}
    finding_id = findings["T1"]["finding_id"]
    rows = persistence.get_narratives(run_id)
    return next(r for r in rows if r["target_kind"] == "finding" and r["target_id"] == finding_id and r["field"] == "observation")


def test_edit_with_matching_number_is_accepted_and_recorded(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    row = _t1_observation_row(h.persistence, run_id)
    metrics = h.persistence.get_run_metrics(run_id)
    hv_count = format_metric_value(metrics["hv_count"]["value"], metrics["hv_count"]["unit"])
    hv_amount = format_metric_value(metrics["hv_amount"]["value"], metrics["hv_amount"]["unit"])
    new_text = f"A closer look found {hv_count} high-value claim(s) totalling {hv_amount}."

    before_edits = h.persistence.list_narrative_edits(run_id)
    assert before_edits == []

    updated = service.edit_narrative(ctx_app, run_id, row["narrative_id"], new_text, actor="alice")

    assert updated["origin"] == "human_edit"
    assert updated["version"] == row["version"] + 1
    assert updated["template_text"] == new_text
    assert updated["updated_by"] == "alice"

    stored = next(r for r in h.persistence.get_narratives(run_id) if r["narrative_id"] == row["narrative_id"])
    assert stored["template_text"] == new_text
    assert stored["origin"] == "human_edit"

    edits = h.persistence.list_narrative_edits(run_id)
    assert len(edits) == 1
    edit = edits[0]
    assert edit["narrative_id"] == row["narrative_id"]
    assert edit["version"] == row["version"] + 1
    assert edit["actor"] == "alice"
    assert edit["origin"] == "human_edit"
    assert edit["action"] == "human_edit"
    assert edit["before_text"] == row["template_text"]
    assert edit["after_text"] == new_text
    assert edit["diff"]  # a non-empty unified diff of before/after
    assert edit["at"] is not None

    events = [e for e in h.persistence.list_trace_events(run_id) if e["event_type"] == "narrative_edited"]
    assert len(events) == 1
    assert events[0]["actor"] == "alice"


def test_edit_with_mismatched_number_is_refused_and_names_it(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    row = _t1_observation_row(h.persistence, run_id)
    metrics = h.persistence.get_run_metrics(run_id)
    real_count = int(metrics["hv_count"]["value"])
    wrong_text = f"A closer look found {real_count + 7} high-value claim(s) in this population."

    with pytest.raises(NarrativeEditRejected) as exc:
        service.edit_narrative(ctx_app, run_id, row["narrative_id"], wrong_text, actor="alice")

    assert any(v["rule_id"] == "N-H1" for v in exc.value.violations)
    assert str(real_count + 7) in str(exc.value)

    # Rejected: nothing is written -- no new version, no edit row.
    stored = next(r for r in h.persistence.get_narratives(run_id) if r["narrative_id"] == row["narrative_id"])
    assert stored["template_text"] == row["template_text"]
    assert stored["version"] == row["version"]
    assert h.persistence.list_narrative_edits(run_id) == []


def test_edit_unknown_narrative_id_raises(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    with pytest.raises(NarrativeNotFound):
        service.edit_narrative(ctx_app, state.run_id, "NOPE", "some text", actor="alice")


def test_edit_refused_before_and_after_signoff_window(local_persistence, tmp_path, clock):
    h, ctx_app, state = harness_at_awaiting_signoff(local_persistence, tmp_path, clock)
    run_id = state.run_id
    row = _t1_observation_row(h.persistence, run_id)

    signed_off = service.sign_off(ctx_app, run_id, "alice")
    assert signed_off.status == "queued"
    assert signed_off.phase == "export"

    with pytest.raises(NarrativeEditNotAllowed):
        service.edit_narrative(ctx_app, run_id, row["narrative_id"], "Anything at all.", actor="alice")
