from __future__ import annotations

import flask
import pytest

import fake_service
from src import run_setup
from src.platform import adapters


def test_home_layout_has_the_prototypes_landing_page_ids():
    layout = run_setup.home_layout()
    ids = set()

    def walk(node):
        if node is None or isinstance(node, (str, int, float, list, tuple)):
            if isinstance(node, (list, tuple)):
                for n in node:
                    walk(n)
            return
        _id = getattr(node, "id", None)
        if isinstance(_id, str):
            ids.add(_id)
        children = getattr(node, "children", None)
        if children is not None:
            walk(children)

    walk(layout)
    for expected in (
        "data-search-input", "data-search-results", "file-upload-area", "uploaded-files-list",
        "mode-selector-row", "skill-cards-container", "playbook-skills-section", "explorer-section",
        "audit-objective", "audit-period", "audit-bu", "audit-materiality", "audit-options",
        "workflow-preview-container", "run-summary-preview", "start-run-btn",
    ):
        assert expected in ids, f"missing id {expected!r} from the prototype's landing page"


def test_auto_bind_uses_suggest_bindings_when_no_matching_upload():
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        bindings, missing = run_setup._auto_bind("SKILL-001")
    assert bindings.get("expense_report") == "test_catalog.tne_source.expense_report"
    # fake_service.suggest_bindings leaves attendee_validity unbound (None) --
    # a genuinely missing source is named, never silently dropped.
    assert missing == ["attendee_validity"]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def test_auto_bind_prefers_a_recent_exact_filename_match_upload_on_local_backend(monkeypatch):
    monkeypatch.setattr(adapters, "is_local_backend", lambda: True)
    monkeypatch.setattr(
        adapters, "list_uploaded_files",
        lambda engagement_id=None: [
            {"filename": "expense_report.csv", "status": "Ready", "volume_path": "/local/uploads/expense_report.csv",
             "uploaded_by": "auditor@example.com", "uploaded_at": _now_iso()},
        ],
    )
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        bindings, missing = run_setup._auto_bind("SKILL-001")
    assert bindings["expense_report"] == "/local/uploads/expense_report.csv"
    assert missing == ["attendee_validity"]


def test_auto_bind_prefers_the_governed_table_over_a_stale_upload(monkeypatch):
    """Independent review 2026-09-24 item 7: "governed tables win unless the
    upload was made in the current page session" -- approximated by recency
    (run_setup._is_recent_upload), since this app has no real session id to
    check an upload against. An upload from well outside that window no
    longer silently overrides a governed table of the same name."""
    monkeypatch.setattr(adapters, "is_local_backend", lambda: True)
    monkeypatch.setattr(
        adapters, "list_uploaded_files",
        lambda engagement_id=None: [
            {"filename": "expense_report.csv", "status": "Ready", "volume_path": "/local/uploads/expense_report.csv",
             "uploaded_by": "auditor@example.com", "uploaded_at": "2020-01-01T00:00:00Z"},
        ],
    )
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        bindings, missing = run_setup._auto_bind("SKILL-001")
    assert bindings["expense_report"] == "test_catalog.tne_source.expense_report"


def test_auto_bind_uses_a_stale_upload_when_there_is_no_governed_table_for_it(monkeypatch):
    """A stale upload only loses to a governed table of the SAME name -- when
    no governed table exists for a source at all, even an old Ready upload
    is still the right (and only) thing to bind."""
    monkeypatch.setattr(adapters, "is_local_backend", lambda: True)
    monkeypatch.setattr(
        adapters, "list_uploaded_files",
        lambda engagement_id=None: [
            {"filename": "attendee_validity.csv", "status": "Ready",
             "volume_path": "/local/uploads/attendee_validity.csv",
             "uploaded_by": "auditor@example.com", "uploaded_at": "2020-01-01T00:00:00Z"},
        ],
    )
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        bindings, missing = run_setup._auto_bind("SKILL-001")
    assert bindings["attendee_validity"] == "/local/uploads/attendee_validity.csv"
    assert "attendee_validity" not in missing


def test_auto_bind_ignores_another_users_upload(monkeypatch):
    """Independent review 2026-09-24 item 7: an upload made by a DIFFERENT
    user, even with an exact filename match, must never be silently bound
    into someone else's run."""
    monkeypatch.setattr(adapters, "is_local_backend", lambda: True)
    monkeypatch.setattr(
        adapters, "list_uploaded_files",
        lambda engagement_id=None: [
            {"filename": "expense_report.csv", "status": "Ready", "volume_path": "/local/uploads/expense_report.csv",
             "uploaded_by": "someone-else@example.com"},
        ],
    )
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        bindings, missing = run_setup._auto_bind("SKILL-001")
    # Falls through to the governed-table suggestion instead of the mismatched upload.
    assert bindings.get("expense_report") == "test_catalog.tne_source.expense_report"


def test_auto_bind_works_on_a_non_local_backend(monkeypatch):
    """Independent review 2026-09-24 item 7: upload matching used to be
    gated on is_local_backend() alone -- an artificial restriction, since
    VolumeUploadAwareDataSource reads an uploaded file the same way
    regardless of which backend serves contract sources."""
    monkeypatch.setattr(adapters, "is_local_backend", lambda: False)
    monkeypatch.setattr(
        adapters, "list_uploaded_files",
        lambda engagement_id=None: [
            {"filename": "expense_report.csv", "status": "Ready", "volume_path": "/Volumes/cat/schema/vol/expense_report.csv",
             "uploaded_by": "auditor@example.com", "uploaded_at": _now_iso()},
        ],
    )
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        bindings, missing = run_setup._auto_bind("SKILL-001")
    assert bindings["expense_report"] == "/Volumes/cat/schema/vol/expense_report.csv"


def test_start_audit_run_creates_an_awaiting_signoff_run():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
    )
    run = adapters.get_run(run_id)
    assert run["status"] == "awaiting_signoff"
    assert run["run_owner"] == "auditor@example.com"


_probe_app = flask.Flask(__name__)


def test_request_owner_uses_forwarded_header_when_present():
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        assert run_setup._request_owner() == "auditor@example.com"


def test_request_owner_falls_back_to_local_user_on_local_backend():
    with _probe_app.test_request_context("/"):
        assert run_setup._request_owner() == "local-user"


def test_request_owner_blocks_on_deployed_backend_with_no_identity_header(monkeypatch):
    """CLAUDE.md §9A.1, P2/P3 gate review item 7: the deployed backend must
    never silently record run_owner='local-user' for an unverified caller."""
    monkeypatch.setattr(fake_service, "ready", lambda ctx: {"ready": True, "backend": "uc", "detail": None})
    with _probe_app.test_request_context("/"):
        with pytest.raises(adapters.MissingIdentityHeader):
            run_setup._request_owner()


def test_start_audit_run_review_plan_first_awaits_confirmation():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
        review_plan_first=True,
    )
    run = adapters.get_run(run_id)
    assert run["status"] == "awaiting_confirmation"


def test_upload_and_list_uploaded_files_roundtrip():
    row = adapters.upload_audit_file("mini.csv", b"a,b\n1,2\n", "auditor@example.com")
    assert row["status"] == "Ready"
    files = adapters.list_uploaded_files()
    assert any(f["upload_id"] == row["upload_id"] for f in files)


def test_propose_plan_returns_the_real_node_sequence():
    plan = adapters.propose_plan({"mode": "playbook", "skill": adapters.get_skill("SKILL-001"), "sources_count": 3})
    assert plan["mock"] is False
    stage_names = [s["stage"] for s in plan["stages"]]
    assert stage_names == [
        "Source data", "Data quality & reconciliation", "Skill / Explorer plan",
        "Deterministic audit tests", "Exception classification", "Evidence-linked findings",
        "Insights & prioritisation", "Management actions", "Export & Jira preview",
    ]
