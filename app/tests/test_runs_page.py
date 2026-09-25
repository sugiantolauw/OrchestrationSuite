"""src/runs_page.py — BUG-RUNS-1's fix (live test round 3, mirroring
tests/test_trace_page.py exactly): the /runs page's "Filter by Skill" and
"Status" dropdowns must actually filter the runs list. Rendered against the
REAL orchestrator.service + LocalPersistence (overriding this directory's
autouse fake_backend fixture)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dash._callback_context import context_value
from dash._utils import AttributeDict
from dash.exceptions import PreventUpdate

from conftest import load_app_entry
from orchestrator.state import RunState
from orchestrator.timeutil import utc_now
from src import runs_page
from src.platform import adapters


@pytest.fixture
def real_ctx(monkeypatch, tmp_path):
    from orchestrator import service as real_service

    repo_root = Path(__file__).resolve().parents[2]
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(repo_root / "skills"),
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))

    ctx = adapters.get_context()
    try:
        yield ctx
    finally:
        ctx.executor.stop()
        adapters._ctx = None


def _fingerprint(fp_id: str) -> dict:
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        reference_data_hashes="{}",
        skill_content_hash=None,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at=utc_now(),
    )


def _make_run(
    ctx, run_id: str, *, objective: str, status: str,
    skill_id: str | None = None, mode: str = "explorer",
) -> None:
    """Explorer-mode runs resolve skill_name to f"Explorer: {objective}"
    (orchestrator/service.py list_runs) without needing a registered Skill
    -- the simplest way to get two runs with distinct, predictable
    skill_name values to filter on. `skill_id="SKILL-001"` (with
    mode="playbook") is used by the View-routing tests below: `real_ctx`
    points SKILLS_DIR at the repo's real skills/ (skills/tne_exco/manifest.
    yaml declares id: SKILL-001), so such a run's `has_workspace` resolves
    True exactly as a genuine SKILL-001 run's would."""
    now = utc_now()
    state = RunState(
        run_id=run_id,
        run_kind="fieldwork",
        engagement_id="ENG-DEFAULT",
        skill_id=skill_id,
        mode=mode,
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective=objective,
        run_owner="alice",
        fingerprint_id=f"FP-{run_id}",
        created_at=now,
        last_state_change_at=now,
        status=status,
    )
    ctx.persistence.create_run(state, _fingerprint(f"FP-{run_id}"))


def _run_dict(run_id: str) -> dict:
    return next(r for r in adapters.list_audit_runs() if r["run_id"] == run_id)


class _FakeApp:
    """Captures each @app.callback-decorated closure by function name,
    mirroring tests/test_run_status_narration.py's own _register() helper
    for the same reason: runs_page.register_callbacks(app) defines its
    callbacks as nested closures, so calling them directly (rather than
    driving a real Dash dispatch) needs something that looks enough like
    `app` to satisfy the `@app.callback(...)` decorator call itself."""

    def __init__(self):
        self.callbacks = {}

    def callback(self, *_args, **_kwargs):
        def decorator(fn):
            self.callbacks[fn.__name__] = fn
            return fn
        return decorator


def _register():
    app = _FakeApp()
    runs_page.register_callbacks(app)
    return app.callbacks


def _set_triggered(component_id: dict) -> None:
    """Dash's own `ctx.triggered_id` is a ContextVar the real callback
    dispatcher populates -- tests/test_run_status_narration.py's own
    _set_triggered docstring explains why this is the documented way to
    fill it in for a directly-invoked callback closure."""
    prop_id = json.dumps(component_id, sort_keys=True) + ".n_clicks"
    context_value.set(AttributeDict(
        triggered_inputs=[{"prop_id": prop_id, "value": 1}],
        inputs_list=[], states_list=[], inputs={}, states={}, outputs_list=[],
        response={"multi": True},
    ))


def test_clearing_both_filters_shows_every_run_default(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="running")

    rows = runs_page._rows_for_filter(None, None)
    text = str(rows)
    assert "RUN-A" in text
    assert "RUN-B" in text


def test_skill_filter_narrows_to_the_matching_skill_name(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="completed")

    rows = runs_page._rows_for_filter("Explorer: Alpha objective", None)
    text = str(rows)
    assert "RUN-A" in text
    assert "RUN-B" not in text


def test_status_filter_narrows_to_the_matching_status(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="running")

    rows = runs_page._rows_for_filter(None, "Running")
    text = str(rows)
    assert "RUN-B" in text
    assert "RUN-A" not in text


def test_combined_skill_and_status_filters_apply_together(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Alpha objective", status="running")
    _make_run(real_ctx, "RUN-C", objective="Beta objective", status="completed")

    rows = runs_page._rows_for_filter("Explorer: Alpha objective", "Completed")
    text = str(rows)
    assert "RUN-A" in text
    assert "RUN-B" not in text
    assert "RUN-C" not in text


def test_needs_review_matches_runs_waiting_at_either_gate(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="awaiting_confirmation")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="awaiting_signoff")
    _make_run(real_ctx, "RUN-C", objective="Gamma objective", status="completed")

    text = str(runs_page._rows_for_filter(None, "Needs review"))
    assert "RUN-A" in text
    assert "RUN-B" in text
    assert "RUN-C" not in text


def test_empty_string_filter_values_behave_like_cleared(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")

    rows = runs_page._rows_for_filter("", "")
    assert "RUN-A" in str(rows)


def test_filtering_issues_exactly_one_call_not_one_per_run(real_ctx, monkeypatch):
    """CLAUDE.md §2.3 rule 4 / orchestrator/service.py list_runs' own N+1
    fix: filtering must not turn one page render into one query per run."""
    calls: list[int] = []
    original = adapters.list_audit_runs

    def _tracked():
        calls.append(1)
        return original()

    monkeypatch.setattr(adapters, "list_audit_runs", _tracked)

    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="running")
    _make_run(real_ctx, "RUN-C", objective="Gamma objective", status="failed")

    runs_page._rows_for_filter("Explorer: Alpha objective", None)
    assert len(calls) == 1


def test_callback_is_registered_on_the_real_app(real_ctx):
    entry = load_app_entry()
    assert "runs-list.children" in entry.app.callback_map


# ── CLAUDE.md §11 "Run cards on /runs (user decision, 2026-09-25)" ────────


def test_view_target_routes_skill_001_awaiting_signoff_to_workspace_tne(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="x", status="awaiting_signoff",
              skill_id="SKILL-001", mode="playbook")
    run = _run_dict("RUN-A")
    assert run["has_workspace"] is True
    assert run["status"] == "Awaiting Signoff"
    assert runs_page._view_target(run) == "/workspace/tne?run_id=RUN-A"


def test_view_target_routes_skill_001_completed_to_workspace_tne(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="x", status="completed",
              skill_id="SKILL-001", mode="playbook")
    assert runs_page._view_target(_run_dict("RUN-A")) == "/workspace/tne?run_id=RUN-A"


def test_view_target_routes_a_skill_001_run_still_running_to_run_page(real_ctx):
    """A run whose Skill has a workspace but has not yet computed every
    number (CLAUDE.md gap #6's own eligibility rule) must not open
    /workspace/tne — nothing trustworthy is there to show yet."""
    _make_run(real_ctx, "RUN-A", objective="x", status="running",
              skill_id="SKILL-001", mode="playbook")
    assert runs_page._view_target(_run_dict("RUN-A")) == "/run/RUN-A"


def test_view_target_routes_an_explorer_run_to_run_page(real_ctx):
    """CLAUDE.md §6 D5: "/workspace/tne stays SKILL-001's" — an Explorer
    run (no skill_id, has_workspace False) never opens it, completed or
    not."""
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="completed")
    run = _run_dict("RUN-B")
    assert run["has_workspace"] is False
    assert runs_page._view_target(run) == "/run/RUN-B"


def test_view_button_click_navigates_to_workspace_tne_for_a_skill_001_run(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="x", status="awaiting_signoff",
              skill_id="SKILL-001", mode="playbook")
    callbacks = _register()
    _set_triggered({"type": "run-view-btn", "index": "RUN-A"})

    pathname, search = callbacks["_view_run"]([1])
    assert pathname == "/workspace/tne"
    assert search == "?run_id=RUN-A"


def test_view_button_click_navigates_to_run_page_for_an_explorer_run(real_ctx):
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="completed")
    callbacks = _register()
    _set_triggered({"type": "run-view-btn", "index": "RUN-B"})

    pathname, search = callbacks["_view_run"]([1])
    assert pathname == "/run/RUN-B"
    assert search == ""


def test_view_button_click_with_no_real_n_click_prevents_update(real_ctx):
    """Every OTHER run-view-btn's n_clicks is None on initial render --
    ALL-pattern Inputs deliver one list per callback firing, so a genuine
    click on one card still arrives as [None, None, 1, None, ...]; only the
    triggered id (set by _set_triggered) identifies which one fired."""
    _make_run(real_ctx, "RUN-A", objective="x", status="completed")
    callbacks = _register()
    with pytest.raises(PreventUpdate):
        callbacks["_view_run"]([None])


def test_trace_button_click_navigates_to_trace_filtered_by_run(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="x", status="completed")
    callbacks = _register()
    _set_triggered({"type": "run-trace-btn", "index": "RUN-A"})

    pathname, search = callbacks["_trace_run"]([1])
    assert pathname == "/trace"
    assert search == "?run_id=RUN-A"


def test_export_button_downloads_the_clicked_runs_xlsx_and_clears_the_message(real_ctx, monkeypatch):
    _make_run(real_ctx, "RUN-A", objective="x", status="completed")
    calls = []

    def _fake_get_export(run_id, kind):
        calls.append((run_id, kind))
        return "RUN-A.xlsx", b"binary-xlsx-bytes"

    monkeypatch.setattr(adapters, "get_export", _fake_get_export)
    callbacks = _register()
    _set_triggered({"type": "run-export-btn", "index": "RUN-A"})

    data, message = callbacks["_export_run"]([1])
    assert calls == [("RUN-A", "xlsx")]
    assert data["filename"] == "RUN-A.xlsx"
    assert message == ""


def test_export_button_shows_the_signoff_message_and_triggers_no_download_before_signoff(real_ctx, monkeypatch):
    """CLAUDE.md §11 "Message line" (2026-09-25): before sign-off no xlsx
    export is recorded yet (orchestrator/service.py get_export raises
    FileNotFoundError), so the click sets the new "runs-export-error" Div
    (src/platform/pages.py) to the stated message and triggers no
    download — never a silent no-op."""
    _make_run(real_ctx, "RUN-A", objective="x", status="running")

    def _raise(run_id, kind):
        raise FileNotFoundError(f"no {kind!r} export recorded for run {run_id!r}")

    monkeypatch.setattr(adapters, "get_export", _raise)
    callbacks = _register()
    _set_triggered({"type": "run-export-btn", "index": "RUN-A"})

    data, message = callbacks["_export_run"]([1])
    assert data is None
    assert message == "This run must be signed off before its export is available."


def test_export_button_shows_an_unexpected_errors_message_too(real_ctx, monkeypatch):
    """CLAUDE.md NN14 — never silent: an export error other than "not
    signed off yet" (here, get_export's own sha256 integrity-check
    ValueError, orchestrator/service.py) also surfaces its message on
    "runs-export-error" rather than crashing the callback or no-opping."""
    _make_run(real_ctx, "RUN-A", objective="x", status="completed")

    def _raise(run_id, kind):
        raise ValueError(f"export {kind!r} for run {run_id!r} failed integrity check")

    monkeypatch.setattr(adapters, "get_export", _raise)
    callbacks = _register()
    _set_triggered({"type": "run-export-btn", "index": "RUN-A"})

    data, message = callbacks["_export_run"]([1])
    assert data is None
    assert "RUN-A" in message and "integrity check" in message


def test_view_export_trace_callbacks_are_registered_on_the_real_app(real_ctx):
    """View and Trace both output ("url", "pathname")/("url", "search")
    with allow_duplicate=True, the same pattern src/run_setup.py's own
    "Start Explorer Mode" callback already registers one of -- so this
    counts rather than checking for a single fixed key: 1 (run_setup.py,
    pre-existing) + 2 (this module's own _view_run/_trace_run) = 3."""
    entry = load_app_entry()
    keys = list(entry.app.callback_map.keys())
    url_pair_keys = [k for k in keys if k.startswith("..url.pathname") and "url.search" in k]
    assert len(url_pair_keys) == 3, keys
    assert "..runs-export-download.data...runs-export-error.children.." in keys
