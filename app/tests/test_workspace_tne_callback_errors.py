"""Filter/chart callbacks in workspace_tne must surface the same explicit
error panel the main layout shows when a run's data fails to load, never
silently render an empty chart/table (CLAUDE.md NN14; mirrors
test_workspace_tne.py's test_workspace_layout_shows_error_panel_when_payload_load_fails
for the closures registered by workspace_tne.register_callbacks, since the
bug lives inside those closures, not in the pure _p1_update/_p2_update/
_p3_update/_render_mgmt_tracker/_render_filtered_findings functions those
tests already exercise directly).

register_callbacks() wraps every function with Dash's own @app.callback,
which requires a live callback dispatch context (outputs_list, etc.) to
invoke. A minimal fake `app` whose `.callback(...)` is a no-op decorator
(returning the plain function unwrapped) lets these closures -- none of
which read dash.ctx -- be called directly with ordinary positional
arguments, the same way the rest of this module's tests call workspace_tne's
other pure functions."""

from __future__ import annotations

import fake_service
from src import workspace_tne
from src.platform import adapters


class _FakeApp:
    def __init__(self):
        self.callbacks: dict[str, callable] = {}

    def callback(self, *_args, **_kwargs):
        def decorator(fn):
            self.callbacks[fn.__name__] = fn
            return fn

        return decorator


def _register():
    app = _FakeApp()
    workspace_tne.register_callbacks(app)
    return app.callbacks


def _completed_run():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
    )
    adapters.sign_off(run_id, "auditor@example.com")
    return run_id


class _BoomError(Exception):
    pass


def _break_payload_loading(monkeypatch):
    def _raise(ctx, rid):
        raise _BoomError("sha256 mismatch for source expense_report")

    monkeypatch.setattr(fake_service, "get_run_payload", _raise)


def test_filter_findings_callback_shows_error_panel_instead_of_empty_list(monkeypatch):
    run_id = _completed_run()
    _break_payload_loading(monkeypatch)
    callbacks = _register()

    out = callbacks["_filter_findings"](["High"], [], "severity", [{"finding_id": "F1"}], run_id)
    text = str(out)
    assert "could not be loaded" in text
    assert "_BoomError" in text
    assert "sha256 mismatch" in text


def test_render_actions_callback_shows_error_panel_instead_of_empty_tracker(monkeypatch):
    run_id = _completed_run()
    _break_payload_loading(monkeypatch)
    callbacks = _register()

    out = callbacks["_render_actions"]({}, run_id)
    text = str(out)
    assert "could not be loaded" in text
    assert "_BoomError" in text


def test_p1_callback_puts_error_panel_in_kpi_and_evidence_slots_leaves_rest_no_update(monkeypatch):
    import dash

    run_id = _completed_run()
    _break_payload_loading(monkeypatch)
    callbacks = _register()

    out = callbacks["_update_p1"]("2025-01-01", "2026-04-30", [], run_id)
    assert len(out) == 10
    kpis, monthly, breach_by_member, breach_dist, missing_tier, precomp, data, columns, style, evidence = out
    assert "could not be loaded" in str(kpis)
    assert "_BoomError" in str(kpis)
    assert "could not be loaded" in str(evidence)
    for middle in (monthly, breach_by_member, breach_dist, missing_tier, precomp, data, columns, style):
        assert middle is dash.no_update


def test_p2_callback_puts_error_panel_in_kpi_slot_leaves_rest_no_update(monkeypatch):
    import dash

    run_id = _completed_run()
    _break_payload_loading(monkeypatch)
    callbacks = _register()

    out = callbacks["_update_p2"]("2025-01-01", "2026-04-30", [], [], run_id)
    assert len(out) == 14
    kpis = out[0]
    assert "could not be loaded" in str(kpis)
    assert "_BoomError" in str(kpis)
    assert all(v is dash.no_update for v in out[1:])


def test_p3_callback_puts_error_panel_in_kpi_slot_leaves_rest_no_update(monkeypatch):
    import dash

    run_id = _completed_run()
    _break_payload_loading(monkeypatch)
    callbacks = _register()

    out = callbacks["_update_p3"]("2025-01-01", "2026-04-30", [], run_id)
    assert len(out) == 8
    kpis = out[0]
    assert "could not be loaded" in str(kpis)
    assert "_BoomError" in str(kpis)
    assert all(v is dash.no_update for v in out[1:])


def test_p1_callback_renders_charts_normally_when_load_succeeds():
    # Guards against the error-panel branch firing unconditionally / always
    # returning no_update -- the normal, no-load-error path must still
    # return real chart/table content.
    run_id = _completed_run()
    callbacks = _register()

    out = callbacks["_update_p1"](None, None, [], run_id)
    assert len(out) == 10
    kpis = out[0]
    assert "could not be loaded" not in str(kpis)
