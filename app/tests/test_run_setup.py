from __future__ import annotations

import threading
import time

import dash
import flask
import pytest

import fake_service
from src import pending_runs, run_setup
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


# ── Async run start (CLAUDE.md §11 "Run start opens the run page at once",
# 2026-09-25): "Start audit analysis" pre-generates the run_id, submits the
# real adapters.start_audit_run(...) call to pending_runs' own background
# worker, and navigates to /run/<run_id> immediately. These tests invoke
# the REAL registered `start_run` Dash callback (found via app.callback_map,
# the same pattern test_explorer_ui.py uses) against the fake backend
# (app/tests/conftest.py's autouse fixture), so they exercise the exact
# closure app.py wires up, not a re-implementation of it.

def _make_landing_app():
    app = dash.Dash(__name__)
    app.layout = lambda: run_setup.home_layout()
    run_setup.register_callbacks(app)
    return app


def _find_callback(app, *, inputs):
    want = [{"id": i, "property": p} for i, p in inputs]
    for entry in app.callback_map.values():
        if entry.get("inputs") == want:
            return entry["callback"].__wrapped__
    available = [e.get("inputs") for e in app.callback_map.values()]
    raise AssertionError(f"no callback registered for inputs {want}; available: {available}")


def _start_run_fn():
    app = _make_landing_app()
    return _find_callback(app, inputs=[("start-run-btn", "n_clicks")])


def _call_start_run(fn, *, n_clicks=1):
    return fn(
        n_clicks, "Assess spend.", "2025-01-01", "2026-04-30", None, None,
        ["preview_plan", "gen_actions"], "SKILL-001", None,
    )


def _patch_full_bindings(monkeypatch):
    """SKILL-001's fake contract has a source (attendee_validity) fake_service's
    own suggest_bindings leaves unbound (test_auto_bind_uses_suggest_bindings_
    when_no_matching_upload above) -- irrelevant to what these async-start
    tests are checking, so _auto_bind is patched to a complete binding set
    instead of also crafting a matching upload fixture per test."""
    monkeypatch.setattr(
        run_setup, "_auto_bind",
        lambda skill_id: (
            {"expense_report": "test_catalog.tne_source.expense_report",
             "attendee_validity": "test_catalog.tne_source.attendee_validity"},
            [],
        ),
    )


def test_start_run_navigates_immediately_even_when_start_audit_run_is_slow(monkeypatch):
    _patch_full_bindings(monkeypatch)
    real_start = adapters.start_audit_run

    def slow_start(**kwargs):
        time.sleep(0.3)
        return real_start(**kwargs)

    monkeypatch.setattr(adapters, "start_audit_run", slow_start)
    fn = _start_run_fn()

    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        started = time.monotonic()
        pathname, summary = _call_start_run(fn)
        elapsed = time.monotonic() - started

    assert elapsed < 0.15, (
        f"start_run took {elapsed:.3f}s -- it must return before the background write finishes"
    )
    assert summary is dash.no_update
    assert pathname.startswith("/run/RUN-")
    run_id = pathname[len("/run/"):]
    # Registered as pending immediately -- the background job may still be
    # sleeping, or (on a slow CI box) may have already settled.
    assert pending_runs.status(run_id) is not None or adapters.get_run(run_id) is not None
    pending_runs._wait_until_settled(run_id)
    assert adapters.get_run(run_id) is not None


def test_start_run_uses_the_pre_generated_run_id_for_the_real_call(monkeypatch):
    _patch_full_bindings(monkeypatch)
    captured = {}
    real_start = adapters.start_audit_run

    def capturing_start(**kwargs):
        captured["run_id"] = kwargs.get("run_id")
        return real_start(**kwargs)

    monkeypatch.setattr(adapters, "start_audit_run", capturing_start)
    fn = _start_run_fn()

    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        pathname, _ = _call_start_run(fn)
    run_id = pathname[len("/run/"):]
    pending_runs._wait_until_settled(run_id)
    assert captured["run_id"] == run_id


def test_start_run_failure_is_recorded_never_swallowed(monkeypatch):
    _patch_full_bindings(monkeypatch)

    def failing_start(**kwargs):
        raise ValueError("contract violation: no binding for X")

    monkeypatch.setattr(adapters, "start_audit_run", failing_start)
    fn = _start_run_fn()

    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        pathname, _ = _call_start_run(fn)
    run_id = pathname[len("/run/"):]
    settled = pending_runs._wait_until_settled(run_id)
    assert settled == ("failed", "ValueError: contract violation: no binding for X")
    # Never removed -- NN14: a still-open /run/<id> tab must be able to see
    # this exactly.
    assert pending_runs.status(run_id) == settled


def test_double_click_reuses_the_same_pending_run_instead_of_starting_two(monkeypatch):
    _patch_full_bindings(monkeypatch)
    call_count = {"n": 0}
    release = threading.Event()
    real_start = adapters.start_audit_run

    def slow_start(**kwargs):
        call_count["n"] += 1
        release.wait(timeout=5)
        return real_start(**kwargs)

    monkeypatch.setattr(adapters, "start_audit_run", slow_start)
    fn = _start_run_fn()

    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        pathname1, _ = _call_start_run(fn, n_clicks=1)
        # A second click, same form state, arriving before the first
        # click's background write has finished -- must reuse the same
        # run_id, not start a second run.
        pathname2, _ = _call_start_run(fn, n_clicks=2)

    assert pathname1 == pathname2
    run_id = pathname1[len("/run/"):]
    release.set()
    pending_runs._wait_until_settled(run_id)
    assert call_count["n"] == 1
    assert adapters.get_run(run_id) is not None


def test_a_later_click_after_the_first_completes_starts_a_genuinely_new_run(monkeypatch):
    """Double-click dedup must not block a legitimate second, later run with
    the same parameters -- only a resubmit still in flight."""
    _patch_full_bindings(monkeypatch)
    fn = _start_run_fn()
    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        pathname1, _ = _call_start_run(fn, n_clicks=1)
    run_id1 = pathname1[len("/run/"):]
    pending_runs._wait_until_settled(run_id1)

    with _probe_app.test_request_context("/", headers={"X-Forwarded-Email": "auditor@example.com"}):
        pathname2, _ = _call_start_run(fn, n_clicks=2)
    run_id2 = pathname2[len("/run/"):]
    pending_runs._wait_until_settled(run_id2)

    assert run_id1 != run_id2
    assert adapters.get_run(run_id1) is not None
    assert adapters.get_run(run_id2) is not None
