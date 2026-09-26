"""P7 review workflow UI (docs/specs/P7_mapping_authoring_design.md §3.8:
UI-R1 to UI-R6), all on /run/<id>'s awaiting_signoff block. Tested against
the fake backend (fake_service.py) the same way every other run_status.py
surface in this test suite is -- rendering functions called directly,
adapters.* calls driving state transitions between renders."""

from __future__ import annotations

from orchestrator.errors import ReviewActionRefused
from src import run_status
from src.platform import adapters


def _new_run():
    return adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
        review_plan_first=False,
    )


def _body_text(run_id: str) -> str:
    run = adapters.get_run(run_id)
    return str(run_status._render_body(run, run_id))


# ── UI-R1: review stage text ─────────────────────────────────────────────────


def test_ui_r1_review_stage_text_at_preparation():
    run_id = _new_run()
    text = _body_text(run_id)
    assert "Review stage: preparation" in text


def test_ui_r1_review_stage_text_awaiting_review():
    run_id = _new_run()
    adapters.prepare_findings(run_id, "alice")
    text = _body_text(run_id)
    assert "Prepared by alice" in text
    assert "awaiting review" in text


def test_ui_r1_review_stage_text_awaiting_signoff():
    run_id = _new_run()
    adapters.prepare_findings(run_id, "alice")
    adapters.mark_reviewed(run_id, "bob")
    text = _body_text(run_id)
    assert "Reviewed by bob" in text
    assert "awaiting sign-off" in text


# ── UI-R2: the three mutually-exclusive stage buttons ───────────────────────


def test_ui_r2_buttons_are_mutually_exclusive_across_stages():
    run_id = _new_run()

    text = _body_text(run_id)
    assert "run-mark-prepared-btn" in text
    assert "run-mark-reviewed-btn" not in text
    assert "run-signoff-open-btn" not in text

    adapters.prepare_findings(run_id, "alice")
    text = _body_text(run_id)
    assert "run-mark-prepared-btn" not in text
    assert "run-mark-reviewed-btn" in text
    assert "run-signoff-open-btn" not in text

    adapters.mark_reviewed(run_id, "bob")
    text = _body_text(run_id)
    assert "run-mark-prepared-btn" not in text
    assert "run-mark-reviewed-btn" not in text
    assert "run-signoff-open-btn" in text


# ── UI-R3: return to preparer row ────────────────────────────────────────────


def test_ui_r3_return_row_hidden_during_preparation():
    run_id = _new_run()
    text = _body_text(run_id)
    assert "run-return-btn" not in text


def test_ui_r3_return_row_shown_during_review_and_approval():
    run_id = _new_run()
    adapters.prepare_findings(run_id, "alice")
    text = _body_text(run_id)
    assert "run-return-btn" in text
    assert "run-return-reason-input" in text

    adapters.mark_reviewed(run_id, "bob")
    text = _body_text(run_id)
    assert "run-return-btn" in text


def test_ui_r3_return_resets_stage_and_findings():
    run_id = _new_run()
    adapters.prepare_findings(run_id, "alice")
    adapters.return_to_preparer(run_id, "bob", "please justify T1")
    run = adapters.get_run(run_id)
    assert run["review"]["stage"] == "preparation"
    text = _body_text(run_id)
    assert "run-mark-prepared-btn" in text


# ── UI-R4: review notes panel ────────────────────────────────────────────────


def test_ui_r4_no_notes_message():
    run_id = _new_run()
    text = _body_text(run_id)
    assert "Review notes" in text
    assert "No review notes on this run." in text


def test_ui_r4_raise_row_only_during_review_or_approval():
    run_id = _new_run()
    text = _body_text(run_id)
    assert "review-note-raise-btn" not in text

    adapters.prepare_findings(run_id, "alice")
    text = _body_text(run_id)
    assert "review-note-raise-btn" in text


def test_ui_r4_note_lifecycle_renders_chip_and_response():
    run_id = _new_run()
    adapters.prepare_findings(run_id, "alice")
    note = adapters.raise_review_note(run_id, "bob", "justify this", None)
    text = _body_text(run_id)
    assert "justify this" in text
    assert "Raised by bob" in text
    assert "Open" in text

    adapters.respond_to_review_note(run_id, note["note_id"], "alice", "because X")
    text = _body_text(run_id)
    assert "Response by alice: because X" in text
    assert "Responded" in text

    adapters.clear_review_note(run_id, note["note_id"], "bob")
    text = _body_text(run_id)
    assert "Cleared" in text


def test_ui_r4_note_target_shown_when_raised_on_a_finding():
    run_id = _new_run()
    adapters.prepare_findings(run_id, "alice")
    run = adapters.get_run(run_id)
    finding_id = run["findings"][0]["finding_id"]
    adapters.raise_review_note(run_id, "bob", "why this one", finding_id)
    text = _body_text(run_id)
    assert "on finding" in text


# ── UI-R5: completed sign-off text ───────────────────────────────────────────


def test_ui_r5_signoff_text_p7_gated():
    run_id = _new_run()
    adapters.prepare_findings(run_id, "alice")
    adapters.mark_reviewed(run_id, "bob")
    adapters.sign_off(run_id, "carol")
    run = adapters.get_run(run_id)
    text = run_status._signoff_text(run)
    assert text.startswith("Prepared by alice, reviewed by bob, signed off by carol at")


def test_ui_r5_signoff_text_legacy_unaffected():
    run_id = _new_run()
    adapters.sign_off(run_id, "auditor@example.com")
    run = adapters.get_run(run_id)
    text = run_status._signoff_text(run)
    assert text.startswith("Signed off by auditor@example.com at")
    assert "Prepared by" not in text


# ── UI-R6: refusal text through _error_panel ─────────────────────────────────


def test_ui_r6_error_panel_renders_the_refusal_reason_verbatim():
    exc = ReviewActionRefused("RUN-X", "reviewed", "You are not in a preparer/reviewer/approver group (audit-reviewers).")
    panel = run_status._error_panel(exc)
    text = str(panel)
    assert "Action blocked" in text
    assert "You are not in a preparer/reviewer/approver group (audit-reviewers)." in text
