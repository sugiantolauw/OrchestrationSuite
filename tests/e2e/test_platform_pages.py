"""/runs, /skills, /skills/<id>, /actions, /trace: fixture-backed tables and
cards render correctly.

Covers feature ids P1-P6 (see features.py).
"""

from __future__ import annotations

from tests.e2e.conftest import goto, has_error_overlay

# Mirrors src/platform/fixtures.py row counts -- if that fixture file grows
# or shrinks, these are the numbers to update (not something this suite
# should silently tolerate drifting, since P1/P4/P5 assert "one row per
# fixture row").
EXPECTED_RUN_COUNT = 3
EXPECTED_ACTION_COUNT = 6
EXPECTED_TRACE_EVENT_COUNT = 18
EXPECTED_SKILL_COUNT = 5


def test_runs_page_renders_one_card_per_fixture_run(watched_page, app_base_url):
    """P1."""
    page, watcher = watched_page
    goto(page, app_base_url, "/runs")

    assert page.locator(".run-card").count() == EXPECTED_RUN_COUNT
    assert page.locator(".kpi-tile").count() >= 4  # summary KPI row

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_skills_page_renders_one_card_per_fixture_skill(watched_page, app_base_url):
    """P2."""
    page, watcher = watched_page
    goto(page, app_base_url, "/skills")

    cards = page.locator(".skill-card")
    assert cards.count() == EXPECTED_SKILL_COUNT
    for i in range(cards.count()):
        assert cards.nth(i).locator('a:has-text("View methodology")').count() == 1

    # Filter dropdowns render (not wired to a callback in this prototype --
    # see adapters.py/app.py: no callback registers Output on their ids).
    assert page.locator("#skill-search").count() == 1
    assert page.locator("#skill-domain-filter").count() == 1
    assert page.locator("#skill-status-filter").count() == 1

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_skill_methodology_sections_render(watched_page, app_base_url):
    """P3."""
    page, watcher = watched_page
    goto(page, app_base_url, "/skills/SKILL-001")

    for anchor_id, heading in [
        ("overview", "Overview"),
        ("data-sources", "Data sources"),
        ("test-catalogue", "Test catalogue"),
        ("risk-scoring", "Risk scoring"),
        ("outputs", "Outputs"),
        ("history", "Version history"),
    ]:
        assert page.locator(f"a#{anchor_id}").count() == 1, f"missing anchor #{anchor_id}"
        assert page.get_by_role("heading", name=heading, exact=False).count() >= 1

    # The test-catalogue section should list all 14 tests via _method_test_row.
    assert page.locator(".method-content table tbody tr").count() >= 14

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_actions_page_renders_one_row_per_fixture_action(watched_page, app_base_url):
    """P4."""
    page, watcher = watched_page
    goto(page, app_base_url, "/actions")

    rows = page.locator("#actions-table-body tr")
    assert rows.count() == EXPECTED_ACTION_COUNT
    assert page.locator(".kpi-tile").count() >= 4

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_trace_page_renders_one_row_per_fixture_event(watched_page, app_base_url):
    """P5."""
    page, watcher = watched_page
    goto(page, app_base_url, "/trace")

    rows = page.locator("#trace-events-body tr")
    assert rows.count() == EXPECTED_TRACE_EVENT_COUNT

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_jira_preview_not_reachable_from_any_ui_control():
    """P6: create_jira_preview() exists in adapters.py but CLAUDE.md §8 says
    Jira submission is not built, and no callback in app.py wires any
    button to it. This test documents that absence rather than asserting a
    UI flow that doesn't exist -- if a future phase wires it up, update
    this test (and the feature inventory) rather than deleting it."""
    import re
    import pathlib

    app_py = pathlib.Path(__file__).resolve().parent.parent.parent / "reference_app" / "app.py"
    source = app_py.read_text()
    assert "create_jira_preview" not in source, (
        "create_jira_preview is now referenced from app.py -- Jira preview may be reachable "
        "from the UI; update feature P6 and the workspace test module to exercise it properly"
    )
