"""src/trace_page.py — BUG-TRACE-1's fix (live test round 2): the /trace
page's "Filter by run" dropdown must actually filter the events table.
Rendered against the REAL orchestrator.service + LocalPersistence
(overriding this directory's autouse fake_backend fixture), matching
tests/test_app_startup.py's own pattern for exercising real persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_app_entry
from src import trace_page
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


def _event(run_id: str, event_id: str, message: str) -> dict:
    return {
        "event_id": event_id,
        "run_id": run_id,
        "event_type": "node_completed",
        "event_time": "2026-09-25T00:00:00",
        "stage": "discover",
        "status": "complete",
        "message": message,
        "actor": "system",
    }


def test_clearing_the_filter_shows_every_run_default(real_ctx):
    real_ctx.persistence.append_trace_event(_event("RUN-A", "EVT-A1", "a event"))
    real_ctx.persistence.append_trace_event(_event("RUN-B", "EVT-B1", "b event"))

    rows = trace_page._rows_for_filter(None)
    text = str(rows)
    assert "a event" in text
    assert "b event" in text


def test_selecting_a_run_filters_to_only_that_runs_events(real_ctx):
    real_ctx.persistence.append_trace_event(_event("RUN-A", "EVT-A1", "a event"))
    real_ctx.persistence.append_trace_event(_event("RUN-A", "EVT-A2", "a second event"))
    real_ctx.persistence.append_trace_event(_event("RUN-B", "EVT-B1", "b event"))

    rows = trace_page._rows_for_filter("RUN-A")
    text = str(rows)
    assert "a event" in text
    assert "a second event" in text
    assert "b event" not in text


def test_empty_string_filter_value_behaves_like_cleared(real_ctx):
    """Dash's own dcc.Dropdown clears to "" as often as None depending on
    how the clear was triggered -- both must show the unfiltered default,
    never an accidental empty-string-matches-nothing result."""
    real_ctx.persistence.append_trace_event(_event("RUN-A", "EVT-A1", "a event"))

    rows = trace_page._rows_for_filter("")
    assert "a event" in str(rows)


def test_filtering_issues_exactly_one_query_not_one_per_run(real_ctx, monkeypatch):
    """CLAUDE.md §2.3 rule 4 / the bug's own requirement: reads must be
    efficient. adapters.list_trace_events already pushes the run_id filter
    into a single WHERE-clause query (orchestrator/adapters/
    persistence_local.py, persistence_delta.py) -- this asserts the
    callback calls it exactly once, rather than looping over runs itself."""
    calls: list[str | None] = []
    original = real_ctx.persistence.list_trace_events

    def _tracked(run_id=None):
        calls.append(run_id)
        return original(run_id)

    monkeypatch.setattr(real_ctx.persistence, "list_trace_events", _tracked)

    real_ctx.persistence.append_trace_event(_event("RUN-A", "EVT-A1", "a event"))
    real_ctx.persistence.append_trace_event(_event("RUN-B", "EVT-B1", "b event"))

    trace_page._rows_for_filter("RUN-A")
    assert calls == ["RUN-A"]

    trace_page._rows_for_filter(None)
    assert calls == ["RUN-A", None]


def test_callback_is_registered_on_the_real_app(real_ctx):
    """app.py registers trace_page alongside run_setup/run_status/
    workspace_tne -- confirms the wiring survives a real app.py load, not
    just a direct call to the module."""
    entry = load_app_entry()
    assert "trace-events-body.children" in entry.app.callback_map
