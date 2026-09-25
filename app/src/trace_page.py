"""Platform Trace page — wires the "Filter by run" control.

BUG-TRACE-1 (live test round 2): `platform_trace_page()`
(src/platform/pages.py) renders the `trace-run-filter` dropdown and the
`trace-events-body` table exactly as the prototype does (same ids, same
classes, same text — CLAUDE.md §11 "UI is the prototype's, exactly"), but
neither the prototype nor this build ever wired a callback to it, so
selecting a run never filtered the table. CLAUDE.md's own parity rule
allows wiring an EXISTING control to the backend without changing layout;
this module does only that, mirroring how src/run_setup.py, src/run_status.py
and src/workspace_tne.py each own their page's callbacks and are registered
from app/app.py's own `register_callbacks(app)` calls.

Reads go through `adapters.list_trace_events(run_id)`, which orchestrator.
service/the persistence adapters already implement as a single
`WHERE run_id = :run_id` query (orchestrator/adapters/persistence_local.py,
persistence_delta.py) — this module adds no new query shape, it only passes
the dropdown's value through to the read that already existed.
"""

from __future__ import annotations

from dash import Input, Output

from src.platform import adapters
from src.platform.components import trace_event_row


def _rows_for_filter(run_id: str | None) -> list:
    """The Output content for a given dropdown value — `None`/`""` (cleared)
    shows every event, exactly `platform_trace_page()`'s own initial render
    (src/platform/pages.py); a selected run_id narrows to that run's events
    only. Split out from the callback below so it can be tested directly,
    the same way run_status.py's callbacks delegate to `_render_body`."""
    events = adapters.list_trace_events(run_id or None)
    return [trace_event_row(e) for e in events]


def register_callbacks(app) -> None:

    # BUG-TRACE-2 (P3/P4 perf gap review 2026-09-25, the /trace cold-load
    # latency pass): without prevent_initial_call, Dash fires this once on
    # every /trace mount, even on a plain /trace with the dropdown at its
    # default (no filter) value -- re-issuing the exact same unfiltered
    # adapters.list_trace_events(None) call platform_trace_page() itself
    # already made to render trace-events-body, replacing that subtree with
    # a second, functionally identical render moments after paint. The
    # ?run_id=<id> preselection case (CLAUDE.md §11 "Run cards on /runs"
    # Trace button) is unaffected: platform_trace_page() now renders that
    # filtered view itself (src/platform/pages.py), so this callback firing
    # was never the only source of the filtered table, only a redundant
    # second fetch of it. Exactly BUG-RUNVIEW-1's fix (src/runs_page.py),
    # applied here.
    @app.callback(
        Output("trace-events-body", "children"),
        Input("trace-run-filter", "value"),
        prevent_initial_call=True,
    )
    def _filter_events(run_id):
        return _rows_for_filter(run_id)
