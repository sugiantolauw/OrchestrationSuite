"""Genuine prototype defects found while building the E2E suite.

Per the task brief: these are NOT fixed in reference_app/. Each is captured
as a strict xfail with the exact reproduction, and listed in the session
report. If reference_app/ is ever fixed for real, these tests will start
passing unexpectedly and pytest's strict xfail will turn that into a
failure -- which is the point: it forces this file to be updated rather
than silently going stale.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import goto


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Loading /workspace/tne opens the 'Management action' modal (#action-modal, "
        "app.py:1717-1738) pre-filled for test T6.1d, with zero user interaction. "
        "app.py sets is_open=False at construction, and the only callback that can "
        "set is_open=True (open_action_editor, app.py:2366-2400, Input pattern "
        "{'type': 'edit-action-btn', 'index': ALL}) is declared prevent_initial_call=True. "
        "The modal's backdrop covers the full page and blocks pointer interaction with "
        "everything underneath until it is dismissed. Reproduced deterministically, "
        "always for T6.1d, across two independent dash/dash-bootstrap-components pairs "
        "(4.4.1/2.0.4 and 2.17.1/1.6.0), so it is not a package-version artifact -- most "
        "likely a pattern-matching-callback ('ALL') initial-fire quirk interacting with "
        "dbc.Tabs eagerly mounting the not-yet-active 'Management Actions' sub-tab (whose "
        "edit-action-btn elements the callback listens on) at first render. See also the "
        "workspace test module's test_management_action_edit_save_updates_tracker_and_log "
        "(W13), where the same callback, once already fired once via this modal's own "
        "Cancel button, then fails to respond reliably to a genuine later 'Edit' click -- "
        "very likely the same underlying pattern-matching fragility."
    ),
)
def test_tne_route_loads_without_spurious_action_modal(page, app_base_url):
    goto(page, app_base_url, "/workspace/tne")
    modal = page.locator("#action-modal")
    is_open = modal.count() > 0 and modal.evaluate("el => getComputedStyle(el).display") != "none"
    assert not is_open, "action-modal is open on initial load with no user interaction (see reason above)"
