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


def test_render_body_awaiting_confirmation_shows_confirm_button():
    run_id = _new_run(review_plan_first=True)
    run = adapters.get_run(run_id)
    body = run_status._render_body(run, run_id)
    assert "run-confirm-plan-btn" in str(body)


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


def test_get_export_pptx_not_yet_available_is_a_clean_failure():
    run_id = _new_run()
    try:
        adapters.get_export(run_id, "pptx")
        raised = False
    except NotImplementedError:
        raised = True
    assert raised
