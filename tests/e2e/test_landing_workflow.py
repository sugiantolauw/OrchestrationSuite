"""Landing page ('/'): the "Start an Audit" platform workflow.

Covers feature ids L1-L6 (see features.py). adapters.py returns mock/demo
data for all of this (CLAUDE.md §0.2), so assertions are about what
renders and what the mocked callbacks do, not about specific values.
"""

from __future__ import annotations

from tests.e2e.conftest import goto, has_error_overlay


def test_demo_indicator_visible(watched_page, app_base_url):
    """L1."""
    page, watcher = watched_page
    goto(page, app_base_url, "/")

    assert page.locator(".demo-indicator").count() >= 1
    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_data_search_filters_asset_cards(watched_page, app_base_url):
    """L2."""
    page, watcher = watched_page
    goto(page, app_base_url, "/")

    before_count = page.locator("#data-search-results .data-asset-card").count()
    assert before_count >= 1

    # debounce=True (app.py:105-112) means Dash only sends the new value on
    # Enter or blur, not on a timer -- fill() alone never triggers the
    # callback.
    page.locator("#data-search-input").fill("this-should-match-nothing-at-all-xyz")
    page.locator("#data-search-input").press("Enter")
    page.wait_for_selector("#data-search-results")
    page.wait_for_timeout(300)

    after_count = page.locator("#data-search-results .data-asset-card").count()
    assert after_count == 0
    assert page.get_by_text("No matching data assets found").count() == 1

    watcher.assert_clean()


def test_file_upload_dropzone_renders(watched_page, app_base_url):
    """L3: renders, but reference_app/app.py wires no callback to
    file-upload-area / uploaded-files-list, so dropping a file is a no-op
    in this prototype -- documented, not exercised as a functional flow."""
    page, watcher = watched_page
    goto(page, app_base_url, "/")

    assert page.locator("#file-upload-area").count() == 1
    watcher.assert_clean()


def test_workflow_preview_renders_stages(watched_page, app_base_url):
    """L4."""
    page, watcher = watched_page
    goto(page, app_base_url, "/")

    container = page.locator("#workflow-preview-container")
    assert container.count() == 1
    assert container.get_by_text("Source data").count() >= 1

    watcher.assert_clean()
    assert not has_error_overlay(page)


def test_run_summary_preview_renders(watched_page, app_base_url):
    """L5."""
    page, watcher = watched_page
    goto(page, app_base_url, "/")

    summary = page.locator("#run-summary-preview")
    assert summary.count() == 1
    assert summary.get_by_text("Playbook").count() >= 1
    assert summary.get_by_text("Demo").count() >= 1

    watcher.assert_clean()


def test_start_audit_analysis_navigates_to_workspace(watched_page, app_base_url):
    """L6."""
    page, watcher = watched_page
    goto(page, app_base_url, "/")

    # This route change is client-side (dash-renderer's dcc.Location, not a
    # full browser navigation), so wait for the URL itself rather than
    # load_state -- the frame can already read as idle before the
    # callback's response lands.
    page.locator("#start-run-btn").click()
    page.wait_for_url("**/workspace/tne")
    page.wait_for_load_state("networkidle")

    assert "/workspace/tne" in page.url
    assert not has_error_overlay(page)
    watcher.assert_clean()


def test_skill_cards_render_with_methodology_links(watched_page, app_base_url):
    """L7."""
    page, watcher = watched_page
    goto(page, app_base_url, "/")

    cards = page.locator("#skill-cards-container .skill-card")
    assert cards.count() >= 1
    first_link = cards.first.locator('a:has-text("View methodology")')
    assert first_link.count() == 1

    first_link.click()
    page.wait_for_url("**/skills/*")
    page.wait_for_load_state("networkidle")
    assert "/skills/" in page.url

    watcher.assert_clean()
    assert not has_error_overlay(page)
