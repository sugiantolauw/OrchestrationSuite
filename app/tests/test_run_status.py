from __future__ import annotations

import flask
import pytest

import fake_service
from src import run_status
from src.platform import adapters


def _new_run(review_plan_first=False):
    return adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
        review_plan_first=review_plan_first,
    )


_probe_app = flask.Flask(__name__)


def test_request_actor_blocks_on_deployed_backend_with_no_identity_header(monkeypatch):
    """CLAUDE.md §9A.1, P2/P3 gate review item 7: confirm_plan/sign_off/resume
    must never silently record actor='local-user' for an unverified caller
    on the deployed backend."""
    monkeypatch.setattr(fake_service, "ready", lambda ctx: {"ready": True, "backend": "uc", "detail": None})
    with _probe_app.test_request_context("/"):
        with pytest.raises(adapters.MissingIdentityHeader):
            run_status._request_actor()


def test_render_body_unknown_run():
    body = run_status._render_body(None, "RUN-MISSING")
    assert "not found" in str(body).lower()


# ── Async run start (CLAUDE.md §11 "Run start opens the run page at once",
# 2026-09-25): while the `runs` row does not exist yet, /run/<id> reads
# pending_runs' own registry instead of get_run.

def test_run_and_narration_shows_the_existing_queued_state_while_pending():
    import threading

    from src import pending_runs

    release = threading.Event()
    pending_runs.start("RUN-PENDING000001", "dedupe-key-1", lambda: release.wait(timeout=5))
    try:
        run, narration = run_status._run_and_narration("RUN-PENDING000001")
        assert run["status"] == "queued"
        assert narration is None
        body = run_status._render_body(run, "RUN-PENDING000001", narration)
        text = str(body)
        # The SAME markup a real queued run renders (_render_body's own
        # status == "queued" branch) -- no new element or text.
        assert "Waiting to start" in text
        assert "Queued — waiting for an available run slot." in text
    finally:
        release.set()
        pending_runs._wait_until_settled("RUN-PENDING000001")


def test_run_and_narration_shows_the_existing_failed_state_on_pending_failure():
    from src import pending_runs

    def fn():
        raise ValueError("no binding for expense_report")

    pending_runs.start("RUN-PENDING000002", "dedupe-key-2", fn)
    pending_runs._wait_until_settled("RUN-PENDING000002")

    run, narration = run_status._run_and_narration("RUN-PENDING000002")
    assert run["status"] == "failed"
    body = run_status._render_body(run, "RUN-PENDING000002", narration)
    text = str(body)
    # The SAME markup a real failed run renders (_render_body's own
    # status == "failed" branch) -- NN14: the exception message, verbatim.
    assert "Run failed" in text
    assert "ValueError: no binding for expense_report" in text


def test_run_and_narration_falls_back_to_not_found_with_no_registry_entry():
    run, narration = run_status._run_and_narration("RUN-NEVER-REGISTERED")
    assert run is None
    assert narration is None
    body = run_status._render_body(run, "RUN-NEVER-REGISTERED", narration)
    assert "not found" in str(body).lower()


def test_run_and_narration_prefers_the_real_run_once_it_exists():
    """Once the background job has actually written the `runs` row,
    get_run_and_narration finds it directly -- the pending-registry
    fallback is never consulted, even if a stale entry for the same id is
    still sitting in the registry (belt-and-braces: pending_runs.start
    itself already drops a successful entry)."""
    from src import pending_runs

    run_id = _new_run()
    assert pending_runs.status(run_id) is None  # nothing registered for a
    # run created directly via adapters.start_audit_run, as this helper does

    run, narration = run_status._run_and_narration(run_id)
    assert run["status"] == "awaiting_signoff"


def test_render_body_awaiting_confirmation_shows_confirm_button():
    run_id = _new_run(review_plan_first=True)
    run = adapters.get_run(run_id)
    body = run_status._render_body(run, run_id)
    assert "run-confirm-plan-btn" in str(body)


# ── UI-M1 (D-P7-2, docs/specs/P7_mapping_authoring_design.md §1.4) ──────────

def test_run_inputs_lines_mapping():
    lines = run_status._run_inputs_lines(
        {"mappings": {"expense_report": {"Employee ID": "Emp No"}}, "not_supplied": {}, "parameters": {}}
    )
    assert lines == ["expense_report: column 'Emp No' used as Employee ID"]


def test_run_inputs_lines_parameter():
    lines = run_status._run_inputs_lines({
        "mappings": {}, "not_supplied": {},
        "parameters": {
            "population_of_interest": {
                "path": "/Volumes/x/y/exco_ids.csv", "sha256": "1a2b3c4d5e6f",
                "provenance": {"owner": "X", "as_of": "2026-07-01"},
            },
        },
    })
    assert lines == ["population_of_interest: exco_ids.csv (sha256 1a2b3c4d…, owner X, as of 2026-07-01)"]


def test_run_inputs_lines_not_supplied():
    lines = run_status._run_inputs_lines({
        "mappings": {}, "parameters": {},
        "not_supplied": {"booking_detail": {"reason": "reason", "affected_tests": ["T3.1b", "T3.3a"]}},
    })
    assert lines == ["booking_detail not supplied (reason) — T3.1b, T3.3a not testable"]


def test_run_inputs_lines_empty():
    assert run_status._run_inputs_lines(None) == []
    assert run_status._run_inputs_lines({"mappings": {}, "not_supplied": {}, "parameters": {}}) == []


def test_render_body_awaiting_confirmation_with_run_inputs_lists_them():
    run_id = _new_run(review_plan_first=True)
    run = adapters.get_run(run_id)
    run = dict(run)
    run["options"] = dict(run.get("options") or {})
    run["options"]["run_inputs"] = {
        "mappings": {}, "parameters": {},
        "not_supplied": {"booking_detail": {"reason": "no data", "affected_tests": ["T3.1b"]}},
    }
    body = run_status._render_body(run, run_id)
    text = str(body)
    assert "This run uses project-specific inputs, so the plan must be confirmed." in text
    assert "booking_detail not supplied (no data) — T3.1b not testable" in text
    assert "run-confirm-plan-btn" in text


def test_render_body_awaiting_confirmation_without_run_inputs_omits_the_list():
    run_id = _new_run(review_plan_first=True)
    run = adapters.get_run(run_id)
    body = run_status._render_body(run, run_id)
    assert "project-specific inputs" not in str(body)


def test_render_body_awaiting_signoff_shows_signoff_button():
    run_id = _new_run()
    run = adapters.get_run(run_id)
    body = run_status._render_body(run, run_id)
    assert "run-signoff-open-btn" in str(body)


def test_render_body_completed_shows_workspace_link_and_download():
    run_id = _new_run()
    adapters.sign_off(run_id, "auditor@example.com")
    run = adapters.get_run(run_id)
    body = run_status._render_body(run, run_id)
    text = str(body)
    assert f"/workspace/tne?run_id={run_id}" in text
    assert "run-download-xlsx-btn" in text


def test_confirm_plan_then_sign_off_transitions_state():
    run_id = _new_run(review_plan_first=True)
    assert adapters.get_run(run_id)["status"] == "awaiting_confirmation"

    adapters.confirm_plan(run_id, "auditor@example.com")
    assert adapters.get_run(run_id)["status"] == "awaiting_signoff"

    adapters.sign_off(run_id, "auditor@example.com")
    run = adapters.get_run(run_id)
    assert run["status"] == "completed"
    assert run["signoff"]["approver"] == "auditor@example.com"


def test_resume_moves_interrupted_run_forward():
    run_id = _new_run()
    ctx = adapters.get_context()
    ctx.runs[run_id]["status"] = "interrupted"
    adapters.resume_run(run_id, "auditor@example.com")
    assert adapters.get_run(run_id)["status"] == "completed"


def test_get_export_streams_bytes_for_a_known_run():
    run_id = _new_run()
    filename, blob = adapters.get_export(run_id, "xlsx")
    assert filename.endswith(".xlsx")
    assert isinstance(blob, (bytes, bytearray))


def test_get_export_pptx_streams_bytes_for_a_known_run():
    """CLAUDE.md §4.7: the PPTX pack ships alongside the XLSX workpaper."""
    run_id = _new_run()
    filename, blob = adapters.get_export(run_id, "pptx")
    assert filename.endswith(".pptx")
    assert isinstance(blob, (bytes, bytearray))


def test_get_export_unknown_kind_is_a_clean_failure():
    run_id = _new_run()
    try:
        adapters.get_export(run_id, "unknown-kind")
        raised = False
    except FileNotFoundError:
        raised = True
    assert raised


def test_poll_stops_at_terminal_and_gate_statuses():
    """Found-live cost review: an open /run/<id> tab must not keep polling
    get_run forever while sat on a gate only this page's own button (Confirm
    plan / Sign off findings / Resume) can clear -- those callbacks already
    re-render run-page-body directly on click, so nothing is missed by not
    polling meanwhile. "queued" and the plain "running" state are NOT in
    this set: those progress on their own and the page must keep polling."""
    assert run_status._TERMINAL_STATUSES == {
        "completed", "failed", "awaiting_confirmation", "awaiting_signoff", "interrupted",
    }
    assert "queued" not in run_status._TERMINAL_STATUSES
    assert "running" not in run_status._TERMINAL_STATUSES


def test_poll_stops_for_a_queued_run_from_a_different_deployment():
    """Independent review 2026-09-24 item 6: a queue_note means THIS
    deployment's executor will never claim this run (a code_revision
    mismatch) -- polling forever costs a query per interval for a status
    that will realistically never change from here."""
    run = {"status": "queued", "queue_note": "queued — created by a different deployment (code revision abc123456789)"}
    assert run_status._should_stop_polling(run, 1) is True


def test_poll_continues_for_an_ordinary_queued_run():
    run = {"status": "queued", "queue_note": None}
    assert run_status._should_stop_polling(run, 1) is False


def test_poll_continues_for_a_running_run_within_the_idle_cap():
    run = {"status": "running"}
    assert run_status._should_stop_polling(run, 5) is False


def test_poll_stops_after_the_max_idle_poll_duration():
    """An abandoned browser tab must not poll forever, whatever the status."""
    run = {"status": "running"}
    assert run_status._should_stop_polling(run, run_status._MAX_IDLE_INTERVALS) is True


def test_poll_stops_for_an_unknown_run_after_the_idle_cap():
    assert run_status._should_stop_polling(None, run_status._MAX_IDLE_INTERVALS) is True
    assert run_status._should_stop_polling(None, 1) is False


# ── §11 "Paused runs across a code deploy" / independent review 2026-09-24
# gap #11: the run-status area states "computed under X, exported under Y"
# only when the two differ -- text appended to the existing sign-off line,
# no other UI change. ───────────────────────────────────────────────────


def test_signoff_text_states_computed_and_exported_revisions_when_they_differ():
    run = {
        "signoff": {"approver": "auditor@example.com", "timestamp": "2026-09-24T00:00:00Z"},
        "computed_code_revision": "rev-old",
        "export_code_revision": "rev-new",
    }
    text = run_status._signoff_text(run)
    assert "Signed off by auditor@example.com" in text
    assert "computed under code revision rev-old, exported under rev-new" in text


def test_signoff_text_omits_the_revision_note_when_they_match():
    run = {
        "signoff": {"approver": "auditor@example.com", "timestamp": "2026-09-24T00:00:00Z"},
        "computed_code_revision": "rev-same",
        "export_code_revision": "rev-same",
    }
    text = run_status._signoff_text(run)
    assert "computed under" not in text


def test_signoff_text_omits_the_revision_note_when_absent():
    """A run that never exported under a different revision -- the common
    case -- carries no export_code_revision at all (CLAUDE.md NN14: never a
    fabricated value)."""
    run = {"signoff": {"approver": "auditor@example.com", "timestamp": "2026-09-24T00:00:00Z"}}
    text = run_status._signoff_text(run)
    assert "computed under" not in text
