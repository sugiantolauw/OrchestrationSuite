"""/workspace/tne: tabs, filters, findings, drill-downs.

Covers feature ids W1-W14 (see features.py). Assertions are about
structure and behaviour (elements present, callbacks succeed, no errors)
rather than specific demo numbers, since those change with the data.
"""

from __future__ import annotations

import re

import pytest

from tests.e2e.conftest import goto_workspace, has_error_overlay


def _select_react_select_option(page, dropdown_id: str, option_text: str):
    """Open a dcc.Dropdown (react-virtualized-select markup under dbc<2) and
    pick the option matching option_text, scoped to that dropdown's own
    menu so it can't pick up another closed dropdown's selected-value
    label (which also carries role="option" in this component version).
    Options are rendered virtualized as .VirtualizedSelectOption divs, not
    the plain .Select-option class of a non-virtualized react-select."""
    page.locator(f"#{dropdown_id}").click()
    menu = page.locator(f"#{dropdown_id} .Select-menu-outer")
    menu.wait_for(state="visible")
    menu.locator(".VirtualizedSelectOption", has_text=option_text).first.click()


def _select_first_option(page, dropdown_id: str):
    """Same as _select_react_select_option, but picks whichever option
    happens to be first -- used where the exact label doesn't matter, only
    that picking *something* updates dependent charts/tables cleanly."""
    page.locator(f"#{dropdown_id}").click()
    menu = page.locator(f"#{dropdown_id} .Select-menu-outer")
    menu.wait_for(state="visible")
    menu.locator(".VirtualizedSelectOption").first.click()


def test_top_level_tabs_switch_content(watched_page, app_base_url):
    """W1."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)

    assert page.get_by_text("Internal Audit executive brief").count() >= 1

    page.get_by_role("tab", name="Findings & Actions").click()
    assert page.locator("#filtered-findings-container").count() == 1

    page.get_by_role("tab", name="Audit Detail", exact=True).click()
    page.wait_for_selector("#p1-kpis .kpi-tile")

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_findings_and_actions_sub_tabs_switch_content(watched_page, app_base_url):
    """W2."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()

    page.get_by_role("tab", name="Findings & Evidence").click()
    assert page.locator("#filtered-findings-container").count() == 1

    page.get_by_role("tab", name="Management Actions").click()
    assert page.get_by_text("Management Action Tracker").count() == 1

    watcher.assert_clean()


def test_audit_detail_sub_tabs_switch_content(watched_page, app_base_url):
    """W3."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Audit Detail", exact=True).click()

    page.get_by_role("tab", name="Executive analysis").click()
    page.wait_for_selector("#p1-kpis .kpi-tile")

    page.get_by_role("tab", name="Detailed risk").click()
    assert page.get_by_text("Top Vendors").count() >= 1

    page.get_by_role("tab", name="Receipt & approver review").click()
    assert page.get_by_text("Approval Detail Table").count() >= 1

    page.get_by_role("tab", name="Test catalogue").click()
    assert page.locator("#catalogue-table").count() == 1

    watcher.assert_clean()


def test_audience_mode_switches_active_tab(watched_page, app_base_url):
    """W4: matches switch_audience_mode()'s explicit mapping
    (executive -> tab-executive, investigator -> tab-audit, else tab-findings)."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)

    _select_react_select_option(page, "audience-mode", "Investigator")
    page.wait_for_timeout(300)
    assert "active" in (page.get_by_role("tab", name="Audit Detail", exact=True).get_attribute("class") or "")

    _select_react_select_option(page, "audience-mode", "Audit Manager")
    page.wait_for_timeout(300)
    assert "active" in (page.get_by_role("tab", name="Findings & Actions").get_attribute("class") or "")

    _select_react_select_option(page, "audience-mode", "Executive")
    page.wait_for_timeout(300)
    assert "active" in (page.get_by_role("tab", name="Executive Brief").get_attribute("class") or "")

    watcher.assert_clean()


def test_page1_filters_update_dependents_without_error(watched_page, app_base_url):
    """W5."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Audit Detail", exact=True).click()
    page.wait_for_selector("#p1-kpis .kpi-tile")

    before = page.locator("#p1-kpis").inner_text()
    _select_first_option(page, "p1-member")
    page.wait_for_timeout(200)

    # Picking any one member should still leave the KPI panel populated (not blank/erroring).
    page.wait_for_selector("#p1-kpis .kpi-tile")
    after = page.locator("#p1-kpis").inner_text()
    assert after != "" and before != ""
    assert page.locator("#p1-attendee-table").count() == 1

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_page2_filters_update_dependents_without_error(watched_page, app_base_url):
    """W6."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Audit Detail", exact=True).click()
    page.get_by_role("tab", name="Detailed risk").click()
    page.wait_for_selector("#p2-kpis .kpi-tile")

    _select_first_option(page, "p2-expense")
    page.wait_for_timeout(300)

    page.wait_for_selector("#p2-kpis .kpi-tile")
    assert page.locator("#p2-vendor-table").count() == 1
    assert page.locator("#p2-exception-table").count() == 1

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_page3_filters_update_dependents_without_error(watched_page, app_base_url):
    """W7."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Audit Detail", exact=True).click()
    page.get_by_role("tab", name="Receipt & approver review").click()
    page.wait_for_selector("#p3-kpis .kpi-tile")

    _select_first_option(page, "p3-approver")
    page.wait_for_timeout(300)

    page.wait_for_selector("#p3-kpis .kpi-tile")
    assert page.locator("#p3-detail").count() == 1

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_findings_filters_update_filtered_container(watched_page, app_base_url):
    """W8."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()
    page.wait_for_selector("#filtered-findings-container article")

    _select_react_select_option(page, "finding-risk-filter", "High")
    page.wait_for_timeout(300)

    cards = page.locator("#filtered-findings-container article")
    assert cards.count() >= 0  # zero is valid if no High findings this run
    for i in range(cards.count()):
        assert cards.nth(i).locator("text=High").count() >= 1

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_findings_show_top_three_and_collapse_rest(watched_page, app_base_url):
    """W9."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()
    page.wait_for_selector("#filtered-findings-container article")

    total_findings = page.locator("#filtered-findings-container article").count()
    priority_label = page.get_by_text("Showing the top", exact=False)
    assert priority_label.count() == 1

    if total_findings > 3:
        details = page.locator("#filtered-findings-container details summary")
        assert details.count() == 1
        assert "additional findings" in details.inner_text()
        details.click()
        page.wait_for_timeout(200)
        assert page.locator("#filtered-findings-container article").count() == total_findings

    watcher.assert_clean()


def test_view_exceptions_opens_drilldown_with_rows(watched_page, app_base_url):
    """W10."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()
    page.wait_for_selector("#filtered-findings-container article")

    page.locator("button:has-text('View exceptions')").first.click()
    page.wait_for_timeout(400)

    body = page.locator("#exception-offcanvas-body")
    assert page.locator("#exception-offcanvas.show").count() == 1
    # Either a populated table, or the explicit no-drilldown message -- both
    # are correct outcomes depending on which finding was clicked first.
    has_table = body.locator(".dash-table-container").count() >= 1
    has_empty_note = body.get_by_text("No transaction-level drill-down").count() >= 1
    assert has_table or has_empty_note

    watcher.assert_clean()


def test_view_evidence_opens_drawer_with_metrics(watched_page, app_base_url):
    """W11."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()
    page.wait_for_selector("#filtered-findings-container article")

    page.locator("button:has-text('View evidence')").first.click()
    page.wait_for_timeout(400)

    assert page.locator("#evidence-offcanvas.show").count() == 1
    body = page.locator("#evidence-offcanvas-body")
    assert body.get_by_text("Observation").count() >= 1
    assert body.get_by_text("Metrics cited").count() >= 1
    assert body.locator("table tbody tr").count() >= 1

    watcher.assert_clean()


def test_catalogue_table_renders_all_fourteen_tests(watched_page, app_base_url):
    """W12."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Audit Detail", exact=True).click()
    page.get_by_role("tab", name="Test catalogue").click()
    page.wait_for_selector("#catalogue-table")

    # dash_table.DataTable renders its own header and (filter_action="native")
    # filter-input row as extra <tr> inside <tbody>, so scope to rows whose
    # first cell actually looks like a test id ("T3.1a", "T6.1d", ...).
    rows = page.locator("#catalogue-table tbody tr")
    test_id_rows = [
        i for i in range(rows.count())
        if re.match(r"^T\d", rows.nth(i).inner_text().strip())
    ]
    assert len(test_id_rows) == 14

    watcher.assert_clean()


def test_copy_finding_fires_clipboard_callback_and_toast(watched_page, app_base_url):
    """W14."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()
    page.wait_for_selector("#filtered-findings-container article")

    page.locator("button:has-text('Copy')").first.click()
    page.wait_for_timeout(400)

    assert page.locator("#copy-toast.show").count() == 1

    watcher.assert_clean()


# Kept last in this file deliberately: this test's Edit click hits the same
# unresponsive pattern-matching callback documented below, which can leave
# reference_app's single-threaded dev server (app.py:2468, no threaded=True)
# stalled on that never-completing request for the rest of the process's
# life. Running it last means only this one xfail is affected, not every
# test after it (as happened before this file was reordered).
@pytest.mark.xfail(
    strict=True,
    reason=(
        "W13: the 'Edit' management-action modal is unreliable after the run's first "
        "action-modal open/close cycle. Sequence: dismiss the spurious auto-opened modal "
        "(test_known_bugs.py) via #action-cancel -- itself a real, successful invocation "
        "of open_action_editor (app.py:2366-2400) -- then navigate to the Management "
        "Actions sub-tab and click a real 'Edit' button. In the large majority of trials "
        "(reproduced repeatedly across fresh app processes and fresh browser instances, "
        "with and without extra page queries in between, with waits up to 45s) the modal "
        "never reopens: #action-modal never gains the 'show' class, even though the "
        "callback's Input pattern ({'type': 'edit-action-btn', 'index': ALL}) and its "
        "Output (#action-modal.is_open) are unchanged from the first, successful "
        "invocation. This is almost certainly the same pattern-matching-ALL fragility "
        "documented in test_known_bugs.py, not a timing issue in this suite -- it "
        "reproduces identically against an already-idle, freshly started server."
    ),
)
def test_management_action_edit_save_updates_tracker_and_log(watched_page, app_base_url):
    """W13."""
    page, watcher = watched_page
    goto_workspace(page, app_base_url)
    page.get_by_role("tab", name="Findings & Actions").click()
    page.get_by_role("tab", name="Management Actions").click()
    page.wait_for_selector("button:has-text('Edit')")

    page.locator("button:has-text('Edit')").first.click()
    page.wait_for_selector("#action-modal.show", timeout=15_000)
    test_id = page.locator("#action-modal-title").inner_text()
    assert test_id

    page.locator("#action-owner").fill("E2E Test Owner")
    page.locator("#action-response").fill("E2E automated response")
    page.locator("#action-save").click()
    page.wait_for_timeout(400)

    assert page.locator("#action-modal.show").count() == 0
    assert page.get_by_text("E2E Test Owner").count() >= 1

    page.locator("details summary:has-text('Audit Log')").click()
    page.wait_for_timeout(200)
    assert page.locator("#audit-log-body li").count() >= 1

    watcher.assert_clean()
